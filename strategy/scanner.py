import asyncio
import json
import logging
from datetime import datetime
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
    return float(np.mean(tr[-period:]))


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


def _score_signal(
    oi_change: float,
    vol_ratio: float,
    funding: float,
    ob_ratio: float,
    price_change: float,
) -> tuple[int, str]:
    """Score 0-100 and classify signal type."""
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
        score += 10

    # Volume spike component (0-30 pts)
    if vol_ratio >= 4:
        score += 30
    elif vol_ratio >= 3:
        score += 22
    elif vol_ratio >= 2:
        score += 15
    elif vol_ratio >= 1.5:
        score += 8

    # Funding extremity (0-15 pts)
    fund_abs = abs(funding)
    if fund_abs >= 0.1:
        score += 15
    elif fund_abs >= 0.05:
        score += 10
    elif fund_abs >= 0.03:
        score += 5

    # Orderbook imbalance (0-15 pts)
    ob_abs = abs(ob_ratio)
    if ob_abs >= 0.30:
        score += 15
    elif ob_abs >= 0.20:
        score += 10
    elif ob_abs >= 0.10:
        score += 5

    # Classify signal type
    if oi_change >= cfg.OI_CHANGE_THRESHOLD and price_change > 0:
        sig_type = "ACCUMULATION"
    elif oi_change >= cfg.OI_CHANGE_THRESHOLD and price_change < 0:
        sig_type = "DISTRIBUTION"
    elif oi_change <= -cfg.OI_CHANGE_THRESHOLD:
        sig_type = "SQUEEZE"
    elif vol_ratio >= cfg.VOL_SPIKE_MULT * 1.5:
        sig_type = "VOLUME_SPIKE"
    elif fund_abs >= cfg.FUNDING_EXTREME:
        sig_type = "FUNDING_EXTREME"
    else:
        sig_type = "MOMENTUM"

    return min(score, 100), sig_type


def _direction(sig_type: str, price_change: float, ob_bias: str, funding: float) -> str:
    if sig_type == "ACCUMULATION":
        return "LONG"
    if sig_type == "DISTRIBUTION":
        return "SHORT"
    if sig_type == "SQUEEZE":
        return "LONG" if price_change > 0 else "SHORT"
    if sig_type == "FUNDING_EXTREME":
        return "SHORT" if funding > 0 else "LONG"
    if ob_bias != "NEUTRAL":
        return "LONG" if ob_bias == "BUY" else "SHORT"
    return "LONG" if price_change > 0 else "SHORT"


def _calc_levels(price: float, atr: float, direction: str) -> dict:
    """ATR-based entry/SL/TP levels. SL = 1.5×ATR, TP targets at 1:1, 1:2, 1:3 R/R."""
    if price <= 0 or atr <= 0:
        return {"entry": price, "sl": 0.0, "tp1": 0.0, "tp2": 0.0, "tp3": 0.0, "rr": 0.0, "sl_pct": 0.0}

    sl_dist = atr * 1.5
    risk = sl_dist  # risk distance = SL distance

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

        # Cheap pre-filter: skip boring tickers
        if abs(price_chg) < cfg.PRICE_CHANGE_MIN and abs(funding) < 0.01:
            return None

        # Fetch OI history, klines, and orderbook concurrently
        oi_hist, klines, ob = await asyncio.gather(
            client.get_open_interest(symbol, interval="4h", limit=12),
            client.get_klines(symbol, interval="240", limit=25),
            client.get_orderbook(symbol, limit=20),
        )

        # OI change over last 4h
        if len(oi_hist) >= 2:
            oi_old    = oi_hist[-2]["oi"]   # second-to-last (4h ago)
            oi_new    = oi_hist[-1]["oi"]   # latest
            oi_change = (oi_new - oi_old) / oi_old * 100 if oi_old > 0 else 0.0
        else:
            oi_change = 0.0

        # Volume ratio vs 20-period avg
        if len(klines) >= 21:
            volumes  = np.array([k["volume"] for k in klines])
            vol_avg  = float(np.mean(volumes[-21:-1]))  # 20-bar avg excluding current
            vol_curr = float(volumes[-1])
            vol_ratio = vol_curr / vol_avg if vol_avg > 0 else 1.0
        else:
            vol_ratio = 1.0

        # ATR
        atr = _calc_atr(klines)
        atr_pct = atr / price * 100 if price > 0 else 0.0

        # Orderbook
        ob_ratio, ob_bias = _ob_imbalance(ob)

        score, sig_type = _score_signal(oi_change, vol_ratio, funding, ob_ratio, price_chg)
        if score < cfg.MIN_SCORE:
            return None

        direction = _direction(sig_type, price_chg, ob_bias, funding)
        levels = _calc_levels(price, atr, direction)

        details = (
            f"{sig_type} | {direction} | score={score} | "
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
        log.debug(f"{symbol}: analysis error — {e}")
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

        log.info(f"scan_all: scanning {len(tickers)} symbols")

        # Process in batches to respect rate limits
        batch_size = cfg.SCAN_BATCH_SIZE
        for i in range(0, len(tickers), batch_size):
            batch = tickers[i : i + batch_size]
            results = await asyncio.gather(
                *[_analyze_symbol(client, t) for t in batch],
                return_exceptions=True,
            )
            for r in results:
                if isinstance(r, Signal):
                    signals.append(r)
            if i + batch_size < len(tickers):
                await asyncio.sleep(cfg.SCAN_BATCH_DELAY)

        signals.sort(key=lambda s: s.score, reverse=True)
        state.last_scan_at = datetime.utcnow()
        state.scan_count += 1
        state.total_signals += len(signals)

        log.info(f"scan_all: found {len(signals)} signals (scan #{state.scan_count})")
        return signals

    except Exception as e:
        log.error(f"scan_all error: {e}")
        return []
    finally:
        _SCANNING = False


async def run_scan_and_broadcast(client: BybitClient, ntfy_url: str = "") -> None:
    """Called by APScheduler: scan, save to DB, broadcast via WS, push via ntfy."""
    # Refresh balance on every scan if API keys are configured
    if client.api_key and client.secret:
        bal = await client.get_balance()
        if bal > 0:
            state.balance = bal

    signals = await scan_all(client)

    for sig in signals:
        await db.save_signal(sig)
        await enter_trade(client, sig)

        # Broadcast to all connected WebSocket clients
        msg = json.dumps({"type": "signal", "data": sig.to_dict()})
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
            icon = "🟢" if sig.direction == "LONG" else "🔴"
            await send_push(
                ntfy_url,
                title=f"{icon} {sig.symbol} — {sig.signal_type}",
                message=sig.details,
                priority="high" if sig.score >= 75 else "default",
                tags=["chart_with_upwards_trend"] if sig.direction == "LONG" else ["chart_with_downwards_trend"],
            )

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
