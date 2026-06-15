"""
tests/test_smc_filters.py

Unit tests for strategy/smc_filters.py.
Run: python -m pytest tests/test_smc_filters.py -v
"""

from __future__ import annotations

from datetime import datetime, timezone

try:
    import pytest  # noqa: F401
except ImportError:
    pytest = None  # type: ignore

from smc_filters import (
    Candle,
    Direction,
    Zone,
    detect_liquidity_sweep,
    evaluate_smc,
    find_htf_order_block,
    get_premium_discount_zone,
    is_in_killzone,
    is_pd_aligned,
    is_price_in_ob,
)


def _mk(ts: int, o: float, h: float, l: float, c: float, v: float = 100) -> Candle:
    return Candle(ts=ts, open=o, high=h, low=l, close=c, volume=v)


# ---------- Premium / Discount ----------

class TestPremiumDiscount:
    def test_discount_zone_for_long(self):
        # Range 100..200, price 110 -> discount
        candles = [_mk(i, 150, 200, 100, 150) for i in range(20)]
        assert get_premium_discount_zone(candles, current_price=110) == Zone.DISCOUNT

    def test_premium_zone_for_short(self):
        candles = [_mk(i, 150, 200, 100, 150) for i in range(20)]
        assert get_premium_discount_zone(candles, current_price=190) == Zone.PREMIUM

    def test_equilibrium_band(self):
        candles = [_mk(i, 150, 200, 100, 150) for i in range(20)]
        assert get_premium_discount_zone(candles, current_price=150) == Zone.EQUILIBRIUM

    def test_pd_alignment(self):
        assert is_pd_aligned(Zone.DISCOUNT, Direction.LONG) is True
        assert is_pd_aligned(Zone.PREMIUM, Direction.LONG) is False
        assert is_pd_aligned(Zone.PREMIUM, Direction.SHORT) is True
        assert is_pd_aligned(Zone.DISCOUNT, Direction.SHORT) is False
        assert is_pd_aligned(Zone.EQUILIBRIUM, Direction.LONG) is True
        assert is_pd_aligned(Zone.EQUILIBRIUM, Direction.SHORT) is True

    def test_insufficient_data_fails_open(self):
        candles = [_mk(0, 100, 100, 100, 100)]
        assert get_premium_discount_zone(candles, 100) == Zone.EQUILIBRIUM


# ---------- Liquidity sweep ----------

class TestLiquiditySweep:
    def test_long_sweep_detected(self):
        candles = []
        for i in range(30):
            candles.append(_mk(i, 105, 110, 100, 107))
        candles.append(_mk(30, 105, 108, 98, 106))   # sweep below 100
        candles.append(_mk(31, 106, 109, 101, 108))  # close back above
        candles.append(_mk(32, 108, 112, 104, 111))
        candles.append(_mk(33, 111, 114, 109, 113))

        level = detect_liquidity_sweep(candles, Direction.LONG)
        assert level is not None
        assert abs(level - 100) < 1

    def test_short_sweep_detected(self):
        candles = []
        for i in range(30):
            candles.append(_mk(i, 105, 110, 100, 103))
        candles.append(_mk(30, 108, 112, 105, 106))  # sweep above 110
        candles.append(_mk(31, 106, 109, 102, 104))  # close back below
        candles.append(_mk(32, 104, 107, 100, 101))
        candles.append(_mk(33, 101, 103, 98, 99))

        level = detect_liquidity_sweep(candles, Direction.SHORT)
        assert level is not None
        assert abs(level - 110) < 1

    def test_no_sweep_in_quiet_market(self):
        candles = [_mk(i, 100, 101, 99, 100) for i in range(50)]
        assert detect_liquidity_sweep(candles, Direction.LONG) is None
        assert detect_liquidity_sweep(candles, Direction.SHORT) is None

    def test_insufficient_data(self):
        candles = [_mk(i, 100, 101, 99, 100) for i in range(5)]
        assert detect_liquidity_sweep(candles, Direction.LONG) is None


# ---------- HTF Order Block ----------

class TestOrderBlock:
    def test_bullish_ob_found(self):
        candles = [_mk(i, 100, 101, 99, 100) for i in range(40)]
        candles.append(_mk(40, 100, 100.5, 98, 98.5))    # bearish OB candle
        candles.append(_mk(41, 98.5, 108, 98.4, 107.5))  # strong bullish impulse
        for i in range(42, 50):
            candles.append(_mk(i, 107, 110, 106, 109))

        ob = find_htf_order_block(candles, Direction.LONG)
        assert ob is not None
        assert ob.direction == Direction.LONG
        assert ob.low == 98

    def test_mitigated_ob_skipped(self):
        candles = [_mk(i, 100, 101, 99, 100) for i in range(40)]
        candles.append(_mk(40, 100, 100.5, 98, 98.5))
        candles.append(_mk(41, 98.5, 108, 98.4, 107.5))
        candles.append(_mk(42, 107, 108, 97, 99))   # mitigation below OB low
        for i in range(43, 50):
            candles.append(_mk(i, 100, 102, 98.5, 101))

        ob = find_htf_order_block(candles, Direction.LONG)
        if ob is not None:
            assert ob.low != 98  # mitigated OB must not be returned

    def test_no_ob_in_chop(self):
        candles = [_mk(i, 100, 101, 99, 100) for i in range(60)]
        assert find_htf_order_block(candles, Direction.LONG) is None

    def test_price_in_ob(self):
        from smc_filters import OrderBlock
        ob = OrderBlock(high=110, low=100, ts=0, direction=Direction.LONG)
        assert is_price_in_ob(105, ob) is True
        assert is_price_in_ob(100, ob) is True
        assert is_price_in_ob(110, ob) is True
        assert is_price_in_ob(99, ob) is False
        assert is_price_in_ob(111, ob) is False


# ---------- Killzone ----------

class TestKillzone:
    def _ts(self, hour_utc: int) -> int:
        return int(datetime(2026, 1, 15, hour_utc, 30, tzinfo=timezone.utc).timestamp() * 1000)

    def test_london_open_inside(self):
        assert is_in_killzone(self._ts(9)) is True

    def test_ny_open_inside(self):
        assert is_in_killzone(self._ts(14)) is True

    def test_asia_outside(self):
        assert is_in_killzone(self._ts(3)) is False

    def test_lunch_gap_outside(self):
        assert is_in_killzone(self._ts(12)) is False

    def test_late_evening_outside(self):
        assert is_in_killzone(self._ts(22)) is False


# ---------- Integration: evaluate_smc ----------

class TestEvaluateSMC:
    def _build_h4_discount(self) -> list[Candle]:
        return [_mk(i, 150, 200, 100, 150) for i in range(60)]

    def _build_h4_premium(self) -> list[Candle]:
        return [_mk(i, 150, 200, 100, 150) for i in range(60)]

    def _build_d1_with_bullish_ob(self) -> list[Candle]:
        candles = [_mk(i, 100, 101, 99, 100) for i in range(40)]
        candles.append(_mk(40, 100, 100.5, 98, 98.5))
        candles.append(_mk(41, 98.5, 130, 98.4, 128))
        for i in range(42, 50):
            candles.append(_mk(i, 120, 125, 110, 122))
        return candles

    def test_long_in_discount_passes(self):
        result = evaluate_smc(
            direction=Direction.LONG,
            current_price=110,
            h1_candles=[_mk(i, 110, 111, 109, 110) for i in range(50)],
            h4_candles=self._build_h4_discount(),
            d1_candles=self._build_d1_with_bullish_ob(),
        )
        assert result.allowed is True
        assert result.hard_blocked is False

    def test_long_in_premium_blocked_when_live(self):
        import smc_filters
        original = smc_filters.SHADOW_MODE
        smc_filters.SHADOW_MODE = False
        try:
            result = evaluate_smc(
                direction=Direction.LONG,
                current_price=190,
                h1_candles=[_mk(i, 190, 191, 189, 190) for i in range(50)],
                h4_candles=self._build_h4_premium(),
                d1_candles=[_mk(i, 100, 101, 99, 100) for i in range(50)],
            )
            assert result.allowed is False
            assert result.hard_blocked is True
        finally:
            smc_filters.SHADOW_MODE = original

    def test_shadow_mode_allows_despite_block(self):
        import smc_filters
        original = smc_filters.SHADOW_MODE
        smc_filters.SHADOW_MODE = True
        try:
            result = evaluate_smc(
                direction=Direction.LONG,
                current_price=190,
                h1_candles=[_mk(i, 190, 191, 189, 190) for i in range(50)],
                h4_candles=self._build_h4_premium(),
                d1_candles=[_mk(i, 100, 101, 99, 100) for i in range(50)],
            )
            assert result.allowed is True
            assert result.hard_blocked is True
        finally:
            smc_filters.SHADOW_MODE = original

    def test_details_contains_required_fields(self):
        result = evaluate_smc(
            direction=Direction.LONG,
            current_price=110,
            h1_candles=[_mk(i, 110, 111, 109, 110) for i in range(50)],
            h4_candles=self._build_h4_discount(),
            d1_candles=self._build_d1_with_bullish_ob(),
        )
        for key in ("direction", "shadow_mode", "pd_zone", "pd_aligned",
                    "liquidity_sweep_level", "htf_ob", "in_killzone",
                    "score_bonus", "reasons"):
            assert key in result.details, f"missing key: {key}"


if __name__ == "__main__":
    if pytest is not None:
        pytest.main([__file__, "-v"])
    else:
        print("pytest not installed")
