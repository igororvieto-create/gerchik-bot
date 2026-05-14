"""
tests/test_orderbook_analyzer.py

Unit tests for strategy/orderbook_analyzer.py.
Run with: python -m pytest tests/test_orderbook_analyzer.py -v
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from strategy.orderbook_analyzer import (
    OrderbookConfig,
    OrderbookSnapshot,
    compute_metrics,
    validate_signal,
)


def _make_snapshot(symbol, mid, bid_usdt, ask_usdt, n_levels=50):
    """
    Build a synthetic snapshot with ~2 bps spread and uniform depth.
    step = 1 bps so spread = 2 bps < 15 bps threshold.
    """
    step = mid * 0.0001  # 1 bps per level
    bids = []
    asks = []
    for i in range(n_levels):
        bp = round(mid - step * (i + 1), 8)
        ap = round(mid + step * (i + 1), 8)
        bids.append((bp, (bid_usdt / n_levels) / bp))
        asks.append((ap, (ask_usdt / n_levels) / ap))
    return OrderbookSnapshot(symbol=symbol, timestamp_ms=0, bids=bids, asks=asks)


def _insert_wall(snap, price, size_usdt, side):
    """Insert a wall-sized order into bids or asks, maintaining price sort order."""
    qty = size_usdt / price
    if side == "ask":
        snap.asks.append((price, qty))
        snap.asks.sort(key=lambda x: x[0])  # ascending
    else:
        snap.bids.append((price, qty))
        snap.bids.sort(key=lambda x: x[0], reverse=True)  # descending


# ── Test 1: SHORT blocked by bid-heavy imbalance + bid wall between entry/TP1 ─

def test_case_sol_blocks_short():
    """
    Bids dominate 5:1 → imbalance > threshold → SHORT blocked.
    Additionally, a large bid wall sits between entry and SHORT TP1,
    meaning buyers are defending that level (wall_blocks_tp1 check).
    """
    mid = 91.15
    snap = _make_snapshot("SOL-USDT", mid, bid_usdt=500_000, ask_usdt=100_000)

    # Bid wall at 4% below mid (between entry and SHORT TP1 at -10%)
    # It's outside 3% band so doesn't skew the imbalance calculation
    _insert_wall(snap, mid * 0.96, 529_000, "bid")

    cfg = OrderbookConfig(imbalance_threshold=0.15, max_spread_bps=15.0)
    metrics = compute_metrics(snap, cfg)

    # For SHORT: entry at mid, sl above, tp1 below
    entry = mid
    sl    = mid * 1.06
    tp1   = mid * 0.90  # -10% target

    result = validate_signal("SHORT", entry, sl, tp1, leverage=5, metrics=metrics, config=cfg)
    assert not result.passed, f"Expected SHORT to be blocked, rejections={result.rejections}"
    assert any("imbalance" in r for r in result.rejections), result.rejections


# ── Test 2: TIA LONG blocked by thin book + high leverage ────────────────────

def test_case_tia_after_climax():
    """After climax volume: thin book (<$100k ±1%) + high leverage → LONG blocked."""
    mid  = 0.4777
    snap = _make_snapshot("TIA-USDT", mid, bid_usdt=2_400, ask_usdt=45_000)
    cfg  = OrderbookConfig(
        thin_book_threshold_usdt=100_000,
        thin_book_max_leverage=3,
        imbalance_threshold=0.15,
        max_spread_bps=15.0,
    )
    metrics = compute_metrics(snap, cfg)
    assert metrics.is_thin, "Book should be classified as thin"

    entry = mid
    sl    = mid * 0.93
    tp1   = mid * 1.07

    result = validate_signal("LONG", entry, sl, tp1, leverage=10, metrics=metrics, config=cfg)
    assert not result.passed, f"Expected LONG to be blocked, rejections={result.rejections}"
    assert any("thin_book" in r for r in result.rejections), result.rejections


# ── Test 3: Thin book at allowed leverage → pass with warning ────────────────

def test_case_thin_book_leverage_suggestion():
    """Thin book at exactly max_leverage should pass but emit a warning."""
    mid  = 150.0
    snap = _make_snapshot("TSLA-USDT", mid, bid_usdt=5_000, ask_usdt=5_000)
    cfg  = OrderbookConfig(
        thin_book_threshold_usdt=100_000,
        thin_book_max_leverage=3,
        max_spread_bps=15.0,
    )
    metrics = compute_metrics(snap, cfg)
    assert metrics.is_thin

    entry = mid
    sl    = mid * 0.94
    tp1   = mid * 1.10

    result = validate_signal("LONG", entry, sl, tp1, leverage=3, metrics=metrics, config=cfg)
    assert result.passed, f"Thin book at max_leverage should pass: {result.rejections}"
    assert any("thin_book" in w for w in result.warnings), result.warnings


# ── Test 4: Healthy balanced book passes all checks ──────────────────────────

def test_case_healthy_long():
    """Deep balanced book with no walls: LONG must pass all 5 checks."""
    mid  = 50_000.0
    snap = _make_snapshot("BTC-USDT", mid, bid_usdt=5_000_000, ask_usdt=5_000_000)
    cfg  = OrderbookConfig(max_spread_bps=15.0)
    metrics = compute_metrics(snap, cfg)

    entry = mid
    sl    = mid * 0.97
    tp1   = mid * 1.06

    result = validate_signal("LONG", entry, sl, tp1, leverage=3, metrics=metrics, config=cfg)
    assert result.passed, f"Healthy book should pass: {result.rejections}"
    assert not result.rejections


if __name__ == "__main__":
    for fn in [
        test_case_sol_blocks_short,
        test_case_tia_after_climax,
        test_case_thin_book_leverage_suggestion,
        test_case_healthy_long,
    ]:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as e:
            print(f"FAIL  {fn.__name__}: {e}")
