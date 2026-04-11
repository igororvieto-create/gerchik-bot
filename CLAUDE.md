# CLAUDE.md — Gerchik Bot

## Project Overview

Gerchik Bot is a Python-based automated cryptocurrency futures trading bot that operates on the BingX exchange via a Telegram interface. It uses multi-timeframe technical analysis (the "Gerchik" strategy) to detect high-probability setups and can trade fully automatically or with manual confirmation.

---

## Repository Structure

```
gerchik-bot/
├── main.py                      # Entry point: bot lifecycle, scheduler setup
├── core/
│   ├── config.py                # Config dataclass, all env vars with defaults
│   └── state.py                 # In-memory state: Position, DayStats, BotState
├── exchange/
│   └── bingx.py                 # BingX async REST API client (HMAC-SHA256)
├── strategy/
│   ├── scanner.py               # Orchestrates scanning, entry, monitoring
│   └── strategy/
│       └── gerchik.py           # Technical analysis: patterns, S/R, signals
├── telegram/
│   └── handlers.py              # Telegram command handlers (aiogram)
├── requirements.txt             # Python dependencies
├── nixpacks.toml                # Nixpacks build + start configuration
├── Procfile                     # Single worker process definition
└── .python-version              # Python 3.11
```

---

## Architecture

### Data Flow

```
APScheduler (every 15m)
  └── Scanner.scan_all()
        └── _analyze(symbol) per pair
              └── BingXClient.get_klines() × 3 timeframes
              └── strategy.gerchik.analyze() → Signal | None
        └── _handle(signal)
              ├── auto mode  → _enter(signal)
              └── manual mode → pending queue → Telegram confirm/skip
                                 └── /confirm_SYMBOL → _enter(signal)
```

### Module Responsibilities

| Module | Responsibility |
|---|---|
| `main.py` | Bot bootstrap, scheduler wiring, startup notification |
| `core/config.py` | Single `cfg` singleton; env-configurable trading parameters |
| `core/state.py` | Single `state` singleton; all in-memory runtime state |
| `exchange/bingx.py` | BingX Perpetual Futures REST API (async/aiohttp) |
| `strategy/strategy/gerchik.py` | Pure functions: EMA, S/R levels, candle patterns, signal scoring |
| `strategy/scanner.py` | Stateful job runner: batched scanning, position lifecycle |
| `telegram/handlers.py` | Telegram command dispatch; mutates `cfg` and `state` |

---

## Key Concepts

### Multi-Timeframe Strategy

Signals require alignment across three timeframes:

1. **D1 (trend filter):** Price must be above/below EMA-200
2. **H4 (intermediate filter):** Price must be above/below EMA-50, aligned with D1 trend; level must have ≤ 3 touches
3. **H1 (entry signal):** Candlestick pattern near H4 or H1 S/R level, with volume surge ≥ `VOLUME_MULT`×

All three must agree on direction (LONG or SHORT) for a signal to be generated.

### Signal Scoring (0–100, base 55)

| Condition | Points |
|---|---|
| Volume ≥ 2.0× MA | +10 |
| Volume 1.5–2.0× MA | +5 |
| Level touches ≤ 2 | +10 |
| Level touches 2–3 | +3 |
| H4 candle pattern confirms | +10 |
| Funding rate neutral (< ±0.02%) | +10 |
| Funding rate acceptable | +5 |

Signals below `MIN_SCORE` (default 65) are discarded. Signals are sorted by score; highest-scoring setups are entered first.

### Candlestick Patterns

Detected on H1 (required) and H4 (bonus score):

- **LONG patterns:** Hammer (`Молот`), Bullish Pin Bar (`Пин-бар (бычий)`), Bullish Engulfing (`Бычье поглощение`)
- **SHORT patterns:** Shooting Star (`Падающая звезда`), Bearish Pin Bar (`Пин-бар (медвежий)`), Bearish Engulfing (`Медвежье поглощение`)

### Position Lifecycle

```
Entry (market order)
  → SL order placed at sl level (STOP_MARKET)
  → TP order placed at tp3 level (TAKE_PROFIT_MARKET)
  → monitor_positions() runs every 30s:
      • TP1 hit → move SL to breakeven
      • TP2 hit (after BE) → partial close 60% of qty
      • TP3 hit or SL hit → full close, update daily stats
```

### Risk Management

- Position size: `qty = (balance × RISK_PER_TRADE%) / (sl_pct × entry_price)`
- Trading halted when any of these are true:
  - `state.paused == True` (manual pause)
  - `paused_until` timer active (auto-pause after 2 consecutive losses, 30 min)
  - `len(positions) >= MAX_POSITIONS`
  - `day.trades >= MAX_DAILY_TRADES`
  - Daily loss ≥ `MAX_DAILY_LOSS`% of balance
  - `loss_streak >= 2`

---

## Configuration (Environment Variables)

All parameters live in `core/config.py` as a `Config` dataclass. Defaults shown.

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_TOKEN` | `""` | Bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | `""` | Authorized user chat ID (auth gate) |
| `BINGX_API_KEY` | `""` | BingX API key |
| `BINGX_SECRET` | `""` | BingX API secret |
| `BOT_MODE` | `"auto"` | `"auto"` or `"manual"` |
| `RISK_PER_TRADE` | `1.0` | % of balance risked per trade |
| `MAX_DAILY_LOSS` | `2.0` | Max daily loss % before halt |
| `MAX_POSITIONS` | `5` | Max concurrent open positions |
| `MAX_DAILY_TRADES` | `10` | Max trades per day |
| `LEVERAGE` | `5` | Futures leverage multiplier |
| `MIN_RR` | `2.0` | Minimum R/R ratio (uses TP2_RR = 2.0) |
| `SL_BUFFER_PCT` | `0.15` | SL buffer beyond candle low/high |
| `VOLUME_MULT` | `1.5` | Minimum volume surge vs MA |
| `VOLUME_MA_PERIOD` | `20` | Volume MA lookback (candles) |
| `MIN_SCORE` | `65` | Minimum signal score to trade |
| `TREND_EMA_D1` | `200` | EMA period for D1 trend |
| `TREND_EMA_H4` | `50` | EMA period for H4 filter |
| `TREND_EMA_H1` | `21` | EMA period (available, not in analyze) |
| `FUNDING_MAX_LONG` | `0.05` | Max funding rate for LONG entries |
| `FUNDING_MAX_SHORT` | `-0.05` | Min funding rate for SHORT entries |
| `WHITELIST` | `""` | Comma-separated pairs to trade (overrides top-N) |
| `BLACKLIST` | `"LUNA-USDT,FTT-USDT"` | Comma-separated pairs to exclude |
| `TOP_N_PAIRS` | `0` | Number of top-volume pairs to scan (0 = all) |
| `SCAN_H1_INTERVAL_MIN` | `5` | Unused (scheduler is hardcoded in main.py) |
| `SCAN_BATCH_SIZE` | `10` | Pairs per scan batch |
| `SCAN_BATCH_DELAY` | `1.0` | Seconds between batches (rate limit) |

**Runtime-mutable config** (via Telegram commands): `MODE`, `RISK_PER_TRADE`, `LEVERAGE`

---

## Telegram Commands

All commands require the message to come from `TELEGRAM_CHAT_ID`.

| Command | Description |
|---|---|
| `/status` | Show open positions with entry, SL, TP3 |
| `/balance` | Show balance, daily stats, win rate, P&L |
| `/pairs` | List current trading pairs (first 15) |
| `/scan` | Manually trigger scan_all() |
| `/pause` | Pause trading (sets `state.paused = True`) |
| `/resume` | Resume trading (clears pause and timer) |
| `/setmode auto\|manual` | Switch trading mode |
| `/setrisk <0.1–3.0>` | Set risk % per trade |
| `/setlev <1–50>` | Set leverage |
| `/closeall` | Force-close all open positions at market |
| `/help` or `/start` | Show command list |
| `/confirm_SYMBOL` | (manual mode) Confirm pending signal entry |
| `/skip_SYMBOL` | (manual mode) Discard pending signal |

Note: `/confirm_SYMBOL` uses `SYMBOL` with `-` replaced by `_` (e.g. `/confirm_BTC_USDT`).

---

## Scheduler Jobs

Defined in `main.py:on_startup()`, all run in UTC:

| Job | Schedule | Description |
|---|---|---|
| `scanner.scan_all` | Every 15 minutes (`cron minute="*/15"`) | Full pair scan for signals |
| `scanner.update_pairs` | Every hour at minute 0 | Refresh pair list |
| `scanner.monitor_positions` | Every 30 seconds | Check SL/TP/BE for open positions |
| `scanner.daily_report` | Daily at 09:00 UTC | Send day summary to Telegram |

---

## State Management

**No persistence** — all state is in-memory and lost on restart.

Key state objects (singletons):
- `state` (`BotState`) — `core/state.py`
- `cfg` (`Config`) — `core/config.py`

### BotState fields

| Field | Type | Description |
|---|---|---|
| `positions` | `Dict[str, Position]` | Open positions keyed by symbol |
| `pending` | `Dict[str, dict]` | Pending signals with `signal` + `expires` |
| `day` | `DayStats` | Today's stats (auto-reset on new day) |
| `pairs` | `List[str]` | Current trading pairs |
| `paused` | `bool` | Manual pause flag |
| `total_pnl` | `float` | Cumulative realized P&L (USDT) |
| `current_balance` | `float` | Last known balance |

---

## Development Conventions

### Language & Localization
- All Telegram-facing messages are in **Russian** (Cyrillic), using emoji prefixes
- Variable names, function names, and comments in code are in **English**
- Telegram messages use **HTML parse mode** (`parse_mode="HTML"`)

### Async Pattern
- Everything is `async/await` using Python asyncio
- Exchange calls use `aiohttp.ClientSession` — always call `await exchange.close()` after ad-hoc usage
- Concurrent scanning uses `asyncio.gather(*tasks)`

### Error Handling
- All scheduled jobs and async operations wrap logic in `try/except`
- Errors are logged via `logging.getLogger(module_name)` and optionally forwarded to Telegram via `_notify()`
- Handlers silently ignore unauthorized messages (return early if `not _auth(msg)`)

### Naming Conventions
- Classes: `CamelCase` (e.g. `BingXClient`, `Scanner`, `BotState`)
- Functions/variables: `snake_case`
- Private methods: `_underscore_prefix` (e.g. `_enter`, `_notify`, `_move_be`)
- Config keys: `UPPER_SNAKE_CASE`

### Imports
- Ad-hoc imports inside handler functions (e.g. `from exchange.bingx import BingXClient` inside `cmd_balance`) are intentional to avoid circular imports and keep module-level imports clean

### Math
- NumPy arrays used for all price/volume calculations in `strategy/strategy/gerchik.py`
- `parse_klines()` converts raw BingX API arrays to `{"ts","open","high","low","close","volume"}` dicts of numpy arrays

---

## Deployment

### Local Run
```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=...
export TELEGRAM_CHAT_ID=...
export BINGX_API_KEY=...
export BINGX_SECRET=...
python main.py
```

### Nixpacks / Railway / Render
`nixpacks.toml` defines the build and start commands:
```toml
[phases.install]
cmds = [
  "pip install --upgrade pip",
  "pip install pydantic-core==2.18.4 --no-build-isolation",
  "pip install -r requirements.txt"
]

[start]
cmd = "python main.py"
```

### Heroku-compatible
`Procfile` defines a single `worker` dyno:
```
worker: python main.py
```

---

## Known Limitations & Gotchas

1. **No persistence** — positions, stats, and pairs list are lost on restart. If the bot restarts with open positions on the exchange, it won't track them.
2. **No tests** — there is no test suite or test infrastructure.
3. **Single user** — the bot is designed for one authorized Telegram chat ID; `TELEGRAM_CHAT_ID` is a single string.
4. **Manual mode confirmation window** — pending signals expire after `CONFIRM_TIMEOUT_SEC` (default 300s = 5 minutes).
5. **Loss-streak pause** — after 2 consecutive losses, trading is paused for `PAUSE_AFTER_LOSS_MIN` minutes (default 30). Both losses AND the pause count reset at the next `reset_day()` call (when the calendar date changes).
6. **`SCAN_H1_INTERVAL_MIN` is unused** — the scan interval is hardcoded in `main.py` (`minute="*/15"`).
7. **Config mutation** — `/setmode`, `/setrisk`, `/setlev` modify the `cfg` singleton directly; changes are not persisted across restarts.
8. **Module path inconsistency** — the strategy module is at `strategy/strategy/gerchik.py` but imported in `scanner.py` as `from strategy.gerchik import ...` — verify `PYTHONPATH` or `sys.path` if adding new modules.
9. **`pydantic-core` pin** — `nixpacks.toml` pins `pydantic-core==2.18.4` as a workaround for build issues; this may need updating.
