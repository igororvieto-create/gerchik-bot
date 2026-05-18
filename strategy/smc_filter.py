# Canonical module is strategy.smc_filters — this file is an alias.
from strategy.smc_filters import *  # noqa: F401, F403


Adds four complementary checks on top of the core Gerchik/VSA signal:
    1. Liquidity sweep detection (soft score + logging)
    2. Premium/Discount zone (HARD filter — direction must match)
    3. HTF Order Block confluence (soft score)
    4. Killzone timing (soft score)

Design principles:
    - Same interface style as orderbook_analyzer.py
    - Pure functions where possible, no side effects beyond logging
    - All rejections/scores written via rejection_logger for shadow-mode audit
    - Shadow-mode flag allows disabling hard filter while still logging decisions
    - Type hints, docstrings, defensive against empty/short candle arrays

Shadow-mode rollout (recommended):
    Week 1-2: SHADOW_MODE = True  -> nothing is blocked, everything logged
    Review:   query rejection_logger, compare winrate of would-be-rejected
              vs would-be-passed signals, validate P/D filter assumption
    Week 3+:  SHADOW_MODE = False -> P/D becomes hard filter, others stay soft
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

# Master switch: True = log decisions but never block signal
# Flip to False after 2 weeks of shadow data review
SHADOW_MODE: bool = True

# Liquidity sweep detection
SWEEP_LOOKBACK_CANDLES: int = 30          # how far back to look for equal H/L cluster
SWEEP_CLUSTER_MIN_TOUCHES: int = 2        # minimum equal highs/lows to call it "liquidity"
SWEEP_CLUSTER_TOLERANCE_PCT: float = 0.0015  # 0.15% tolerance for "equal" level
SWEEP_RETURN_WINDOW: int = 3              # candles within which price must return inside

# Premium/Discount
PD_IMPULSE_LOOKBACK: int = 50             # candles to find last significant impulse on H4
PD_EQUILIBRIUM_BAND: float = 0.05         # ±5% around 0.5 fib = neutral zone (no signal block)

# HTF Order Block (D1)
OB_IMPULSE_MIN_BODY_RATIO: float = 1.5    # impulse candle body must be >= 1.5x avg body
OB_LOOKBACK_DAYS: int = 60                # search window for valid unmitigated OBs

# Killzones (UTC hours)
KILLZONE_LONDON = (8, 11)   # 08:00 - 10:59 UTC
KILLZONE_NY = (13, 16)      # 13:00 - 15:59 UTC

# Soft score weights (additive bonus to the existing Gerchik signal score)
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
    """OHLCV candle. Timestamp is epoch ms (BingX convention)."""
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class OrderBlock:
    """Last opposing candle before an impulsive BOS move."""
    high: float
    low: float
    ts: int
    direction: Direction      # direction of the impulse that followed (bullish OB = LONG)


@dataclass
class SMCResult:
    """
    Aggregated SMC verdict for a candidate signal.

    Attributes:
        allowed:        Final decision (after considering SHADOW_MODE).
        hard_blocked:   True if a hard filter would block in non-shadow mode.
        score_bonus:    Additive bonus to add to the Gerchik signal score.
        reasons:        Human-readable list of contributing factors (for logging).
        details:        Structured dict for rejection_logger.
    """
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
    """
    Detect a recent liquidity sweep that aligns with the trade direction.

    A long-side sweep = price swept equal LOWS (took out sell-stops below
    a cluster of lows) and returned back inside. This is the SMC formalisation
    of Gerchik's false-breakout.

    Args:
        candles: chronological list, oldest first. Need >= lookback + 1.
        direction: direction of the candidate trade.
        lookback: how many recent candles to scan for the equal-level cluster.

    Returns:
        The swept price level if a valid sweep is found, else None.
    """
    if len(candles) < lookback + SWEEP_RETURN_WINDOW + 1:
        return None

    window = candles[-(lookback + SWEEP_RETURN_WINDOW + 1):-SWEEP_RETURN_WINDOW]
    recent = candles[-SWEEP_RETURN_WINDOW - 1:]  # candidate sweep + return candles

    # For LONG we look for equal LOWS swept down; for SHORT — equal HIGHS swept up
    if direction == Direction.LONG:
        levels = [c.low for c in window]
        cluster_level = _find_equal_cluster(levels, mode="low")
        if cluster_level is None:
            return None
        # Sweep candle: wicked BELOW cluster, then close ABOVE cluster within window
        for i, c in enumerate(recent[:-1]):
            if c.low < cluster_level * (1 - SWEEP_CLUSTER_TOLERANCE_PCT):
                if any(later.close > cluster_level for later in recent[i + 1:]):
                    return cluster_level
        return None

    # SHORT mirror
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
    """Find the most-touched price level within tolerance. Returns the level or None."""
    if not levels:
        return None
    best_level: Optional[float] = None
    best_count = 0
    for anchor in levels:
        count = sum(
            1 for v in levels
            if abs(v - anchor) / anchor <= SWEEP_CLUSTER_TOLERANCE_PCT
        )
        if count > best_count:
            best_count = count
            best_level = (
                min(v for v in levels if abs(v - anchor) / anchor <= SWEEP_CLUSTER_TOLERANCE_PCT)
                if mode == "low"
                else max(v for v in levels if abs(v - anchor) / anchor <= SWEEP_CLUSTER_TOLERANCE_PCT)
            )
    return best_level if best_count >= SWEEP_CLUSTER_MIN_TOUCHES else None


# ---------------------------------------------------------------------------
# 2. Premium / Discount  (HARD FILTER)
# ---------------------------------------------------------------------------

def get_premium_discount_zone(
    h4_candles: Sequence[Candle],
    current_price: float,
    lookback: int = PD_IMPULSE_LOOKBACK,
) -> Zone:
    """
    Classify the current price within the last significant H4 impulse.

    Lower half (below 0.5 fib) = discount  -> longs allowed
    Upper half (above 0.5 fib) = premium   -> shorts allowed
    Narrow band around 0.5    = equilibrium -> no P/D bias, do not block

    Args:
        h4_candles: chronological H4 candles, oldest first.
        current_price: latest mark price.
        lookback: candles to scan for swing high/low.
    """
    if len(h4_candles) < 10:
        return Zone.EQUILIBRIUM  # not enough data, fail-open

    window = h4_candles[-lookback:]
    swing_high = max(c.high for c in window)
    swing_low = min(c.low for c in window)

    if swing_high <= swing_low:
        return Zone.EQUILIBRIUM

    mid = (swing_high + swing_low) / 2
    band = (swing_high - swing_low) * PD_EQUILIBRIUM_BAND

    if current_price > mid + band:
        return Zone.PREMIUM
    if current_price < mid - band:
        return Zone.DISCOUNT
    return Zone.EQUILIBRIUM


def is_pd_aligned(zone: Zone, direction: Direction) -> bool:
    """Premium aligns with SHORT, discount with LONG, equilibrium passes."""
    if zone == Zone.EQUILIBRIUM:
        return True
    if direction == Direction.LONG:
        return zone == Zone.DISCOUNT
    return zone == Zone.PREMIUM


# ---------------------------------------------------------------------------
# 3. HTF Order Block confluence (D1)
# ---------------------------------------------------------------------------

def find_htf_order_block(
    d1_candles: Sequence[Candle],
    direction: Direction,
) -> Optional[OrderBlock]:
    """
    Find the most recent unmitigated D1 order block in the trade direction.

    Definition used here:
        Bullish OB (for LONG) = last bearish D1 candle before an impulsive
        bullish move that broke the prior swing high.
        Bearish OB (for SHORT) = mirror.

    Returns the OB or None. Unmitigated = price has not yet traded back
    through the OB after its formation.
    """
    if len(d1_candles) < 5:
        return None

    avg_body = sum(abs(c.close - c.open) for c in d1_candles[-30:]) / min(30, len(d1_candles))
    if avg_body <= 0:
        return None

    lookback = min(OB_LOOKBACK_DAYS, len(d1_candles) - 2)

    # Walk backwards looking for impulse candle preceded by an opposing candle
    for i in range(len(d1_candles) - 2, len(d1_candles) - lookback - 1, -1):
        impulse = d1_candles[i]
        prev = d1_candles[i - 1]
        body = abs(impulse.close - impulse.open)
        if body < avg_body * OB_IMPULSE_MIN_BODY_RATIO:
            continue

        if direction == Direction.LONG:
            # bullish impulse after a bearish/neutral candle
            if impulse.close <= impulse.open or prev.close > prev.open:
                continue
            ob = OrderBlock(high=prev.high, low=prev.low, ts=prev.ts, direction=Direction.LONG)
            # Check unmitigated: no candle after i has traded back through OB low
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
    """Is current price inside the OB zone?"""
    return ob.low <= price <= ob.high


# ---------------------------------------------------------------------------
# 4. Killzone timing
# ---------------------------------------------------------------------------

def is_in_killzone(ts_ms: Optional[int] = None) -> bool:
    """True if timestamp falls in London or NY open killzone (UTC)."""
    dt = (
        datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        if ts_ms is not None
        else datetime.now(tz=timezone.utc)
    )
    hour = dt.hour
    return (
        KILLZONE_LONDON[0] <= hour < KILLZONE_LONDON[1]
        or KILLZONE_NY[0] <= hour < KILLZONE_NY[1]
    )


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
    """
    Run all SMC checks and return aggregated verdict.

    Call this AFTER the core Gerchik/VSA signal is generated but BEFORE
    risk_validator. Use result.allowed to gate execution and
    result.score_bonus to adjust the signal score.
    """
    reasons: list[str] = []
    details: dict = {"direction": direction.value, "shadow_mode": SHADOW_MODE}
    score_bonus = 0
    hard_blocked = False

    # --- HARD: Premium / Discount ---
    pd_zone = get_premium_discount_zone(h4_candles, current_price)
    pd_ok = is_pd_aligned(pd_zone, direction)
    details["pd_zone"] = pd_zone.value
    details["pd_aligned"] = pd_ok
    if not pd_ok:
        hard_blocked = True
        reasons.append(f"P/D: {direction.value} в {pd_zone.value} зоне")
    else:
        reasons.append(f"P/D: {pd_zone.value} ✓")

    # --- SOFT: Liquidity sweep ---
    sweep_level = detect_liquidity_sweep(h1_candles, direction)
    details["liquidity_sweep_level"] = sweep_level
    if sweep_level is not None:
        score_bonus += SCORE_LIQUIDITY_SWEEP
        reasons.append(f"Sweep ликвидности @ {sweep_level:.6g} +{SCORE_LIQUIDITY_SWEEP}")

    # --- SOFT: HTF Order Block confluence ---
    ob = find_htf_order_block(d1_candles, direction)
    ob_confluence = ob is not None and is_price_in_ob(current_price, ob)
    details["htf_ob"] = (
        {"high": ob.high, "low": ob.low, "ts": ob.ts, "in_zone": ob_confluence}
        if ob else None
    )
    if ob_confluence:
        score_bonus += SCORE_HTF_OB_CONFLUENCE
        reasons.append(f"D1 OB [{ob.low:.6g}–{ob.high:.6g}] +{SCORE_HTF_OB_CONFLUENCE}")

    # --- SOFT: Killzone ---
    in_kz = is_in_killzone(signal_ts_ms)
    details["in_killzone"] = in_kz
    if in_kz:
        score_bonus += SCORE_KILLZONE
        reasons.append(f"Killzone (London/NY) +{SCORE_KILLZONE}")

    details["score_bonus"] = score_bonus
    details["reasons"] = reasons

    # Final decision honours shadow-mode flag
    allowed = True if SHADOW_MODE else not hard_blocked

    logger.info(
        "SMC %s %s: allowed=%s hard_blocked=%s bonus=%+d | %s",
        direction.value, "SHADOW" if SHADOW_MODE else "LIVE",
        allowed, hard_blocked, score_bonus, "; ".join(reasons),
    )

    return SMCResult(
        allowed=allowed,
        hard_blocked=hard_blocked,
        score_bonus=score_bonus,
        reasons=reasons,
        details=details,
    )


# ---------------------------------------------------------------------------
# Helper: convert parse_klines() dict → list[Candle]
# ---------------------------------------------------------------------------

def klines_to_candles(kl: dict) -> list[Candle]:
    """Convert parse_klines() numpy-array dict to a list of Candle objects."""
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
