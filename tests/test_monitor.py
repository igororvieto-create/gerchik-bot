import pytest
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


async def test_close_without_a_trades_row_does_not_swallow_the_pnl(db_tmp, monkeypatch):
    """Регрессия, внесённая переходом на три состояния save_trade_close.

    У восстановленной позиции нет order_id, и строку в trades пишут прямо
    перед учётом закрытия. Если эта запись провалилась, save_trade_close не
    находит открытой строки и возвращает CLOSE_ABSENT — а он означает «PnL
    учёл тот, кто закрыл». Здесь не учёл НИКТО: убыток исчезал молча, слот
    освобождался. Возврат save_trade_open обязан проверяться."""
    db = db_tmp
    await db.init_db()
    pos = Position(symbol="NOROWUSDT", side="Buy", entry=100.0, sl=98.0, tp1=0.0,
                   tp2=104.0, tp3=0.0, qty=1.0, score=60,
                   signal_type="RECOVERED", order_id="")   # без order_id
    pos.ts = datetime.utcnow() - timedelta(minutes=30)
    state.positions["NOROWUSDT"] = pos

    async def _fail_open(*a, **kw):
        return False                      # база недоступна на запись
    monkeypatch.setattr(db, "save_trade_open", _fail_open)

    ex = FakeExchange(positions=[], closed_pnl=[{
        "symbol": "NOROWUSDT", "closedPnl": "-9.0", "avgExitPrice": "98.0",
        "updatedTime": _ms(datetime.utcnow()),
    }])
    await tr.monitor_positions(ex)

    assert state.daily_realized_pnl == 0.0, \
        f"PnL учтён без строки в trades: {state.daily_realized_pnl}"
    assert "NOROWUSDT" in state.positions, \
        "позиция снята с учёта — убыток потерян навсегда, повторять некому"


async def test_partial_reduction_does_not_replace_the_real_exit(db_tmp):
    """У позиции бывают СВОИ ранние записи closed-pnl: риск-гард сокращает
    размер сразу после входа, частичный залив close_and_verify тоже
    оставляет запись. Прежний выбор «самой ранней» брал сокращение на -0.12
    и терял реальный выход по стопу на -15.0 — предохранитель видел 2%
    убытка вместо полного, а в trades писалась цена выхода, равная входу."""
    db = db_tmp
    await db.init_db()
    pos = await _tracked_position(db, symbol="PARTUSDT", age_min=240)
    now = datetime.utcnow()
    # closedSize присутствует, как в настоящем ответе Bybit: сокращение
    # закрыло 0.2 от позиции, выход — оставшиеся 0.8. Сумма = размер
    # позиции, и алгоритм останавливается ровно на ней.
    ex = FakeExchange(positions=[], closed_pnl=[
        {"symbol": "PARTUSDT", "closedPnl": "-0.12", "avgExitPrice": "100.0",
         "closedSize": "0.2", "updatedTime": _ms(now - timedelta(hours=4))},
        {"symbol": "PARTUSDT", "closedPnl": "-15.0", "avgExitPrice": "98.0",
         "closedSize": "0.8", "updatedTime": _ms(now)},
    ])
    await tr.monitor_positions(ex)

    assert state.daily_realized_pnl == pytest.approx(-15.12), (
        f"учтена не сумма записей позиции: {state.daily_realized_pnl}")
    row = sqlite3.connect(db.DB_PATH).execute(
        "SELECT exit_price, pnl FROM trades WHERE symbol='PARTUSDT'").fetchone()
    assert row[0] == pytest.approx(98.0), "цена выхода взята у сокращения, а не у выхода"
    assert row[1] == pytest.approx(-15.12)


async def test_single_record_close_is_unchanged(db_tmp):
    """Обычный случай (одна запись) не должен пострадать от суммирования."""
    db = db_tmp
    await db.init_db()
    await _tracked_position(db, symbol="ONEUSDT")
    ex = FakeExchange(positions=[], closed_pnl=[{
        "symbol": "ONEUSDT", "closedPnl": "-7.5", "avgExitPrice": "98.0",
        "updatedTime": _ms(datetime.utcnow()),
    }])
    await tr.monitor_positions(ex)
    assert state.daily_realized_pnl == pytest.approx(-7.5)


async def test_foreign_record_outside_the_window_is_still_excluded(db_tmp):
    """Суммирование не должно вернуть старый дефект: запись ДО открытия
    позиции (чужая сделка пользователя) в сумму не входит."""
    db = db_tmp
    await db.init_db()
    pos = await _tracked_position(db, symbol="FOREIGNUSDT", age_min=30)
    now = datetime.utcnow()
    ex = FakeExchange(positions=[], closed_pnl=[
        {"symbol": "FOREIGNUSDT", "closedPnl": "+500.0", "avgExitPrice": "150.0",
         "updatedTime": _ms(now - timedelta(hours=8))},     # задолго до открытия
        {"symbol": "FOREIGNUSDT", "closedPnl": "-9.0", "avgExitPrice": "98.0",
         "updatedTime": _ms(now)},
    ])
    await tr.monitor_positions(ex)
    assert state.daily_realized_pnl == pytest.approx(-9.0), (
        f"чужой PnL попал в предохранитель: {state.daily_realized_pnl}")


async def test_monitor_detects_risk_above_the_ceiling(db_tmp, caplog):
    """Потолок 3% проверялся ТОЛЬКО в enter_trade и только при удачной
    верификации. Дальше позиция жила без надзора: монитор принимает
    увеличенный размер с биржи (ручная доливка), и инвариант «риск 1-3%»
    нарушался молча."""
    import logging
    db = db_tmp
    await db.init_db()
    pos = Position(symbol="BIGUSDT", side="Buy", entry=100.0, sl=95.0, tp1=0.0,
                   tp2=110.0, tp3=0.0, qty=1.0, score=60,
                   signal_type="VSA_CLIMAX", order_id="big-1")
    pos.ts = datetime.utcnow() - timedelta(minutes=30)
    await db.save_trade_open(pos)
    state.positions["BIGUSDT"] = pos
    state.balance = 1000.0          # риск 1*5 = 5 USDT = 0.5% — норма
    tr._RISK_WARNED.clear()

    live = [{"symbol": "BIGUSDT", "side": "Buy", "size": "1.0", "avgPrice": "100.0",
             "stopLoss": "95.0", "takeProfit": "110.0", "unrealisedPnl": "0"}]
    with caplog.at_level(logging.CRITICAL):
        await tr.monitor_positions(FakeExchange(positions=live))
    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL], \
        "предупреждение при нормальном риске"

    # доливка вручную: размер вырос в 10 раз -> риск 5% при потолке 3%
    live[0]["size"] = "10.0"
    caplog.clear()
    with caplog.at_level(logging.CRITICAL):
        await tr.monitor_positions(FakeExchange(positions=live))
    crit = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert crit, "превышение потолка риска не обнаружено"
    assert "3%" in crit[0].getMessage()

    # и не повторяется каждые 30 секунд
    caplog.clear()
    with caplog.at_level(logging.CRITICAL):
        await tr.monitor_positions(FakeExchange(positions=live))
    assert not [r for r in caplog.records if r.levelno >= logging.CRITICAL], \
        "CRITICAL печатается на каждом тике — лог утонет"


async def test_foreign_trade_after_open_is_not_summed_into_our_pnl(db_tmp):
    """Регрессия суммирования: окно тянется от ОТКРЫТИЯ позиции до «сейчас»,
    а у восстановленной строки это всё время простоя. Ручная сделка
    пользователя по тому же символу попадала в сумму — реальный убыток -15
    превращался в фиктивную победу, и чужие деньги уходили в дневной
    предохранитель.

    Позицию размера Q могут закрыть только записи суммарным объёмом Q.
    Всё, что дальше, — чужое."""
    db = db_tmp
    await db.init_db()
    pos = await _tracked_position(db, symbol="GHOSTUSDT", age_min=180)
    now = datetime.utcnow()
    ex = FakeExchange(positions=[], closed_pnl=[
        # наш стоп: закрыл позицию целиком (qty=1.0)
        {"symbol": "GHOSTUSDT", "closedPnl": "-15.0", "avgExitPrice": "98.0",
         "closedSize": "1.0", "updatedTime": _ms(now - timedelta(hours=2))},
        # ручная сделка пользователя ПОСЛЕ нашей, тот же символ
        {"symbol": "GHOSTUSDT", "closedPnl": "+200.0", "avgExitPrice": "150.0",
         "closedSize": "5.0", "updatedTime": _ms(now - timedelta(minutes=10))},
    ])
    await tr.monitor_positions(ex)

    assert state.daily_realized_pnl == pytest.approx(-15.0), (
        f"чужой PnL попал в предохранитель: {state.daily_realized_pnl}")
    row = sqlite3.connect(db.DB_PATH).execute(
        "SELECT exit_price, pnl FROM trades WHERE symbol='GHOSTUSDT'").fetchone()
    assert row[1] == pytest.approx(-15.0), "в историю записана чужая сделка"
    assert row[0] == pytest.approx(98.0), "цена выхода взята у чужой сделки"


async def test_missing_closed_size_falls_back_to_one_record(db_tmp):
    """Если биржа не прислала closedSize, объём проверить нечем. Занизить
    учёт безопаснее, чем приписать себе чужую сделку: берём только первую
    запись и пишем предупреждение."""
    db = db_tmp
    await db.init_db()
    await _tracked_position(db, symbol="NOSIZEUSDT", age_min=180)
    now = datetime.utcnow()
    ex = FakeExchange(positions=[], closed_pnl=[
        {"symbol": "NOSIZEUSDT", "closedPnl": "-5.0", "avgExitPrice": "98.0",
         "updatedTime": _ms(now - timedelta(hours=2))},
        {"symbol": "NOSIZEUSDT", "closedPnl": "+300.0", "avgExitPrice": "150.0",
         "updatedTime": _ms(now - timedelta(minutes=5))},
    ])
    await tr.monitor_positions(ex)
    assert state.daily_realized_pnl == pytest.approx(-5.0), (
        f"без closedSize учтено больше одной записи: {state.daily_realized_pnl}")


async def test_monitor_syncs_entry_price_from_the_exchange(db_tmp, caplog):
    """При неудачной верификации входа pos.entry оставался ценой СИГНАЛА.
    Из-за этого детектор потолка риска был слеп ровно в том случае, ради
    которого написан: при проскальзывании 1.5% реальные 4.5% риска
    выглядели как ровно 3.00%, и CRITICAL не печатался."""
    import logging
    db = db_tmp
    await db.init_db()
    pos = Position(symbol="SLIPUSDT", side="Buy", entry=100.0, sl=97.0, tp1=0.0,
                   tp2=106.0, tp3=0.0, qty=10.0, score=60,
                   signal_type="VSA_CLIMAX", order_id="slip-1")
    pos.ts = datetime.utcnow() - timedelta(minutes=30)
    await db.save_trade_open(pos)
    state.positions["SLIPUSDT"] = pos
    state.balance = 1000.0
    tr._RISK_WARNED.clear()

    # биржа сообщает фактический залив 101.5 — риск 10*4.5 = 4.5% баланса
    live = [{"symbol": "SLIPUSDT", "side": "Buy", "size": "10.0",
             "avgPrice": "101.5", "stopLoss": "97.0", "takeProfit": "106.0",
             "unrealisedPnl": "0"}]
    with caplog.at_level(logging.CRITICAL):
        await tr.monitor_positions(FakeExchange(positions=live))

    assert pos.entry == pytest.approx(101.5), "цена входа не сверена с биржей"
    crit = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
    assert crit, "превышение потолка риска не замечено из-за цены сигнала"
    row = sqlite3.connect(db.DB_PATH).execute(
        "SELECT entry FROM trades WHERE order_id='slip-1'").fetchone()
    assert row[0] == pytest.approx(101.5), "в trades осталась цена сигнала"


# ── Падение монитора обязано быть видно ─────────────────────────────────────

async def test_monitor_crash_is_recorded_and_does_not_kill_the_job(monkeypatch):
    """Монитор досылает и удерживает стоп. У его задачи не было обработки
    исключений: падение повторялось каждые 30 секунд, позиции оставались
    без присмотра, а наружу не выходило НИЧЕГО — счётчик сканов рос, пульс
    горел зелёным."""
    import main as M
    from core.state import state
    state.last_monitor_ok = None
    state.last_monitor_error = ""
    monkeypatch.setattr(M, "_client", object())

    async def boom(_c):
        raise RuntimeError("биржа недоступна")
    monkeypatch.setattr(M, "monitor_positions", boom)
    await M._monitor_job()          # НЕ должно бросить наружу
    assert "биржа недоступна" in state.last_monitor_error, \
        "падение монитора не зафиксировано"
    assert state.last_monitor_ok is None, "провал засчитан за успешный проход"


async def test_successful_monitor_pass_clears_the_error(monkeypatch):
    import main as M
    from core.state import state
    state.last_monitor_error = "старая ошибка"
    state.last_monitor_ok = None
    monkeypatch.setattr(M, "_client", object())

    async def fine(_c):
        return None
    monkeypatch.setattr(M, "monitor_positions", fine)
    await M._monitor_job()
    assert state.last_monitor_error == "", "ошибка висит после успешного прохода"
    assert state.last_monitor_ok is not None, "успешный проход не отмечен"


async def test_stats_exposes_monitor_staleness(monkeypatch):
    """Признак обязан доходить до экрана: в логе такое уже писалось."""
    import json as _j
    from datetime import datetime, timedelta
    from api import routes as R
    from core.state import state
    monkeypatch.delenv("DASHBOARD_TOKEN", raising=False)
    state.last_monitor_ok = datetime.utcnow() - timedelta(minutes=17)
    state.last_monitor_error = ""

    # Заглушка строится ЗДЕСЬ, а не импортируется из соседнего теста:
    # кросс-импорт между файлами тестов ломает mypy ("Source file found
    # twice under different module names") — уже наступали.
    class _Req:
        headers: dict = {}
        query_params: dict = {}
        method = "GET"
        url = type("U", (), {"path": "/api/stats"})()

    body = _j.loads(bytes((await R.get_stats(_Req())).body).decode())
    assert body["monitor_stale_min"] == pytest.approx(17, abs=1), \
        "простой монитора не виден снаружи"


# ── Учёт закрытий опирается на ИСХОДНЫЙ размер позиции ─────────────────────

def _rec(ms, pnl, size, exit_px):
    return {"updatedTime": str(ms), "closedPnl": str(pnl),
            "closedSize": str(size), "avgExitPrice": str(exit_px)}


async def test_partial_close_does_not_lose_the_final_exit(monkeypatch):
    """pos.qty — ОСТАТОК после частичного закрытия, а записи закрытия
    относятся к полному размеру. Ограничитель обрывался на первой же
    записи и терял PnL финального выхода: дневной предохранитель
    недосчитывал большую часть убытка."""
    import strategy.trader as tr
    from core.state import Position
    from datetime import datetime
    pos = Position(symbol="AAAUSDT", side="Buy", entry=100.0, sl=95.0,
                   tp1=0.0, tp2=110.0, tp3=0.0, qty=4.0, qty_opened=10.0,
                   score=50, signal_type="X")
    pos.ts = datetime.utcnow()
    base = int(pos.ts.timestamp() * 1000)

    class C:
        async def get_closed_pnl(self, symbol, limit=1):
            return [_rec(base + 1000, -3.0, 6.0, 99.5),
                    _rec(base + 2000, -8.0, 4.0, 96.0)]
    px, pnl = await tr.fetch_matching_closed_pnl(C(), pos, attempts=1)
    assert pnl == pytest.approx(-11.0), \
        f"потерян PnL финального выхода: получено {pnl}, ожидалось -11.0"
    assert px == pytest.approx(96.0), "цена выхода взята от частичного залива"


async def test_zero_qty_does_not_reopen_the_foreign_pnl_hole(monkeypatch):
    """Риск-гард обнуляет pos.qty, когда сокращение закрыло позицию целиком.
    Условие `qty > 0` тогда отключало ограничитель, и в сумму снова
    попадали ЧУЖИЕ сделки по тому же символу — ровно тот баг, ради
    которого ограничитель написан (-15 реального убытка превращались в
    +185 фиктивной победы)."""
    import strategy.trader as tr
    from core.state import Position
    from datetime import datetime
    pos = Position(symbol="AAAUSDT", side="Buy", entry=100.0, sl=95.0,
                   tp1=0.0, tp2=110.0, tp3=0.0, qty=0.0, qty_opened=0.0,
                   score=50, signal_type="X")
    pos.ts = datetime.utcnow()
    base = int(pos.ts.timestamp() * 1000)

    class C:
        async def get_closed_pnl(self, symbol, limit=1):
            return [_rec(base + 1000, -15.0, 10.0, 98.0),
                    _rec(base + 9000, +200.0, 50.0, 120.0)]   # ручная сделка
    px, pnl = await tr.fetch_matching_closed_pnl(C(), pos, attempts=1)
    assert pnl == pytest.approx(-15.0), \
        f"в учёт попал чужой PnL: получено {pnl}"


async def test_full_size_bound_still_excludes_foreign_trades():
    """Обратная проверка: ограничитель обязан продолжать отсекать чужое,
    когда исходный размер известен."""
    import strategy.trader as tr
    from core.state import Position
    from datetime import datetime
    pos = Position(symbol="AAAUSDT", side="Buy", entry=100.0, sl=95.0,
                   tp1=0.0, tp2=110.0, tp3=0.0, qty=10.0, qty_opened=10.0,
                   score=50, signal_type="X")
    pos.ts = datetime.utcnow()
    base = int(pos.ts.timestamp() * 1000)

    class C:
        async def get_closed_pnl(self, symbol, limit=1):
            return [_rec(base + 1000, -15.0, 10.0, 98.0),
                    _rec(base + 9000, +200.0, 50.0, 120.0)]
    _, pnl = await tr.fetch_matching_closed_pnl(C(), pos, attempts=1)
    assert pnl == pytest.approx(-15.0), "чужая сделка снова в сумме"


# ── Стоп-кран снимает только тот, кто его поставил ─────────────────────────

async def test_daily_rollover_does_not_clear_a_database_halt(monkeypatch):
    """trading_halted взводит не только дневной лимит: main.py ставит его
    при провале init_db («без миграции сигналы не пишутся»). Ролловер
    суток снимал флаг безусловно, и бот начинал торговать реальными
    деньгами с неинициализированной базой."""
    import strategy.trader as tr
    from core.state import state
    state.trading_halted = True
    state.halt_reason = "db_init_failed"
    state.daily_pnl_date = None

    async def pnl(_since):
        return 0.0
    monkeypatch.setattr(tr.db, "get_realized_pnl_since", pnl)
    await tr._ensure_daily_state()
    assert state.trading_halted is True, \
        "суточный ролловер снял халт, поставленный из-за неисправной базы"
    assert state.halt_reason == "db_init_failed"


async def test_daily_rollover_clears_a_daily_loss_halt(monkeypatch):
    """Обратная сторона: халт по дневному лимиту суточным ролловером
    сниматься ОБЯЗАН, иначе бот встанет навсегда."""
    import strategy.trader as tr
    from core.state import state
    state.trading_halted = True
    state.halt_reason = "daily_loss"
    state.daily_pnl_date = None

    async def pnl(_since):
        return 0.0
    monkeypatch.setattr(tr.db, "get_realized_pnl_since", pnl)
    await tr._ensure_daily_state()
    assert state.trading_halted is False, "халт по дневному лимиту не снят"
    assert state.halt_reason == ""


async def test_min_lot_bump_cannot_break_the_margin_cap(monkeypatch):
    """Подъём до минимального лота проверял только РИСК, а верхнюю границу
    нотионала — нет. На малом счёте это пробивало MAX_MARGIN_PCT в разы:
    баланс 50 USDT, минимальный лот BTCUSDT 0.001, цена 60000 -> нотионал
    60 USDT, маржа 12 USDT = 24% баланса при потолке 10%. Инвариант по
    риску при этом цел, поэтому проверка риска подъём и пропускала. Бьёт
    тем сильнее, чем меньше счёт, то есть на первых реальных сделках."""
    import strategy.trader as tr
    from core.config import cfg
    from core.state import state, Signal
    monkeypatch.setattr(cfg, "AUTO_TRADE", True)
    monkeypatch.setattr(cfg, "RISK_PER_TRADE", 1.0)
    monkeypatch.setattr(cfg, "LEVERAGE", 5)
    monkeypatch.setattr(cfg, "MAX_MARGIN_PCT", 10.0)
    monkeypatch.setattr(cfg, "TRADE_MIN_SCORE", 0)
    monkeypatch.setattr(cfg, "MIN_TRADE_HEADROOM_R", 0.0)
    state.balance = 50.0
    state.positions.clear()
    state.pending_entries.clear()
    state.trading_halted = False
    state.halt_reason = ""

    placed = []

    class C:
        api_key = "k"
        secret = "s"

        async def get_balance(self):
            return 50.0

        async def get_instrument_info(self, symbol):
            # Шаг МЕНЬШЕ минимального лота — иначе без подъёма объём
            # округляется в ноль и отсекается минимальным нотионалом, а
            # мутация «подъёма не было» проходит незамеченной.
            return {"lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.01",
                                      "minNotionalValue": "5"},
                    "priceFilter": {"tickSize": "0.1"}}
        async def get_positions(self, symbol=None):
            return []
        async def set_leverage(self, symbol, lev):
            return True
        async def place_order(self, **kw):
            placed.append(kw)
            return {"retCode": 0, "result": {"orderId": "1"}}
        async def get_tickers(self, symbol=None):
            return [{"symbol": "BTCUSDT", "lastPrice": "6000"}]

    sig = Signal(symbol="BTCUSDT", signal_type="X", direction="LONG", score=99,
                 price=6000.0, oi_change=0.0, vol_ratio=0.0, funding=0.0,
                 ob_bias="NEUTRAL", atr_pct=1.0, details="",
                 entry=6000.0, sl=5940.0, tp1=6060.0, tp2=6120.0,
                 tp3=6180.0, rr=2.0, headroom=3.0, sl_pct=1.0)
    ok = await tr.enter_trade(C(), sig)
    # Заглушка обязана быть ПОЛНОЙ: без get_balance вход падал на
    # AttributeError, ордер не отправлялся, и тест проходил бы даже со
    # снятой проверкой потолка — ровно это и показала мутация.
    assert not any("AttributeError" in str(r) for r in placed)
    assert not placed, (
        f"ордер отправлен: нотионал {placed[0].get('qty', 0) * 6000:.0f} USDT "
        f"при потолке {50.0 * 10 / 100 * 5:.0f} — потолок маржи пробит")
    assert ok is False


async def test_order_error_still_checks_the_exchange(monkeypatch):
    """Ветка потерянного ответа спрашивала биржу, соседняя — нет: любой
    ненулевой retCode считался доказательством, что позиции не возникло.
    Если ордер всё же приняли, а ответили ошибкой, позиция осталась бы без
    строки в trades, монитор усыновил бы её как MANUAL, а ручным стоп НЕ
    ДОСЫЛАЕТСЯ — буквальный рецидив бага №1."""
    import strategy.trader as tr
    from core.config import cfg
    from core.state import state, Signal
    monkeypatch.setattr(cfg, "AUTO_TRADE", True)
    monkeypatch.setattr(cfg, "TRADE_MIN_SCORE", 0)
    monkeypatch.setattr(cfg, "MIN_TRADE_HEADROOM_R", 0.0)
    monkeypatch.setattr(cfg, "RISK_PER_TRADE", 1.0)
    state.balance = 10000.0
    state.positions.clear(); state.pending_entries.clear()
    state.trading_halted = False; state.halt_reason = ""
    asked = []

    class C:
        api_key = "k"
        secret = "s"

        async def get_balance(self):
            return 10000.0

        async def get_instrument_info(self, symbol):
            return {"lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0.01",
                                      "minNotionalValue": "5"},
                    "priceFilter": {"tickSize": "0.01"}}

        async def get_positions(self, symbol=None):
            return []

        async def set_leverage(self, symbol, lev):
            return True

        async def place_order(self, **kw):
            return {"retCode": 10001, "retMsg": "some error"}

        async def get_position(self, symbol):
            asked.append(symbol)
            return {"side": "Buy", "size": "1.0", "avgPrice": "100.0"}

        async def get_tickers(self, symbol=None):
            return [{"symbol": "AAAUSDT", "lastPrice": "100"}]

    sig = Signal(symbol="AAAUSDT", signal_type="X", direction="LONG", score=99,
                 price=100.0, oi_change=0.0, vol_ratio=0.0, funding=0.0,
                 ob_bias="NEUTRAL", atr_pct=1.0, details="",
                 entry=100.0, sl=99.0, tp1=101.0, tp2=102.0, tp3=103.0,
                 rr=2.0, headroom=3.0, sl_pct=1.0)
    await tr.enter_trade(C(), sig)
    assert asked, ("после отказа ордера биржу не спросили — живая позиция "
                   "осталась бы без строки в trades и без стопа")
    # Главное не число запросов, а исход: позиция взята под учёт, а не
    # брошена. Брошенную монитор усыновил бы как MANUAL, а ручным стоп не
    # досылается.
    assert "AAAUSDT" in state.positions, \
        "живая позиция брошена после ошибочного ответа на ордер"


async def test_over_risk_positions_reach_the_dashboard(monkeypatch):
    """Инвариант «потолок 3% жёсткий» превращается в «жёсткий, если
    сокращение прошло»: при отказе биржи или превышении меньше
    минимального лота позиция живёт с повышенным риском. Бот её намеренно
    не трогает — это решение владельца, — но признак жил ОДНОЙ строкой в
    логе за всю жизнь позиции, то есть при отсутствии алертинга нигде."""
    import strategy.trader as tr
    from core.state import state, Position
    state.over_risk.clear()
    state.balance = 1000.0
    pos = Position(symbol="RISKUSDT", side="Buy", entry=100.0, sl=90.0,
                   tp1=0.0, tp2=0.0, tp3=0.0, qty=5.0, qty_opened=5.0,
                   score=50, signal_type="X", order_id="o")
    # риск = 5 * 10 = 50 USDT = 5% баланса при потолке 3%
    state.positions["RISKUSDT"] = pos
    tr._RISK_WARNED.discard("RISKUSDT")

    live = {"symbol": "RISKUSDT", "side": "Buy", "size": "5",
            "avgPrice": "100.0", "stopLoss": "90.0", "takeProfit": "0",
            "unrealisedPnl": "0"}

    class C:
        api_key = "k"
        secret = "s"

        async def get_positions(self, symbol=None):
            return [live]

        async def get_balance(self):
            return 1000.0

        async def get_tickers(self, symbol=None):
            return [{"symbol": "RISKUSDT", "lastPrice": "100"}]

    await tr.monitor_positions(C())
    assert state.over_risk.get("RISKUSDT") == pytest.approx(5.0), (
        f"превышение риска не доходит до экрана: {state.over_risk}")


async def test_over_risk_clears_when_the_position_is_reduced(monkeypatch):
    """Признак обязан быть АКТУАЛЬНЫМ: сократили вручную — строка уходит
    сама. Иначе предупреждение висит вечно и его перестают читать."""
    import strategy.trader as tr
    from core.state import state, Position
    state.over_risk["RISKUSDT"] = 5.0
    state.balance = 1000.0
    pos = Position(symbol="RISKUSDT", side="Buy", entry=100.0, sl=90.0,
                   tp1=0.0, tp2=0.0, tp3=0.0, qty=1.0, qty_opened=5.0,
                   score=50, signal_type="X", order_id="o")
    state.positions["RISKUSDT"] = pos
    live = {"symbol": "RISKUSDT", "side": "Buy", "size": "1",
            "avgPrice": "100.0", "stopLoss": "90.0", "takeProfit": "0",
            "unrealisedPnl": "0"}

    class C:
        api_key = "k"
        secret = "s"

        async def get_positions(self, symbol=None):
            return [live]

        async def get_balance(self):
            return 1000.0

        async def get_tickers(self, symbol=None):
            return [{"symbol": "RISKUSDT", "lastPrice": "100"}]

    await tr.monitor_positions(C())
    assert "RISKUSDT" not in state.over_risk, \
        "предупреждение висит после сокращения позиции"
