"""
Tests for Scanner._record_close — the central position-close accounting function.

Key invariants:
  - unknown=True  → no wins/losses/streak changes, returns 0.0
  - WIN close     → wins++, streak reset to 0
  - LOSS close    → losses++, streak++
  - streak == 3   → notification sent exactly once
  - double call   → second call is silently ignored (returns 0.0)
"""
import pytest
from core.state import state


@pytest.fixture
def pos(make_pos):
    return make_pos()


# ── unknown=True ────────────────────────────────────────────────────────────

async def test_unknown_skips_win_loss(scanner, pos, mock_scanner_db):
    """unknown=True must not touch wins/losses/streak and must return 0.0."""
    state.positions["BTC-USDT"] = pos
    state.day.wins = 2
    state.day.losses = 1
    state.day.loss_streak = 1

    pnl = await scanner._record_close(pos, price=51000.0, unknown=True)

    assert pnl == 0.0
    assert state.day.wins == 2
    assert state.day.losses == 1
    assert state.day.loss_streak == 1


async def test_unknown_still_removes_position(scanner, pos, mock_scanner_db):
    """unknown=True must still remove the position from state."""
    state.positions["BTC-USDT"] = pos

    await scanner._record_close(pos, price=50000.0, unknown=True)

    assert "BTC-USDT" not in state.positions


# ── normal WIN ───────────────────────────────────────────────────────────────

async def test_win_increments_wins_and_resets_streak(scanner, pos, mock_scanner_db):
    """Profitable close: wins++, loss_streak reset."""
    state.positions["BTC-USDT"] = pos
    state.day.loss_streak = 2

    pnl = await scanner._record_close(pos, price=52000.0)

    assert state.day.wins == 1
    assert state.day.losses == 0
    assert state.day.loss_streak == 0
    assert pnl > 0


# ── normal LOSS ──────────────────────────────────────────────────────────────

async def test_loss_increments_streak(scanner, pos, mock_scanner_db):
    """Losing close: losses++, streak++."""
    state.positions["BTC-USDT"] = pos

    await scanner._record_close(pos, price=48000.0)

    assert state.day.losses == 1
    assert state.day.loss_streak == 1


async def test_streak_3_sends_notification_once(scanner, pos, mock_scanner_db):
    """The 3rd consecutive loss triggers the Telegram notification exactly once."""
    state.positions["BTC-USDT"] = pos
    state.day.loss_streak = 2

    await scanner._record_close(pos, price=48000.0)

    assert state.day.loss_streak == 3
    scanner._notify.assert_awaited_once()


async def test_streak_4_sends_notification(scanner, make_pos, mock_scanner_db):
    """The 4th consecutive loss must also send a pause notification (streak >= 3)."""
    pos1 = make_pos(symbol="BTC-USDT")
    state.positions["BTC-USDT"] = pos1
    state.day.loss_streak = 3

    await scanner._record_close(pos1, price=48000.0)

    assert state.day.loss_streak == 4
    scanner._notify.assert_awaited_once()


# ── double-call guard ────────────────────────────────────────────────────────

async def test_double_call_is_ignored(scanner, pos, mock_scanner_db):
    """Second _record_close for an already-removed symbol returns 0.0 without crash."""
    state.positions["BTC-USDT"] = pos

    await scanner._record_close(pos, price=52000.0)
    result = await scanner._record_close(pos, price=52000.0)

    assert result == 0.0
