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
│   ├── state.py                 # In-memory state: Position, DayStats, BotState
│   └── db.py                    # SQLite persistence: trade history, KV store
├── exchange/
│   └── bingx.py                 # BingX async REST API client (HMAC-SHA256)
├── strategy/
│   ├── scanner.py               # Orchestrates scanning, entry, monitoring
│   └── strategy/
│       └── gerchik.py           # Technical analysis: patterns, S/R, signals
├── telegram/
│   └── handlers.py              # Telegram command handlers (aiogram)
├── utils/
│   └── chart.py                 # matplotlib candlestick chart generator
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
              ├── auto mode  → _enter(signal) + notify with chart
              └── manual mode → pending queue → inline KB (✅/❌) or /confirm_SYMBOL
                                 └── callback/confirm → _enter(signal)
```

### Module Responsibilities

| Module | Responsibility |
|---|---|
| `main.py` | Bot bootstrap, scheduler wiring, startup notification |
| `core/config.py` | Single `cfg` singleton; env-configurable trading parameters |
| `core/state.py` | Single `state` singleton; all in-memory runtime state |
| `core/db.py` | SQLite at `data/gerchik.db`; trade history, total PnL persistence |
| `exchange/bingx.py` | BingX Perpetual Futures REST API (async/aiohttp, 3× retry) |
| `strategy/strategy/gerchik.py` | Pure functions: EMA, RSI, ATR, S/R levels, candle patterns, signals |
| `strategy/scanner.py` | Stateful job runner: scan lock, batched scanning, position lifecycle |
| `telegram/handlers.py` | Telegram command dispatch; reply keyboard; mutates `cfg` and `state` |
| `utils/chart.py` | Dark-theme candlestick chart with Entry/SL/TP lines (matplotlib) |

---

## Key Concepts

### Multi-Timeframe Strategy

Signals require alignment across three timeframes:

1. **D1 (trend filter):** Price must be above/below EMA-200
2. **H4 (intermediate filter):** Price must be aligned with EMA-50 or within 2.0% of it
3. **H1 (entry signal):** Candlestick pattern near S/R level (H4 + H1 combined, 1.5% tolerance), with volume surge ≥ `VOLUME_MULT`×

Additional filters:
- RSI < 75 for LONG, RSI > 25 for SHORT
- Funding rate within `FUNDING_MAX_LONG` / `FUNDING_MAX_SHORT`
- SL width ≤ 5% from entry (ATR-based SL)
- S/R level touches ≤ 6

### Signal Scoring (0–100, base 50)

| Condition | Points |
|---|---|
| Volume ≥ 2.5× MA | +12 |
| Volume ≥ 2.0× MA | +10 |
| Volume ≥ 1.5× MA | +6 |
| Volume ≥ 1.3× MA | +3 |
| Level touches ≤ 2 | +12 |
| Level touches ≤ 3 | +7 |
| Level touches ≤ 4 | +3 |
| H4 candle pattern confirms | +10 |
| H4 aligned with trend | +5 |
| RSI in ideal zone (35–60 LONG, 40–65 SHORT) | +8 |
| Funding rate < ±0.01% | +8 |
| Funding rate < ±0.03% | +4 |

Signals below `MIN_SCORE` (default 60) are discarded. Signals are sorted by score; highest-scoring setups are entered first.

### Candlestick Patterns

Detected on H1 (required) and H4 (bonus score):

- **LONG patterns:** Hammer (`Молот`), Bullish Pin Bar (`Пин-бар (бычий)`), Bullish Engulfing (`Бычье поглощение`)
- **SHORT patterns:** Shooting Star (`Падающая звезда`), Bearish Pin Bar (`Пин-бар (медвежий)`), Bearish Engulfing (`Медвежье поглощение`)
- **Doji** — treated as trend-aligned if both EMA filters agree

### Position Lifecycle

```
Entry (market order)
  → SL order placed at sl level (STOP_MARKET)
  → TP order placed at tp3 level (TAKE_PROFIT_MARKET)
  → monitor_positions() runs every 30s:
      • Price moves BE_TRIGGER_PCT% in profit → move SL to breakeven (entry + BE_BUFFER_PCT%)
      • TP2 hit (after BE) → partial close 60% of qty, re-place SL for remainder
      • After BE: trailing stop moves SL behind peak price by TRAIL_PCT%
      • TP3 hit or SL hit → full close, update daily stats, save to DB
```

### Risk Management

- Position size: `qty = (balance × RISK_PER_TRADE%) / (sl_pct × entry_price)`
- Minimum position: `MAX(qty, MIN_POSITION_USDT / entry_price)` (default 20 USDT notional)
- Auto-leverage based on balance: < 100 USDT → x10, < 500 USDT → x7, < 2000 USDT → x5, ≥ 2000 USDT → x3
- Trading halted when any of these are true:
  - `state.paused == True` (manual pause)
  - `paused_until` timer active (auto-pause after consecutive loss, 30 min)
  - `len(positions) >= MAX_POSITIONS`
  - `day.trades >= MAX_DAILY_TRADES`
  - Daily loss ≥ `MAX_DAILY_LOSS`% of balance

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
| `LEVERAGE` | `5` | Futures leverage multiplier (overridden by AUTO_LEVERAGE) |
| `MIN_RR` | `2.0` | Minimum R/R ratio (uses TP2_RR = 2.0) |
| `SL_BUFFER_PCT` | `0.15` | SL buffer beyond candle low/high |
| `VOLUME_MULT` | `1.3` | Minimum volume surge vs MA |
| `VOLUME_MA_PERIOD` | `20` | Volume MA lookback (candles) |
| `MIN_SCORE` | `60` | Minimum signal score to trade |
| `TREND_EMA_D1` | `200` | EMA period for D1 trend |
| `TREND_EMA_H4` | `50` | EMA period for H4 filter |
| `TREND_EMA_H1` | `21` | EMA period (unused in analyze, available for future use) |
| `FUNDING_MAX_LONG` | `0.05` | Max funding rate for LONG entries |
| `FUNDING_MAX_SHORT` | `-0.05` | Min funding rate for SHORT entries |
| `WHITELIST` | `""` | Comma-separated pairs to trade (overrides top-N) |
| `BLACKLIST` | `"LUNA-USDT,FTT-USDT"` | Comma-separated pairs to exclude |
| `TOP_N_PAIRS` | `0` | Number of top-volume pairs to scan (0 = all) |
| `SCAN_BATCH_SIZE` | `10` | Pairs per scan batch |
| `SCAN_BATCH_DELAY` | `1.0` | Seconds between batches (rate limit) |
| `BE_TRIGGER_PCT` | `0.5` | % move from entry to trigger breakeven (0 = use TP1) |
| `BE_BUFFER_PCT` | `0.05` | SL buffer above entry at breakeven (locks tiny profit) |
| `TRAIL_PCT` | `1.0` | Trailing stop: SL trails this % behind peak price |
| `MIN_POSITION_USDT` | `20.0` | Minimum position notional value in USDT |
| `AUTO_LEVERAGE` | `true` | Auto-adjust leverage based on balance tiers |

**Runtime-mutable config** (via Telegram commands): `MODE`, `RISK_PER_TRADE`, `LEVERAGE`, `BE_TRIGGER_PCT`, `TRAIL_PCT`, `MIN_POSITION_USDT`

---

## Telegram Commands

All commands require the message to come from `TELEGRAM_CHAT_ID`.

| Command | Description |
|---|---|
| `/status` | Show live open positions (from BingX + in-memory) |
| `/balance` | Show balance, daily stats, win rate, P&L |
| `/pairs` | List current trading pairs (first 15) |
| `/scan` | Manually trigger scan_all() in background |
| `/pause` | Pause trading (sets `state.paused = True`) |
| `/resume` | Resume trading (clears pause and timer) |
| `/setmode auto\|manual` | Switch trading mode |
| `/setrisk <0.1–3.0>` | Set risk % per trade |
| `/setlev <1–50>` | Set leverage |
| `/setbe <pct>` | Set breakeven trigger % (e.g. `/setbe 0.5`) |
| `/settrail <pct>` | Set trailing stop % (e.g. `/settrail 1.0`) |
| `/setminpos <usdt>` | Set minimum position size in USDT |
| `/setpairs SYM1,SYM2` | Set custom pair whitelist (or `reset` for auto) |
| `/settings` | Show all current config values |
| `/history` | Last 15 closed trades with all-time stats from DB |
| `/top` | Top 20 pairs by volume |
| `/debug` | Show raw BingX balance API response (diagnostics) |
| `/closeall` | Force-close all open positions at market |
| `/help` or `/start` | Show command list with persistent keyboard |
| `/confirm_SYMBOL` | (manual mode) Confirm pending signal entry |
| `/skip_SYMBOL` | (manual mode) Discard pending signal |

Note: `/confirm_SYMBOL` uses `SYMBOL` with `-` replaced by `_` (e.g. `/confirm_BTC_USDT`).
Inline keyboard buttons (✅ Войти / ❌ Пропустить) appear on signals in manual mode.

### Persistent Reply Keyboard

A 15-button reply keyboard is always visible with shortcuts for the most common commands:
📊 Статус, 💰 Баланс, ⚙️ Настройки, 📋 Пары, 🔍 Скан, 📈 Отчёт, 📜 История, 🏆 Топ пары, 🔄 Безубыток, 📉 Трейлинг, ⏸ Пауза, ▶️ Продолжить, 🤖 Авто, ✋ Ручной, ❌ Закрыть всё

---

## Scheduler Jobs

Defined in `main.py`, all run in UTC:

| Job | Schedule | Description |
|---|---|---|
| `scanner.scan_all` | Every 15 minutes (`cron minute="*/15"`) | Full pair scan for signals |
| `scanner.update_pairs` | Every hour at minute 0 | Refresh pair list |
| `scanner.monitor_positions` | Every 30 seconds | Check SL/TP/BE/trail for open positions |
| `scanner.daily_report` | Daily at 09:00 UTC | Send day summary to Telegram |
| `scanner.weekly_report` | Monday at 09:05 UTC | 7-day stats from SQLite |
| `scanner.monthly_report` | 1st of month at 09:10 UTC | 30-day stats from SQLite |

---

## State Management

### In-Memory (lost on restart)

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
| `total_pnl` | `float` | Cumulative realized P&L (USDT), restored from DB on startup |
| `current_balance` | `float` | Last known balance |

### Position fields (added vs original)

| Field | Type | Description |
|---|---|---|
| `be_moved` | `bool` | Whether breakeven SL has been placed |
| `tp2_hit` | `bool` | Whether TP2 partial close has been executed |
| `trail_price` | `float` | Peak price seen since BE (trailing stop reference) |

### SQLite Persistence

`core/db.py` writes to `data/gerchik.db` (auto-created):
- `trades` table: all closed positions (symbol, side, entry, exit, SL, TP1/2/3, qty, pnl, result, pattern, tf, rr, score)
- `kv` table: key-value store (currently stores `total_pnl`)
- `total_pnl` is restored from DB on every startup via `db.load_total_pnl()`

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
- Background scan on startup uses `asyncio.create_task(startup_tasks())`

### Error Handling
- All scheduled jobs and async operations wrap logic in `try/except`
- Errors are logged via `logging.getLogger(module_name)` and optionally forwarded to Telegram via `_notify()`
- Handlers silently ignore unauthorized messages (return early if `not _auth(msg)`)
- `utils/chart.py` wraps matplotlib import in try/except — chart generation is optional (bot works without it)

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

1. **Open positions lost on restart** — positions in `state.positions` are in-memory. If the bot restarts with open positions on the exchange, it won't track them (trade history in SQLite is only written on close).
2. **No tests** — there is no test suite or test infrastructure.
3. **Single user** — the bot is designed for one authorized Telegram chat ID; `TELEGRAM_CHAT_ID` is a single string.
4. **Manual mode confirmation window** — pending signals expire after `CONFIRM_TIMEOUT_SEC` (default 300s = 5 minutes).
5. **Loss-streak pause** — after a consecutive loss, trading is paused for `PAUSE_AFTER_LOSS_MIN` minutes (default 30). Resets at `reset_day()` (calendar date change).
6. **Config mutation** — `/setmode`, `/setrisk`, `/setlev`, `/setbe`, `/settrail`, `/setminpos` modify the `cfg` singleton directly; changes are not persisted across restarts.
7. **Module path** — the strategy module is at `strategy/strategy/gerchik.py`, imported as `from strategy.strategy.gerchik import ...` in `scanner.py`. The extra `strategy/` directory level is intentional to match the import path.
8. **Concurrent scan prevention** — `Scanner._scanning` bool flag prevents overlapping scans. If a manual `/scan` fires while the scheduler scan is running, the manual one is silently dropped.
9. **"No signals" spam reduction** — `Scanner._scan_count` tracks scan runs; "no signals" notification is only sent every 4th scan (~1 hour) to avoid flooding Telegram.
