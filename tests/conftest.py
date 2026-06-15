import sys
import os

# Add project root so `from strategy.xxx import ...` works
_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _root not in sys.path:
    sys.path.insert(0, _root)

# Add strategy/ so `import smc_filters` resolves to strategy/smc_filters.py
_strategy = os.path.join(_root, "strategy")
if _strategy not in sys.path:
    sys.path.insert(0, _strategy)
