"""Инварианты CLAUDE.md — нарушение любого из них означает потерю денег.

Каждый тест здесь соответствует багу, который УЖЕ случался в этом проекте.
Если тест падает — значит вернулся конкретный баг, а не «что-то сломалось».
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from core.config import cfg
from core.state import state, Signal, Position
import strategy.trader as tr


def mk_signal(symbol="TESTUSDT", direction="LONG", entry=100.0, sl=98.5,
              tp2=103.0, headroom=2.5, score=60):
    return Signal(
        symbol=symbol, signal_type="VSA_CLIMAX", direction=direction, score=score,
        price=entry, oi_change=5, vol_ratio=3, funding=-0.04, ob_bias="BUY",
        atr_pct=1.5, details="", entry=entry, sl=sl, tp1=0.0, tp2=tp2, tp3=0.0,
        rr=2.0, headroom=headroom,
        sl_pct=(abs(entry - sl) / entry * 100) if entry > 0 else 0.0,
    )


class FakeExchange:
    """Подставная биржа, которая ЧЕСТНО моделирует прикрепление стопа."""
    api_key = "k"
    secret = "s"

    def __init__(self, attach_sl=True, attach_tp=True, fill_px=None,
                 tick="0.00001", step="0.001"):
        self.attach_sl = attach_sl
        self.attach_tp = attach_tp
        self.fill_px = fill_px
        self.tick, self.step = tick, step
        self.pos = None
        self.orders = []
        self.stop_calls = []

    async def get_balance(self):
        return 1000.0

    async def get_instrument_info(self, symbol):
        return {"lotSizeFilter": {"qtyStep": self.step, "minOrderQty": self.step,
                                  "minNotionalValue": "5"},
                "priceFilter": {"tickSize": self.tick}}

    async def set_leverage(self, symbol, lev):
        return True

    async def place_order(self, symbol, side, qty, sl, tp):
        self.orders.append({"symbol": symbol, "side": side, "qty": qty,
                            "sl": sl, "tp": tp})
        self.pos = {"symbol": symbol, "side": side, "size": str(qty),
                    "avgPrice": str(self.fill_px or 100.0),
                    "markPrice": str(self.fill_px or 100.0),
                    "stopLoss": str(sl) if self.attach_sl else "",
                    "takeProfit": str(tp) if self.attach_tp else ""}
        return {"retCode": 0, "result": {"orderId": "oid-1"}}

    async def get_positions(self):
        return [self.pos] if self.pos else []

    async def get_position(self, symbol):
        return self.pos if (self.pos and self.pos["symbol"] == symbol) else {}

    async def set_trading_stop(self, symbol, sl=0.0, tp=0.0):
        self.stop_calls.append({"sl": sl, "tp": tp})
        if self.pos:
            if sl > 0 and self.attach_sl:
                self.pos["stopLoss"] = str(sl)
            if tp > 0 and self.attach_tp:
                self.pos["takeProfit"] = str(tp)
        return True

    async def close_position(self, symbol, side, qty):
        if self.pos:
            left = abs(float(self.pos["size"])) - qty
            self.pos["size"] = str(max(left, 0.0))
            if left <= 0:
                self.pos = None
        return {"retCode": 0}

    async def get_closed_pnl(self, symbol, limit=5):
        return [{"updatedTime": str(int(datetime.utcnow().timestamp() * 1000) + 5000),
                 "avgExitPrice": "99", "closedPnl": "-1.0"}]


@pytest.fixture(autouse=True)
def clean_state():
    """Каждый тест стартует с чистого состояния и штатных настроек."""
    saved = (cfg.AUTO_TRADE, cfg.RISK_PER_TRADE, cfg.MAX_MARGIN_PCT)
    state.positions.clear()
    state.pending_entries.clear()
    tr._SL_RETRIES.clear()
    tr._TP_RETRIES.clear()
    tr._ENTERING = 0
    state.daily_pnl_date = tr._today_utc()
    state.daily_realized_pnl = 0.0
    state.trading_halted = False
    state.balance = 1000.0
    cfg.AUTO_TRADE = True
    yield
    cfg.AUTO_TRADE, cfg.RISK_PER_TRADE, cfg.MAX_MARGIN_PCT = saved
    state.positions.clear()
    state.pending_entries.clear()


# ── Рецидивирующий баг №1: SL как чарт-маркер ────────────────────────────────

async def test_sl_reaches_exchange_and_is_verified():
    """Стоп обязан оказаться НА БИРЖЕ, а не только в ответе retCode==0."""
    ex = FakeExchange()
    ok = await tr.enter_trade(ex, mk_signal())
    assert ok is True
    assert ex.pos is not None
    assert float(ex.pos["stopLoss"]) > 0, "позиция открыта без стопа на бирже"


async def test_unprotected_position_is_emergency_closed():
    """Если биржа не прикрепила стоп — позиция закрывается, а не остаётся жить."""
    ex = FakeExchange(attach_sl=False)
    ok = await tr.enter_trade(ex, mk_signal())
    assert ok is False
    assert ex.pos is None, "незащищённая позиция осталась на бирже"
    assert not isinstance(state.positions.get("TESTUSDT"), Position)


async def test_missing_tp_does_not_close_protected_position():
    """Отсутствие ТОЛЬКО тейка не повод закрывать: риск закрыт стопом."""
    ex = FakeExchange(attach_tp=False)
    ok = await tr.enter_trade(ex, mk_signal())
    assert ok is True
    assert ex.pos is not None
    assert float(ex.pos["stopLoss"]) > 0


# ── Инвариант: риск на сделку не выше 3% ─────────────────────────────────────

@pytest.mark.parametrize("fill_px,label", [(100.0, "без проскальзывания"),
                                           (100.8, "проскальзывание 0.8%"),
                                           (101.5, "проскальзывание 1.5%")])
async def test_risk_never_exceeds_three_percent(fill_px, label):
    """Потолок 3% считается по ФАКТИЧЕСКОЙ цене входа, а не по цене сигнала."""
    cfg.RISK_PER_TRADE = 3.0
    cfg.MAX_MARGIN_PCT = 50.0   # снимаем потолок маржи, чтобы дойти до ветки риска
    ex = FakeExchange(fill_px=fill_px)
    await tr.enter_trade(ex, mk_signal())
    if ex.pos is None:
        return  # вход отменён — инвариант не нарушен
    qty = abs(float(ex.pos["size"]))
    sl_on_exch = float(ex.pos["stopLoss"])
    risk = qty * abs(fill_px - sl_on_exch)
    assert risk <= 1000.0 * 3.0 / 100 + 1e-6, f"{label}: риск {risk/10:.2f}% > 3%"


async def test_position_qty_stays_in_sync_after_reduction():
    """После сокращения позиции учёт обязан совпадать с биржей."""
    cfg.RISK_PER_TRADE = 3.0
    cfg.MAX_MARGIN_PCT = 50.0
    ex = FakeExchange(fill_px=100.8)
    await tr.enter_trade(ex, mk_signal())
    pos = state.positions.get("TESTUSDT")
    if ex.pos and isinstance(pos, Position):
        assert abs(pos.qty - abs(float(ex.pos["size"]))) < 1e-9


# ── Инвариант: SL обязателен ДО входа ────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    {"sl": 0.0},          # стопа нет вовсе
    {"entry": 0.0},       # нет цены входа
])
async def test_entry_refused_without_valid_stop(bad):
    sig = mk_signal(**bad)
    if bad.get("sl") == 0.0:
        sig.sl_pct = 0.0
    ex = FakeExchange()
    assert await tr.enter_trade(ex, sig) is False
    assert ex.pos is None


# ── Гейт запаса до цели ──────────────────────────────────────────────────────

async def test_low_headroom_signal_is_not_traded():
    """Запас меньше MIN_TRADE_HEADROOM_R: TP2 стоит за уровнем — не торгуем."""
    ex = FakeExchange()
    ok = await tr.enter_trade(ex, mk_signal(headroom=1.7))
    assert ok is False
    assert ex.pos is None
    assert "TESTUSDT" not in state.positions, "слот остался занят"
    assert "TESTUSDT" not in state.pending_entries, "бронь не снята"


async def test_missing_headroom_is_refusal_not_bypass():
    """headroom==0 означает «данных нет» и обязан быть отказом."""
    ex = FakeExchange()
    assert await tr.enter_trade(ex, mk_signal(headroom=0.0)) is False


# ── Регрессия: утечка брони останавливала торговлю ───────────────────────────

async def test_pending_entry_released_on_success():
    """Бронь снимается при успешном входе, иначе позиция считается дважды."""
    ex = FakeExchange()
    await tr.enter_trade(ex, mk_signal())
    assert "TESTUSDT" not in state.pending_entries


async def test_direction_cap_counts_each_position_once():
    """MAX_SAME_DIRECTION=2 обязан пропускать ВТОРОЙ вход в ту же сторону."""
    assert cfg.MAX_SAME_DIRECTION >= 2
    ex = FakeExchange()
    assert await tr.enter_trade(ex, mk_signal(symbol="AAAUSDT")) is True
    ex2 = FakeExchange()
    assert await tr.enter_trade(ex2, mk_signal(symbol="BBBUSDT")) is True, \
        "живая позиция посчиталась дважды (Position + собственная бронь)"


async def test_retry_counters_die_with_position():
    """Счётчики попыток обязаны умирать вместе с символом."""
    tr._SL_RETRIES["ZZZUSDT"] = 2
    tr._TP_RETRIES["ZZZUSDT"] = 3
    tr._forget_symbol("ZZZUSDT")
    assert "ZZZUSDT" not in tr._SL_RETRIES
    assert "ZZZUSDT" not in tr._TP_RETRIES


# ── Частичное закрытие ───────────────────────────────────────────────────────

async def test_partial_close_keeps_position_tracked():
    """retCode==0 не значит «закрыто»: остаток обязан остаться под наблюдением."""
    class PartialEx(FakeExchange):
        async def close_position(self, symbol, side, qty):
            self.pos["size"] = "600"     # залилось частично
            return {"retCode": 0}

    ex = PartialEx()
    ex.pos = {"symbol": "TESTUSDT", "side": "Buy", "size": "1000",
              "avgPrice": "100", "markPrice": "100", "stopLoss": "98", "takeProfit": "103"}
    closed, remaining = await tr.close_and_verify(ex, "TESTUSDT", "Buy", 1000)
    assert closed is False
    assert remaining == 600.0


async def test_close_and_verify_reports_unknown_state():
    """Недоступный API — это «неизвестно», а не «закрыто»."""
    class BlindEx(FakeExchange):
        async def get_position(self, symbol):
            return None

    ex = BlindEx()
    ex.pos = {"symbol": "T", "side": "Buy", "size": "10", "avgPrice": "100",
              "stopLoss": "98", "takeProfit": "103"}
    closed, remaining = await tr.close_and_verify(ex, "T", "Buy", 10)
    assert closed is False
    assert remaining == -1.0
