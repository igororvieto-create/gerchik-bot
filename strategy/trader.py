import logging
import math
from datetime import datetime, timezone

from core.config import cfg
from core.state import Signal, Position, state
from core import db
from exchange.bybit import BybitClient

log = logging.getLogger("trader")

_MONITORING = False


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return round(value, 8)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(round(value / step) * step, decimals)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ensure_daily_state() -> None:
    """Lazily init daily-loss-tracking attributes on `state` and roll them over
    at UTC midnight. Works even if state.py wasn't updated with these fields
    yet — attaches them at runtime. For a cleaner setup, add daily_pnl_date,
    daily_realized_pnl, and trading_halted as real fields on the State class."""
    today = _today_utc()
    if getattr(state, "daily_pnl_date", None) != today:
        state.daily_pnl_date = today
        state.daily_realized_pnl = 0.0
        state.trading_halted = False


async def enter_trade(client: BybitClient, sig: Signal) -> bool:
    """Try to open a position based on a signal. Returns True if order placed."""
    if not cfg.AUTO_TRADE:
        return False
    if not client.api_key or not client.secret:
        log.warning("AUTO_TRADE=true but BYBIT_API_KEY/BYBIT_SECRET not set")
        return False
    if sig.score < cfg.TRADE_MIN_SCORE:
        return False
    if sig.direction == "NEUTRAL":
        return False
    if sig.sl_pct <= 0 or sig.entry <= 0 or sig.sl <= 0:
        return False

    _ensure_daily_state()
    if state.trading_halted:
        log.warning(f"{sig.symbol}: daily loss limit hit — trading halted until UTC reset, skip")
        return False

    if sig.symbol in state.positions:
        log.debug(f"{sig.symbol}: already in position, skip")
        return False
    if len(state.positions) >= cfg.MAX_POSITIONS:
        log.info(f"Max positions ({cfg.MAX_POSITIONS}) reached, skip {sig.symbol}")
        return False

    # Correlation guard: cap how many concurrent positions can face the same
    # direction. Three uncorrelated setups is diversification; three LONGs in
    # correlated alts is one oversized directional bet wearing three tickets.
    side = "Buy" if sig.direction == "LONG" else "Sell"
    same_dir_count = sum(
        1 for p in state.positions.values() if p is not None and p.side == side
    )
    if same_dir_count >= cfg.MAX_SAME_DIRECTION:
        log.info(
            f"{sig.symbol}: {same_dir_count} {side} position(s) already open "
            f"(correlation cap {cfg.MAX_SAME_DIRECTION}), skip"
        )
        return False

    # Reserve slot immediately to prevent concurrent duplicate entries
    state.positions[sig.symbol] = None  # sentinel; replaced with real Position or removed

    try:
        balance = await client.get_balance()
        if balance < 10:
            log.warning(f"Insufficient balance: {balance:.2f} USDT")
            state.positions.pop(sig.symbol, None)
            return False

        state.balance = balance

        # Position size: risk RISK_PER_TRADE% of balance
        risk_usdt     = balance * cfg.RISK_PER_TRADE / 100
        position_usdt = risk_usdt / (sig.sl_pct / 100)  # notional value

        # Cap by max margin: never use more than MAX_MARGIN_PCT% of balance as margin
        max_notional_margin = balance * cfg.MAX_MARGIN_PCT / 100 * cfg.LEVERAGE
        # Also cap at balance × leverage to avoid margin rejection
        max_notional = min(balance * cfg.LEVERAGE, max_notional_margin)
        if position_usdt > max_notional:
            position_usdt = max_notional
            log.debug(f"{sig.symbol}: notional capped to {max_notional:.2f}")

        # Qty precision from instrument info
        info = await client.get_instrument_info(sig.symbol)
        lot  = info.get("lotSizeFilter", {})
        qty_step = float(lot.get("qtyStep",      "0.001"))
        min_qty  = float(lot.get("minOrderQty",  "0.001"))

        qty = _round_step(position_usdt / sig.entry, qty_step)
        qty = max(qty, min_qty)

        if qty * sig.entry < 5.0:
            log.debug(f"{sig.symbol}: notional {qty*sig.entry:.2f} < 5 USDT min")
            state.positions.pop(sig.symbol, None)
            return False

        ok = await client.set_leverage(sig.symbol, cfg.LEVERAGE)
        if not ok:
            # Previously this only logged a warning and continued — placing a
            # trade whose position sizing assumed cfg.LEVERAGE margin usage,
            # without confirming the exchange actually applied it. If the
            # account's leverage differs from what we assumed, actual margin
            # usage and liquidation distance are wrong. Abort instead; this
            # is configurable in case you want the old "continue anyway"
            # behavior back for a specific reason.
            log.error(f"{sig.symbol}: set_leverage({cfg.LEVERAGE}) failed")
            if cfg.ABORT_ON_LEVERAGE_FAIL:
                state.positions.pop(sig.symbol, None)
                return False
            log.warning(f"{sig.symbol}: continuing despite leverage-set failure (ABORT_ON_LEVERAGE_FAIL=False)")

        # Use TP2 (1:2 R:R) as primary target — more likely to be hit than TP3
        result = await client.place_order(
            symbol=sig.symbol, side=side, qty=qty, sl=sig.sl, tp=sig.tp2,
        )

        if result.get("retCode", -1) != 0:
            log.error(f"{sig.symbol}: order failed — {result.get('retMsg')}")
            state.positions.pop(sig.symbol, None)
            return False

        # IMPORTANT — recurring historical bug: SL/TP have previously been
        # placed as chart markers only, without actually reaching the
        # exchange. retCode==0 confirms the entry order was accepted; it does
        # NOT confirm the exchange attached stopLoss/takeProfit. Verify by
        # reading the live position back before trusting it's protected.
        try:
            live = await client.get_positions()
            live_pos = next((p for p in (live or []) if p.get("symbol") == sig.symbol), None)
            exch_sl = float(live_pos.get("stopLoss") or 0) if live_pos else 0.0
            exch_tp = float(live_pos.get("takeProfit") or 0) if live_pos else 0.0
            if exch_sl <= 0 or exch_tp <= 0:
                log.error(
                    f"{sig.symbol}: order filled but exchange shows "
                    f"SL={exch_sl} TP={exch_tp} — position may be UNPROTECTED. "
                    f"Attempting to close immediately."
                )
                try:
                    await client.close_position(sig.symbol, side, qty)
                    log.error(f"{sig.symbol}: emergency close sent due to missing SL/TP")
                except Exception as ce:
                    log.critical(
                        f"{sig.symbol}: emergency close FAILED — position is live and "
                        f"UNPROTECTED on the exchange, manual intervention required — {ce}"
                    )
                state.positions.pop(sig.symbol, None)
                return False
        except Exception as ve:
            # Verification itself failing shouldn't silently pass the trade
            # through as "fine" — log loudly so it's visible in monitoring.
            log.error(f"{sig.symbol}: could not verify SL/TP attachment after fill — {ve}")

        order_id = result.get("result", {}).get("orderId", "")
        pos = Position(
            symbol=sig.symbol, side=side,
            entry=sig.entry, sl=sig.sl,
            tp1=sig.tp1, tp2=sig.tp2, tp3=sig.tp3,
            qty=qty, score=sig.score,
            signal_type=sig.signal_type, order_id=order_id,
        )
        # Track the position in state BEFORE the DB write. The order is live
        # on the exchange at this point regardless of what happens next — a
        # DB failure must not cause us to lose track of a real, open,
        # exchange-side position (previously it would: any exception here
        # popped sig.symbol from state.positions even after a successful
        # fill, orphaning it from monitor_positions).
        state.positions[sig.symbol] = pos
        try:
            await db.save_trade_open(pos)
        except Exception as dbe:
            log.error(
                f"{sig.symbol}: db.save_trade_open failed — position IS live on "
                f"exchange and IS tracked in memory, but won't appear in trade "
                f"history until this is investigated — {dbe}"
            )

        log.info(
            f"Opened {side} {sig.symbol} qty={qty} "
            f"entry≈{sig.entry:.4f} SL={sig.sl:.4f} TP={sig.tp2:.4f} "
            f"risk={risk_usdt:.2f} USDT orderId={order_id}"
        )
        return True

    except Exception as e:
        # Only clear the slot if we haven't registered a live Position yet —
        # once a real Position is tracked, the order filled and must stay
        # visible to monitor_positions even if something after it raised.
        if not isinstance(state.positions.get(sig.symbol), Position):
            state.positions.pop(sig.symbol, None)
        log.error(f"enter_trade {sig.symbol}: {e}")
        return False


async def monitor_positions(client: BybitClient) -> None:
    """Check if exchange positions still exist; detect SL/TP closes."""
    global _MONITORING
    if _MONITORING:
        log.warning("monitor_positions: previous call still running, skipping this tick")
        return
    _MONITORING = True
    try:
        _ensure_daily_state()

        # Always monitor even if AUTO_TRADE was toggled off mid-session —
        # otherwise open positions become orphaned (never recorded as closed)
        if not client.api_key:
            return

        # Always update balance
        bal = await client.get_balance()
        if bal > 0:
            state.balance = bal

        if not state.positions:
            return

        live = await client.get_positions()
        if live is None:
            # API failure — do NOT wipe positions; wait for next cycle
            log.warning("monitor_positions: get_positions API failed, skipping close check")
            return

        live_map = {p["symbol"]: p for p in live}

        for sym in list(state.positions.keys()):
            pos = state.positions[sym]
            if pos is None:
                continue  # sentinel slot from enter_trade in progress
            if sym not in live_map:
                # Position closed by exchange (SL or TP hit)
                # Fetch actual exit price and PnL before removing from state
                exit_price, pnl = 0.0, 0.0
                try:
                    closed = await client.get_closed_pnl(sym, limit=1)
                    if closed:
                        exit_price = float(closed[0].get("avgExitPrice", 0))
                        pnl = float(closed[0].get("closedPnl", 0))
                except Exception as ce:
                    log.warning(f"{sym}: could not fetch closed PnL — {ce}")
                await db.save_trade_close(pos, exit_price=exit_price, pnl=pnl)
                state.positions.pop(sym, None)
                log.info(f"{sym}: closed (SL/TP) exit={exit_price:.4f} pnl={pnl:+.2f}")

                # Daily circuit breaker: accumulate realized PnL for the day
                # and halt new entries if the loss limit is breached. Doesn't
                # touch positions already open — only blocks new ones via the
                # state.trading_halted check in enter_trade().
                state.daily_realized_pnl += pnl
                if state.balance > 0:
                    loss_pct = -state.daily_realized_pnl / state.balance * 100
                    if loss_pct >= cfg.DAILY_LOSS_LIMIT_PCT and not state.trading_halted:
                        state.trading_halted = True
                        log.error(
                            f"DAILY LOSS LIMIT HIT: {loss_pct:.2f}% >= "
                            f"{cfg.DAILY_LOSS_LIMIT_PCT}% — halting new trades until UTC reset"
                        )
            else:
                pos.unrealised_pnl = float(live_map[sym].get("unrealisedPnl", 0))

    except Exception as e:
        log.error(f"monitor_positions error: {e}")
    finally:
        _MONITORING = False
