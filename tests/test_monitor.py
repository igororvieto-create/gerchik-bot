"""monitor_positions — непрерывная страховка, на которую опирается вход.

enter_trade в трёх местах явно делегирует безопасность монитору
(«keeping position tracked, monitor re-checks SL»), то есть проверяемая
половина опиралась на непроверяемую: до этого файла монитор не вызывался
ни одним тестом. Мутационное тестирование этого не видит по построению —
мутации ставятся туда, где тесты есть.
"""
import importlib
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

from core.config import cfg
from core.state import state, Position
import strategy.trader as tr


def _ms(dt):
    return str(int(dt.timestamp() * 1000))


class FakeExchange:
    """Биржа с явно управляемым поведением стопа."""
    api_key = "k"
    secret = "s"

    def __init__(self, positions=None, attach_sl=True, closed_pnl=None):
        self._positions = positions if positions is not None else []
        self.attach_sl = attach_sl
        self.closed_pnl = closed_pnl if closed_pnl is not None else []
        self.stop_calls = []
        self.close_calls = []

    async def get_balance(self):
        return 1000.0

    async def get_positions(self):
        return list(self._positions)

    async def get_position(self, symbol):
        return next((p for p in self._positions if p["symbol"] == symbol), {})

    async def set_trading_stop(self, symbol, sl=0.0, tp=0.0):
        self.stop_calls.append({"symbol": symbol, "sl": sl, "tp": tp})
        if self.attach_sl and sl > 0:
            for p in self._positions:
                if p["symbol"] == symbol:
                    p["stopLoss"] = str(sl)
        return True   # биржа отвечает успехом ВСЕГДА — в этом и суть бага №1

    async def close_position(self, symbol, side, qty):
        self.close_calls.append({"symbol": symbol, "qty": qty})
        self._positions = [p for p in self._positions if p["symbol"] != symbol]
        return {"retCode": 0}

    async def get_closed_pnl(self, symbol, limit=5):
        return [r for r in self.closed_pnl if r.get("symbol", symbol) == symbol]

    async def get_instrument_info(self, symbol):
        return {"priceFilter": {"tickSize": "0.01"},
                "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"}}


def live(symbol="XUSDT", side="Buy", size="10", sl="98.5", entry="100"):
    return {"symbol": symbol, "side": side, "size": size, "avgPrice": entry,
            "markPrice": entry, "stopLoss": sl, "takeProfit": "103"}


def tracked(symbol="XUSDT", age_s=200, sl=98.5, signal_type="VSA_CLIMAX",
            order_id="ord-1"):
    p = Position(symbol=symbol, side="Buy", entry=100.0, sl=sl, tp1=0.0,
                 tp2=103.0, tp3=0.0, qty=10.0, score=60,
                 signal_type=signal_type, order_id=order_id)
    p.ts = datetime.utcnow() - timedelta(seconds=age_s)
    return p


@pytest.fixture
def db_tmp(tmp_path, monkeypatch):
    """Настоящая БД во временном файле — пути записи проверяются всерьёз."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "m.db"))
    for mod in [m for m in sys.modules if m.startswith("core.db")]:
        del sys.modules[mod]
    import core.db as db
    importlib.reload(db)
    monkeypatch.setattr(tr, "db", db)
    return db


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    state.positions.clear()
    state.pending_entries.clear()
    tr._SL_RETRIES.clear()
    tr._TP_RETRIES.clear()
    tr._MONITORING = False
    tr._ENTERING = 0
    state.daily_pnl_date = tr._today_utc()
    state.daily_realized_pnl = 0.0
    state.trading_halted = False
    state.balance = 1000.0
    # без пауз: close_and_verify спит 1с, а проверок много
    async def _no_sleep(*_a, **_kw):
        return None
    monkeypatch.setattr(tr.asyncio, "sleep", _no_sleep)
    yield
    state.positions.clear()
    state.pending_entries.clear()


# ── Рецидивирующий баг №1: «биржа сказала ок» ≠ «стоп на бирже» ──────────────

async def test_escalates_to_close_only_on_third_unconfirmed_tick(db_tmp):
    """set_trading_stop отдаёт retCode==0 при tpslMode=Partial, но поле
    stopLoss остаётся пустым. Без подтверждения ЧТЕНИЕМ монитор каждые 30
    секунд рапортовал «SL восстановлен», и позиция жила голой до
    ликвидации. Эскалация обязана наступить ровно на третьей осечке."""
    ex = FakeExchange([live(sl="")], attach_sl=False)
    state.positions["XUSDT"] = tracked()

    for tick in (1, 2):
        await tr.monitor_positions(ex)
        assert tr._SL_RETRIES.get("XUSDT") == tick, f"тик {tick}: счётчик не рос"
        assert not ex.close_calls, f"тик {tick}: преждевременное закрытие"
        assert "XUSDT" in state.positions

    await tr.monitor_positions(ex)
    assert ex.close_calls, "третья осечка не привела к аварийному закрытию"


async def test_retry_counter_means_consecutive_not_cumulative(db_tmp):
    """Bybit периодически отдаёт пустой stopLoss в снимке, хотя стоп стоит.
    Без сброса счётчика три такие осечки за часы закрывали ЗДОРОВУЮ
    прибыльную позицию по рынку — спред плюс две тейкерских комиссии."""
    ex = FakeExchange([live(sl="")], attach_sl=False)
    state.positions["XUSDT"] = tracked()
    await tr.monitor_positions(ex)
    assert tr._SL_RETRIES.get("XUSDT") == 1

    # снимок со стопом — счётчик обязан обнулиться
    ex._positions = [live(sl="98.5")]
    await tr.monitor_positions(ex)
    assert "XUSDT" not in tr._SL_RETRIES, "счётчик не сброшен при живом стопе"
    assert not ex.close_calls

    # снова осечка — считаем с единицы, а не с двойки
    ex._positions = [live(sl="")]
    await tr.monitor_positions(ex)
    assert tr._SL_RETRIES.get("XUSDT") == 1
    assert not ex.close_calls, "позиция закрыта из-за несброшенного счётчика"


async def test_stop_reattach_sends_only_stop_not_take_profit(db_tmp):
    """Bybit валидирует запрос целиком: невалидный takeProfit (цена ушла
    за него после рестарта) отклонял ВЕСЬ запрос, и здоровая прибыльная
    позиция шла в аварийное закрытие."""
    ex = FakeExchange([live(sl="")])
    state.positions["XUSDT"] = tracked()
    await tr.monitor_positions(ex)
    assert ex.stop_calls, "стоп не досылался вовсе"
    assert all(c["tp"] == 0 for c in ex.stop_calls if c["sl"] > 0), \
        "вместе со стопом отправлен takeProfit"


# ── Усыновление: своя позиция не должна стать MANUAL ─────────────────────────

async def test_db_failure_defers_adoption_instead_of_marking_manual(db_tmp,
                                                                    monkeypatch):
    """[] от get_open_trades читается как «своих сделок нет», и КАЖДАЯ
    позиция бота получала ярлык MANUAL: без досылки стопа, вне слотов,
    мимо дневного предохранителя."""
    async def boom():
        raise RuntimeError("БД недоступна")
    monkeypatch.setattr(db_tmp, "get_open_trades", boom)

    ex = FakeExchange([live()])
    await tr.monitor_positions(ex)
    assert "XUSDT" not in state.positions, "позиция усыновлена при недоступной БД"


async def test_orphan_with_db_row_is_adopted_as_own(db_tmp):
    """Строка в trades — единственный признак «позиция моя» после
    рестарта: state.positions живёт только в памяти."""
    await db_tmp.init_db()
    await db_tmp.save_trade_open(tracked(order_id="ord-77"))

    ex = FakeExchange([live()])
    await tr.monitor_positions(ex)
    pos = state.positions.get("XUSDT")
    assert isinstance(pos, Position)
    assert pos.signal_type != "MANUAL", "своя позиция помечена как чужая"
    assert pos.order_id == "ord-77", "цели и order_id не восстановлены из БД"


async def test_unknown_position_is_manual_and_never_touched(db_tmp):
    """Ручная сделка пользователя: наблюдаем, но не трогаем — ни досылки
    стопа, ни закрытия."""
    await db_tmp.init_db()
    ex = FakeExchange([live(sl="")])       # без стопа, но НЕ наша
    await tr.monitor_positions(ex)
    pos = state.positions.get("XUSDT")
    assert isinstance(pos, Position) and pos.signal_type == "MANUAL"
    assert not ex.close_calls, "чужая позиция закрыта ботом"
    assert not ex.stop_calls, "боту дослали стоп на чужую позицию"


# ── Убыток во время простоя ──────────────────────────────────────────────────

async def test_loss_during_downtime_reaches_daily_breaker(db_tmp):
    """Оба цикла монитора идут от того, что видно СЕЙЧАС. Позиция, чей
    стоп сработал во время рестарта, не лежит ни на бирже, ни в памяти:
    строка оставалась open с pnl=NULL, а get_realized_pnl_since считает
    только closed — убыток не входил в лимит НИКОГДА."""
    await db_tmp.init_db()
    ghost = tracked(order_id="ord-gh", age_s=1800)
    await db_tmp.save_trade_open(ghost)

    ex = FakeExchange([], closed_pnl=[{
        "updatedTime": _ms(ghost.ts + timedelta(minutes=2)),
        "avgExitPrice": "98.5", "closedPnl": "-10.2"}])
    await tr.monitor_positions(ex)

    assert state.daily_realized_pnl == pytest.approx(-10.2)
    from_db = await db_tmp.get_realized_pnl_since(tr._today_utc() + "T00:00:00")
    assert from_db == pytest.approx(-10.2), "закрытие не записано в БД"


async def test_downtime_loss_counted_exactly_once(db_tmp):
    """Идемпотентность на штатном пути: после успешной записи строка уже
    не open, и следующий тик её не видит."""
    await db_tmp.init_db()
    ghost = tracked(order_id="ord-once", age_s=1800)
    await db_tmp.save_trade_open(ghost)
    ex = FakeExchange([], closed_pnl=[{
        "updatedTime": _ms(ghost.ts + timedelta(minutes=2)),
        "avgExitPrice": "98.5", "closedPnl": "-10.2"}])

    await tr.monitor_positions(ex)
    first = state.daily_realized_pnl
    await tr.monitor_positions(ex)
    assert state.daily_realized_pnl == pytest.approx(first), "двойной учёт"


async def test_pnl_not_counted_when_db_write_fails(db_tmp, monkeypatch):
    """Ключевая проверка идемпотентности: save_trade_close ГЛУШИТ свои
    исключения, поэтому при заблокированной базе (WAL + параллельная
    чистка) строка остаётся open, а PnL капал в счётчик КАЖДЫЕ 30 секунд —
    три тика давали тройной убыток. Учитывать можно только после
    подтверждённой записи."""
    await db_tmp.init_db()
    ghost = tracked(order_id="ord-lock", age_s=1800)
    await db_tmp.save_trade_open(ghost)

    async def failing_close(*_a, **_kw):
        return False          # запись не удалась
    monkeypatch.setattr(db_tmp, "save_trade_close", failing_close)

    ex = FakeExchange([], closed_pnl=[{
        "updatedTime": _ms(ghost.ts + timedelta(minutes=2)),
        "avgExitPrice": "98.5", "closedPnl": "-10.2"}])

    for tick in (1, 2, 3):
        await tr.monitor_positions(ex)
        assert state.daily_realized_pnl == 0.0, (
            f"тик {tick}: PnL учтён без успешной записи "
            f"({state.daily_realized_pnl})")


async def test_unconfirmed_downtime_close_is_not_sealed_with_zero(db_tmp):
    """(0.0, 0.0) означает «не нашли», а не «PnL равен нулю». Запись нулём
    выводила строку из open — и убыток терялся НАВСЕГДА, ровно то, что
    этот блок должен был чинить."""
    await db_tmp.init_db()
    ghost = tracked(order_id="ord-nil", age_s=1800)
    await db_tmp.save_trade_open(ghost)

    ex = FakeExchange([], closed_pnl=[])   # биржа записи не отдала
    await tr.monitor_positions(ex)

    rows = await db_tmp.get_open_trades()
    assert any(r["order_id"] == "ord-nil" for r in rows), \
        "строка закрыта нулём и больше не будет перепроверена"
    assert state.daily_realized_pnl == 0.0


async def test_foreign_pnl_does_not_enter_the_breaker(db_tmp):
    """Матчер с одной лишь нижней границей брал САМУЮ СВЕЖУЮ запись по
    символу — то есть чужую сделку. Ручные сделки специально исключены из
    предохранителя, а этот путь заносил их в обход."""
    await db_tmp.init_db()
    ghost = tracked(order_id="ord-fgn", age_s=1800)
    await db_tmp.save_trade_open(ghost)

    ex = FakeExchange([], closed_pnl=[{
        # запись СИЛЬНО позже закрытия нашей позиции — чужая сделка
        "updatedTime": _ms(datetime.utcnow() + timedelta(minutes=30)),
        "avgExitPrice": "120", "closedPnl": "50.0"}])
    await tr.monitor_positions(ex)
    assert state.daily_realized_pnl == 0.0, "чужой PnL попал в дневной лимит"


# ── Бронь входа ──────────────────────────────────────────────────────────────

async def test_stale_reservation_is_released_when_no_position(db_tmp):
    """Sentinel, переживший свой enter_trade, навсегда занимал слот из
    MAX_POSITIONS и блокировал повторный вход по символу."""
    await db_tmp.init_db()
    state.positions["XUSDT"] = None
    state.pending_entries["XUSDT"] = ("Buy", datetime.utcnow() - timedelta(seconds=200))
    await tr.monitor_positions(FakeExchange([]))
    assert "XUSDT" not in state.positions
    assert "XUSDT" not in state.pending_entries


async def test_reservation_not_touched_while_entry_in_flight(db_tmp):
    """При деградировавших POST-путях enter_trade живёт дольше 90 секунд.
    Монитор не должен забирать его позицию: иначе он аварийно её закроет,
    а enter_trade следом отрапортует успешный вход по несуществующей."""
    await db_tmp.init_db()
    state.positions["XUSDT"] = None
    state.pending_entries["XUSDT"] = ("Buy", datetime.utcnow() - timedelta(seconds=200))
    tr._ENTERING = 1
    try:
        await tr.monitor_positions(FakeExchange([live()]))
        assert state.positions.get("XUSDT") is None, "слот перехвачен у активного входа"
    finally:
        tr._ENTERING = 0


# ── Перенос стопа в безубыток ────────────────────────────────────────────────

async def test_breakeven_moves_stop_only_forward(db_tmp):
    """Перенос обязан двигать стоп только В ПЛЮС: откат уже подтянутого
    стопа вернул бы позиции полный 1R риска."""
    saved = cfg.BREAKEVEN_AT_R
    cfg.BREAKEVEN_AT_R = 1.0
    try:
        await db_tmp.init_db()
        # стоп уже выгоднее безубытка — трогать нельзя
        pos = tracked(sl=98.5)
        state.positions["XUSDT"] = pos
        ex = FakeExchange([live(sl="101.0", entry="100")])
        ex._positions[0]["markPrice"] = "103"
        await tr.monitor_positions(ex)
        assert not [c for c in ex.stop_calls if c["sl"] > 0], \
            "стоп откачен назад с уже достигнутого уровня"
    finally:
        cfg.BREAKEVEN_AT_R = saved


async def test_breakeven_disabled_by_default_does_not_touch_stop(db_tmp):
    assert cfg.BREAKEVEN_AT_R == 0.0
    await db_tmp.init_db()
    state.positions["XUSDT"] = tracked()
    ex = FakeExchange([live(sl="98.5")])
    ex._positions[0]["markPrice"] = "103"      # +2R
    await tr.monitor_positions(ex)
    assert not ex.stop_calls, "механизм выключен, но стоп двигали"


# ── Устойчивость ─────────────────────────────────────────────────────────────

async def test_api_failure_exits_cleanly_without_touching_positions(db_tmp, caplog):
    """get_positions() = None означает «не знаю», а не «позиций нет».
    Проверяется И сохранность позиции, И ЧИСТЫЙ ранний выход: без него
    None доезжает до построения live_map и роняет тик исключением, а
    молчаливое падение монитора — это прекращение проверки стопов."""
    import logging

    class Blind(FakeExchange):
        async def get_positions(self):
            return None

    await db_tmp.init_db()
    state.positions["XUSDT"] = tracked()
    with caplog.at_level(logging.ERROR, logger="trader"):
        await tr.monitor_positions(Blind([]))
    assert "XUSDT" in state.positions, "позиция снята с учёта из-за сбоя API"
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, f"сбой API уронил тик вместо чистого выхода: {errors}"


async def test_monitoring_flag_released_on_exception(db_tmp, monkeypatch):
    """_MONITORING обязан сниматься даже при исключении: иначе монитор
    больше никогда не запустится и проверка стопов встанет навсегда."""
    class Broken(FakeExchange):
        async def get_positions(self):
            raise RuntimeError("сеть легла")

    await db_tmp.init_db()
    await tr.monitor_positions(Broken([]))
    assert tr._MONITORING is False


# ── Основной путь закрытия (штатный SL/TP) ───────────────────────────────────
# Блок реконсиляции простоя имел обе охраны, а ОСНОВНОЙ путь — ни одной, хотя
# именно он отрабатывает почти каждое закрытие. Тесты ниже гоняют позицию из
# state.positions, исчезнувшую из снимка биржи, а не строку в БД.

async def _tracked_position(db, symbol="MAINUSDT", age_min=30):
    pos = Position(symbol=symbol, side="Buy", entry=100.0, sl=98.0, tp1=0.0,
                   tp2=104.0, tp3=0.0, qty=1.0, score=60,
                   signal_type="VSA_CLIMAX", order_id="ord-main")
    pos.ts = datetime.utcnow() - timedelta(minutes=age_min)
    await db.save_trade_open(pos)
    state.positions[symbol] = pos
    return pos


async def test_main_close_path_does_not_seal_unconfirmed_close(db_tmp):
    """Биржа не отдала запись closed-pnl (лаг, 403 через прокси). Писать
    (0,0) НЕЛЬЗЯ: строка перестанет быть open, следующий тик её не увидит,
    и реальный убыток исчезнет из дневного предохранителя НАВСЕГДА."""
    db = db_tmp
    await db.init_db()
    await _tracked_position(db)
    ex = FakeExchange(positions=[], closed_pnl=[])   # позиции нет, записи нет

    await tr.monitor_positions(ex)

    row = sqlite3.connect(db.DB_PATH).execute(
        "SELECT status, pnl FROM trades WHERE symbol='MAINUSDT'").fetchone()
    assert row[0] == "open", "неподтверждённое закрытие запечатало строку"
    assert state.daily_realized_pnl == 0.0, "нулевой PnL попал в предохранитель"
    assert "MAINUSDT" in state.positions, "позиция снята с учёта — повторять некому"


async def test_main_close_path_counts_pnl_once_and_frees_the_slot(db_tmp):
    """Штатный путь: запись найдена, PnL учтён ровно один раз, слот
    освобождён. Три тика подряд не должны учесть один убыток трижды."""
    db = db_tmp
    await db.init_db()
    pos = await _tracked_position(db)
    ex = FakeExchange(positions=[], closed_pnl=[{
        "symbol": "MAINUSDT", "closedPnl": "-12.5", "avgExitPrice": "98.0",
        "updatedTime": _ms(datetime.utcnow()),
    }])

    for _ in range(3):
        await tr.monitor_positions(ex)

    assert state.daily_realized_pnl == pytest.approx(-12.5), \
        f"PnL учтён не один раз: {state.daily_realized_pnl}"
    row = sqlite3.connect(db.DB_PATH).execute(
        "SELECT status, pnl FROM trades WHERE symbol='MAINUSDT'").fetchone()
    assert row == ("closed", -12.5)
    assert "MAINUSDT" not in state.positions, "слот не освобождён"


async def test_main_close_path_defers_pnl_when_db_write_fails(db_tmp, monkeypatch):
    """save_trade_close вернула отказ (база занята). PnL учитывать нельзя:
    строка осталась open, символ ещё под наблюдением, и блок реконсиляции
    в этом же тике учёл бы тот же убыток повторно."""
    db = db_tmp
    await db.init_db()
    await _tracked_position(db)
    ex = FakeExchange(positions=[], closed_pnl=[{
        "symbol": "MAINUSDT", "closedPnl": "-12.5", "avgExitPrice": "98.0",
        "updatedTime": _ms(datetime.utcnow()),
    }])

    async def _refuse(*a, **kw):
        return db.CLOSE_FAILED
    monkeypatch.setattr(db, "save_trade_close", _refuse)

    for _ in range(3):
        await tr.monitor_positions(ex)

    assert state.daily_realized_pnl == 0.0, \
        f"PnL учтён без подтверждённой записи: {state.daily_realized_pnl}"
    assert "MAINUSDT" in state.positions


async def test_main_close_path_does_not_count_a_row_someone_else_closed(db_tmp, monkeypatch):
    """CLOSE_ABSENT: переводить нечего, строку закрыл другой путь и он же
    учёл PnL. Повторный учёт дал бы двойной счёт, а вечный повтор — вечно
    занятый слот. Поэтому: не учитываем, но и не повторяем."""
    db = db_tmp
    await db.init_db()
    await _tracked_position(db)
    ex = FakeExchange(positions=[], closed_pnl=[{
        "symbol": "MAINUSDT", "closedPnl": "-12.5", "avgExitPrice": "98.0",
        "updatedTime": _ms(datetime.utcnow()),
    }])

    async def _absent(*a, **kw):
        return db.CLOSE_ABSENT
    monkeypatch.setattr(db, "save_trade_close", _absent)

    await tr.monitor_positions(ex)

    assert state.daily_realized_pnl == 0.0, "учтён чужой PnL"
    assert "MAINUSDT" not in state.positions, "слот занят навсегда"


async def test_main_close_path_treats_unknown_outcome_as_not_counted(db_tmp, monkeypatch):
    """Контракт строковый, и проверка обязана быть ПОЛОЖИТЕЛЬНОЙ. Обратная
    форма (`if outcome == CLOSE_FAILED: return`) молча проваливается в учёт
    PnL при любом неожиданном значении — например при старом булевом False,
    который возвращала прежняя версия save_trade_close. Именно так и была
    внесена регрессия при переходе на три состояния."""
    db = db_tmp
    await db.init_db()
    await _tracked_position(db)
    ex = FakeExchange(positions=[], closed_pnl=[{
        "symbol": "MAINUSDT", "closedPnl": "-12.5", "avgExitPrice": "98.0",
        "updatedTime": _ms(datetime.utcnow()),
    }])

    async def _legacy_false(*a, **kw):
        return False          # значение вне трёх состояний контракта
    monkeypatch.setattr(db, "save_trade_close", _legacy_false)

    await tr.monitor_positions(ex)

    assert state.daily_realized_pnl == 0.0, (
        f"неизвестный исход записи учтён как успех: {state.daily_realized_pnl}")
    assert "MAINUSDT" in state.positions, "позиция снята с учёта при неясном исходе"
