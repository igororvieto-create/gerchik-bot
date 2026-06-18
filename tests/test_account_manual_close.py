"""
Tests for telegram/handlers._account_manual_close.

Key invariants:
  - DB write failure must NOT block in-memory state update
  - 3rd consecutive loss sends notification exactly once (== 3, not >= 3)
  - 4th loss sends no extra notification
"""
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from core.state import state


@pytest.fixture
def pos(make_pos):
    return make_pos(symbol="ETH-USDT", entry=3000.0, sl=2900.0,
                    tp1=3100.0, tp2=3200.0, tp3=3300.0, qty=0.1)


@pytest.fixture
def mock_ex_win():
    """Exchange mock returning a price above entry → WIN."""
    ex = MagicMock()
    ex.get_ticker = AsyncMock(return_value={"lastPrice": "3200.0"})
    return ex


@pytest.fixture
def mock_ex_loss():
    """Exchange mock returning a price below entry → LOSS."""
    ex = MagicMock()
    ex.get_ticker = AsyncMock(return_value={"lastPrice": "2800.0"})
    return ex


# ── DB failure resilience ────────────────────────────────────────────────────

async def test_db_failure_still_updates_state(pos, mock_ex_win):
    """When db.save_trade raises, state (total_pnl, wins) must still be updated."""
    with patch("core.db.save_trade", side_effect=Exception("disk full")), \
         patch("core.db.save_kv"), \
         patch("strategy.scanner._global_scanner", None):

        from telegram.handlers import _account_manual_close
        close_px, leg_pnl = await _account_manual_close(mock_ex_win, pos)

    assert state.day.wins == 1, "win must be counted even when DB fails"
    assert state.total_pnl == pytest.approx(leg_pnl)


# ── loss streak notifications ────────────────────────────────────────────────

async def test_loss_streak_3_notifies_once(pos, mock_ex_loss):
    """The 3rd consecutive loss must trigger exactly one Telegram notification."""
    state.day.loss_streak = 2

    notify_mock = AsyncMock()
    gs = MagicMock()
    gs._notify = notify_mock

    with patch("core.db.save_trade"), \
         patch("core.db.save_kv"), \
         patch("strategy.scanner._global_scanner", gs):

        from telegram.handlers import _account_manual_close
        await _account_manual_close(mock_ex_loss, pos)

    assert state.day.loss_streak == 3
    notify_mock.assert_awaited_once()


async def test_loss_streak_4_no_extra_notification(pos, mock_ex_loss):
    """The 4th (and higher) loss must NOT send another notification."""
    state.day.loss_streak = 3  # already at 3

    notify_mock = AsyncMock()
    gs = MagicMock()
    gs._notify = notify_mock

    with patch("core.db.save_trade"), \
         patch("core.db.save_kv"), \
         patch("strategy.scanner._global_scanner", gs):

        from telegram.handlers import _account_manual_close
        await _account_manual_close(mock_ex_loss, pos)

    assert state.day.loss_streak == 4
    notify_mock.assert_not_awaited()
