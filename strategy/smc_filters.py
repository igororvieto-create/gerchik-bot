"""
SMC/ICT filters module for Gerchik Bot.

Adds four complementary checks on top of the core Gerchik/VSA signal:
    1. Liquidity sweep detection (soft score + logging)
    2. Premium/Discount zone (HARD filter — direction must match)
    3. HTF Order Block confluence (soft score)
    4. Killzone timing (soft score)

Shadow-mode rollout (recommended):
    Week 1-2: SHADOW_MODE = True  -> nothing blocked, everything logged
    Review:   compare winrate of would-be-rejected vs would-be-passed
    Week 3+:  SHADOW_MODE = False -> P/D becomes a hard filter
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

import os as _os
SHADOW_MODE: bool = _os.getenv("SMC_SHADOW_MODE", "true").strip().lower() != "false"

SWEEP_LOOKBACK_CANDLES: int = 30
SWEEP_CLUSTER_MIN_TOUCHES: int = 2
SWEEP_CLUSTER_TOLERANCE_PCT: float = 0.0015
SWEEP_RETURN_WINDOW: int = 3

PD_IMPULSE_LOOKBACK: int = 50
PD_EQUILIBRIUM_BAND: float = 0.05

OB_IMPULSE_MIN_BODY_RATIO: float = 1.5
OB_LOOKBACK_DAYS: int = 60

KILLZONE_LONDON = (8, 11)
KILLZONE_NY = (13, 16)

SCORE_LIQUIDITY_SWEEP: int = 2
SCORE_HTF_OB_CONFLUENCE: int = 3
SCORE_KILLZONE: int = 1


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class Zone(str, Enum):
    PREMIUM = "premium"
    DISCOUNT = "discount"
    EQUILIBRIUM = "equilibrium"


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class OrderBlock:
    high: float
    low: float
    ts: int
    direction: Direction


@dataclass
class SMCResult:
    allowed: bool
    hard_blocked: bool
    score_bonus: int
    reasons: list[str]
    details: dict


# ---------------------------------------------------------------------------
# 1. Liquidity sweep
# ---------------------------------------------------------------------------

def detect_liquidity_sweep(
    candles: Sequence[Candle],
    direction: Direction,
    lookback: int = SWEEP_LOOKBACK_CANDLES,
) -> Optional[float]:
    """Return swept price level if a valid liquidity sweep is found, else None."""
    if len(candles) < lookback + SWEEP_RETURN_WINDOW + 1:
        return None

    window = candles[-(lookback + SWEEP_RETURN_WINDOW + 1):-SWEEP_RETURN_WINDOW]
    recent = candles[-SWEEP_RETURN_WINDOW - 1:]

    if direction == Direction.LONG:
        levels = [c.low for c in window]
        cluster_level = _find_equal_cluster(levels, mode="low")
        if cluster_level is None:
            return None
        for i, c in enumerate(recent[:-1]):
            if c.low < cluster_level * (1 - SWEEP_CLUSTER_TOLERANCE_PCT):
                if any(later.close > cluster_level for later in recent[i + 1:]):
                    return cluster_level
        return None

    levels = [c.high for c in window]
    cluster_level = _find_equal_cluster(levels, mode="high")
    if cluster_level is None:
        return None
    for i, c in enumerate(recent[:-1]):
        if c.high > cluster_level * (1 + SWEEP_CLUSTER_TOLERANCE_PCT):
            if any(later.close < cluster_level for later in recent[i + 1:]):
                return cluster_level
    return None


def _find_equal_cluster(levels: Sequence[float], mode: str) -> Optional[float]:
    if not levels:
        return None
    best_level: Optional[float] = None
    best_count = 0
    for anchor in levels:
        if anchor == 0:
            continue
        count = sum(1 for v in levels if abs(v - anchor) / anchor <= SWEEP_CLUSTER_TOLERANCE_PCT)
        if count > best_count:
            best_count = count
            matched = [v for v in levels if abs(v - anchor) / anchor <= SWEEP_CLUSTER_TOLERANCE_PCT]
            best_level = min(matched) if mode == "low" else max(matched)
    return best_level if best_count >= SWEEP_CLUSTER_MIN_TOUCHES else None


# ---------------------------------------------------------------------------
# 2. Premium / Discount  (HARD FILTER)
# ---------------------------------------------------------------------------

def get_premium_discount_zone(
    h4_candles: Sequence[Candle],
    current_price: float,
    lookback: int = PD_IMPULSE_LOOKBACK,
) -> Zone:
    """Classify current price within the last H4 swing range."""
    if len(h4_candles) < 10:
        return Zone.EQUILIBRIUM

    window = h4_candles[-lookback:]
    swing_high = max(c.high for c in window)
    swing_low  = min(c.low  for c in window)

    if swing_high <= swing_low:
        return Zone.EQUILIBRIUM

    mid  = (swing_high + swing_low) / 2
    band = (swing_high - swing_low) * PD_EQUILIBRIUM_BAND

    if current_price > mid + band:
        return Zone.PREMIUM
    if current_price < mid - band:
        return Zone.DISCOUNT
    return Zone.EQUILIBRIUM


def is_pd_aligned(zone: Zone, direction: Direction) -> bool:
    """Premium → SHORT, Discount → LONG, Equilibrium → pass both."""
    if zone == Zone.EQUILIBRIUM:
        return True
    return zone == Zone.DISCOUNT if direction == Direction.LONG else zone == Zone.PREMIUM


# ---------------------------------------------------------------------------
# 3. HTF Order Block confluence (D1)
# ---------------------------------------------------------------------------

def find_htf_order_block(
    d1_candles: Sequence[Candle],
    direction: Direction,
) -> Optional[OrderBlock]:
    """Find the most recent unmitigated D1 order block aligned with direction."""
    if len(d1_candles) < 5:
        return None

    recent = d1_candles[-30:]
    avg_body = sum(abs(c.close - c.open) for c in recent) / len(recent)
    if avg_body <= 0:
        return None

    lookback = min(OB_LOOKBACK_DAYS, len(d1_candles) - 2)

    for i in range(len(d1_candles) - 2, len(d1_candles) - lookback - 1, -1):
        impulse = d1_candles[i]
        prev    = d1_candles[i - 1]
        body    = abs(impulse.close - impulse.open)
        if body < avg_body * OB_IMPULSE_MIN_BODY_RATIO:
            continue

        if direction == Direction.LONG:
            if impulse.close <= impulse.open or prev.close > prev.open:
                continue
            ob = OrderBlock(high=prev.high, low=prev.low, ts=prev.ts, direction=Direction.LONG)
            if all(c.low > ob.low for c in d1_candles[i + 1:]):
                return ob
        else:
            if impulse.close >= impulse.open or prev.close < prev.open:
                continue
            ob = OrderBlock(high=prev.high, low=prev.low, ts=prev.ts, direction=Direction.SHORT)
            if all(c.high < ob.high for c in d1_candles[i + 1:]):
                return ob

    return None


def is_price_in_ob(price: float, ob: OrderBlock) -> bool:
    return ob.low <= price <= ob.high


# ---------------------------------------------------------------------------
# 4. Killzone timing
# ---------------------------------------------------------------------------

def is_in_killzone(ts_ms: Optional[int] = None) -> bool:
    """True if timestamp is in London (08-11 UTC) or NY (13-16 UTC) killzone."""
    dt = (
        datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        if ts_ms is not None
        else datetime.now(tz=timezone.utc)
    )
    h = dt.hour
    return KILLZONE_LONDON[0] <= h < KILLZONE_LONDON[1] or KILLZONE_NY[0] <= h < KILLZONE_NY[1]


# ---------------------------------------------------------------------------
# Top-level evaluator
# ---------------------------------------------------------------------------

def evaluate_smc(
    *,
    direction: Direction,
    current_price: float,
    h1_candles: Sequence[Candle],
    h4_candles: Sequence[Candle],
    d1_candles: Sequence[Candle],
    signal_ts_ms: Optional[int] = None,
) -> SMCResult:
    """Run all SMC checks. Call after Gerchik signal is confirmed."""
    reasons: list[str] = []
    details: dict = {"direction": direction.value, "shadow_mode": SHADOW_MODE}
    score_bonus = 0
    hard_blocked = False

    # HARD: Premium / Discount
    pd_zone = get_premium_discount_zone(h4_candles, current_price)
    pd_ok   = is_pd_aligned(pd_zone, direction)
    details.update(pd_zone=pd_zone.value, pd_aligned=pd_ok)
    if not pd_ok:
        hard_blocked = True
        reasons.append(f"P/D: {direction.value} в {pd_zone.value} зоне")
    else:
        reasons.append(f"P/D: {pd_zone.value} ✓")

    # SOFT: Liquidity sweep
    sweep_level = detect_liquidity_sweep(h1_candles, direction)
    details["liquidity_sweep_level"] = sweep_level
    if sweep_level is not None:
        score_bonus += SCORE_LIQUIDITY_SWEEP
        reasons.append(f"Sweep @ {sweep_level:.6g} +{SCORE_LIQUIDITY_SWEEP}")

    # SOFT: HTF Order Block
    ob = find_htf_order_block(d1_candles, direction)
    ob_in_zone = ob is not None and is_price_in_ob(current_price, ob)
    details["htf_ob"] = (
        {"high": ob.high, "low": ob.low, "ts": ob.ts, "in_zone": ob_in_zone} if ob else None
    )
    if ob_in_zone and ob is not None:
        score_bonus += SCORE_HTF_OB_CONFLUENCE
        reasons.append(f"D1 OB [{ob.low:.6g}–{ob.high:.6g}] +{SCORE_HTF_OB_CONFLUENCE}")

    # SOFT: Killzone
    in_kz = is_in_killzone(signal_ts_ms)
    details["in_killzone"] = in_kz
    if in_kz:
        score_bonus += SCORE_KILLZONE
        reasons.append(f"Killzone +{SCORE_KILLZONE}")

    details.update(score_bonus=score_bonus, reasons=reasons)

    allowed = True if SHADOW_MODE else not hard_blocked
    logger.info(
        "SMC %s [%s]: allowed=%s hard_blocked=%s bonus=%+d | %s",
        direction.value, "SHADOW" if SHADOW_MODE else "LIVE",
        allowed, hard_blocked, score_bonus, "; ".join(reasons),
    )

    return SMCResult(
        allowed=allowed, hard_blocked=hard_blocked,
        score_bonus=score_bonus, reasons=reasons, details=details,
    )


# ---------------------------------------------------------------------------
# Helper: convert parse_klines() numpy-array dict → list[Candle]
# ---------------------------------------------------------------------------

def klines_to_candles(kl: dict) -> list[Candle]:
    """Convert parse_klines() dict-of-numpy-arrays to a list of Candle objects."""
    ts_arr = kl.get("ts", [])
    n = len(ts_arr)
    if n == 0:
        return []
    return [
        Candle(
            ts=int(ts_arr[i]),
            open=float(kl["open"][i]),
            high=float(kl["high"][i]),
            low=float(kl["low"][i]),
            close=float(kl["close"][i]),
            volume=float(kl["volume"][i]),
        )
        for i in range(n)
    ]
