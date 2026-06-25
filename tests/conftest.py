import sys
import os

# Add project root so `from strategy.xxx import ...` works
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

# Add strategy/ so `import smc_filters` resolves to strategy/smc_filters.py
# IMPORTANT: append (not insert at 0) so `import strategy` still resolves to
# the top-level strategy/ package, not to the nested strategy/strategy/ sub-package.
_strategy = os.path.join(_root, "strategy")
if _strategy not in sys.path:
    sys.path.append(_strategy)

import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from core.state import DayStats


@pytest.fixture(autouse=True)
def reset_state():
    """Reset shared state singleton before every test."""
    from core.state import state
    state.positions = {}
    state.day = DayStats()
    state.total_pnl = 0.0
    state.current_balance = 0.0
    state.paused = False
    yield
    state.positions = {}
    state.day = DayStats()
    state.total_pnl = 0.0


@pytest.fixture
def mock_scanner_db():
    """Patch strategy.scanner.db so Scanner works without SQLite."""
    m = MagicMock()
    m.load_all_loss_streaks.return_value = {}
    m.load_all_cooldowns.return_value = {}
    m.async_save_trade = AsyncMock()
    m.async_save_kv = AsyncMock()
    m.async_save_open_position = AsyncMock()
    m.async_delete_open_position = AsyncMock()
    with patch("strategy.scanner.db", m):
        yield m


@pytest.fixture
def scanner(mock_scanner_db):
    """Scanner with mocked exchange, bot, and _notify."""
    from strategy.scanner import Scanner
    s = Scanner(MagicMock(), MagicMock())
    s._notify = AsyncMock()
    return s


@pytest.fixture
def make_pos():
    """Factory for Position objects with sensible defaults."""
    from core.state import Position

    def _make(symbol="BTC-USDT", side="LONG", entry=50000.0, sl=49000.0,
              tp1=51000.0, tp2=52000.0, tp3=53000.0, qty=0.01,
              risk_usdt=10.0, partial_pnl_taken=0.0):
        return Position(
            symbol=symbol, side=side, entry=entry, sl=sl,
            tp1=tp1, tp2=tp2, tp3=tp3, qty=qty, risk_usdt=risk_usdt,
            partial_pnl_taken=partial_pnl_taken,
        )

    return _make
