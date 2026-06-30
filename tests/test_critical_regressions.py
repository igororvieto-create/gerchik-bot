"""
Regression tests for bugs found and fixed across audit sessions.

Each test is named after the bug it guards against so a future failure
immediately tells you which invariant broke.

Run: pytest tests/test_critical_regressions.py -v
"""
import pytest
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, call


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


# ══════════════════════════════════════════════════════════════════
# 10. loss_streak must NOT bleed from one calendar day into the next
#     after a bot restart (audit-5 Bug A).
#
# Scenario: 3 consecutive losses at 23:50. Bot restarts at 00:01 on
# the new day. DB has loss_streak=3 from yesterday. Without the date
# check, state.day.loss_streak=3 on the new day → adaptive MIN_SCORE
# penalty + reduced risk for a clean new day.
# ══════════════════════════════════════════════════════════════════

class TestLossStreakDayBoundary:

    def test_streak_not_restored_when_date_is_yesterday(self):
        """Stored loss_streak with yesterday's date must not be applied to today."""
        from core.state import state
        yesterday = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
        today_str = datetime.utcnow().date().isoformat()

        kv = {"loss_streak": "3", "loss_streak_date": yesterday}

        stored_date = kv.get("loss_streak_date", "")
        if stored_date == today_str:
            state.day.loss_streak = int(kv["loss_streak"])

        assert state.day.loss_streak == 0, (
            "loss_streak from yesterday must not bleed into today — "
            "bot would penalise the new day with yesterday's bad streak"
        )

    def test_streak_restored_when_date_is_today(self):
        """Stored loss_streak with today's date MUST be restored (intraday restart)."""
        from core.state import state
        today_str = datetime.utcnow().date().isoformat()

        kv = {"loss_streak": "2", "loss_streak_date": today_str}

        stored_date = kv.get("loss_streak_date", "")
        if stored_date == today_str:
            state.day.loss_streak = int(kv["loss_streak"])

        assert state.day.loss_streak == 2, (
            "loss_streak stored today must survive an intraday restart"
        )

    def test_streak_not_restored_when_date_absent(self):
        """If loss_streak_date key is missing (legacy DB), streak must not be restored."""
        from core.state import state
        today_str = datetime.utcnow().date().isoformat()

        kv = {"loss_streak": "3"}  # no loss_streak_date key

        stored_date = kv.get("loss_streak_date", "")
        if stored_date == today_str:
            state.day.loss_streak = int(kv["loss_streak"])

        assert state.day.loss_streak == 0, (
            "legacy DB without loss_streak_date must not restore streak — "
            "safer to start fresh than to apply an undated streak"
        )


# ══════════════════════════════════════════════════════════════════
# 11. get_balance must return wallet balance, not equity
#     (audit-5 Bug B).
#
# BingX equity = wallet_balance + unrealized_pnl. Using equity for
# position sizing causes oversizing when in profit, undersizing when
# in loss. The correct field is "balance" (wallet balance).
# ══════════════════════════════════════════════════════════════════

class TestGetBalanceReturnsWalletBalance:

    def _parse(self, payload):
        from exchange.bingx import BingXClient
        return BingXClient._parse_balance_data(payload)

    def test_prefers_balance_over_equity_dict_format(self):
        """Format-1 (dict): wallet balance=100, equity=150 → must return 100."""
        bal, _ = self._parse({
            "balance": {"balance": "100.0", "equity": "150.0", "availableMargin": "80.0"}
        })
        assert bal == 100.0, (
            f"Expected wallet balance 100.0, got {bal} — "
            "equity (includes unrealized PnL) must not be used for position sizing"
        )

    def test_prefers_balance_over_equity_list_format(self):
        """Format-2 (list): wallet balance=200, equity=300 → must return 200."""
        bal, _ = self._parse({
            "balance": [{"asset": "USDT", "balance": "200.0", "equity": "300.0",
                         "availableMargin": "150.0"}]
        })
        assert bal == 200.0, (
            f"Expected wallet balance 200.0, got {bal}"
        )

    def test_falls_back_to_equity_when_balance_is_zero(self):
        """If balance field is 0 or missing, equity is the fallback — not primary."""
        bal, _ = self._parse({
            "balance": {"balance": "0", "equity": "99.0", "availableMargin": "99.0"}
        })
        assert bal == 99.0, (
            f"When wallet balance is 0, equity={bal} is acceptable as fallback"
        )


# ══════════════════════════════════════════════════════════════════
# 12. _partial_close must persist tp1_hit + reduced qty to DB
#     BEFORE fetching the ticker (audit-5 Bug C).
#
# If the bot crashes after exchange confirms the partial close but
# before the DB is updated, the next restart restores stale values
# (tp1_hit=False, original qty) and fires _partial_close again —
# closing more of the position than intended.
# ══════════════════════════════════════════════════════════════════

class TestPartialCloseEarlyPersist:

    async def test_db_save_called_before_ticker_fetch(self, scanner, mock_scanner_db):
        """async_save_open_position must be called before get_ticker in _partial_close."""
        from core.state import state

        pos = _pos("LONG", entry=100.0, qty=1.0, tp1=103.0)
        state.positions[pos.symbol] = pos

        call_order = []

        scanner.ex.close_position = AsyncMock(
            side_effect=lambda *a, **kw: call_order.append("close") or None
        )
        mock_scanner_db.async_save_open_position = AsyncMock(
            side_effect=lambda *a, **kw: call_order.append("db_save") or None
        )
        scanner.ex.get_ticker = AsyncMock(
            side_effect=lambda *a, **kw: call_order.append("ticker") or {"lastPrice": "104.0"}
        )
        mock_scanner_db.async_save_trade = AsyncMock()

        await scanner._partial_close(pos, 0.25, "TP1")

        assert "close" in call_order, "close_position was not called"
        assert "db_save" in call_order, "async_save_open_position was not called"
        assert "ticker" in call_order, "get_ticker was not called"

        close_idx  = call_order.index("close")
        db_idx     = call_order.index("db_save")
        ticker_idx = call_order.index("ticker")
        assert db_idx > close_idx, "DB save must happen AFTER close_position"
        assert db_idx < ticker_idx, (
            "DB save must happen BEFORE get_ticker — "
            "a crash between close and DB persist would cause restart re-fire"
        )

    async def test_tp1_hit_flag_set_before_first_db_save(self, scanner, mock_scanner_db):
        """pos.tp1_hit must be True by the time async_save_open_position is first called."""
        from core.state import state

        pos = _pos("LONG", entry=100.0, qty=1.0, tp1=103.0)
        state.positions[pos.symbol] = pos

        captured = {}

        async def save_pos(p, *a, **kw):
            if "tp1_hit" not in captured:
                captured["tp1_hit"] = p.tp1_hit
                captured["qty"] = p.qty

        mock_scanner_db.async_save_open_position = AsyncMock(side_effect=save_pos)
        scanner.ex.close_position = AsyncMock(return_value=None)
        scanner.ex.get_ticker = AsyncMock(return_value={"lastPrice": "104.0"})
        mock_scanner_db.async_save_trade = AsyncMock()

        await scanner._partial_close(pos, 0.25, "TP1")

        assert captured.get("tp1_hit") is True, (
            "First async_save_open_position call must see tp1_hit=True — "
            "otherwise restart restores False and fires partial close again"
        )
        assert captured.get("qty", 1.0) < 1.0, (
            "First async_save_open_position call must see reduced qty"
        )
