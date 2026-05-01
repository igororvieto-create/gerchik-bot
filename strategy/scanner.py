import asyncio
import logging
import math
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

from core.config import cfg
from core.state import Position, state
from core import db
from exchange.bingx import BingXClient
from strategy.strategy.gerchik import Signal, analyze, parse_klines, reset_stats, get_stats

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


SL_COOLDOWN_MIN = 60  # minutes to skip a symbol after SL hit


class Scanner:
    def __init__(self, exchange: BingXClient, bot: Bot):
        global _global_scanner
        self.ex          = exchange
        self.bot         = bot
        self._scan_count = 0
        self._sl_cooldown: dict = {}  # symbol → datetime of last SL hit
        _global_scanner = self
        self._restore_cooldowns()

    def _set_cooldown(self, symbol: str):
        """Set cooldown in memory and persist to DB for restart survival."""
        now = datetime.utcnow()
        self._sl_cooldown[symbol] = now
        try:
            db.save_kv(f"sl_cd:{symbol}", now.isoformat())
        except Exception as e:
            log.warning(f"cooldown save {symbol}: {e}")

    def _in_cooldown(self, symbol: str) -> bool:
        """Check in-memory cooldown only (DB loaded once at startup)."""
        if symbol not in self._sl_cooldown:
            return False
        return (datetime.utcnow() - self._sl_cooldown[symbol]).total_seconds() < SL_COOLDOWN_MIN * 60

    def _restore_cooldowns(self):
        """Load all cooldowns from DB in one query at startup."""
        try:
            loaded = db.load_all_cooldowns()
            now = datetime.utcnow()
            active = 0
            for sym, cd_time in loaded.items():
                if (now - cd_time).total_seconds() < SL_COOLDOWN_MIN * 60:
                    self._sl_cooldown[sym] = cd_time
                    active += 1
            if active:
                log.info(f"Восстановлено {active} кулдаунов из БД")
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

    async def update_pairs(self):
        try:
            if cfg.WHITELIST:
                state.pairs = list(cfg.WHITELIST)
            else:
                symbols = await self.ex.get_top_symbols(cfg.TOP_N_PAIRS)
                state.pairs = [s for s in symbols if s not in cfg.BLACKLIST]
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
            return

        signals.sort(key=lambda s: s.score, reverse=True)
        qualified = [s for s in signals if s.score >= cfg.MIN_SCORE]
        skipped   = len(signals) - len(qualified)
        if skipped:
            log.info(f"Отфильтровано по MIN_SCORE ({cfg.MIN_SCORE}): {skipped} сигналов")
        if not qualified:
            log.info(f"Нет сигналов с достаточным score. Причины отсева: {diag}")
            if self._scan_count % 4 == 1:
                await self._notify(
                    f"🔍 Скан: {len(state.pairs)} пар — {len(signals)} сигналов ниже MIN_SCORE {cfg.MIN_SCORE}\n"
                    f"📊 Фильтры: {diag}\n"
                    f"Следующий через 15 мин"
                )
            return

        # Take only top 3 by score
        qualified = qualified[:cfg.MAX_POSITIONS]
        top = "\n".join(f"• {s.symbol} {s.side} ⭐{s.score}" for s in qualified)
        await self._notify(f"🔍 Найдено <b>{len(qualified)}</b> сигналов (топ по score):\n{top}")

        # BTC trend filter: fetch BTC once before handling signals
        btc_bias = "NEUTRAL"
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

        for sig in qualified:
            can, _ = state.can_trade(cfg.MAX_DAILY_LOSS, cfg.MAX_POSITIONS, cfg.MAX_DAILY_TRADES)
            if not can:
                break
            # BTC filter: skip signals against BTC trend
            if btc_bias == "DOWN" and sig.side == "LONG":
                log.info(f"BTC падает ({btc_bias}) — пропуск LONG {sig.symbol}")
                continue
            if btc_bias == "UP" and sig.side == "SHORT":
                log.info(f"BTC растёт ({btc_bias}) — пропуск SHORT {sig.symbol}")
                continue
            # Correlation filter: max 2 bot-opened positions in same direction
            same_dir = sum(1 for p in state.positions.values() if p.side == sig.side and p.sl > 0)
            if same_dir >= 2:
                log.info(f"Корреляция: пропуск {sig.symbol} {sig.side} (уже {same_dir} в том же направлении)")
                continue
            await self._handle(sig)

    async def _analyze(self, symbol: str):
        try:
            d1      = parse_klines(await self.ex.get_klines(symbol, cfg.TREND_TF,  limit=250))
            h4      = parse_klines(await self.ex.get_klines(symbol, cfg.H4_TF,     limit=150))
            h1      = parse_klines(await self.ex.get_klines(symbol, cfg.SIGNAL_TF, limit=100))
            funding = await self.ex.get_funding_rate(symbol)

            # Funding rate extreme alert
            if funding > 0.1 or funding < -0.1:
                log.warning(f"⚠️ Экстремальный фандинг {symbol}: {funding:.4f}%")

            return analyze(symbol, d1, h4, h1, funding, cfg)
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
                    against = (sig.side == "LONG"  and cur_price < sig.entry * 0.992) or \
                              (sig.side == "SHORT" and cur_price > sig.entry * 1.008)
                    if against:
                        drift = abs(cur_price - sig.entry) / sig.entry * 100
                        log.info(f"{sig.symbol}: цена ушла против сигнала на {drift:.2f}% — тихий пропуск")
                        if drift > 3.0:
                            self._set_cooldown(sig.symbol)
                        return  # Silent — no notification to avoid confusion
                    sig.entry = cur_price  # Update to current price before notify
                    # Recalculate TP from new entry (SL is structural, stays fixed)
                    sld = abs(sig.entry - sig.sl)
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

            # Auto-leverage based on balance tiers
            leverage = cfg.LEVERAGE
            if cfg.AUTO_LEVERAGE:
                if balance < 100:
                    leverage = 10
                elif balance < 500:
                    leverage = 7
                elif balance < 2000:
                    leverage = 5
                else:
                    leverage = 3
                log.info(f"Авто-плечо: баланс {balance:.2f} → x{leverage}")

            risk_usdt = balance * cfg.RISK_PER_TRADE / 100
            if risk_usdt > cfg.MAX_RISK_USDT:
                log.info(f"risk_usdt {risk_usdt:.2f} > MAX_RISK_USDT {cfg.MAX_RISK_USDT} — обрезаем")
                risk_usdt = cfg.MAX_RISK_USDT
            sl_pct = abs(sig.entry - sig.sl) / sig.entry
            if sl_pct == 0:
                log.warning(f"{sig.symbol}: sl_pct=0, пропуск")
                return
            qty = (risk_usdt / sl_pct) / sig.entry
            notional = qty * sig.entry
            # MIN_POSITION_USDT is margin — notional minimum = margin * leverage
            min_notional = cfg.MIN_POSITION_USDT * leverage
            max_notional = max(balance * 0.3 * leverage, min_notional)
            if notional > max_notional:
                log.warning(f"Позиция {sig.symbol}: {notional:.2f} > {max_notional:.2f} — обрезаем")
                qty = max_notional / sig.entry
                risk_usdt = qty * sig.entry * sl_pct
            min_qty = min_notional / sig.entry
            if qty < min_qty:
                qty = min_qty
                log.info(f"qty увеличен до минимума {min_notional:.2f} USDT (маржа {cfg.MIN_POSITION_USDT}×{leverage}) для {sig.symbol}")
            qty = round(qty, 3)
            # After rounding, notional may dip below minimum — correct with ceiling
            if qty * sig.entry < min_notional:
                qty = math.ceil(min_notional / sig.entry * 1000) / 1000
            if qty <= 0:
                log.warning(f"{sig.symbol}: qty=0, пропуск")
                return

            # Pre-check available margin to avoid "Insufficient margin" rejection
            required_margin = min_notional / leverage
            try:
                avail_margin = await self.ex.get_available_margin()
                if avail_margin > 0 and avail_margin < required_margin * 1.05:
                    log.warning(
                        f"{sig.symbol}: недостаточно свободной маржи "
                        f"{avail_margin:.2f} < {required_margin:.2f} USDT — пропуск"
                    )
                    await self._notify(
                        f"⚠️ <b>{sig.symbol}</b> пропущен\n"
                        f"Недостаточно маржи: {avail_margin:.2f} USDT\n"
                        f"Требуется: {required_margin:.2f} USDT (мин. позиция)"
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
                    against = (sig.side == "LONG"  and cur_price < sig.entry * 0.992) or \
                              (sig.side == "SHORT" and cur_price > sig.entry * 1.008)
                    if against:
                        drift = abs(cur_price - sig.entry) / sig.entry * 100
                        log.info(f"{sig.symbol}: цена ушла против сигнала на {drift:.2f}% — пропуск")
                        if drift > 3.0:
                            self._set_cooldown(sig.symbol)
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
            await self.ex.set_leverage(sig.symbol, leverage)
            side  = "BUY" if sig.side == "LONG" else "SELL"
            order = await self.ex.place_order(sig.symbol, side, qty, position_side=sig.side)
            if order.get("code") != 0:
                log.error(f"Ордер входа отклонён {sig.symbol}: {order}")
                await self._notify(f"❌ Вход отклонён <b>{sig.symbol}</b>: {order.get('msg', '')}")
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
                            min_notional_check = cfg.MIN_POSITION_USDT * leverage
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
                    self._set_cooldown(sig.symbol)
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
                self._set_cooldown(sig.symbol)
                return
            if not sl_id:
                log.warning(f"SL выставлен (code=0) но orderId не получен {sig.symbol} — отмена SL позже недоступна")

            tp_order = await self.ex.place_take_profit(sig.symbol, side, qty, sig.tp3)
            tp_id    = str(tp_order.get("data", {}).get("orderId", ""))
            if tp_order.get("code") != 0:
                log.warning(f"TP не выставился {sig.symbol}: {tp_order} — позиция без TP")

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
            await self._notify(f"❌ Ошибка входа {sig.symbol}: {e}")

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

    async def _monitor_inner(self):
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
                    pnl = (price - pos.entry) * pos.qty if pos.side == "LONG" \
                          else (pos.entry - price) * pos.qty
                    state.total_pnl    += pnl
                    state.day.pnl_usdt += pnl
                    result = "WIN" if pnl > 0 else "LOSS"
                    if pnl > 0:
                        state.day.wins += 1
                        state.day.loss_streak = 0
                    else:
                        state.day.losses += 1
                        state.day.loss_streak += 1
                        state.day.paused_until = datetime.utcnow() + timedelta(minutes=cfg.PAUSE_AFTER_LOSS_MIN)
                    try:
                        from core import db
                        db.save_trade(pos, price, pnl, result)
                    except Exception as e:
                        log.error(f"{symbol}: ошибка сохранения сделки в БД: {e}")
                    del state.positions[symbol]
                    db.delete_open_position(symbol)
                    # Set cooldown if closed at a loss (likely SL hit)
                    if pnl <= 0:
                        self._set_cooldown(symbol)
                    sign = "+" if pnl >= 0 else ""
                    await self._notify(
                        f"{'✅ WIN' if pnl > 0 else '❌ LOSS'} | {symbol} {pos.side}\n"
                        f"Закрыто на бирже | Цена: <code>{price:.4f}</code>\n"
                        f"PnL: <code>{sign}{pnl:.2f} USDT</code>"
                    )
        except Exception as e:
            log.error(f"monitor sync: {e}")

        # Clean up stale sl_cooldown entries (older than 2x cooldown window)
        cutoff = datetime.utcnow() - timedelta(minutes=SL_COOLDOWN_MIN * 2)
        self._sl_cooldown = {s: t for s, t in self._sl_cooldown.items() if t > cutoff}

        for symbol, pos in list(state.positions.items()):
            try:
                ticker = await self.ex.get_ticker(symbol)
                if not ticker:
                    continue
                price = float(ticker.get("lastPrice", pos.entry))

                # Breakeven trigger — skip for exchange-synced positions (tp1/sl unknown)
                if not pos.be_moved and pos.sl > 0:
                    be_triggered = False
                    if cfg.BE_TRIGGER_PCT > 0:
                        # Price moved BE_TRIGGER_PCT% from entry in profit direction
                        if pos.side == "LONG":
                            be_triggered = price >= pos.entry * (1 + cfg.BE_TRIGGER_PCT / 100)
                        else:
                            be_triggered = price <= pos.entry * (1 - cfg.BE_TRIGGER_PCT / 100)
                    elif pos.tp1 > 0:
                        # Fallback: TP1 trigger (only if TP1 is known)
                        be_triggered = (pos.side == "LONG" and price >= pos.tp1) or \
                                       (pos.side == "SHORT" and price <= pos.tp1)
                    if be_triggered:
                        await self._move_be(pos)

                # TP2 → partial close — skip if tp2 unknown (synced position)
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
                pos.trail_price = price
                new_sl = _px(price * (1 - cfg.TRAIL_PCT / 100))
            else:
                if price >= pos.trail_price and pos.trail_price != 0:
                    return
                pos.trail_price = price
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
            log.info(f"Trail SL {pos.symbol} → {new_sl:.4f}")
            db.save_open_position(pos)
        except Exception as e:
            log.error(f"trail_sl {pos.symbol}: {e}")

    async def _partial_close(self, pos: Position, pct: float, label: str):
        try:
            qty = round(pos.qty * pct, 3)
            if qty <= 0:
                return
            await self.ex.close_position(pos.symbol, qty, pos.side)

            # Record PnL for the closed portion (use tp2 as fallback if ticker fails)
            close_price = pos.tp2
            try:
                ticker = await self.ex.get_ticker(pos.symbol)
                if ticker and float(ticker.get("lastPrice", 0)) > 0:
                    close_price = float(ticker["lastPrice"])
            except Exception as pe:
                log.warning(f"partial_close ticker {pos.symbol}: {pe}")
            partial_pnl = (close_price - pos.entry) * qty if pos.side == "LONG" \
                          else (pos.entry - close_price) * qty
            state.total_pnl    += partial_pnl
            state.day.pnl_usdt += partial_pnl
            try:
                from dataclasses import replace as dc_replace
                pos_snap = dc_replace(pos, qty=qty)
                db.save_trade(pos_snap, close_price, round(partial_pnl, 4),
                              "WIN" if partial_pnl > 0 else "LOSS")
            except Exception as pe:
                log.error(f"partial_close db.save_trade {pos.symbol}: {pe}")

            pos.qty    -= qty
            pos.tp2_hit = True
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

            sign = "+" if partial_pnl >= 0 else ""
            await self._notify(
                f"💚 <b>{label}</b> | {pos.symbol}\n"
                f"Закрыто {int(pct * 100)}% позиции | цена <code>{close_price:.4f}</code>\n"
                f"PnL частичный: <code>{sign}{partial_pnl:.2f} USDT</code>\n"
                f"Остаток {int((1-pct)*100)}% → TP3 <code>{pos.tp3:.4f}</code>"
            )
        except Exception as e:
            log.error(f"partial_close {pos.symbol}: {e}")

    async def _check_closed(self, pos: Position, price: float):
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

        pnl = (price - pos.entry) * pos.qty if pos.side == "LONG" \
              else (pos.entry - price) * pos.qty
        state.total_pnl    += pnl
        state.day.pnl_usdt += pnl
        result = "WIN" if pnl > 0 else "LOSS"

        if pnl > 0:
            state.day.wins       += 1
            state.day.loss_streak = 0
        else:
            state.day.losses     += 1
            state.day.loss_streak += 1
            state.day.paused_until = datetime.utcnow() + timedelta(minutes=cfg.PAUSE_AFTER_LOSS_MIN)

        # Save to DB
        try:
            db.save_trade(pos, price, pnl, result)
        except Exception as e:
            log.error(f"db.save_trade: {e}")

        del state.positions[pos.symbol]
        db.delete_open_position(pos.symbol)

        # Cooldown after SL hit — don't re-enter same symbol for 1 hour
        if sl_hit:
            self._set_cooldown(pos.symbol)

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
        await self._notify(
            f"📊 <b>Недельный отчёт</b>\n\n"
            f"Сделок: {s['total']}  |  WR: {s['wr']}%\n"
            f"Прибыльных: {s['wins']}  |  Убыточных: {s['total'] - s['wins']}\n"
            f"PnL за 7 дней: <code>{sign}{s['pnl']:.2f} USDT</code>"
        )

    async def monthly_report(self):
        s    = db.get_stats(days=30)
        sign = "+" if s["pnl"] >= 0 else ""
        await self._notify(
            f"🗓 <b>Месячный отчёт</b>\n\n"
            f"Сделок: {s['total']}  |  WR: {s['wr']}%\n"
            f"Прибыльных: {s['wins']}  |  Убыточных: {s['total'] - s['wins']}\n"
            f"PnL за 30 дней: <code>{sign}{s['pnl']:.2f} USDT</code>"
        )
