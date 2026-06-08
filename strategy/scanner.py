import asyncio
import html as _html
import logging
import math
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

from core.config import cfg
from core.state import Position, state
from core import db
from exchange.bingx import BingXClient
from strategy.strategy.gerchik import (
    Signal, analyze, analyze_false_breakout, analyze_breakout,
    analyze_range_breakout, parse_klines, reset_stats, get_stats,
    nearest_weekly_levels, near_level, level_last_touch_age, find_levels,
)

log = logging.getLogger("scanner")

_SCANNING   = False   # prevents overlapping scan cycles
_MONITORING = False   # prevents overlapping monitor cycles
_global_scanner = None  # shared instance for /scan command in handlers


def _px(p: float) -> float:
    """Round price to exchange-compatible precision."""
    if p >= 10:   return round(p, 2)
    if p >= 1:    return round(p, 4)
    if p >= 0.01: return round(p, 5)
    return round(p, 6)


SL_COOLDOWN_MIN = cfg.SL_COOLDOWN_MIN  # loaded from config / env


class Scanner:
    def __init__(self, exchange: BingXClient, bot: Bot):
        global _global_scanner
        self.ex          = exchange
        self.bot         = bot
        self._scan_count = 0
        self._sl_cooldown: dict = {}        # symbol → datetime of last SL hit
        self._symbol_loss_streak: dict = {} # symbol → consecutive SL hits count
        self._stale_alerted: set = set()
        self._last_signal_time: datetime | None = None
        self._monitor_count: int = 0
        self._funding_warned: set = set()
        _global_scanner = self
        self._restore_cooldowns()

    def _loss_cooldown(self, symbol: str) -> None:
        """Record a loss for this symbol, increment streak, and set cooldown."""
        self._symbol_loss_streak[symbol] = self._symbol_loss_streak.get(symbol, 0) + 1
        streak = self._symbol_loss_streak[symbol]
        if streak >= cfg.SYMBOL_LOSS_STREAK_LIMIT:
            log.warning(
                f"{symbol}: {streak} убытков подряд — расширенный кулдаун {cfg.SYMBOL_LOSS_COOLDOWN_MIN} мин"
            )
        try:
            db.save_kv(f"sl_streak:{symbol}", str(streak))
        except Exception as e:
            log.warning(f"streak save {symbol}: {e}")
        self._normal_cooldown(symbol)

    def _normal_cooldown(self, symbol: str) -> None:
        """Set standard cooldown (no streak increment) in memory and DB."""
        now = datetime.utcnow()
        self._sl_cooldown[symbol] = now
        try:
            db.save_kv(f"sl_cd:{symbol}", now.isoformat())
        except Exception as e:
            log.warning(f"cooldown save {symbol}: {e}")

    @staticmethod
    def _calc_pnl(pos, price: float) -> float:
        """Realised PnL in USDT for a position closed at price."""
        return (price - pos.entry) * pos.qty if pos.side == "LONG" \
               else (pos.entry - price) * pos.qty

    async def _record_close(self, pos, price: float) -> float:
        """Single entry point for all position-close accounting.

        Updates state, DB, and cooldowns. Returns realised PnL.
        Each call site is responsible only for its own Telegram notification
        and any path-specific logic (e.g. balance refresh, extended-streak alert).
        """
        symbol = pos.symbol
        pnl = self._calc_pnl(pos, price)
        state.total_pnl    += pnl
        state.day.pnl_usdt += pnl
        result = "WIN" if pnl > 0 else "LOSS"
        if pnl > 0:
            state.day.wins += 1
            state.day.loss_streak = 0
            state.day.paused_until = None
            db.save_kv("paused_until", "")
        else:
            state.day.losses += 1
            state.day.loss_streak += 1
            if state.day.loss_streak >= 3:
                pause_min = cfg.PAUSE_3X_LOSS_MIN
                if state.day.loss_streak == 3:
                    await self._notify(
                        f"⛔ <b>3 убытка подряд</b> — пауза {pause_min} мин\n"
                        f"Серия: {state.day.loss_streak} | "
                        f"PnL сегодня: <code>{state.day.pnl_usdt:+.2f} USDT</code>"
                    )
            else:
                pause_min = cfg.PAUSE_AFTER_LOSS_MIN
            state.day.paused_until = datetime.utcnow() + timedelta(minutes=pause_min)
            db.save_kv("paused_until", state.day.paused_until.isoformat())
        try:
            db.save_trade(pos, price, pnl, result)
        except Exception as e:
            log.error(f"db.save_trade {symbol}: {e}")
        del state.positions[symbol]
        db.delete_open_position(symbol)
        self._stale_alerted.discard(symbol)
        self._funding_warned.discard(symbol)
        if pnl <= 0:
            self._loss_cooldown(symbol)
        else:
            self._symbol_loss_streak.pop(symbol, None)
        return pnl

    async def _drought_alert(self, diag: str = "") -> None:
        """Send a 24h no-signal drought alert at most once every ~2 hours."""
        if self._last_signal_time is None:
            return
        drought_h = (datetime.utcnow() - self._last_signal_time).total_seconds() / 3600
        if drought_h < 24 or self._scan_count % 8 != 1:
            return
        msg = (
            f"⏳ <b>Нет сигналов уже {drought_h:.0f}ч</b>\n"
            f"MIN_SCORE: {cfg.MIN_SCORE} | Пар: {len(state.pairs)}"
        )
        if diag:
            msg += f"\n📊 Топ причин отсева: {diag}\nРассмотри снижение MIN_SCORE или смену пар"
        await self._notify(msg)

    def _cooldown_minutes(self, symbol: str) -> int:
        """Return active cooldown duration in minutes for symbol (extended on loss streak)."""
        streak = self._symbol_loss_streak.get(symbol, 0)
        return cfg.SYMBOL_LOSS_COOLDOWN_MIN if streak >= cfg.SYMBOL_LOSS_STREAK_LIMIT else SL_COOLDOWN_MIN

    def _in_cooldown(self, symbol: str) -> bool:
        """Check in-memory cooldown. Uses extended duration on loss streak."""
        if symbol not in self._sl_cooldown:
            return False
        return (datetime.utcnow() - self._sl_cooldown[symbol]).total_seconds() < self._cooldown_minutes(symbol) * 60

    def _restore_cooldowns(self):
        """Load all cooldowns and loss streaks from DB at startup."""
        try:
            streaks = db.load_all_loss_streaks()
            for sym, streak in streaks.items():
                if streak > 0:
                    self._symbol_loss_streak[sym] = streak
            loaded = db.load_all_cooldowns()
            now = datetime.utcnow()
            active = 0
            for sym, cd_time in loaded.items():
                if (now - cd_time).total_seconds() < self._cooldown_minutes(sym) * 60:
                    self._sl_cooldown[sym] = cd_time
                    active += 1
            if active:
                log.info(f"Восстановлено {active} кулдаунов из БД (серий: {len(streaks)})")
        except Exception as e:
            log.warning(f"restore_cooldowns: {e}")

    # ------------------------------------------------------------------ notify

    async def _notify(self, text: str, markup=None):
        try:
            await self.bot.send_message(
                cfg.TELEGRAM_CHAT_ID, text,
                parse_mode="HTML", reply_markup=markup,
            )
        except Exception as e:
            log.error(f"TG notify: {e}")

    async def _notify_photo(self, photo: bytes, caption: str, markup=None):
        try:
            await self.bot.send_photo(
                cfg.TELEGRAM_CHAT_ID,
                photo=BufferedInputFile(photo, filename="chart.png"),
                caption=caption,
                parse_mode="HTML",
                reply_markup=markup,
            )
        except Exception as e:
            log.error(f"TG photo: {e}")

    # ------------------------------------------------------------------ pairs

    # Prefixes of synthetic/index instruments — not real crypto, skip them
    _SYNTHETIC_PREFIXES = ("NCC", "NCSI", "NCCO")

    async def _get_binance_symbols(self) -> set:
        """Fetch USDT perpetual futures symbols from Binance public API."""
        import aiohttp
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(url) as r:
                    data = await r.json()
            result = set()
            for sym in data.get("symbols", []):
                if sym.get("quoteAsset") == "USDT" and sym.get("status") == "TRADING":
                    # Binance: "BTCUSDT" → BingX: "BTC-USDT"
                    base = sym.get("baseAsset", "")
                    if base:
                        result.add(f"{base}-USDT")
            log.info(f"Binance Futures: {len(result)} USDT пар получено")
            return result
        except Exception as e:
            log.warning(f"Не удалось получить пары Binance: {e}")
            return set()

    async def update_pairs(self):
        try:
            if cfg.WHITELIST:
                state.pairs = list(cfg.WHITELIST)
            else:
                symbols = await self.ex.get_top_symbols(cfg.TOP_N_PAIRS)
                state.pairs = [
                    s for s in symbols
                    if s not in cfg.BLACKLIST
                    and not any(s.startswith(p) for p in self._SYNTHETIC_PREFIXES)
                ]
                # Optional: keep only pairs that also exist on Binance Futures
                if cfg.BINANCE_FILTER:
                    binance_syms = await self._get_binance_symbols()
                    if binance_syms:
                        before = len(state.pairs)
                        state.pairs = [s for s in state.pairs if s in binance_syms]
                        log.info(f"Binance фильтр: {before} → {len(state.pairs)} пар")
            log.info(f"Пар: {len(state.pairs)} | топ-5: {state.pairs[:5]}")
        except Exception as e:
            log.error(f"update_pairs: {e}")

    # ------------------------------------------------------------------ scan

    async def scan_all(self):
        global _SCANNING
        if _SCANNING:
            log.info("Скан уже запущен — пропуск")
            return
        _SCANNING = True
        try:
            await self._scan_all_inner()
        finally:
            _SCANNING = False

    async def _scan_all_inner(self):
        # Clean up expired pending signals — blocked symbols get unlocked
        now = datetime.utcnow()
        expired = [sym for sym, p in list(state.pending.items()) if now > p["expires"]]
        for sym in expired:
            del state.pending[sym]
            log.info(f"Pending сигнал {sym} истёк — символ разблокирован")

        if not state.pairs:
            await self.update_pairs()
        can, reason = state.can_trade(cfg.MAX_DAILY_LOSS, cfg.MAX_POSITIONS, cfg.MAX_DAILY_TRADES)
        if not can:
            log.info(f"Пропуск скана: {reason}")
            return
        # Time filter: skip low-liquidity hours
        hour = datetime.utcnow().hour
        if cfg.QUIET_HOURS_START <= hour < cfg.QUIET_HOURS_END:
            log.info(f"Тихая сессия {hour}:00 UTC ({cfg.QUIET_HOURS_START}-{cfg.QUIET_HOURS_END}) — скан пропущен")
            return
        log.info(f"Сканирую {len(state.pairs)} пар...")
        reset_stats()
        signals = []
        for i in range(0, len(state.pairs), cfg.SCAN_BATCH_SIZE):
            batch = state.pairs[i:i + cfg.SCAN_BATCH_SIZE]
            tasks = [
                self._analyze(s) for s in batch
                if s not in state.positions and s not in state.pending
                and not self._in_cooldown(s)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    log.error(f"Ошибка анализа пары: {r}")
                elif isinstance(r, Signal):
                    signals.append(r)
            if i + cfg.SCAN_BATCH_SIZE < len(state.pairs):
                await asyncio.sleep(cfg.SCAN_BATCH_DELAY)

        self._scan_count += 1
        scan_stats = get_stats()
        top_reasons = sorted(scan_stats.items(), key=lambda x: x[1], reverse=True)[:4]
        diag = " | ".join(f"{r}: {n}" for r, n in top_reasons) if top_reasons else "—"

        if not signals:
            log.info(f"Сигналов нет. Причины: {diag}")
            # Notify only every 4th scan (~1 hour) to avoid spam
            if self._scan_count % 4 == 1:
                await self._notify(
                    f"🔍 Скан: {len(state.pairs)} пар — сигналов нет\n"
                    f"📊 Фильтры: {diag}\n"
                    f"Следующий через 15 мин"
                )
            await self._drought_alert(diag)
            return

        signals.sort(key=lambda s: s.score, reverse=True)
        # Adaptive MIN_SCORE: raise bar on consecutive loss streak
        streak = state.day.loss_streak
        effective_min_score = cfg.MIN_SCORE + (10 if streak >= 3 else 0)
        if effective_min_score != cfg.MIN_SCORE:
            log.info(f"Адаптивный MIN_SCORE: streak={streak} → {effective_min_score}")
        qualified = [s for s in signals if s.score >= effective_min_score]
        skipped   = len(signals) - len(qualified)
        if skipped:
            log.info(f"Отфильтровано по MIN_SCORE ({effective_min_score}): {skipped} сигналов")

        # Filter out signals where SL is too far from entry (hard limit on SL width)
        if cfg.MAX_SL_PCT > 0:
            wide_sl = [s for s in qualified
                       if s.sl > 0 and abs(s.entry - s.sl) / s.entry * 100 > cfg.MAX_SL_PCT]
            if wide_sl:
                for ws in wide_sl:
                    sl_pct = abs(ws.entry - ws.sl) / ws.entry * 100
                    log.info(f"{ws.symbol}: SL {sl_pct:.1f}% > MAX_SL_PCT {cfg.MAX_SL_PCT}% — отброшен")
                qualified = [s for s in qualified if s not in wide_sl]
        if not qualified:
            log.info(f"Нет сигналов с достаточным score. Причины отсева: {diag}")
            if self._scan_count % 4 == 1:
                await self._notify(
                    f"🔍 Скан: {len(state.pairs)} пар — {len(signals)} сигналов ниже MIN_SCORE {effective_min_score}\n"
                    f"📊 Фильтры: {diag}\n"
                    f"Следующий через 15 мин"
                )
            await self._drought_alert(diag)
            return

        # Qualified signals found — update last signal time
        self._last_signal_time = datetime.utcnow()

        # Take only top N by score
        qualified = qualified[:cfg.MAX_POSITIONS]

        # BTC trend filter: fetch BTC BEFORE notification to show accurate signal count
        btc_bias   = "NEUTRAL"
        btc_change = 0.0
        if cfg.BTC_FILTER:
            try:
                btc_h1 = parse_klines(await self.ex.get_klines("BTC-USDT", cfg.SIGNAL_TF, limit=10))
                if btc_h1 and len(btc_h1["close"]) >= 4 and btc_h1["close"][-4] > 0:
                    btc_change = (btc_h1["close"][-1] - btc_h1["close"][-4]) / btc_h1["close"][-4] * 100
                    if btc_change < -cfg.BTC_FILTER_PCT:
                        btc_bias = "DOWN"
                    elif btc_change > cfg.BTC_FILTER_PCT:
                        btc_bias = "UP"
                    log.info(f"BTC 3h: {btc_change:+.2f}% → bias={btc_bias}")
            except Exception as e:
                log.warning(f"BTC filter: {e}")

        # Apply BTC filter before notifying user
        if btc_bias != "NEUTRAL":
            pre_count = len(qualified)
            qualified = [s for s in qualified if not (
                (btc_bias == "DOWN" and s.side == "LONG") or
                (btc_bias == "UP"   and s.side == "SHORT")
            )]
            if len(qualified) < pre_count:
                log.info(f"BTC filter (bias={btc_bias}): {pre_count - len(qualified)} сигналов убрано")
            if not qualified:
                log.info("BTC filter убрал все сигналы")
                if self._scan_count % 4 == 1:
                    bias_icon = "📉" if btc_bias == "DOWN" else "📈"
                    await self._notify(
                        f"🔍 Скан: {len(signals)} сигналов — все убраны BTC-фильтром {bias_icon}\n"
                        f"BTC: {btc_change:+.2f}% за 3ч"
                    )
                return

        top = "\n".join(f"• {s.symbol} {s.side} ⭐{s.score}" for s in qualified)
        await self._notify(f"🔍 Найдено <b>{len(qualified)}</b> сигналов:\n{top}")

        for sig in qualified:
            can, _ = state.can_trade(cfg.MAX_DAILY_LOSS, cfg.MAX_POSITIONS, cfg.MAX_DAILY_TRADES)
            if not can:
                break
            # Correlation filter: max 1 bot-opened position in same direction
            same_dir = sum(1 for p in state.positions.values() if p.side == sig.side)
            if same_dir >= 1:
                log.info(f"Корреляция: пропуск {sig.symbol} {sig.side} (уже {same_dir} в том же направлении)")
                continue
            await self._handle(sig)

    async def _analyze(self, symbol: str):
        try:
            raw_d1, raw_h4, raw_h1, raw_w1, funding = await asyncio.gather(
                self.ex.get_klines(symbol, cfg.TREND_TF,  limit=250),
                self.ex.get_klines(symbol, cfg.H4_TF,     limit=150),
                self.ex.get_klines(symbol, cfg.SIGNAL_TF, limit=100),
                self.ex.get_klines(symbol, "1w",          limit=60),
                self.ex.get_funding_rate(symbol),
            )
            d1 = parse_klines(raw_d1)
            h4 = parse_klines(raw_h4)
            h1 = parse_klines(raw_h1)
            w1 = parse_klines(raw_w1)

            if funding > 0.1 or funding < -0.1:
                log.warning(f"⚠️ Экстремальный фандинг {symbol}: {funding:.4f}%")

            # Pre-compute D1 levels once — reused by all 4 analyze functions
            d1_levels = find_levels(
                d1["high"], d1["low"], lookback=min(120, len(d1["high"]))
            ) if d1 else None

            # Priority order (Gerchik methodology):
            # 1. Pullback to S/R (откат к уровню — базовый вход)
            sig = analyze(symbol, d1, h4, h1, funding, cfg, d1_levels=d1_levels)
            # 2. False breakout (ложный пробой — сетап №1 по Герчику)
            if sig is None:
                sig = analyze_false_breakout(symbol, d1, h4, h1, funding, cfg, d1_levels=d1_levels)
            # 3. Accumulation range breakout (накопление — пробой диапазона)
            if sig is None:
                sig = analyze_range_breakout(symbol, d1, h4, h1, funding, cfg, d1_levels=d1_levels)
            # 4. Momentum breakout through a single level
            if sig is None:
                sig = analyze_breakout(symbol, d1, h4, h1, funding, cfg, d1_levels=d1_levels)

            # Orderbook filter (optional, controlled by ORDERBOOK_ENABLED)
            if sig is not None and cfg.ORDERBOOK_ENABLED:
                try:
                    from strategy.orderbook_analyzer import (
                        validate_signal_with_orderbook, OrderbookConfig,
                    )
                    ob_cfg = OrderbookConfig(
                        imbalance_threshold=cfg.OB_IMBALANCE_THRESHOLD,
                        thin_book_threshold_usdt=cfg.OB_THIN_THRESHOLD_USDT,
                        max_spread_bps=cfg.OB_MAX_SPREAD_BPS,
                    )
                    # Auto-leverage for orderbook validation (same tier logic)
                    ob_lev = cfg.LEVERAGE
                    if cfg.AUTO_LEVERAGE:
                        try:
                            bal = state.current_balance or await self.ex.get_balance()
                            ob_lev = 5 if bal < 1000 else 3
                        except Exception:
                            pass
                    ob_val = await validate_signal_with_orderbook(
                        sig, self.ex, ob_lev, ob_cfg,
                        log_only=cfg.ORDERBOOK_LOG_ONLY,
                    )
                    if not ob_val.passed:
                        for r in ob_val.rejections:
                            from strategy.strategy.gerchik import _reject as _rej
                            _rej(f"OB:{r}")
                        sig = None
                    elif ob_val.suggested_leverage is not None:
                        sig._ob_suggested_leverage = ob_val.suggested_leverage
                except Exception as obe:
                    log.warning(f"orderbook filter {symbol}: {obe}")

            # Weekly level bonus: +8 score if signal entry is near a key W1 level
            if sig is not None and w1 and len(w1.get("close", [])) >= 10:
                try:
                    w1_lvls = nearest_weekly_levels(sig.entry, w1, count=5)
                    all_w1  = w1_lvls["support"] + w1_lvls["resistance"]
                    is_near_w1, w1_lvl = near_level(sig.entry, all_w1, tol=1.5)
                    if is_near_w1:
                        sig.score = min(100, sig.score + 8)
                        sig.reason += f"\n📅 <b>Недельный уровень</b> <code>{w1_lvl:.4f}</code> +8"
                        log.info(f"{symbol}: +8 W1 уровень {w1_lvl:.4f}")
                except Exception as we:
                    log.debug(f"w1 bonus {symbol}: {we}")

            # SMC/ICT filter — shadow mode by default (logs, doesn't block)
            if sig is not None:
                try:
                    from strategy.smc_filters import (
                        evaluate_smc, klines_to_candles, Direction as SMCDir,
                    )
                    smc = evaluate_smc(
                        direction=SMCDir.LONG if sig.side == "LONG" else SMCDir.SHORT,
                        current_price=sig.entry,
                        h1_candles=klines_to_candles(h1),
                        h4_candles=klines_to_candles(h4),
                        d1_candles=klines_to_candles(d1),
                    )
                    if not smc.allowed:
                        log.info(f"SMC block {symbol}: {'; '.join(smc.reasons)}")
                        sig = None
                    else:
                        if smc.score_bonus:
                            sig.score = min(100, sig.score + smc.score_bonus)
                        if smc.hard_blocked:
                            # Shadow mode: signal passes but log the would-be block
                            sig.reason += (
                                f"\n🔮 SMC shadow: {', '.join(smc.reasons)} "
                                f"(P/D несовпадение — включи SHADOW_MODE=False)"
                            )
                        elif smc.score_bonus:
                            sig.reason += f"\n🔮 SMC: {', '.join(smc.reasons)} {smc.score_bonus:+d}"
                except Exception as sme:
                    log.debug(f"smc_filter {symbol}: {sme}")

            return sig
        except Exception as e:
            log.error(f"analyze {symbol}: {e}")
            return None

    # ------------------------------------------------------------------ handle

    async def _handle(self, sig: Signal):
        # In auto mode: validate price BEFORE notifying to avoid "signal → skipped" spam
        price_checked = False
        if cfg.MODE == "auto":
            try:
                ticker = await self.ex.get_ticker(sig.symbol)
                cur_price = float(ticker.get("lastPrice", sig.entry))
                if cur_price > 0:
                    orig_entry = sig.entry
                    _drift_mul = cfg.PRICE_DRIFT_PCT / 100
                    against = (sig.side == "LONG"  and cur_price < sig.entry * (1 - _drift_mul)) or \
                              (sig.side == "SHORT" and cur_price > sig.entry * (1 + _drift_mul))
                    if against:
                        drift = abs(cur_price - orig_entry) / orig_entry * 100
                        log.info(f"{sig.symbol}: цена ушла против сигнала на {drift:.2f}% — тихий пропуск")
                        if drift > 3.0:
                            self._normal_cooldown(sig.symbol)
                        return  # Silent — no notification to avoid confusion
                    # Validate SL width with updated entry (price may have run far in our favor)
                    sld = abs(cur_price - sig.sl)
                    sl_limit = cfg.MAX_SL_PCT / 100 if cfg.MAX_SL_PCT > 0 else 0.08
                    if sig.sl > 0 and sld / cur_price > sl_limit:
                        drift = abs(cur_price - orig_entry) / orig_entry * 100 if orig_entry > 0 else 0
                        log.info(
                            f"{sig.symbol}: SL стал {sld/cur_price*100:.1f}% от новой цены "
                            f"(дрейф +{drift:.1f}%) — сигнал устарел, пропуск"
                        )
                        if drift > 3.0:
                            self._normal_cooldown(sig.symbol)
                        return  # Silent — price ran too far before we could enter
                    sig.entry = cur_price  # Update to current price before notify
                    # Recalculate TP from new entry (SL is structural, stays fixed)
                    if sld > 0:
                        if sig.side == "LONG":
                            sig.tp1 = _px(sig.entry + sld * cfg.TP1_RR)
                            sig.tp2 = _px(sig.entry + sld * cfg.TP2_RR)
                            sig.tp3 = _px(sig.entry + sld * cfg.TP3_RR)
                        else:
                            sig.tp1 = _px(sig.entry - sld * cfg.TP1_RR)
                            sig.tp2 = _px(sig.entry - sld * cfg.TP2_RR)
                            sig.tp3 = _px(sig.entry - sld * cfg.TP3_RR)
                    price_checked = True
            except Exception as e:
                log.warning(f"_handle pre-check {sig.symbol}: {e}")

        # Build chart
        chart_bytes = None
        try:
            h1_raw = await self.ex.get_klines(sig.symbol, cfg.SIGNAL_TF, limit=60)
            h1     = parse_klines(h1_raw)
            from utils.chart import generate_chart
            chart_bytes = generate_chart(h1, sig.symbol, sig)
        except Exception as e:
            log.error(f"chart build {sig.symbol}: {e}")

        if cfg.MODE == "auto":
            caption = (
                f"🤖 <b>СИГНАЛ</b> | {sig.symbol} {sig.side}\n"
                f"🕯 {sig.pattern} | ⭐ {sig.score}/100\n"
                f"🟡 Entry: <code>{sig.entry:.4f}</code>\n"
                f"🔴 SL: <code>{sig.sl:.4f}</code>\n"
                f"🟢 TP1: <code>{sig.tp1:.4f}</code> "
                f"TP2: <code>{sig.tp2:.4f}</code> "
                f"TP3: <code>{sig.tp3:.4f}</code>"
            )
            if chart_bytes:
                await self._notify_photo(chart_bytes, caption)
            else:
                await self._notify(caption)
            await self._enter(sig, price_checked=price_checked)
        else:
            expires = datetime.utcnow() + timedelta(seconds=cfg.CONFIRM_TIMEOUT_SEC)
            state.pending[sig.symbol] = {"signal": sig, "expires": expires}
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="✅ Войти",     callback_data=f"confirm:{sig.symbol}"),
                InlineKeyboardButton(text="❌ Пропустить", callback_data=f"skip:{sig.symbol}"),
            ]])
            caption = (
                f"🔔 <b>СЕТАП</b> | {sig.symbol} {sig.side}\n"
                f"🕯 {sig.pattern} | ⭐ {sig.score}/100\n"
                f"🟡 Entry: <code>{sig.entry:.4f}</code>\n"
                f"🔴 SL: <code>{sig.sl:.4f}</code>\n"
                f"🟢 TP1: <code>{sig.tp1:.4f}</code> "
                f"TP2: <code>{sig.tp2:.4f}</code> "
                f"TP3: <code>{sig.tp3:.4f}</code>\n\n"
                f"⏱ Истекает через {cfg.CONFIRM_TIMEOUT_SEC//60} мин"
            )
            if chart_bytes:
                await self._notify_photo(chart_bytes, caption, markup=kb)
            else:
                await self._notify(caption, markup=kb)

    # ------------------------------------------------------------------ enter

    async def _enter(self, sig: Signal, confirmed: bool = False, price_checked: bool = False):
        try:
            balance = await self.ex.get_balance()
            if balance <= 0:
                await self._notify("⚠️ Нет баланса для входа")
                return
            state.current_balance = balance

            # Auto-leverage based on balance tiers (conservative, max x5)
            leverage = cfg.LEVERAGE
            if cfg.AUTO_LEVERAGE:
                if balance < 100:
                    leverage = 3   # very small account — protect capital
                elif balance < 500:
                    leverage = 5
                elif balance < 2000:
                    leverage = 5
                else:
                    leverage = 3
                log.info(f"Авто-плечо: баланс {balance:.2f} → x{leverage}")

            # Orderbook module may suggest lower leverage (e.g. thin book)
            ob_lev = getattr(sig, "_ob_suggested_leverage", None)
            if ob_lev is not None and ob_lev < leverage:
                log.info(f"{sig.symbol}: стакан рекомендует x{ob_lev} (было x{leverage}) — применяем")
                leverage = ob_lev

            # Safety cap: SL must not be below the liquidation price.
            # maint_rate ≈ 0.5% for BingX isolated margin.
            # max_sl_pct = 1/leverage - maint_rate  →  at 10x: 10% - 0.5% = 9.5%
            # If signal SL is wider, reduce leverage until SL fits safely.
            maint_rate = 0.005
            sl_pct_check = abs(sig.entry - sig.sl) / sig.entry if sig.sl > 0 else 0
            if sl_pct_check > 0:
                while leverage > 1:
                    max_safe_sl = 1.0 / leverage - maint_rate
                    if sl_pct_check < max_safe_sl:
                        break
                    leverage -= 1
                else:
                    if sl_pct_check >= 1.0 / 1 - maint_rate:
                        log.warning(f"{sig.symbol}: SL {sl_pct_check*100:.1f}% слишком широкий даже при x1 — пропуск")
                        await self._notify(
                            f"⚠️ <b>{sig.symbol}</b> пропущен\n"
                            f"SL {sl_pct_check*100:.1f}% от входа — слишком широкий для любого плеча"
                        )
                        return
                orig_lev = (3 if balance < 100 else 5 if balance < 2000 else 3) if cfg.AUTO_LEVERAGE else cfg.LEVERAGE
                if leverage < orig_lev:
                    log.info(
                        f"{sig.symbol}: плечо снижено x{orig_lev}→x{leverage} "
                        f"(SL {sl_pct_check*100:.1f}% — безопасный лимит x{orig_lev}: "
                        f"{(1/orig_lev - maint_rate)*100:.1f}%)"
                    )

            risk_usdt = balance * cfg.RISK_PER_TRADE / 100
            if risk_usdt > cfg.MAX_RISK_USDT:
                log.info(f"risk_usdt {risk_usdt:.2f} > MAX_RISK_USDT {cfg.MAX_RISK_USDT} — обрезаем")
                risk_usdt = cfg.MAX_RISK_USDT

            # Adaptive risk: reduce on consecutive loss streak
            streak = state.day.loss_streak
            if streak >= 3:
                risk_usdt *= 0.5
                log.info(f"Адаптивный риск: streak={streak} → ×0.5 = {risk_usdt:.2f} USDT")
            elif streak >= 2:
                risk_usdt *= 0.75
                log.info(f"Адаптивный риск: streak={streak} → ×0.75 = {risk_usdt:.2f} USDT")

            # Pre-check: projected daily loss must not exceed limit BEFORE entry
            projected_loss = (
                abs(min(state.day.pnl_usdt, 0)) +
                abs(min(state.unrealized_pnl(), 0)) +
                risk_usdt
            )
            if balance > 0 and projected_loss / balance * 100 >= cfg.MAX_DAILY_LOSS:
                log.info(
                    f"{sig.symbol}: пропуск — добавление {risk_usdt:.2f} USDT риска "
                    f"превысит дневной лимит {cfg.MAX_DAILY_LOSS}%"
                )
                await self._notify(
                    f"⚠️ <b>{sig.symbol}</b> пропущен\n"
                    f"Добавление риска {risk_usdt:.2f} USDT превысит дневной лимит "
                    f"{cfg.MAX_DAILY_LOSS}% (баланс {balance:.2f} USDT)"
                )
                return

            sl_pct = abs(sig.entry - sig.sl) / sig.entry
            if sl_pct == 0:
                log.warning(f"{sig.symbol}: sl_pct=0, пропуск")
                return
            qty = (risk_usdt / sl_pct) / sig.entry
            notional = qty * sig.entry
            # MIN_POSITION_USDT is minimum notional exposure (not margin)
            min_notional = cfg.MIN_POSITION_USDT
            # Cap at 15% of balance as margin (not notional) — prevents over-sizing on small accounts
            max_margin   = balance * cfg.MAX_MARGIN_PCT / 100
            max_notional = max(max_margin * leverage, min_notional)
            if notional > max_notional:
                log.warning(f"Позиция {sig.symbol}: {notional:.2f} > {max_notional:.2f} — обрезаем")
                qty = max_notional / sig.entry
                risk_usdt = qty * sig.entry * sl_pct
            min_qty = min_notional / sig.entry
            if qty < min_qty:
                qty = min_qty
                # Guard: bumping to min size must not create excessive actual risk
                actual_risk = min_notional * sl_pct
                if balance > 0 and actual_risk / balance * 100 > cfg.RISK_PER_TRADE * 3:
                    log.warning(
                        f"{sig.symbol}: мин. позиция {min_notional:.0f} USDT "
                        f"создаёт риск {actual_risk:.2f} USDT "
                        f"({actual_risk/balance*100:.1f}% > {cfg.RISK_PER_TRADE*3:.1f}%) "
                        f"при балансе {balance:.2f} — пропуск"
                    )
                    await self._notify(
                        f"⚠️ <b>{sig.symbol}</b> пропущен\n"
                        f"Баланс слишком мал: мин. позиция {min_notional:.0f} USDT "
                        f"создаёт риск {actual_risk:.2f} USDT "
                        f"({actual_risk/balance*100:.1f}% от баланса)"
                    )
                    return
                log.info(f"qty увеличен до минимального нотионала {min_notional:.2f} USDT для {sig.symbol}")
            qty = round(qty, 3)
            # After rounding, notional may dip below minimum — correct with ceiling
            if qty * sig.entry < min_notional:
                qty = math.ceil(min_notional / sig.entry * 1000) / 1000
            if qty <= 0:
                log.warning(f"{sig.symbol}: qty=0, пропуск")
                return

            # Pre-check available margin to avoid "Insufficient margin" rejection
            actual_notional  = qty * sig.entry
            required_margin  = actual_notional / leverage
            try:
                avail_margin = await self.ex.get_available_margin()
                if avail_margin > 0 and avail_margin < required_margin * 1.1:
                    log.warning(
                        f"{sig.symbol}: недостаточно свободной маржи "
                        f"{avail_margin:.2f} < {required_margin:.2f} USDT — пропуск"
                    )
                    await self._notify(
                        f"⚠️ <b>{sig.symbol}</b> пропущен\n"
                        f"Недостаточно маржи: {avail_margin:.2f} USDT\n"
                        f"Требуется: {required_margin:.2f} USDT"
                    )
                    return
            except Exception as e:
                log.warning(f"{sig.symbol}: не удалось проверить маржу — продолжаем: {e}")

            # Staleness check: skip only if price moved AGAINST the signal.
            # Skipped in auto mode when _handle already validated the price (price_checked=True).
            if not price_checked:
                try:
                    ticker = await self.ex.get_ticker(sig.symbol)
                    cur_price = float(ticker.get("lastPrice", sig.entry))
                    _drift_mul = cfg.PRICE_DRIFT_PCT / 100
                    against = (sig.side == "LONG"  and cur_price < sig.entry * (1 - _drift_mul)) or \
                              (sig.side == "SHORT" and cur_price > sig.entry * (1 + _drift_mul))
                    if against:
                        drift = abs(cur_price - sig.entry) / sig.entry * 100
                        log.info(f"{sig.symbol}: цена ушла против сигнала на {drift:.2f}% — пропуск")
                        if drift > 3.0:
                            self._normal_cooldown(sig.symbol)
                            log.info(f"{sig.symbol}: дрейф {drift:.1f}% > 3% — кулдаун 1ч")
                        await self._notify(
                            f"⏭ <b>{sig.symbol}</b> пропущен\n"
                            f"Цена ушла против сигнала: {drift:.1f}%\n"
                            f"Сигнал: <code>{sig.entry:.6f}</code> → Сейчас: <code>{cur_price:.6f}</code>"
                        )
                        return
                    sig.entry = cur_price
                except Exception as e:
                    log.warning(f"{sig.symbol}: не удалось получить текущую цену перед входом — используется цена сигнала: {e}")

            await self.ex.set_margin_type(sig.symbol)
            lev_ok = await self.ex.set_leverage(sig.symbol, leverage)
            if not lev_ok:
                log.warning(f"{sig.symbol}: не удалось выставить плечо x{leverage} — пропуск")
                await self._notify(
                    f"⚠️ <b>{sig.symbol}</b> пропущен\n"
                    f"Не удалось выставить плечо x{leverage}"
                )
                return
            side  = "BUY" if sig.side == "LONG" else "SELL"
            order = await self.ex.place_order(sig.symbol, side, qty, position_side=sig.side)
            if order.get("code") != 0:
                log.error(f"Ордер входа отклонён {sig.symbol}: {order}")
                await self._notify(f"❌ Вход отклонён <b>{sig.symbol}</b>: {_html.escape(str(order.get('msg', '')))}")
                return
            order_id = str(order.get("data", {}).get("orderId", ""))

            # Get actual filled qty from exchange (may differ from calculated)
            await asyncio.sleep(0.5)
            try:
                live = await self.ex.get_open_positions()
                for p in live:
                    if p.get("symbol") == sig.symbol and p.get("positionSide") == sig.side:
                        actual = abs(float(p.get("positionAmt", 0)))
                        if actual > 0:
                            qty = round(actual, 3)
                            actual_notional = qty * sig.entry
                            min_notional_check = cfg.MIN_POSITION_USDT
                            if actual_notional < min_notional_check:
                                log.warning(
                                    f"{sig.symbol}: реальный объём {actual_notional:.2f} USDT "
                                    f"< минимума {min_notional_check:.2f} USDT (биржа округлила qty)"
                                )
                            break
            except Exception as pe:
                log.warning(f"Не удалось получить реальный qty {sig.symbol}: {pe}")

            # Pre-validate SL price vs current mark price (avoids error 110411)
            # For LONG: SL (SELL stop) must be below current price
            # For SHORT: SL (BUY stop) must be above current price
            # Also check that SL is above the estimated liquidation price —
            # if SL <= liq_price, position gets liquidated before SL fires.
            try:
                mk = await self.ex.get_ticker(sig.symbol)
                mark = float(mk.get("lastPrice", sig.entry))
                sl_invalid = (sig.side == "LONG"  and sig.sl >= mark) or \
                             (sig.side == "SHORT" and sig.sl <= mark)
                if sl_invalid:
                    log.warning(f"{sig.symbol}: SL {sig.sl} уже за mark {mark:.4f} — аварийное закрытие")
                    await self._notify(
                        f"⚠️ <b>{sig.symbol}</b>: цена уже за SL при входе — закрываем"
                    )
                    try:
                        await self.ex.close_position(sig.symbol, qty, sig.side)
                    except Exception as ce:
                        log.error(f"emergency close (pre-SL) {sig.symbol}: {ce}")
                    self._loss_cooldown(sig.symbol)
                    return
                # Liquidation check: SL must be inside the margin safety zone
                # Approximate liq price (isolated margin, maintenance rate ~0.5%)
                maint_rate = 0.005
                if sig.side == "LONG":
                    liq_price = sig.entry * (1 - 1.0 / leverage + maint_rate)
                    if sig.sl <= liq_price:
                        log.warning(
                            f"{sig.symbol}: SL {sig.sl:.4f} ≤ цены ликвидации {liq_price:.4f} "
                            f"(x{leverage}) — пропуск во избежание принудительного закрытия"
                        )
                        await self._notify(
                            f"⚠️ <b>{sig.symbol}</b> пропущен\n"
                            f"SL <code>{sig.sl:.4f}</code> ниже цены ликвидации "
                            f"<code>{liq_price:.4f}</code> при плече x{leverage}\n"
                            f"Снизь плечо или расширь SL"
                        )
                        try:
                            await self.ex.close_position(sig.symbol, qty, sig.side)
                        except Exception as ce:
                            log.error(f"emergency close (liq check) {sig.symbol}: {ce}")
                        self._loss_cooldown(sig.symbol)
                        return
                else:
                    liq_price = sig.entry * (1 + 1.0 / leverage - maint_rate)
                    if sig.sl >= liq_price:
                        log.warning(
                            f"{sig.symbol}: SL {sig.sl:.4f} ≥ цены ликвидации {liq_price:.4f} "
                            f"(x{leverage}) — пропуск"
                        )
                        await self._notify(
                            f"⚠️ <b>{sig.symbol}</b> пропущен\n"
                            f"SL <code>{sig.sl:.4f}</code> выше цены ликвидации "
                            f"<code>{liq_price:.4f}</code> при плече x{leverage}\n"
                            f"Снизь плечо или расширь SL"
                        )
                        try:
                            await self.ex.close_position(sig.symbol, qty, sig.side)
                        except Exception as ce:
                            log.error(f"emergency close (liq check) {sig.symbol}: {ce}")
                        self._loss_cooldown(sig.symbol)
                        return
            except Exception as e:
                log.warning(f"{sig.symbol}: не удалось проверить mark price перед SL: {e}")

            sl_order = await self.ex.place_stop_loss(sig.symbol, side, qty, sig.sl)
            sl_id    = str(sl_order.get("data", {}).get("orderId", ""))
            if sl_order.get("code") != 0:
                err_code = sl_order.get("code", "?")
                log.error(f"SL не выставился {sig.symbol}: код {err_code} — аварийное закрытие")
                await self._notify(
                    f"⚠️ SL не выставился <b>{sig.symbol}</b> (код {err_code}) — закрываем"
                )
                try:
                    await self.ex.close_position(sig.symbol, qty, sig.side)
                except Exception as ce:
                    log.error(f"emergency close {sig.symbol}: {ce}")
                self._loss_cooldown(sig.symbol)  # позиция открылась без SL = реальная потеря
                return
            if not sl_id:
                log.warning(f"SL выставлен (code=0) но orderId не получен {sig.symbol} — отмена SL позже недоступна")

            tp_order = await self.ex.place_take_profit(sig.symbol, side, qty, sig.tp3)
            tp_id    = str(tp_order.get("data", {}).get("orderId", ""))
            if tp_order.get("code") != 0:
                await asyncio.sleep(1.5)
                tp_order = await self.ex.place_take_profit(sig.symbol, side, qty, sig.tp3)
                tp_id = str(tp_order.get("data", {}).get("orderId", ""))
                if tp_order.get("code") != 0:
                    log.error(f"TP не выставился {sig.symbol} после 2 попыток: {tp_order}")
                    await self._notify(
                        f"⚠️ <b>{sig.symbol}</b>: TP ордер не выставился после 2 попыток\n"
                        f"При достижении TP3 <code>{sig.tp3:.4f}</code> закрой вручную"
                    )

            pos = Position(
                symbol=sig.symbol, side=sig.side,
                entry=sig.entry, sl=sig.sl,
                tp1=sig.tp1, tp2=sig.tp2, tp3=sig.tp3,
                qty=qty, risk_usdt=risk_usdt,
                order_id=order_id, sl_order_id=sl_id, tp_order_id=tp_id,
                pattern=sig.pattern, tf=sig.tf, rr=sig.rr, score=sig.score,
            )
            state.positions[sig.symbol] = pos
            state.day.trades += 1
            state.pending.pop(sig.symbol, None)
            db.save_open_position(pos)  # persist for restart recovery

            icon = "✅" if confirmed else "🤖"
            await self._notify(
                f"{icon} <b>ВХОД</b> | {sig.symbol} {sig.side}\n"
                f"🕯 {sig.pattern} | ⭐ {sig.score}/100\n"
                f"🟡 <code>{sig.entry:.4f}</code>  "
                f"🔴 SL: <code>{sig.sl:.4f}</code>\n"
                f"🟢 TP2: <code>{sig.tp2:.4f}</code>  "
                f"TP3: <code>{sig.tp3:.4f}</code>\n"
                f"💰 Риск: <code>{risk_usdt:.2f} USDT</code>  x{leverage}"
            )
        except Exception as e:
            log.error(f"enter {sig.symbol}: {e}")
            await self._notify(f"❌ Ошибка входа {sig.symbol}: {_html.escape(str(e))}")

    # ------------------------------------------------------------------ monitor

    async def monitor_positions(self):
        global _MONITORING
        if _MONITORING:
            return
        _MONITORING = True
        try:
            await self._monitor_inner()
        finally:
            _MONITORING = False

    async def _check_open_funding(self):
        """Warn if open positions are facing adverse funding rates (> ±0.05% per 8h)."""
        try:
            for symbol, pos in list(state.positions.items()):
                if pos.side not in ("LONG", "SHORT"):
                    continue
                funding = await self.ex.get_funding_rate(symbol)
                if pos.side == "LONG" and funding > 0.05:
                    if symbol not in self._funding_warned:
                        self._funding_warned.add(symbol)
                        await self._notify(
                            f"⚠️ <b>Высокий фандинг</b> | {symbol} LONG\n"
                            f"Фандинг: <code>{funding:.4f}%</code> каждые 8ч\n"
                            f"Позиция медленно теряет деньги — рассмотри закрытие"
                        )
                elif pos.side == "SHORT" and funding < -0.05:
                    if symbol not in self._funding_warned:
                        self._funding_warned.add(symbol)
                        await self._notify(
                            f"⚠️ <b>Высокий фандинг</b> | {symbol} SHORT\n"
                            f"Фандинг: <code>{funding:.4f}%</code> каждые 8ч\n"
                            f"Позиция медленно теряет деньги — рассмотри закрытие"
                        )
                else:
                    # Funding normalized — reset warning so it can fire again if it spikes
                    if symbol in self._funding_warned:
                        if (pos.side == "LONG"  and funding <= 0.03) or \
                           (pos.side == "SHORT" and funding >= -0.03):
                            self._funding_warned.discard(symbol)
        except Exception as e:
            log.warning(f"_check_open_funding: {e}")

    async def _monitor_inner(self):
        self._monitor_count += 1
        # Check funding on open positions every ~30 min (60s × 30 cycles)
        if self._monitor_count % 30 == 0:
            await self._check_open_funding()

        # Sync with BingX: detect positions closed externally (SL/TP hit on exchange)
        try:
            live = await self.ex.get_open_positions()
            live_syms = {p.get("symbol") for p in live}
            for symbol, pos in list(state.positions.items()):
                age = (datetime.utcnow() - pos.opened_at).total_seconds()
                if age > 60 and symbol not in live_syms:
                    # Position no longer on exchange — clean up state
                    if pos.sl == 0:
                        # Manual/synced position — just remove from state, don't track PnL
                        del state.positions[symbol]
                        log.info(f"Ручная позиция {symbol} закрыта на бирже — убрана из памяти")
                        continue
                    try:
                        ticker = await self.ex.get_ticker(symbol)
                        price = float(ticker.get("lastPrice", pos.entry)) if ticker else pos.entry
                    except Exception as e:
                        log.warning(f"{symbol}: ошибка получения цены при закрытии — P&L посчитан по цене входа: {e}")
                        price = pos.entry
                    pnl = await self._record_close(pos, price)
                    # Refresh balance so next position sizing uses real balance
                    try:
                        state.current_balance = await self.ex.get_balance()
                    except Exception:
                        pass
                    sign = "+" if pnl >= 0 else ""
                    await self._notify(
                        f"{'✅ WIN' if pnl > 0 else '❌ LOSS'} | {symbol} {pos.side}\n"
                        f"Закрыто на бирже | Цена: <code>{price:.4f}</code>\n"
                        f"PnL: <code>{sign}{pnl:.2f} USDT</code>"
                    )
        except Exception as e:
            log.error(f"monitor sync: {e}")

        # Alert on stale positions (open > 48h) and auto-close at MAX_POSITION_HOURS
        for symbol, pos in list(state.positions.items()):
            if pos.sl == 0:
                continue
            age_h = (datetime.utcnow() - pos.opened_at).total_seconds() / 3600
            if cfg.MAX_POSITION_HOURS > 0 and age_h >= cfg.MAX_POSITION_HOURS:
                log.warning(f"{symbol}: позиция открыта {age_h:.0f}ч — авто-закрытие")
                try:
                    ticker = await self.ex.get_ticker(symbol)
                    price = float(ticker.get("lastPrice", pos.entry)) if ticker else pos.entry
                    await self.ex.close_position(symbol, pos.qty, pos.side)
                    pnl = await self._record_close(pos, price)
                    sign = "+" if pnl >= 0 else ""
                    await self._notify(
                        f"⏰ {'✅' if pnl > 0 else '❌'} Авто-закрытие | {symbol} {pos.side}\n"
                        f"Открыта {age_h:.0f}ч (лимит {cfg.MAX_POSITION_HOURS}ч)\n"
                        f"Цена: <code>{price:.4f}</code> | PnL: <code>{sign}{pnl:.2f} USDT</code>"
                    )
                except Exception as e:
                    log.error(f"{symbol}: ошибка авто-закрытия: {e}")
                    await self._notify(f"⚠️ Ошибка авто-закрытия {symbol}: {_html.escape(str(e))}")
            elif age_h > 48 and symbol not in self._stale_alerted:
                self._stale_alerted.add(symbol)
                auto_close_note = (
                    f"Авто-закрытие через {cfg.MAX_POSITION_HOURS - age_h:.0f}ч"
                    if cfg.MAX_POSITION_HOURS > 0
                    else "Авто-закрытие выключено"
                )
                await self._notify(
                    f"⏰ <b>Позиция завязла</b> | {symbol} {pos.side}\n"
                    f"Открыта {age_h:.0f}ч назад без движения к TP\n"
                    f"Вход: <code>{pos.entry:.4f}</code> | TP2: <code>{pos.tp2:.4f}</code>\n"
                    f"{auto_close_note}"
                )

        # Clean up stale sl_cooldown entries — use max possible cooldown to not evict early
        max_cooldown = max(SL_COOLDOWN_MIN, cfg.SYMBOL_LOSS_COOLDOWN_MIN)
        cutoff = datetime.utcnow() - timedelta(minutes=max_cooldown * 2)
        self._sl_cooldown = {s: t for s, t in self._sl_cooldown.items() if t > cutoff}

        for symbol, pos in list(state.positions.items()):
            try:
                # Guard: position may have been removed by exchange sync earlier in this cycle
                if symbol not in state.positions:
                    continue
                ticker = await self.ex.get_ticker(symbol)
                if not ticker:
                    continue
                price = float(ticker.get("lastPrice", pos.entry))

                # Breakeven trigger — skip for exchange-synced positions (tp1/sl unknown)
                _be_just_fired = False
                if not pos.be_moved and pos.sl > 0:
                    be_triggered = False
                    if cfg.BE_TRIGGER_PCT > 0:
                        # Price moved BE_TRIGGER_PCT% from entry in profit direction
                        if pos.side == "LONG":
                            be_triggered = price >= pos.entry * (1 + cfg.BE_TRIGGER_PCT / 100)
                        else:
                            be_triggered = price <= pos.entry * (1 - cfg.BE_TRIGGER_PCT / 100)
                    elif pos.tp1 > 0:
                        # Fallback: use TP1 as BE trigger (BE_TRIGGER_PCT=0)
                        be_triggered = (pos.side == "LONG" and price >= pos.tp1) or \
                                       (pos.side == "SHORT" and price <= pos.tp1)
                    if be_triggered:
                        await self._move_be(pos)
                        _be_just_fired = True

                # TP1 → partial close 25% (lock early profit)
                # Skip if BE just fired in the same cycle via the TP1-fallback path —
                # _move_be already placed SL at BE; re-firing would cause a double SL churn.
                if not pos.tp1_hit and pos.tp1 > 0 and not _be_just_fired:
                    tp1_triggered = (pos.side == "LONG" and price >= pos.tp1) or \
                                    (pos.side == "SHORT" and price <= pos.tp1)
                    if tp1_triggered:
                        await self._partial_close(pos, 0.25, "TP1")

                # TP2 → partial close 60% of remaining — skip if tp2 unknown
                if pos.be_moved and not pos.tp2_hit and pos.tp2 > 0:
                    tp2_hit = (pos.side == "LONG" and price >= pos.tp2) or \
                              (pos.side == "SHORT" and price <= pos.tp2)
                    if tp2_hit:
                        await self._partial_close(pos, cfg.TP2_CLOSE_PCT, "TP2")

                # Trailing stop (only after BE is moved)
                if pos.be_moved:
                    await self._trail_sl(pos, price)

                await self._check_closed(pos, price)
            except Exception as e:
                log.error(f"monitor {symbol}: {e}")

    async def _move_be(self, pos: Position):
        try:
            # Place SL at entry + small buffer to lock in tiny profit above fees
            buffer = pos.entry * cfg.BE_BUFFER_PCT / 100
            if pos.side == "LONG":
                be_price = _px(pos.entry + buffer)
            else:
                be_price = _px(pos.entry - buffer)

            if pos.sl_order_id:
                await self.ex.cancel_order(pos.symbol, pos.sl_order_id)
            side = "BUY" if pos.side == "LONG" else "SELL"
            r = await self.ex.place_stop_loss(pos.symbol, side, pos.qty, be_price)
            if r.get("code") != 0:
                log.error(f"BE SL не выставился {pos.symbol}: {r.get('code')} {r.get('msg','')}")
                await self._notify(
                    f"⚠️ <b>{pos.symbol}</b>: SL на безубыток не выставился "
                    f"(код {r.get('code')}) — позиция без защиты!"
                )
                return
            pos.sl_order_id = str(r.get("data", {}).get("orderId", ""))
            pos.sl          = be_price
            pos.be_moved    = True
            pos.trail_price = be_price
            db.save_open_position(pos)  # persist BE state change

            trigger_info = (
                f"+{cfg.BE_TRIGGER_PCT}% от входа"
                if cfg.BE_TRIGGER_PCT > 0
                else "TP1"
            )
            await self._notify(
                f"🔄 <b>БЕЗУБЫТОК</b> | {pos.symbol}\n"
                f"Триггер: {trigger_info}\n"
                f"SL перенесён → <code>{be_price:.4f}</code>"
                + (f" (+{cfg.BE_BUFFER_PCT}% буфер)" if cfg.BE_BUFFER_PCT > 0 else "")
            )
        except Exception as e:
            log.error(f"move_be {pos.symbol}: {e}")

    async def _trail_sl(self, pos: Position, price: float):
        """Move SL to trail TRAIL_PCT% behind the peak price."""
        try:
            if pos.side == "LONG":
                if price <= pos.trail_price:
                    return
                new_trail = price
                new_sl = _px(price * (1 - cfg.TRAIL_PCT / 100))
            else:
                if price >= pos.trail_price and pos.trail_price != 0:
                    return
                new_trail = price
                new_sl = _px(price * (1 + cfg.TRAIL_PCT / 100))

            # Only move if improvement is meaningful (≥0.1%)
            min_move = pos.entry * 0.001
            if pos.side == "LONG" and new_sl <= pos.sl + min_move:
                return
            if pos.side == "SHORT" and new_sl >= pos.sl - min_move:
                return

            if pos.sl_order_id:
                await self.ex.cancel_order(pos.symbol, pos.sl_order_id)
            side = "BUY" if pos.side == "LONG" else "SELL"
            r = await self.ex.place_stop_loss(pos.symbol, side, pos.qty, new_sl)
            new_id = str(r.get("data", {}).get("orderId", ""))
            if r.get("code") != 0 or not new_id:
                log.warning(f"Trail SL не обновился {pos.symbol}: код {r.get('code')}")
                return
            pos.sl_order_id = new_id
            pos.sl = new_sl
            pos.trail_price = new_trail  # update only after exchange confirms
            log.info(f"Trail SL {pos.symbol} → {new_sl:.4f}")
            db.save_open_position(pos)
        except Exception as e:
            log.error(f"trail_sl {pos.symbol}: {e}")

    async def _partial_close(self, pos: Position, pct: float, label: str):
        try:
            qty = round(pos.qty * pct, 3)
            if qty <= 0:
                return
            qty = min(qty, pos.qty)  # safety: never close more than held
            await self.ex.close_position(pos.symbol, qty, pos.side)

            # Record PnL for the closed portion
            fallback = pos.tp1 if label == "TP1" else pos.tp2
            close_price = fallback if fallback > 0 else pos.entry
            try:
                ticker = await self.ex.get_ticker(pos.symbol)
                if ticker and float(ticker.get("lastPrice", 0)) > 0:
                    close_price = float(ticker.get("lastPrice", close_price))
            except Exception as pe:
                log.warning(f"partial_close ticker {pos.symbol}: {pe}")
            partial_pnl = (close_price - pos.entry) * qty if pos.side == "LONG" \
                          else (pos.entry - close_price) * qty
            state.total_pnl    += partial_pnl
            state.day.pnl_usdt += partial_pnl
            # A profitable partial close means the trade is winning — reset streak so
            # a subsequent SL on the remainder doesn't compound the loss count.
            if partial_pnl > 0:
                state.day.loss_streak = 0
                state.day.paused_until = None
                db.save_kv("paused_until", "")
            try:
                from dataclasses import replace as dc_replace
                pos_snap = dc_replace(pos, qty=qty)
                db.save_trade(pos_snap, close_price, round(partial_pnl, 4),
                              "WIN" if partial_pnl > 0 else "LOSS")
            except Exception as pe:
                log.error(f"partial_close db.save_trade {pos.symbol}: {pe}")

            pos.qty -= qty
            if label == "TP1":
                pos.tp1_hit = True
                sign = "+" if partial_pnl >= 0 else ""
                await self._notify(
                    f"🎯 <b>TP1</b> | {pos.symbol} {pos.side}\n"
                    f"Закрыто 25% позиции по <code>{close_price:.4f}</code>\n"
                    f"PnL частичный: <code>{sign}{partial_pnl:.2f} USDT</code>\n"
                    f"Остаток: <code>{pos.qty:.3f}</code> | SL → безубыток"
                )
            else:
                pos.tp2_hit = True
                sign = "+" if partial_pnl >= 0 else ""
                await self._notify(
                    f"🎯 <b>TP2</b> | {pos.symbol} {pos.side}\n"
                    f"Закрыто {int(cfg.TP2_CLOSE_PCT*100)}% позиции по <code>{close_price:.4f}</code>\n"
                    f"PnL частичный: <code>{sign}{partial_pnl:.2f} USDT</code>\n"
                    f"Остаток: <code>{pos.qty:.3f}</code> | Трейлинг активен"
                )
            side = "BUY" if pos.side == "LONG" else "SELL"
            # Re-place SL for remaining qty
            if pos.sl_order_id:
                await self.ex.cancel_order(pos.symbol, pos.sl_order_id)
                r = await self.ex.place_stop_loss(pos.symbol, side, pos.qty, pos.sl)
                pos.sl_order_id = str(r.get("data", {}).get("orderId", ""))
            # Re-place TP3 for remaining qty (old order had original full qty)
            if pos.tp_order_id:
                await self.ex.cancel_order(pos.symbol, pos.tp_order_id)
                pos.tp_order_id = ""
            if pos.tp3 > 0 and pos.qty > 0:
                r = await self.ex.place_take_profit(pos.symbol, side, pos.qty, pos.tp3)
                pos.tp_order_id = str(r.get("data", {}).get("orderId", ""))

            db.save_open_position(pos)  # persist updated qty and tp2_hit flag
        except Exception as e:
            log.error(f"partial_close {pos.symbol}: {e}")

    async def _check_closed(self, pos: Position, price: float):
        # Guard: exchange sync may have already removed this position in the same monitor cycle
        if pos.symbol not in state.positions:
            return
        # Skip check for exchange-synced positions without SL/TP info
        if pos.sl == 0 or pos.tp3 == 0:
            return
        sl_hit  = (pos.side == "LONG"  and price <= pos.sl) or \
                  (pos.side == "SHORT" and price >= pos.sl)
        tp3_hit = (pos.side == "LONG"  and price >= pos.tp3) or \
                  (pos.side == "SHORT" and price <= pos.tp3)
        if not sl_hit and not tp3_hit:
            return
        try:
            if sl_hit and pos.tp_order_id:
                await self.ex.cancel_order(pos.symbol, pos.tp_order_id)
            elif tp3_hit and pos.sl_order_id:
                await self.ex.cancel_order(pos.symbol, pos.sl_order_id)
        except Exception as e:
            log.warning(f"cancel opposite order {pos.symbol}: {e}")

        pnl = await self._record_close(pos, price)

        if sl_hit:
            # SL fired at breakeven (pnl > 0): _record_close cleared the streak but
            # the stop was still hit — apply a normal cooldown without incrementing streak
            if pnl > 0:
                self._normal_cooldown(pos.symbol)
            # Notify if extended streak cooldown activated (streak was incremented by _record_close on loss)
            streak = self._symbol_loss_streak.get(pos.symbol, 0)
            if streak >= cfg.SYMBOL_LOSS_STREAK_LIMIT:
                await self._notify(
                    f"🚫 <b>{pos.symbol}</b>: {streak} стопа подряд\n"
                    f"Кулдаун {cfg.SYMBOL_LOSS_COOLDOWN_MIN // 60}ч — не входим в эту монету"
                )

        trade_sign = "+" if pnl >= 0 else ""
        total_sign = "+" if state.total_pnl >= 0 else ""
        icon = "✅ WIN" if pnl > 0 else "❌ LOSS"
        reason = "TP3 🎯" if tp3_hit else "SL 🛑"
        await self._notify(
            f"{icon} | {pos.symbol} {pos.side}\n"
            f"{reason} | Цена: <code>{price:.4f}</code>\n"
            f"PnL: <code>{trade_sign}{pnl:.2f} USDT</code>\n"
            f"Итого: <code>{total_sign}{state.total_pnl:.2f} USDT</code>"
        )

    async def health_check(self):
        """Каждые 15 минут: проверяет состояние бота и пишет в лог (+ Telegram при проблемах)."""
        try:
            issues = []

            # Баланс + drawdown protection
            try:
                balance = await self.ex.get_balance()
                state.current_balance = balance
                if balance <= 0:
                    issues.append("⚠️ Баланс = 0 USDT")
                else:
                    # Track peak balance (persist to DB)
                    if balance > state.peak_balance:
                        state.peak_balance = balance
                        db.save_kv("peak_balance", str(balance))
                    # Check drawdown from peak
                    if cfg.MAX_DRAWDOWN_PCT > 0 and state.peak_balance > 0:
                        drawdown = (state.peak_balance - balance) / state.peak_balance * 100
                        if drawdown >= cfg.MAX_DRAWDOWN_PCT and not state.paused:
                            state.paused = True
                            db.save_kv("paused", "1")
                            await self._notify(
                                f"🚨 <b>Drawdown protection</b>\n"
                                f"Баланс упал на <code>{drawdown:.1f}%</code> от пика\n"
                                f"Пик: <code>{state.peak_balance:.2f} USDT</code> → "
                                f"Сейчас: <code>{balance:.2f} USDT</code>\n"
                                f"Бот на паузе. Возобновить: /resume"
                            )
            except Exception as e:
                issues.append(f"⚠️ Не удалось получить баланс: {_html.escape(str(e))}")
                balance = state.current_balance

            # Открытые позиции
            pos_count = len(state.positions)

            # Сверка с биржей
            try:
                live = await self.ex.get_open_positions()
                live_count = len(live)
                if live_count != pos_count:
                    issues.append(
                        f"⚠️ Расхождение позиций: бот={pos_count}, биржа={live_count}"
                    )
            except Exception as e:
                issues.append(f"⚠️ Не удалось получить позиции с биржи: {_html.escape(str(e))}")

            # Пауза без причины
            if state.is_paused and pos_count == 0:
                issues.append("ℹ️ Бот на паузе, открытых позиций нет")

            # SL order health check: verify each position has a live SL on exchange
            for symbol, pos in list(state.positions.items()):
                if pos.sl == 0 or not pos.sl_order_id:
                    if pos.sl == 0:
                        issues.append(f"⚠️ {symbol}: нет SL-уровня (внешняя позиция?)")
                    else:
                        # Has SL price but no order ID — try to re-place
                        log.warning(f"{symbol}: нет sl_order_id, перевыставляем SL @ {pos.sl}")
                        try:
                            side_str = "BUY" if pos.side == "LONG" else "SELL"
                            r = await self.ex.place_stop_loss(symbol, side_str, pos.qty, pos.sl)
                            if r.get("code") == 0:
                                pos.sl_order_id = str(r.get("data", {}).get("orderId", ""))
                                db.save_open_position(pos)
                                issues.append(f"ℹ️ {symbol}: SL перевыставлен @ {pos.sl:.4f}")
                            else:
                                issues.append(
                                    f"⚠️ {symbol}: не удалось перевыставить SL "
                                    f"(код {r.get('code')})"
                                )
                        except Exception as se:
                            issues.append(f"⚠️ {symbol}: ошибка SL re-place: {_html.escape(str(se))}")
                    continue
                # Verify the order is still open on exchange
                try:
                    open_orders = await self.ex.get_open_orders(symbol)
                    order_ids = {str(o.get("orderId", "")) for o in open_orders}
                    if pos.sl_order_id and pos.sl_order_id not in order_ids:
                        # SL order is gone — re-place it
                        log.warning(f"{symbol}: SL ордер {pos.sl_order_id} исчез, перевыставляем")
                        side_str = "BUY" if pos.side == "LONG" else "SELL"
                        r = await self.ex.place_stop_loss(symbol, side_str, pos.qty, pos.sl)
                        if r.get("code") == 0:
                            pos.sl_order_id = str(r.get("data", {}).get("orderId", ""))
                            db.save_open_position(pos)
                            issues.append(f"🔄 {symbol}: SL ордер потерялся — перевыставлен @ {pos.sl:.4f}")
                        else:
                            issues.append(
                                f"🚨 {symbol}: SL ОТСУТСТВУЕТ на бирже! "
                                f"Ошибка перевыставления: код {r.get('code')}"
                            )
                except Exception as oe:
                    log.warning(f"health SL check {symbol}: {oe}")

            log.info(
                f"[healthcheck] баланс={balance:.2f} USDT | "
                f"позиций={pos_count} | пауза={state.is_paused} | "
                f"сканов={self._scan_count} | проблем={len(issues)}"
            )

            if issues:
                text = "🔎 <b>Проверка бота</b>\n" + "\n".join(issues)
                await self._notify(text)

        except Exception as e:
            log.error(f"health_check: {e}")

    # ------------------------------------------------------------------ reports

    async def daily_report(self):
        d  = state.day
        wr = round(d.wins / d.trades * 100) if d.trades else 0
        sign = "+" if d.pnl_usdt >= 0 else ""
        await self._notify(
            f"📋 <b>Дневной отчёт</b> {d.date}\n\n"
            f"Сделок: {d.trades}  |  WR: {wr}%\n"
            f"Прибыльных: {d.wins}  |  Убыточных: {d.losses}\n"
            f"PnL: <code>{sign}{d.pnl_usdt:.2f} USDT</code>\n"
            f"Итого всего: <code>{'+' if state.total_pnl >= 0 else ''}{state.total_pnl:.2f} USDT</code>"
        )

    async def weekly_report(self):
        s    = db.get_stats(days=7)
        sign = "+" if s["pnl"] >= 0 else ""
        patterns = db.get_stats_by_pattern(days=7)
        pat_lines = ""
        for pname, total, wins, pnl in patterns[:5]:
            wr = round(wins / total * 100) if total else 0
            ps = "+" if pnl >= 0 else ""
            short = pname[:18]
            pat_lines += f"  {short}: {total}сд WR{wr}% {ps}{pnl:.2f}₮\n"
        await self._notify(
            f"📊 <b>Недельный отчёт</b>\n\n"
            f"Сделок: {s['total']}  |  WR: {s['wr']}%\n"
            f"Прибыльных: {s['wins']}  |  Убыточных: {s['total'] - s['wins']}\n"
            f"PnL за 7 дней: <code>{sign}{s['pnl']:.2f} USDT</code>"
            + (f"\n\n<b>По паттернам:</b>\n<code>{pat_lines.rstrip()}</code>" if pat_lines else "")
        )

    async def monthly_report(self):
        s    = db.get_stats(days=30)
        sign = "+" if s["pnl"] >= 0 else ""
        patterns = db.get_stats_by_pattern(days=30)
        pat_lines = ""
        for pname, total, wins, pnl in patterns[:6]:
            wr = round(wins / total * 100) if total else 0
            ps = "+" if pnl >= 0 else ""
            short = pname[:18]
            pat_lines += f"  {short}: {total}сд WR{wr}% {ps}{pnl:.2f}₮\n"
        await self._notify(
            f"🗓 <b>Месячный отчёт</b>\n\n"
            f"Сделок: {s['total']}  |  WR: {s['wr']}%\n"
            f"Прибыльных: {s['wins']}  |  Убыточных: {s['total'] - s['wins']}\n"
            f"PnL за 30 дней: <code>{sign}{s['pnl']:.2f} USDT</code>"
            + (f"\n\n<b>По паттернам:</b>\n<code>{pat_lines.rstrip()}</code>" if pat_lines else "")
        )

    async def btc_weekly_alert(self):
        """
        Fetch BTC W1 data and report the nearest key weekly levels.
        Runs every hour — gives Gerchik-style level awareness.
        """
        try:
            w1_raw = parse_klines(await self.ex.get_klines("BTC-USDT", "1w", limit=60))
            h1_raw = parse_klines(await self.ex.get_klines("BTC-USDT", cfg.SIGNAL_TF, limit=5))
            if not w1_raw or not h1_raw:
                return
            price  = float(h1_raw["close"][-1])
            lvls   = nearest_weekly_levels(price, w1_raw, count=3)
            sups   = lvls["support"]
            ress   = lvls["resistance"]
            if not sups and not ress:
                return

            sup_lines = "\n".join(
                f"  🟢 <code>{l:,.0f}</code>  (-{abs(price-l)/price*100:.1f}%)" for l in sups
            )
            res_lines = "\n".join(
                f"  🔴 <code>{l:,.0f}</code>  (+{abs(l-price)/price*100:.1f}%)" for l in ress
            )
            await self._notify(
                f"📐 <b>BTC недельные уровни</b>\n"
                f"Текущая цена: <code>{price:,.0f}</code>\n\n"
                f"Сопротивления (цель вверх):\n{res_lines or '  —'}\n\n"
                f"Поддержки (цель вниз):\n{sup_lines or '  —'}"
            )
        except Exception as e:
            log.warning(f"btc_weekly_alert: {e}")

    async def funding_scan(self) -> list[tuple[str, float]]:
        """
        Сканирует фандинг по всем парам. Возвращает список (symbol, rate).
        Также отправляет алерт в Telegram если есть экстремальные значения.
        Запускается планировщиком каждые 8 часов.
        """
        ALERT_THRESHOLD = 0.05   # % — алерт при |funding| > 0.05%
        TOP_N = 15               # сколько пар показывать в сводке
        BATCH = 20               # параллельных запросов за раз

        pairs = state.pairs if state.pairs else []
        if not pairs:
            return []

        async def _fetch(symbol):
            try:
                rate = await self.ex.get_funding_rate(symbol)
                return (symbol, rate)
            except Exception:
                return None

        results = []
        for i in range(0, len(pairs), BATCH):
            batch = pairs[i:i + BATCH]
            fetched = await asyncio.gather(*[_fetch(s) for s in batch])
            results.extend(r for r in fetched if r is not None)
            if i + BATCH < len(pairs):
                await asyncio.sleep(0.3)

        results.sort(key=lambda x: abs(x[1]), reverse=True)

        extremes_long  = [(s, r) for s, r in results if r < -ALERT_THRESHOLD]
        extremes_short = [(s, r) for s, r in results if r >  ALERT_THRESHOLD]

        lines = []
        if extremes_short:
            lines.append("📈 <b>Высокий фандинг (LONG переплачивает → SHORT выгоден):</b>")
            for s, r in extremes_short[:10]:
                lines.append(f"  {s}: <code>{r:+.4f}%</code>")
        if extremes_long:
            lines.append("📉 <b>Отрицательный фандинг (SHORT переплачивает → LONG выгоден):</b>")
            for s, r in extremes_long[:10]:
                lines.append(f"  {s}: <code>{r:+.4f}%</code>")

        top_str = "\n".join(
            f"  {s}: <code>{r:+.4f}%</code>"
            for s, r in results[:TOP_N]
        )
        text = (
            f"💸 <b>Фандинг-сводка</b> ({len(results)} пар)\n\n"
            + ("\n".join(lines) + "\n\n" if lines else "Экстремальных значений нет\n\n")
            + f"<b>Топ-{TOP_N} по абс. значению:</b>\n{top_str}"
        )
        await self._notify(text)
        log.info(f"funding_scan: {len(results)} пар, экстремов: {len(extremes_short)+len(extremes_long)}")
        return results
