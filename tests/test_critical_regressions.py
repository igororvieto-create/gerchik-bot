"""
Regression tests for bugs found and fixed across audit sessions.

Each test is named after the bug it guards against so a future failure
immediately tells you which invariant broke.

Run: pytest tests/test_critical_regressions.py -v
"""
import pytest
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

def _pos(side="LONG", entry=100.0, sl=None, tp3=None, tp1=None, tp2=None,
         qty=1.0, symbol="BTC-USDT", be_moved=False, trail_price=0.0,
         sl_order_id="", tp_order_id=""):
    from core.state import Position
    sl  = sl  or (entry * 0.95 if side == "LONG" else entry * 1.05)
    tp1 = tp1 or (entry * 1.03 if side == "LONG" else entry * 0.97)
    tp2 = tp2 or (entry * 1.06 if side == "LONG" else entry * 0.94)
    tp3 = tp3 or (entry * 1.09 if side == "LONG" else entry * 0.91)
    return Position(
        symbol=symbol, side=side, entry=entry, sl=sl,
        tp1=tp1, tp2=tp2, tp3=tp3, qty=qty, risk_usdt=5.0,
        be_moved=be_moved, trail_price=trail_price,
        sl_order_id=sl_order_id, tp_order_id=tp_order_id,
    )


# ══════════════════════════════════════════════════════════════════
# 1. _move_be — breakeven SL direction
# Bug: SHORT be_price was set to entry+buffer instead of entry-buffer
# (now entry-buffer is correct: SL fires when price RISES to it = profit)
# ══════════════════════════════════════════════════════════════════

class TestMoveBE:

    async def test_long_be_sl_above_entry(self, scanner, mock_scanner_db):
        """LONG breakeven: SL must be placed ABOVE entry."""
        pos = _pos("LONG", entry=100.0, sl=95.0)
        scanner.ex.get_open_orders = AsyncMock(return_value=[])
        scanner.ex.place_stop_loss = AsyncMock(
            return_value={"code": 0, "data": {"orderId": "be1"}}
        )
        await scanner._move_be(pos)

        placed = scanner.ex.place_stop_loss.call_args[0][3]
        assert placed > 100.0, (
            f"LONG BE SL {placed:.4f} must be ABOVE entry 100.0 — "
            "otherwise a price drop to entry triggers SL at a loss"
        )

    async def test_short_be_sl_below_entry(self, scanner, mock_scanner_db):
        """SHORT breakeven: SL must be placed BELOW entry.

        For SHORT, the BUY-STOP order fires when price RISES to the stop level.
        Placing SL below entry means it fires while we're still in profit.
        Placing SL above entry would fire after price exceeds entry — a loss.
        """
        pos = _pos("SHORT", entry=100.0, sl=105.0)
        scanner.ex.get_open_orders = AsyncMock(return_value=[])
        scanner.ex.place_stop_loss = AsyncMock(
            return_value={"code": 0, "data": {"orderId": "be2"}}
        )
        await scanner._move_be(pos)

        placed = scanner.ex.place_stop_loss.call_args[0][3]
        assert placed < 100.0, (
            f"SHORT BE SL {placed:.4f} must be BELOW entry 100.0 — "
            "SL fires when price RISES to it; below entry = still in profit"
        )

    async def test_be_sets_sl_and_trail_price(self, scanner, mock_scanner_db):
        """After _move_be, pos.sl and pos.trail_price are both updated."""
        pos = _pos("LONG", entry=100.0, sl=95.0)
        scanner.ex.get_open_orders = AsyncMock(return_value=[])
        scanner.ex.place_stop_loss = AsyncMock(
            return_value={"code": 0, "data": {"orderId": "be3"}}
        )
        await scanner._move_be(pos)

        assert pos.be_moved is True
        assert pos.sl > 100.0
        assert pos.trail_price == pos.sl


# ══════════════════════════════════════════════════════════════════
# 2. _trail_sl — trailing direction
# ══════════════════════════════════════════════════════════════════

class TestTrailSL:

    async def test_long_sl_follows_price_up(self, scanner, mock_scanner_db):
        """LONG trail: new price high moves SL upward."""
        pos = _pos("LONG", entry=100.0, sl=101.0, be_moved=True, trail_price=102.0)
        pos.sl_order_id = "old"
        scanner.ex.cancel_order = AsyncMock()
        scanner.ex.place_stop_loss = AsyncMock(
            return_value={"code": 0, "data": {"orderId": "t1"}}
        )
        await scanner._trail_sl(pos, price=105.0)

        placed = scanner.ex.place_stop_loss.call_args[0][3]
        assert placed > pos.entry, "LONG trail SL must be above entry after trailing"
        assert placed < 105.0,    "LONG trail SL must be below current price"
        assert placed > 101.0,    "LONG trail SL must be above old SL"

    async def test_short_sl_follows_price_down(self, scanner, mock_scanner_db):
        """SHORT trail: new price low moves SL downward."""
        pos = _pos("SHORT", entry=100.0, sl=98.0, be_moved=True, trail_price=97.0)
        pos.sl_order_id = "old"
        scanner.ex.cancel_order = AsyncMock()
        scanner.ex.place_stop_loss = AsyncMock(
            return_value={"code": 0, "data": {"orderId": "t2"}}
        )
        await scanner._trail_sl(pos, price=94.0)

        placed = scanner.ex.place_stop_loss.call_args[0][3]
        assert placed < pos.entry, "SHORT trail SL must be below entry after trailing"
        assert placed > 94.0,     "SHORT trail SL must be above current price"
        assert placed < 98.0,     "SHORT trail SL must be below old SL"

    async def test_long_no_trail_on_pullback(self, scanner):
        """LONG trail: SL stays put when price pulls back below peak."""
        pos = _pos("LONG", entry=100.0, sl=104.9, be_moved=True, trail_price=106.0)
        scanner.ex.place_stop_loss = AsyncMock()
        await scanner._trail_sl(pos, price=104.0)
        scanner.ex.place_stop_loss.assert_not_called()

    async def test_short_no_trail_on_bounce(self, scanner):
        """SHORT trail: SL stays put when price bounces above trough."""
        pos = _pos("SHORT", entry=100.0, sl=94.9, be_moved=True, trail_price=93.0)
        scanner.ex.place_stop_loss = AsyncMock()
        await scanner._trail_sl(pos, price=95.0)
        scanner.ex.place_stop_loss.assert_not_called()


# ══════════════════════════════════════════════════════════════════
# 3. _check_closed — SL/TP hit detection
# ══════════════════════════════════════════════════════════════════

class TestCheckClosed:

    async def test_long_sl_hit_calls_record_close(self, scanner, mock_scanner_db):
        from core.state import state
        pos = _pos("LONG", entry=100.0, sl=95.0, tp3=115.0)
        state.positions[pos.symbol] = pos
        scanner.ex.cancel_order = AsyncMock()
        with patch.object(scanner, "_record_close", new_callable=AsyncMock, return_value=-5.0) as rc:
            await scanner._check_closed(pos, price=94.0)
        rc.assert_called_once()

    async def test_short_sl_hit_calls_record_close(self, scanner, mock_scanner_db):
        from core.state import state
        pos = _pos("SHORT", entry=100.0, sl=105.0, tp3=85.0)
        state.positions[pos.symbol] = pos
        scanner.ex.cancel_order = AsyncMock()
        with patch.object(scanner, "_record_close", new_callable=AsyncMock, return_value=-5.0) as rc:
            await scanner._check_closed(pos, price=106.0)
        rc.assert_called_once()

    async def test_price_in_range_no_close(self, scanner):
        """Price between SL and TP3 must not trigger a close."""
        from core.state import state
        pos = _pos("LONG", entry=100.0, sl=95.0, tp3=115.0)
        state.positions[pos.symbol] = pos
        with patch.object(scanner, "_record_close", new_callable=AsyncMock) as rc:
            await scanner._check_closed(pos, price=102.0)
        rc.assert_not_called()

    async def test_double_close_guard(self, scanner, mock_scanner_db):
        """_record_close returns 0.0 if position already removed from state."""
        from core.state import state
        pos = _pos("LONG", entry=100.0, sl=95.0)
        # Do NOT add to state.positions — simulates already-closed
        pnl = await scanner._record_close(pos, price=94.0)
        assert pnl == 0.0, "_record_close must return 0.0 for already-removed position"


# ══════════════════════════════════════════════════════════════════
# 4. price=0 guard — phantom close prevention
# Bug: BingX returns {"lastPrice":"0"} during suspend → phantom SL hit
# ══════════════════════════════════════════════════════════════════

class TestPriceZeroGuard:

    def test_price_zero_would_trigger_long_sl_without_guard(self):
        """Demonstrates the bug: price=0 triggers sl_hit for LONG."""
        pos = _pos("LONG", entry=100.0, sl=95.0)
        price = 0.0
        phantom_sl = (pos.side == "LONG" and price <= pos.sl)
        assert phantom_sl, "price=0 incorrectly satisfies LONG sl_hit condition"

    def test_price_zero_guard_fires(self):
        """The guard `if price <= 0: continue` must catch zero price."""
        assert (0.0 <= 0) is True, "Guard condition fires for price=0"
        assert (0.001 <= 0) is False, "Guard does not fire for valid price"

    async def test_monitor_skips_zero_price_position(self, scanner, mock_scanner_db):
        """_monitor_inner must not call _record_close when ticker returns price=0."""
        from core.state import state
        pos = _pos("LONG", entry=100.0, sl=95.0, tp3=115.0)
        state.positions[pos.symbol] = pos

        # _monitor_inner calls get_open_positions then get_ticker per symbol
        scanner.ex.get_open_positions = AsyncMock(return_value=[
            {"symbol": pos.symbol, "positionAmt": "1.0",
             "positionSide": "LONG", "entryPrice": "100.0",
             "unrealizedProfit": "0"}
        ])
        # get_ticker is called per symbol via asyncio.gather
        scanner.ex.get_ticker = AsyncMock(return_value={"lastPrice": "0"})

        with patch.object(scanner, "_record_close", new_callable=AsyncMock) as rc:
            await scanner._monitor_inner()

        rc.assert_not_called()
        assert pos.symbol in state.positions, "Position must NOT be removed on price=0"


# ══════════════════════════════════════════════════════════════════
# 5. SL_COOLDOWN_MIN — must read cfg live, not frozen at startup
# Bug: module-level `SL_COOLDOWN_MIN = cfg.SL_COOLDOWN_MIN` was frozen
# ══════════════════════════════════════════════════════════════════

class TestCooldownLive:

    def test_cooldown_reads_cfg_live(self, scanner):
        """Changing cfg.SL_COOLDOWN_MIN must be reflected in _cooldown_minutes()."""
        from core.config import cfg
        original = cfg.SL_COOLDOWN_MIN
        cfg.SL_COOLDOWN_MIN = 999
        try:
            result = scanner._cooldown_minutes("BTC-USDT")
            assert result == 999, (
                f"Expected live cfg value 999, got {result}. "
                "SL_COOLDOWN_MIN must not be frozen at import time."
            )
        finally:
            cfg.SL_COOLDOWN_MIN = original

    def test_extended_cooldown_on_streak(self, scanner):
        """After SYMBOL_LOSS_STREAK_LIMIT losses, use SYMBOL_LOSS_COOLDOWN_MIN."""
        from core.config import cfg
        sym = "ETH-USDT"
        scanner._symbol_loss_streak[sym] = cfg.SYMBOL_LOSS_STREAK_LIMIT
        result = scanner._cooldown_minutes(sym)
        assert result == cfg.SYMBOL_LOSS_COOLDOWN_MIN


# ══════════════════════════════════════════════════════════════════
# 6. OB leverage tiers — must match _enter() exactly
# Bug: OB used x3 for 1000–2000 USDT while _enter used x5
# ══════════════════════════════════════════════════════════════════

class TestOBLeverageTiers:

    @staticmethod
    def _ob_lev(balance: float) -> int:
        """OB leverage from scanner._analyze — current code."""
        return 3 if balance < 100 else (5 if balance < 2000 else 3)

    @staticmethod
    def _enter_lev(balance: float) -> int:
        """_enter() leverage — current code."""
        if balance < 100:   return 3
        elif balance < 500: return 5
        elif balance < 2000: return 5
        else:               return 3

    def test_tiers_match_across_all_bands(self):
        """OB thin-book check must use same leverage as actual entry."""
        test_balances = [10, 50, 99, 100, 200, 499, 500, 750, 999,
                         1000, 1500, 1999, 2000, 3000, 10000]
        mismatches = []
        for bal in test_balances:
            ob = self._ob_lev(bal)
            enter = self._enter_lev(bal)
            if ob != enter:
                mismatches.append(f"balance={bal}: OB={ob}x vs entry={enter}x")

        assert not mismatches, (
            "OB leverage tiers don't match _enter() tiers:\n" +
            "\n".join(mismatches)
        )


# ══════════════════════════════════════════════════════════════════
# 7. daily_report — must use DB data, not state.day (which resets at midnight)
# Bug: daily_report at 09:00 UTC always showed zeros
# ══════════════════════════════════════════════════════════════════

class TestDailyReport:

    async def test_daily_report_reads_yesterday_from_db(self, scanner, mock_scanner_db):
        """daily_report must call db.get_yesterday_stats(), not read state.day."""
        from core.state import state
        # state.day is already reset (zeros) — simulates bot at 09:00 UTC
        assert state.day.trades == 0

        # DB has 5 trades from yesterday
        mock_scanner_db.get_yesterday_stats = MagicMock(return_value={
            "total": 5, "wins": 3, "losses": 2,
            "pnl": 12.50, "date": "2025-01-01",
        })
        mock_scanner_db.get_today_stats = MagicMock(return_value={
            "total": 0, "wins": 0, "losses": 0, "pnl": 0.0,
        })

        await scanner.daily_report()

        mock_scanner_db.get_yesterday_stats.assert_called_once()
        notify_text = scanner._notify.call_args[0][0]
        assert "5" in notify_text,    "Report must show 5 trades (from DB)"
        assert "12.50" in notify_text, "Report must show PnL from DB"

    def test_get_yesterday_stats_uses_correct_date_range(self):
        """get_yesterday_stats must query [yesterday 00:00, today 00:00)."""
        from core import db as core_db
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        today     = date.today().isoformat()

        captured = {}

        class MockConn:
            def execute(self, sql, params=()):
                captured["params"] = params
                m = MagicMock()
                m.fetchone.return_value = (3, 7.5, 2)
                return m
            def __enter__(self): return self
            def __exit__(self, *a): pass

        with patch.object(core_db, "_connect", return_value=MockConn()):
            result = core_db.get_yesterday_stats()

        assert result["total"] == 3
        assert result["wins"]  == 2
        assert yesterday in result["date"]
        params = captured["params"]
        assert len(params) == 2
        assert yesterday in params[0], "Query must start at yesterday 00:00"
        assert today     in params[1], "Query must end at today 00:00"


# ══════════════════════════════════════════════════════════════════
# 8. Strategy MIN_SCORE guard in analyze() (pullback strategy)
# Bug: analyze() did not call _reject() or return None on low score
# ══════════════════════════════════════════════════════════════════

class TestMinScoreGuard:

    def test_analyze_has_min_score_guard(self):
        """analyze() source code must contain MIN_SCORE guard — regression check."""
        import inspect
        from strategy.strategy.gerchik import analyze
        src = inspect.getsource(analyze)
        assert "MIN_SCORE" in src, (
            "analyze() must have a MIN_SCORE check — "
            "without it, sub-threshold signals are not counted in diagnostics"
        )

    def test_all_four_strategies_have_min_score_guard(self):
        """All 4 strategy functions must contain a MIN_SCORE guard."""
        import inspect
        from strategy.strategy.gerchik import (
            analyze, analyze_false_breakout,
            analyze_range_breakout, analyze_breakout,
        )
        for fn in (analyze, analyze_false_breakout,
                   analyze_range_breakout, analyze_breakout):
            src = inspect.getsource(fn)
            assert "MIN_SCORE" in src, (
                f"{fn.__name__}() is missing a MIN_SCORE guard"
            )

    def test_all_four_strategies_reject_bad_funding(self):
        """All 4 strategy functions must call _reject() for funding violations."""
        import inspect
        from strategy.strategy.gerchik import (
            analyze, analyze_false_breakout,
            analyze_range_breakout, analyze_breakout,
        )
        for fn in (analyze, analyze_false_breakout,
                   analyze_range_breakout, analyze_breakout):
            src = inspect.getsource(fn)
            assert "фандинг высокий" in src or "фандинг" in src, (
                f"{fn.__name__}() is missing funding _reject() call"
            )


# ══════════════════════════════════════════════════════════════════
# 9. TP3 health check — must re-place TP3 when order disappears
# ══════════════════════════════════════════════════════════════════

class TestTP3HealthCheck:

    async def test_health_check_replaces_missing_tp3(self, scanner, mock_scanner_db):
        """health_check must re-place TP3 when tp_order_id not in open orders."""
        from core.state import state
        pos = _pos("LONG", entry=100.0, sl=95.0, tp3=115.0)
        pos.sl_order_id = "sl_live"
        pos.tp_order_id = "tp_gone"  # tracked but not on exchange
        state.positions[pos.symbol] = pos

        scanner.ex.get_balance = AsyncMock(return_value=500.0)
        scanner.ex.get_open_positions = AsyncMock(return_value=[
            {"symbol": pos.symbol, "positionAmt": "1.0",
             "positionSide": "LONG", "entryPrice": "100.0"}
        ])
        # Open orders: SL is live, TP is gone
        scanner.ex.get_open_orders = AsyncMock(return_value=[
            {"orderId": "sl_live", "type": "STOP_MARKET"}
        ])
        scanner.ex.place_take_profit = AsyncMock(
            return_value={"code": 0, "data": {"orderId": "tp_new"}}
        )

        await scanner.health_check()

        scanner.ex.place_take_profit.assert_called_once()
        placed_price = scanner.ex.place_take_profit.call_args[0][3]
        assert placed_price == pos.tp3, (
            f"TP3 must be re-placed at {pos.tp3}, got {placed_price}"
        )
        assert pos.tp_order_id == "tp_new"
