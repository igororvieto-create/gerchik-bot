import logging
import math

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
    if sig.symbol in state.positions:
        log.debug(f"{sig.symbol}: already in position, skip")
        return False
    if len(state.positions) >= cfg.MAX_POSITIONS:
        log.info(f"Max positions ({cfg.MAX_POSITIONS}) reached, skip {sig.symbol}")
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
            log.warning(f"{sig.symbol}: set_leverage({cfg.LEVERAGE}) returned False — continuing")

        side   = "Buy" if sig.direction == "LONG" else "Sell"
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
        state.positions[sig.symbol] = pos
        await db.save_trade_open(pos)

        log.info(
            f"Opened {side} {sig.symbol} qty={qty} "
            f"entry≈{sig.entry:.4f} SL={sig.sl:.4f} TP={sig.tp2:.4f} "
            f"risk={risk_usdt:.2f} USDT orderId={order_id}"
        )
        return True

    except Exception as e:
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
        if not cfg.AUTO_TRADE:
            return
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
            else:
                pos.unrealised_pnl = float(live_map[sym].get("unrealisedPnl", 0))

    except Exception as e:
        log.error(f"monitor_positions error: {e}")
    finally:
        _MONITORING = False
