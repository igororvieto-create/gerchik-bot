"""
Tests for BingXClient.get_funding_rate.

Key invariants:
  - Normal response  → returns float (rate * 100)
  - Missing field    → returns None (not 0.0, not raises)
  - Null data        → returns None
"""
import pytest
from unittest.mock import AsyncMock, patch


async def test_get_funding_rate_success():
    """Valid API response returns the rate as a float (multiplied by 100)."""
    from exchange.bingx import BingXClient
    client = BingXClient("key", "secret")

    good = {"code": 0, "data": {"lastFundingRate": "0.0001"}}
    with patch.object(client, "_get", new=AsyncMock(return_value=good)):
        rate = await client.get_funding_rate("BTC-USDT")

    assert isinstance(rate, float)
    assert rate == pytest.approx(0.01)   # 0.0001 * 100
    await client.close()


async def test_get_funding_rate_missing_field_returns_none():
    """Missing lastFundingRate field must return None, not raise or return 0."""
    from exchange.bingx import BingXClient
    client = BingXClient("key", "secret")

    bad = {"code": 0, "data": {}}  # no lastFundingRate
    with patch.object(client, "_get", new=AsyncMock(return_value=bad)):
        rate = await client.get_funding_rate("BTC-USDT")

    assert rate is None, "parse failure must be None so callers skip the funding filter"
    await client.close()


async def test_get_funding_rate_null_data_returns_none():
    """data=None in response must return None without crashing."""
    from exchange.bingx import BingXClient
    client = BingXClient("key", "secret")

    null_data = {"code": -1, "data": None}
    with patch.object(client, "_get", new=AsyncMock(return_value=null_data)):
        rate = await client.get_funding_rate("BTC-USDT")

    assert rate is None
    await client.close()


async def test_get_funding_rate_none_is_not_zero():
    """Critically: a parse error must NOT silently return 0.0.

    0.0 would pass the funding filter (0 < FUNDING_MAX_LONG = 0.02),
    allowing trades even when the funding API is unavailable.
    """
    from exchange.bingx import BingXClient
    client = BingXClient("key", "secret")

    bad = {"code": 0, "data": {}}
    with patch.object(client, "_get", new=AsyncMock(return_value=bad)):
        rate = await client.get_funding_rate("BTC-USDT")

    assert rate != 0.0, "parse failure must return None, not 0.0"
    await client.close()
