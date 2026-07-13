import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional

import numpy as np

from core.config import cfg
from core.state import Signal, state
from core import db
from exchange.bybit import BybitClient
from notifications.ntfy import send_push
from strategy.trader import enter_trade

log = logging.getLogger("scanner")

_SCANNING = False


def _calc_atr(klines: list, period: int = 14) -> float:
    if len(klines) < period + 1:
        return 0.0
    highs  = np.array([k["high"]  for k in klines])
    lows   = np.array([k["low"]   for k in klines])
    closes = np.array([k["close"] for k in klines])
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:]  - closes[:-1]),
        ),
    )
    # Proper Wilder's: seed with SMA of first 'period' TR bars, then EMA the rest
    atr_val = float(np.mean(tr[:period]))
    for t in tr[period:]:
        atr_val = (atr_val * (period - 1) + float(t)) / period
    return atr_val


def _ob_imbalance(ob: dict) -> tuple[float, str]:
    """Returns (imbalance ratio, bias). ratio > 0 = more bids (buy pressure)."""
    bids = ob.get("bids", [])
    asks = ob.get("asks", [])
    bid_vol = sum(p * q for p, q in bids)
    ask_vol = sum(p * q for p, q in asks)
    total = bid_vol + ask_vol
    if total < 1:
        return 0.0, "NEUTRAL"
    ratio = (bid_vol - ask_vol) / total
    if ratio > cfg.OB_IMBALANCE_THRESHOLD:
        return ratio, "BUY"
    if ratio < -cfg.OB_IMBALANCE_THRESHOLD:
        return ratio, "SELL"
    return ratio, "NEUTRAL"


def _classify(oi_change: float, vol_ratio: float, funding: float, price_change: float) -> str:
    """Classify the signal type from market data."""
    fund_abs = abs(funding)
    if oi_change >= cfg.OI_CHANGE_THRESHOLD and price_change < -0.3:
        return "DISTRIBUTION"
    elif oi_change >= cfg.OI_CHANGE_THRESHOLD:
        return "ACCUMULATION"
    elif oi_change <= -cfg.OI_CHANGE_THRESHOLD:
        return "SQUEEZE"
    elif vol_ratio >= cfg.VOL_SPIKE_MULT * 1.5:
        return "VOLUME_SPIKE"
    elif fund_abs >= cfg.FUNDING_EXTREME:
        return "FUNDING_EXTREME"
    else:
        return "MOMENTUM"


def _direction(
    sig_type: str, price_change: float, ob_bias: str, funding: float, ob_ratio: float
) -> tuple[str, float]:
    """Return (direction, confidence) where confidence is fraction of votes agreeing."""
    votes: list[str] = []
    if abs(price_change) > 0.1:
        votes.append("LONG" if price_change > 0 else "SHORT")
    if ob_bias != "NEUTRAL":
        votes.append("LONG" if ob_bias == "BUY" else "SHORT")
    if abs(funding) >= 0.01:
        # Positive funding = longs pay shorts → contrarian SHORT signal
        votes.append("SHORT" if funding > 0 else "LONG")

    if sig_type == "ACCUMULATION":
        primary = "LONG"
    elif sig_type == "DISTRIBUTION":
        primary = "SHORT"
    elif sig_type == "SQUEEZE":
        if votes:
            long_votes = sum(1 for v in votes if v == "LONG")
            short_votes = len(votes) - long_votes
            if long_votes > short_votes:
                primary = "LONG"
            elif short_votes > long_votes:
                primary = "SHORT"
            else:
                primary = "LONG" if price_change > 0 else "SHORT"
        else:
            primary = "LONG" if price_change > 0 else "SHORT"
    elif sig_type == "FUNDING_EXTREME":
        primary = "SHORT" if funding > 0 else "LONG"
    elif ob_bias != "NEUTRAL":
        primary = "LONG" if ob_bias == "BUY" else "SHORT"
    else:
        primary = "LONG" if price_change > 0 else "SHORT"

    if votes:
        agree = sum(1 for v in votes if v == primary)
        confidence = agree / len(votes)
    else:
        confidence = 0.4

    return primary, confidence


def _score_signal(
    oi_change: float,
    vol_ratio: float,
    funding: float,
    ob_ratio: float,
    confidence: float,
) -> int:
    """Score 0-100, capped by directional confluence (confidence)."""
    score = 0

    # OI change component (0-40 pts)
    oi_abs = abs(oi_change)
    if oi_abs >= 10:
        score += 40
    elif oi_abs >= 7:
        score += 30
    elif oi_abs >= 5:
        score += 20
    elif oi_abs >= 3:
        score += 12
    elif oi_abs >= 2:
        score += 6

    # Volume spike component (0-30 pts)
    if vol_ratio >= 4:
        score += 30
    elif vol_ratio >= 3:
        score += 22
    elif vol_ratio >= 2:
        score += 15
    elif vol_ratio >= 1.5:
        score += 8
    elif vol_ratio >= 1.3:
        score += 4

    # Funding extremity (0-15 pts) — raised thresholds vs old version
    fund_abs = abs(funding)
    if fund_abs >= 0.5:
        score += 15
    elif fund_abs >= 0.25:
        score += 10
    elif fund_abs >= 0.1:
        score += 6
    elif fund_abs >= 0.03:
        score += 3

    # Orderbook imbalance (0-15 pts)
    ob_abs = abs(ob_ratio)
    if ob_abs >= 0.30:
        score += 15
    elif ob_abs >= 0.20:
        score += 10
    elif ob_abs >= 0.10:
        score += 6
    elif ob_abs >= 0.05:
        score += 3

    raw_score = min(score, 100)

    # Confluence cap: contradicting signals reduce maximum achievable score
    if confidence < 0.34:
        raw_score = min(raw_score, 35)
    elif confidence < 0.5:
        raw_score = min(raw_score, 55)
    elif confidence < 0.75:
        raw_score = min(raw_score, 75)

    return raw_score


def _calc_levels(price: float, atr: float, direction: str) -> dict:
    """ATR-based entry/SL/TP levels. SL = 1.5×ATR, TP targets at 1:1, 1:2, 1:3 R/R."""
    if price <= 0 or atr <= 0:
        return {"entry": price, "sl": 0.0, "tp1": 0.0, "tp2": 0.0, "tp3": 0.0, "rr": 0.0, "sl_pct": 0.0}

    # 1.5×ATR SL — tighter than 2× gives better R:R while still avoiding noise
    sl_dist = max(atr * 1.5, price * 0.003)
    risk = sl_dist

    if direction == "LONG":
        entry = price
        sl    = price - sl_dist
        tp1   = price + risk * 1.0
        tp2   = price + risk * 2.0
        tp3   = price + risk * 3.0
    else:  # SHORT
        entry = price
        sl    = price + sl_dist
        tp1   = price - risk * 1.0
        tp2   = price - risk * 2.0
        tp3   = price - risk * 3.0

    sl_pct = sl_dist / price * 100
    rr = 2.0  # always 1:2 at TP2

    return {"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3, "rr": rr, "sl_pct": sl_pct}


async def _analyze_symbol(client: BybitClient, ticker: dict) -> Optional[Signal]:
    symbol = ticker.get("symbol", "")
    if symbol in cfg.BLACKLIST:
        return None

    try:
        price       = float(ticker.get("lastPrice",     0))
        price_chg   = float(ticker.get("price24hPcnt",  0)) * 100
        funding     = float(ticker.get("fundingRate",   0)) * 100
        vol_24h     = float(ticker.get("volume24h",     0))
        oi_usdt_now = float(ticker.get("openInterestValue", 0))

        if price <= 0:
            return None

        # Minimum 24h volume filter — skip illiquid pairs
        if vol_24h < cfg.MIN_VOL_24H:
            return None

        # Pre-filter: skip dead tickers (tiny move AND near-zero funding)
        if abs(price_chg) < cfg.PRICE_CHANGE_MIN and abs(funding) < 0.01:
            return None

        # Fetch OI history, klines, and orderbook concurrently
        # Request limit=26 klines: [-1]=current incomplete, [-2]=last completed
        oi_hist, klines, ob = await asyncio.gather(
            client.get_open_interest(symbol, interval="4h", limit=2),
            client.get_klines(symbol, interval="240", limit=26),
            client.get_orderbook(symbol, limit=20),
        )

        if not oi_hist or not klines:
            log.warning(f"{symbol}: partial data — oi_hist={len(oi_hist)} klines={len(klines)}")
            if not klines:
                return None

        # OI change: compare real-time USDT OI vs start of current 4h period
        # oi_hist[-1] is the most-recent 4h bar open (coin count); convert to USDT via price
        if len(oi_hist) >= 1 and price > 0:
            oi_prev_usdt = oi_hist[-1]["oi"] * price
            oi_change = (oi_usdt_now - oi_prev_usdt) / oi_prev_usdt * 100 if oi_prev_usdt > 0 else 0.0
        else:
            oi_change = 0.0

        # Volume ratio: compare last completed 4h candle vs 20-bar avg
        # klines[-1] is the current incomplete candle; use klines[-2] as "current"
        if len(klines) >= 22:
            volumes  = np.array([k["volume"] for k in klines])
            vol_avg  = float(np.mean(volumes[-22:-2]))  # 20 completed bars
            vol_curr = float(volumes[-2])               # last completed bar
            vol_ratio = vol_curr / vol_avg if vol_avg > 0 else 1.0
        elif len(klines) >= 3:
            volumes  = np.array([k["volume"] for k in klines])
            vol_avg  = float(np.mean(volumes[:-2])) if len(volumes) > 2 else 1.0
            vol_curr = float(volumes[-2])
            vol_ratio = vol_curr / vol_avg if vol_avg > 0 else 1.0
        else:
            vol_ratio = 1.0

        # ATR — exclude current incomplete candle (klines[-1])
        atr = _calc_atr(klines[:-1])
        atr_pct = atr / price * 100 if price > 0 else 0.0

        # Orderbook
        ob_ratio, ob_bias = _ob_imbalance(ob)

        sig_type = _classify(oi_change, vol_ratio, funding, price_chg)
        direction, confidence = _direction(sig_type, price_chg, ob_bias, funding, ob_ratio)
        score = _score_signal(oi_change, vol_ratio, funding, ob_ratio, confidence)
        if score < cfg.MIN_SCORE:
            return None

        levels = _calc_levels(price, atr, direction)

        details = (
            f"{sig_type} | {direction} | score={score} | conf={confidence:.2f} | "
            f"OI {oi_change:+.1f}% | vol {vol_ratio:.1f}x | "
            f"funding {funding:+.3f}% | OB {ob_bias} | ATR {atr_pct:.2f}%"
        )

        return Signal(
            symbol=symbol,
            signal_type=sig_type,
            direction=direction,
            score=score,
            price=price,
            oi_change=oi_change,
            vol_ratio=vol_ratio,
            funding=funding,
            ob_bias=ob_bias,
            atr_pct=atr_pct,
            details=details,
            entry=levels["entry"],
            sl=levels["sl"],
            tp1=levels["tp1"],
            tp2=levels["tp2"],
            tp3=levels["tp3"],
            rr=levels["rr"],
            sl_pct=levels["sl_pct"],
        )
    except Exception as e:
        log.warning(f"{symbol}: analysis error — {e}")
        return None


async def scan_all(client: BybitClient) -> List[Signal]:
    global _SCANNING
    if _SCANNING:
        log.info("scan_all: already running, skipping")
        return []
    _SCANNING = True
    signals: List[Signal] = []

    try:
        tickers = await client.get_tickers()
        # Keep only USDT linear perps, not inverse
        tickers = [
            t for t in tickers
            if t.get("symbol", "").endswith("USDT")
            and t.get("symbol") not in cfg.BLACKLIST
        ]
        # Sort by 24h volume descending, keep TOP_N_PAIRS
        try:
            tickers.sort(key=lambda t: float(t.get("volume24h", 0)), reverse=True)
        except Exception:
            pass
        if cfg.TOP_N_PAIRS > 0:
            tickers = tickers[:cfg.TOP_N_PAIRS]

        log.info(f"scan_all: {len(tickers)} symbols to scan "
                 f"(batch={cfg.SCAN_BATCH_SIZE} delay={cfg.SCAN_BATCH_DELAY}s)")
        if not tickers:
            log.warning("scan_all: 0 symbols after filter — Bybit API may be unreachable")
            return []

        # Process in batches to respect rate limits
        batch_size = cfg.SCAN_BATCH_SIZE
        errors = 0
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i : i + batch_size]
            results = await asyncio.gather(
                *[_analyze_symbol(client, t) for t in batch],
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, Signal):
                    signals.append(r)
                elif isinstance(r, Exception):
                    errors += 1
            if i + batch_size < len(tickers):
                await asyncio.sleep(cfg.SCAN_BATCH_DELAY)

        if errors:
            log.warning(f"scan_all: {errors}/{len(tickers)} symbols failed with exceptions")

        signals.sort(key=lambda s: s.score, reverse=True)
        state.last_scan_at = datetime.utcnow()
        state.scan_count += 1
        state.total_signals += len(signals)

        log.info(f"scan_all: found {len(signals)} signals (scan #{state.scan_count})")
        if signals:
            top = signals[:3]
            log.info("Top signals: " + " | ".join(
                f"{s.symbol} score={s.score} {s.direction} {s.signal_type}" for s in top
            ))
        else:
            log.info(f"scan_all: no signals above MIN_SCORE={cfg.MIN_SCORE}")
        return signals

    except Exception as e:
        log.error(f"scan_all error: {e}")
        return []
    finally:
        _SCANNING = False


async def run_scan_and_broadcast(client: BybitClient, ntfy_url: str = "") -> List[Signal]:
    """Called by APScheduler: scan, save to DB, broadcast via WS, push via ntfy."""
    # Refresh balance on every scan if API keys are configured
    if client.api_key and client.secret:
        try:
            bal = await client.get_balance()
            if bal > 0:
                state.balance = bal
        except Exception as be:
            log.warning(f"run_scan_and_broadcast: get_balance failed — {be}")

    signals = await scan_all(client)

    now = datetime.utcnow()
    cooldown = timedelta(minutes=cfg.SIGNAL_COOLDOWN_MIN)

    for sig in signals:
        # Dedup: skip if this symbol already signalled within the cooldown window
        last_seen = state.signal_seen.get(sig.symbol)
        if last_seen and (now - last_seen) < cooldown:
            continue
        state.signal_seen[sig.symbol] = now

        try:
            await db.save_signal(sig)
        except Exception as dbe:
            log.error(f"run_scan_and_broadcast: db.save_signal({sig.symbol}) failed — {dbe}")

        # Trade only if score meets TRADE_MIN_SCORE
        await enter_trade(client, sig)

        # Broadcast to all connected WebSocket clients
        try:
            msg = json.dumps({"type": "signal", "data": sig.to_dict()})
        except Exception as je:
            log.error(f"run_scan_and_broadcast: sig.to_dict() failed for {sig.symbol} — {je}")
            continue
        dead = set()
        for ws in state.ws_clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        for ws in dead:
            state.remove_ws(ws)

        # ntfy push for high-score signals
        if ntfy_url and sig.score >= 60:
            try:
                icon = "🟢" if sig.direction == "LONG" else "🔴"
                await send_push(
                    ntfy_url,
                    title=f"{icon} {sig.symbol} — {sig.signal_type}",
                    message=sig.details,
                    priority="high" if sig.score >= 75 else "default",
                    tags=["chart_with_upwards_trend"] if sig.direction == "LONG" else ["chart_with_downwards_trend"],
                )
            except Exception as pe:
                log.warning(f"run_scan_and_broadcast: send_push({sig.symbol}) failed — {pe}")

    # Broadcast scan heartbeat
    heartbeat = json.dumps({
        "type":         "heartbeat",
        "scan_count":   state.scan_count,
        "last_scan_at": state.last_scan_at.isoformat() + "Z" if state.last_scan_at else None,
        "signals_found": len(signals),
    })
    dead = set()
    for ws in state.ws_clients:
        try:
            await ws.send_text(heartbeat)
        except Exception:
            dead.add(ws)
    for ws in dead:
        state.remove_ws(ws)

    return signals
