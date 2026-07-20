import asyncio
import logging
import math
from datetime import datetime, timezone

from core.config import cfg
from core.state import Signal, Position, state
from core import db
from exchange.bybit import BybitClient

log = logging.getLogger("trader")

_MONITORING = False

# Skip exchange-side "position disappeared" detection for positions younger
# than this: a monitor tick whose get_positions() request was already in
# flight when the entry filled would otherwise see a stale snapshot and
# wrongly mark the brand-new position as closed.
_MIN_POSITION_AGE_S = 90


def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return round(value, 8)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(round(value / step) * step, decimals)


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _ensure_daily_state() -> None:
    """Init/rollover daily-loss tracking on `state` at UTC midnight.
    On rollover (including process start) the counter is rebuilt from the DB,
    so a deploy or crash mid-day cannot silently reset the circuit breaker."""
    today = _today_utc()
    if getattr(state, "daily_pnl_date", None) != today:
        state.daily_pnl_date = today
        state.daily_realized_pnl = await db.get_realized_pnl_since(f"{today}T00:00:00")
        state.trading_halted = False
        if state.balance > 0 and state.daily_realized_pnl < 0:
            loss_pct = -state.daily_realized_pnl / state.balance * 100
            if loss_pct >= cfg.DAILY_LOSS_LIMIT_PCT:
                state.trading_halted = True
                log.warning(
                    f"daily state restored from DB: loss {loss_pct:.2f}% >= "
                    f"{cfg.DAILY_LOSS_LIMIT_PCT}% — trading stays halted"
                )


def record_realized_close(pnl: float) -> None:
    """Accumulate realized PnL into the daily circuit breaker and halt new
    entries when the limit is breached. Single entry point used by BOTH the
    monitor loop and the manual dashboard close — a close path that skips this
    lets losses bypass the daily limit."""
    state.daily_realized_pnl = getattr(state, "daily_realized_pnl", 0.0) + pnl
    if state.balance > 0:
        loss_pct = -state.daily_realized_pnl / state.balance * 100
        if loss_pct >= cfg.DAILY_LOSS_LIMIT_PCT and not getattr(state, "trading_halted", False):
            state.trading_halted = True
            log.error(
                f"DAILY LOSS LIMIT HIT: {loss_pct:.2f}% >= "
                f"{cfg.DAILY_LOSS_LIMIT_PCT}% — halting new trades until UTC reset"
            )


async def fetch_matching_closed_pnl(client: BybitClient, pos: Position) -> tuple[float, float]:
    """(exit_price, pnl) for THIS position's close. Bybit's closed-pnl list can
    lag and/or lead: closed[0] may be a previous trade on the same symbol.
    Match by record time >= position open time instead of blindly taking [0]."""
    opened_ms = pos.ts.timestamp() * 1000
    try:
        closed = await client.get_closed_pnl(pos.symbol, limit=5)
        for rec in closed:
            rec_ms = float(rec.get("updatedTime") or rec.get("createdTime") or 0)
            if rec_ms >= opened_ms - 60_000:  # 1min slack for clock skew
                return float(rec.get("avgExitPrice", 0)), float(rec.get("closedPnl", 0))
    except Exception as ce:
        log.warning(f"{pos.symbol}: could not fetch closed PnL — {ce}")
    return 0.0, 0.0


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

    await _ensure_daily_state()
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
        if qty < min_qty:
            # Bumping to min_qty silently multiplies risk: on a symbol whose
            # minimum lot is worth several times the intended notional, the
            # loss at SL would blow through the per-trade risk ceiling.
            # Only accept the bump when actual risk stays within 1.5x target.
            bumped_risk = min_qty * sig.entry * sig.sl_pct / 100
            if bumped_risk > risk_usdt * 1.5:
                log.info(
                    f"{sig.symbol}: min lot {min_qty} would risk "
                    f"{bumped_risk:.2f} USDT vs target {risk_usdt:.2f} — skip"
                )
                state.positions.pop(sig.symbol, None)
                return False
            qty = min_qty

        if qty * sig.entry < 5.0:
            log.debug(f"{sig.symbol}: notional {qty*sig.entry:.2f} < 5 USDT min")
            state.positions.pop(sig.symbol, None)
            return False

        ok = await client.set_leverage(sig.symbol, cfg.LEVERAGE)
        if not ok:
            # Placing a trade whose sizing assumed cfg.LEVERAGE without
            # confirming the exchange applied it => wrong margin usage and
            # liquidation distance. Abort by default (ABORT_ON_LEVERAGE_FAIL).
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

        order_id = result.get("result", {}).get("orderId", "")
        pos = Position(
            symbol=sig.symbol, side=side,
            entry=sig.entry, sl=sig.sl,
            tp1=sig.tp1, tp2=sig.tp2, tp3=sig.tp3,
            qty=qty, score=sig.score,
            signal_type=sig.signal_type, order_id=order_id,
        )
        # Track the position BEFORE verification/DB writes: the order is live
        # on the exchange from this point, and losing track of it (on any
        # later exception) is worse than any bookkeeping failure.
        state.positions[sig.symbol] = pos

        # IMPORTANT — recurring historical bug: SL/TP have previously been
        # placed as chart markers only, without actually reaching the
        # exchange. retCode==0 confirms the entry order was accepted; it does
        # NOT confirm the exchange attached stopLoss/takeProfit. Verify by
        # reading the live position back.
        #
        # Distinguish three outcomes:
        #  - verified protected  -> proceed
        #  - verified UNPROTECTED (position visible, SL/TP zero) -> emergency close
        #  - could NOT verify (API failure / propagation lag)    -> DO NOT close;
        #    keep tracking and let the next monitor tick re-check. Closing a
        #    healthy position on a transient API hiccup pays the spread twice.
        exch_sl = exch_tp = 0.0
        verified = False
        for attempt in range(3):
            await asyncio.sleep(0.5 if attempt == 0 else 1.5)
            live = await client.get_positions()
            if live is None:
                continue  # API failure — retry
            live_pos = next((p for p in live if p.get("symbol") == sig.symbol), None)
            if live_pos is None:
                continue  # propagation lag — retry
            exch_sl = float(live_pos.get("stopLoss") or 0)
            exch_tp = float(live_pos.get("takeProfit") or 0)
            verified = True
            if exch_sl > 0 and exch_tp > 0:
                break

        if verified and (exch_sl <= 0 or exch_tp <= 0):
            log.error(
                f"{sig.symbol}: order filled but exchange shows "
                f"SL={exch_sl} TP={exch_tp} — position UNPROTECTED, closing immediately."
            )
            close_ok = False
            try:
                close_res = await client.close_position(sig.symbol, side, qty)
                close_ok = close_res.get("retCode", -1) == 0
                if not close_ok:
                    log.critical(
                        f"{sig.symbol}: emergency close REJECTED — "
                        f"{close_res.get('retMsg', 'no response')}"
                    )
            except Exception as ce:
                log.critical(f"{sig.symbol}: emergency close FAILED — {ce}")
            if close_ok:
                state.positions.pop(sig.symbol, None)
                return False
            # Close failed: position is live and unprotected — KEEP it tracked
            # so monitor_positions keeps watching; manual intervention needed.
            log.critical(
                f"{sig.symbol}: UNPROTECTED position remains open and tracked — "
                f"manual intervention required"
            )
        elif not verified:
            log.error(
                f"{sig.symbol}: could not verify SL/TP attachment (API failures "
                f"or propagation lag) — keeping position tracked, monitor will re-check"
            )

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
        await _ensure_daily_state()

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
        now_utc = datetime.utcnow()

        for sym in list(state.positions.keys()):
            pos = state.positions.get(sym)
            if pos is None:
                continue  # sentinel slot from enter_trade in progress
            if sym not in live_map:
                # Grace period: a position opened while THIS get_positions
                # request was in flight is missing from the (stale) snapshot —
                # don't mark it closed until it has had time to appear.
                age_s = (now_utc - pos.ts).total_seconds()
                if age_s < _MIN_POSITION_AGE_S:
                    log.debug(f"{sym}: {age_s:.0f}s old, absent from snapshot — grace period")
                    continue
                # Position closed by exchange (SL or TP hit)
                exit_price, pnl = await fetch_matching_closed_pnl(client, pos)
                await db.save_trade_close(pos, exit_price=exit_price, pnl=pnl)
                state.positions.pop(sym, None)
                log.info(f"{sym}: closed (SL/TP) exit={exit_price:.4f} pnl={pnl:+.2f}")

                # Daily circuit breaker (shared path with manual close)
                record_realized_close(pnl)
            else:
                pos.unrealised_pnl = float(live_map[sym].get("unrealisedPnl", 0))

    except Exception as e:
        log.error(f"monitor_positions error: {e}")
    finally:
        _MONITORING = False
