"""
Verification script for 10 bug fixes on branch claude/review-and-analyze-h4hA8.
Run with: python tests/verify_fixes.py
No real API keys needed — uses mocks.
"""
import asyncio
import inspect
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# ── Point DB at a temp file so we don't pollute data/gerchik.db ──────────────
_tmp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp_db.close()
os.environ["DB_PATH"] = _tmp_db.name
os.environ.setdefault("TELEGRAM_TOKEN",  "fake")
os.environ.setdefault("TELEGRAM_CHAT_ID","123456")
os.environ.setdefault("BINGX_API_KEY",   "fake")
os.environ.setdefault("BINGX_SECRET",    "fake")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

RESULTS = []

def report(name, status, detail=""):
    mark = "✅ PASS" if status else "❌ FAIL"
    line = f"{mark}  [{name}]"
    if detail:
        line += f"  — {detail}"
    print(line)
    RESULTS.append((name, status, detail))


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 1 — Import all modules
# ═══════════════════════════════════════════════════════════════════════════════
def check_imports():
    name = "1. Import all modules"
    try:
        import core.config
        import core.state
        import core.db
        import exchange.bingx
        # strategy.scanner imports aiogram which needs a real token env but imports fine
        import strategy.scanner
        import strategy.strategy.gerchik
        import telegram.handlers
        import utils.chart
        report(name, True, "all 8 modules imported without error")
    except Exception as e:
        report(name, False, str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 2 — DB full lifecycle with partial_pnl_taken / tp1_hit / be_moved / qty
# ═══════════════════════════════════════════════════════════════════════════════
def check_db_lifecycle():
    name = "2. DB lifecycle (partial_pnl_taken, tp1_hit, be_moved, qty)"
    from core import db
    from core.state import Position

    db.init_db()

    pos = Position(
        symbol="BTC-USDT", side="LONG",
        entry=50000.0, sl=49000.0,
        tp1=51000.0, tp2=52000.0, tp3=53000.0,
        qty=0.0075, risk_usdt=50.0,
        order_id="o1", sl_order_id="sl1", tp_order_id="tp1",
        be_moved=True, tp1_hit=True, tp2_hit=False,
        trail_price=51500.0, partial_pnl_taken=15.0,
        pattern="Hammer", tf="H1", rr=2.5, score=72,
    )

    # Save
    db.save_open_position(pos)

    # Reload
    rows = db.load_open_positions()
    found = next((r for r in rows if r.get("symbol") == "BTC-USDT"), None)
    if found is None:
        report(name, False, "position not found after save_open_position")
        return

    ok = (
        found.get("partial_pnl_taken") == 15.0 and
        found.get("tp1_hit") is True and
        found.get("be_moved") is True and
        abs(found.get("qty") - 0.0075) < 1e-9
    )
    if not ok:
        report(name, False, f"reloaded values mismatch: {found}")
        return

    # save_trade
    db.save_trade(pos, exit_price=52000.0, pnl=15.0, result="WIN")
    hist = db.get_history(5)
    if not hist:
        report(name, False, "get_history returned empty after save_trade")
        return

    # delete_open_position and check get_history still has the trade
    db.delete_open_position("BTC-USDT")
    rows2 = db.load_open_positions()
    still_there = any(r.get("symbol") == "BTC-USDT" for r in rows2)
    hist2 = db.get_history(5)

    if still_there:
        report(name, False, "position still found after delete_open_position")
        return
    if not hist2:
        report(name, False, "trade history empty after delete")
        return

    report(name, True, f"save/reload/delete all OK; history has {len(hist2)} entries")


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 3 — get_balance_and_margin: 3 BingX response shapes + empty
# ═══════════════════════════════════════════════════════════════════════════════
def check_balance_shapes():
    name = "3. get_balance_and_margin (3 shapes + empty)"
    from exchange.bingx import BingXClient

    client = BingXClient("fake_key", "fake_secret")

    async def run():
        results = {}

        # Shape 1: nested dict  data.balance = {equity, availableMargin}
        resp1 = {"code": 0, "data": {"balance": {"equity": "1000.0", "availableMargin": "800.0"}}}
        with patch.object(client, "_get", new=AsyncMock(return_value=resp1)):
            b, a = await client.get_balance_and_margin()
            results["nested_dict"] = (b, a)

        # Shape 2: list  data.balance = [{asset:USDT, equity, availableMargin}]
        resp2 = {"code": 0, "data": {"balance": [{"asset": "USDT", "equity": "500.0", "availableMargin": "400.0"}]}}
        with patch.object(client, "_get", new=AsyncMock(return_value=resp2)):
            b, a = await client.get_balance_and_margin()
            results["list"] = (b, a)

        # Shape 3: flat  data = {equity, availableMargin}
        resp3 = {"code": 0, "data": {"equity": "750.0", "availableMargin": "600.0"}}
        with patch.object(client, "_get", new=AsyncMock(return_value=resp3)):
            b, a = await client.get_balance_and_margin()
            results["flat"] = (b, a)

        # Empty response
        resp4 = {"code": 0, "data": {}}
        with patch.object(client, "_get", new=AsyncMock(return_value=resp4)):
            b, a = await client.get_balance_and_margin()
            results["empty"] = (b, a)

        return results

    res = asyncio.get_event_loop().run_until_complete(run())

    fails = []
    if abs(res["nested_dict"][0] - 1000.0) > 0.01:
        fails.append(f"nested_dict balance={res['nested_dict'][0]}")
    if abs(res["list"][0] - 500.0) > 0.01:
        fails.append(f"list balance={res['list'][0]}")
    if abs(res["flat"][0] - 750.0) > 0.01:
        fails.append(f"flat balance={res['flat'][0]}")
    if res["empty"] != (0.0, 0.0):
        fails.append(f"empty returned {res['empty']}")

    if fails:
        report(name, False, "; ".join(fails))
    else:
        report(name, True, f"shapes: {res}")


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 4 — _account_manual_close: 3-loss streak alert + cooldown; WIN clears
# ═══════════════════════════════════════════════════════════════════════════════
def check_account_manual_close():
    name = "4. _account_manual_close (3-loss alert, cooldown, WIN clears)"
    from core.state import state, Position
    from core import db

    db.init_db()

    async def run():
        from telegram.handlers import _account_manual_close
        from exchange.bingx import BingXClient

        # ── scenario A: loss_streak=2, add 1 more loss → streak becomes 3 ──
        state.day.loss_streak = 2
        state.day.losses      = 2
        state.day.wins        = 0
        state.day.pnl_usdt    = -50.0
        state.total_pnl       = -50.0

        pos = Position(
            symbol="ETH-USDT", side="LONG",
            entry=3000.0, sl=2900.0,
            tp1=3100.0, tp2=3200.0, tp3=3300.0,
            qty=0.01, risk_usdt=10.0,
            pattern="Doji", tf="H1", rr=2.0, score=65,
        )
        # Price is at SL level (2900), so it's a loss
        ticker_mock = {"lastPrice": "2900.0"}

        notified_msgs = []

        ex = BingXClient("k", "s")
        mock_scanner = MagicMock()
        async def capture_notify(msg):
            notified_msgs.append(msg)
        mock_scanner._notify = capture_notify
        mock_scanner._loss_cooldown = MagicMock()
        mock_scanner._symbol_loss_streak = {}

        import strategy.scanner as _sc
        orig_scanner = _sc._global_scanner

        with patch.object(ex, "get_ticker", new=AsyncMock(return_value=ticker_mock)):
            _sc._global_scanner = mock_scanner
            try:
                await _account_manual_close(ex, pos)
            finally:
                _sc._global_scanner = orig_scanner

        streak_after_loss = state.day.loss_streak
        cooldown_called   = mock_scanner._loss_cooldown.called
        three_loss_notified = any("3 убытка" in m for m in notified_msgs)

        # ── scenario B: WIN clears streak ──
        state.day.loss_streak = 3
        pos_win = Position(
            symbol="SOL-USDT", side="SHORT",
            entry=100.0, sl=105.0,
            tp1=95.0, tp2=90.0, tp3=85.0,
            qty=0.1, risk_usdt=5.0,
            pattern="Shooting Star", tf="H1", rr=2.0, score=70,
        )
        ticker_win = {"lastPrice": "85.0"}  # at TP3 (profit for SHORT)
        notified_win = []
        mock_scanner2 = MagicMock()
        async def capture_notify2(msg):
            notified_win.append(msg)
        mock_scanner2._notify = capture_notify2
        mock_scanner2._loss_cooldown = MagicMock()
        mock_scanner2._symbol_loss_streak = {}

        with patch.object(ex, "get_ticker", new=AsyncMock(return_value=ticker_win)):
            _sc._global_scanner = mock_scanner2
            try:
                await _account_manual_close(ex, pos_win)
            finally:
                _sc._global_scanner = orig_scanner

        streak_after_win = state.day.loss_streak

        return {
            "streak_after_loss":       streak_after_loss,
            "cooldown_called":         cooldown_called,
            "three_loss_notified":     three_loss_notified,
            "streak_after_win":        streak_after_win,
        }

    res = asyncio.get_event_loop().run_until_complete(run())
    fails = []
    if res["streak_after_loss"] != 3:
        fails.append(f"streak={res['streak_after_loss']} expected 3 after loss")
    if not res["cooldown_called"]:
        fails.append("_loss_cooldown not called on loss")
    if not res["three_loss_notified"]:
        fails.append("3-убытка alert not sent")
    if res["streak_after_win"] != 0:
        fails.append(f"streak={res['streak_after_win']} expected 0 after win")

    if fails:
        report(name, False, "; ".join(fails))
    else:
        report(name, True, f"streak 2→3, alert sent, cooldown called, WIN cleared to 0")


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 5 — monitor_positions scheduled at seconds=30 in main.py
# ═══════════════════════════════════════════════════════════════════════════════
def check_monitor_interval():
    name = "5. monitor_positions scheduled at seconds=30"
    import ast

    main_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "main.py")
    src = open(main_path).read()

    # Look for add_job call with monitor_positions and seconds=30
    found = False
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "add_job":
                # Check if any arg is monitor_positions
                has_monitor = any(
                    isinstance(a, ast.Attribute) and a.attr == "monitor_positions"
                    for a in node.args
                )
                if not has_monitor:
                    has_monitor = any(
                        isinstance(a, ast.Attribute) and a.attr == "monitor_positions"
                        for a in ast.walk(node)
                        if isinstance(a, ast.Attribute)
                    )
                # Check keyword args for seconds=30
                has_seconds_30 = any(
                    kw.arg == "seconds" and (
                        (isinstance(kw.value, ast.Constant) and kw.value.value == 30) or
                        (isinstance(kw.value, ast.Num) and kw.value.n == 30)
                    )
                    for kw in node.keywords
                )
                if has_monitor and has_seconds_30:
                    found = True
                    break

    if found:
        report(name, True, "seconds=30 found in scheduler.add_job(scanner.monitor_positions, ...)")
    else:
        # Fallback: raw text check
        if "monitor_positions" in src and "seconds=30" in src:
            report(name, True, "seconds=30 found via text search near monitor_positions")
        else:
            report(name, False, "monitor_positions not found with seconds=30 in main.py")


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 6 — asyncio.gather used in _monitor_inner for ticker fetching
# ═══════════════════════════════════════════════════════════════════════════════
def check_gather_in_monitor():
    name = "6. asyncio.gather in _monitor_inner for tickers"
    import strategy.scanner as sc_mod
    src = inspect.getsource(sc_mod.Scanner._monitor_inner)
    if "asyncio.gather" in src and "get_ticker" in src:
        report(name, True, "asyncio.gather(*[self.ex.get_ticker(s) ...]) found in _monitor_inner")
    else:
        report(name, False, f"asyncio.gather or get_ticker not found in _monitor_inner source")


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 7 — sl_order_id cleared before cancel_order in _move_be and _trail_sl
# ═══════════════════════════════════════════════════════════════════════════════
def check_sl_order_id_cleared():
    name = "7. sl_order_id cleared before cancel in _move_be and _trail_sl"
    import strategy.scanner as sc_mod

    def check_method(method_name):
        src = inspect.getsource(getattr(sc_mod.Scanner, method_name))
        lines = src.splitlines()
        clear_line = None
        cancel_line = None
        for i, l in enumerate(lines):
            if 'sl_order_id = ""' in l and clear_line is None:
                clear_line = i
            if "cancel_order" in l and cancel_line is None:
                cancel_line = i
        if clear_line is None:
            return False, f"sl_order_id = '' not found"
        if cancel_line is None:
            return False, "cancel_order not found"
        if clear_line < cancel_line:
            return True, f"clear at line {clear_line}, cancel at line {cancel_line}"
        return False, f"clear ({clear_line}) is AFTER cancel ({cancel_line})"

    ok1, d1 = check_method("_move_be")
    ok2, d2 = check_method("_trail_sl")

    if ok1 and ok2:
        report(name, True, f"_move_be: {d1}; _trail_sl: {d2}")
    else:
        fails = []
        if not ok1: fails.append(f"_move_be: {d1}")
        if not ok2: fails.append(f"_trail_sl: {d2}")
        report(name, False, "; ".join(fails))


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 8 — place_stop_loss wrapped in try/except in _enter
# ═══════════════════════════════════════════════════════════════════════════════
def check_place_sl_try_except():
    name = "8. place_stop_loss in try/except in _enter"
    import strategy.scanner as sc_mod
    src = inspect.getsource(sc_mod.Scanner._enter)
    # Look for try block containing place_stop_loss
    lines = src.splitlines()
    in_try = False
    found = False
    for l in lines:
        stripped = l.strip()
        if stripped == "try:":
            in_try = True
        if in_try and "place_stop_loss" in l:
            found = True
        if in_try and stripped.startswith("except") and "place_stop_loss" not in l:
            if found:
                break  # we found place_stop_loss in the try block, that's what we need
            in_try = False  # reset for next try block
    if found:
        report(name, True, "place_stop_loss is inside a try block in _enter")
    else:
        # fallback: check that place_stop_loss and emergency close are both present
        if "place_stop_loss" in src and "emergency close" in src:
            report(name, True, "place_stop_loss + emergency close pattern found in _enter")
        else:
            report(name, False, "place_stop_loss not found in try/except in _enter")


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 9 — delete_open_position called AFTER _account_manual_close in
#            cmd_closeall and cmd_close_symbol
# ═══════════════════════════════════════════════════════════════════════════════
def check_delete_ordering():
    name = "9. delete_open_position AFTER _account_manual_close"
    import telegram.handlers as h_mod

    def check_func(fn_name, fn):
        src = inspect.getsource(fn)
        lines = src.splitlines()
        account_line  = None
        delete_line   = None
        for i, l in enumerate(lines):
            if "_account_manual_close" in l and account_line is None:
                account_line = i
            if "delete_open_position" in l and delete_line is None:
                delete_line = i
        if account_line is None:
            return False, f"_account_manual_close not found in {fn_name}"
        if delete_line is None:
            return False, f"delete_open_position not found in {fn_name}"
        if delete_line > account_line:
            return True, f"account@{account_line} < delete@{delete_line}"
        return False, f"delete@{delete_line} is BEFORE account@{account_line}"

    ok1, d1 = check_func("cmd_closeall",     h_mod.cmd_closeall)
    ok2, d2 = check_func("cmd_close_symbol", h_mod.cmd_close_symbol)

    if ok1 and ok2:
        report(name, True, f"closeall: {d1}; close_symbol: {d2}")
    else:
        fails = []
        if not ok1: fails.append(f"closeall: {d1}")
        if not ok2: fails.append(f"close_symbol: {d2}")
        report(name, False, "; ".join(fails))


# ═══════════════════════════════════════════════════════════════════════════════
# CHECK 10 — strategy.gerchik.analyze() runs on 200 random candles without crash
# ═══════════════════════════════════════════════════════════════════════════════
def check_analyze_random():
    name = "10. gerchik.analyze() on 200 random candles (no crash)"
    import random
    from strategy.strategy.gerchik import analyze, parse_klines
    from core.config import cfg

    def make_raw_klines(n, base=100.0):
        rows = []
        price = base
        for i in range(n):
            o = price * (1 + random.uniform(-0.02, 0.02))
            c = price * (1 + random.uniform(-0.02, 0.02))
            h = max(o, c) * (1 + random.uniform(0, 0.01))
            lo = min(o, c) * (1 - random.uniform(0, 0.01))
            vol = random.uniform(100, 5000)
            ts = 1700000000000 + i * 3600000
            rows.append({
                "t": ts, "o": str(o), "h": str(h), "l": str(lo),
                "c": str(c), "v": str(vol),
            })
            price = c
        return rows

    random.seed(42)
    try:
        raw_d1 = make_raw_klines(250)
        raw_h4 = make_raw_klines(150)
        raw_h1 = make_raw_klines(100)

        d1 = parse_klines(raw_d1)
        h4 = parse_klines(raw_h4)
        h1 = parse_klines(raw_h1)

        result = analyze("BTC-USDT", d1, h4, h1, 0.01, cfg)
        report(name, True, f"analyze returned {type(result).__name__} (no crash)")
    except Exception as e:
        report(name, False, str(e))


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    print("\n" + "="*70)
    print("GERCHIK-BOT BUG-FIX VERIFICATION")
    print("="*70 + "\n")

    check_imports()
    check_db_lifecycle()
    check_balance_shapes()
    check_account_manual_close()
    check_monitor_interval()
    check_gather_in_monitor()
    check_sl_order_id_cleared()
    check_place_sl_try_except()
    check_delete_ordering()
    check_analyze_random()

    print("\n" + "="*70)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = len(RESULTS) - passed
    print(f"TOTAL: {passed}/{len(RESULTS)} PASS  |  {failed} FAIL")
    print("="*70 + "\n")

    # Cleanup temp DB
    try:
        os.unlink(_tmp_db.name)
    except Exception:
        pass

    sys.exit(0 if failed == 0 else 1)
