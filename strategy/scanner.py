import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile

from core.config import cfg
from core.state import Position, state
from core import db
from exchange.bingx import BingXClient
from strategy.strategy.gerchik import Signal, analyze, parse_klines

log = logging.getLogger("scanner")

_SCANNING = False   # module-level lock — shared by ALL Scanner instances


SL_COOLDOWN_MIN = 60  # minutes to skip a symbol after SL hit


class Scanner:
    def __init__(self, exchange: BingXClient, bot: Bot):
        self.ex          = exchange
        self.bot         = bot
        self._scan_count = 0
        self._sl_cooldown: dict = {}  # symbol → datetime of last SL hit

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
            log.info(f"Пар: {len(state.pairs)}")
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
        if not state.pairs:
            await self.update_pairs()
        can, reason = state.can_trade(cfg.MAX_DAILY_LOSS, cfg.MAX_POSITIONS, cfg.MAX_DAILY_TRADES)
        if not can:
            log.info(f"Пропуск скана: {reason}")
            return
        log.info(f"Сканирую {len(state.pairs)} пар...")
        signals = []
        for i in range(0, len(state.pairs), cfg.SCAN_BATCH_SIZE):
            batch = state.pairs[i:i + cfg.SCAN_BATCH_SIZE]
            now = datetime.utcnow()
            tasks = [
                self._analyze(s) for s in batch
                if s not in state.positions and s not in state.pending
                and (s not in self._sl_cooldown or
                     (now - self._sl_cooldown[s]).total_seconds() > SL_COOLDOWN_MIN * 60)
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
        if not signals:
            log.info("Сигналов нет")
            # Notify only every 4th scan (~1 hour) to avoid spam
            if self._scan_count % 4 == 1:
                await self._notify(
                    f"🔍 Скан: {len(state.pairs)} пар — сигналов нет\n"
                    f"Следующий через 15 мин"
                )
            return

        signals.sort(key=lambda s: s.score, reverse=True)
        qualified = [s for s in signals if s.score >= cfg.MIN_SCORE]
        skipped   = len(signals) - len(qualified)
        if skipped:
            log.info(f"Отфильтровано по MIN_SCORE ({cfg.MIN_SCORE}): {skipped} сигналов")
        if not qualified:
            log.info("Нет сигналов с достаточным score")
            if self._scan_count % 4 == 1:
                await self._notify(
                    f"🔍 Скан: {len(state.pairs)} пар — {len(signals)} сигналов ниже MIN_SCORE {cfg.MIN_SCORE}\n"
                    f"Следующий через 15 мин"
                )
            return

        # Take only top 3 by score
        qualified = qualified[:cfg.MAX_POSITIONS]
        top = "\n".join(f"• {s.symbol} {s.side} ⭐{s.score}" for s in qualified)
        await self._notify(f"🔍 Найдено <b>{len(qualified)}</b> сигналов (топ по score):\n{top}")

        for sig in qualified:
            can, _ = state.can_trade(cfg.MAX_DAILY_LOSS, cfg.MAX_POSITIONS, cfg.MAX_DAILY_TRADES)
            if not can:
                break
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
            await self._enter(sig)
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

    async def _enter(self, sig: Signal, confirmed: bool = False):
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
            max_notional = max(balance * 0.3, cfg.MIN_POSITION_USDT)
            if notional > max_notional:
                log.warning(f"Позиция {sig.symbol}: {notional:.2f} > {max_notional:.2f} — обрезаем")
                qty = max_notional / sig.entry
                risk_usdt = qty * sig.entry * sl_pct
            min_qty = cfg.MIN_POSITION_USDT / sig.entry
            if qty < min_qty:
                qty = min_qty
                log.info(f"qty увеличен до минимума {cfg.MIN_POSITION_USDT} USDT для {sig.symbol}")
            qty = round(qty, 3)
            if qty <= 0:
                log.warning(f"{sig.symbol}: qty=0, пропуск")
                return

            # Staleness check: skip only if price moved AGAINST the signal
            try:
                ticker = await self.ex.get_ticker(sig.symbol)
                cur_price = float(ticker.get("lastPrice", sig.entry))
                against = (sig.side == "LONG"  and cur_price < sig.entry * 0.992) or \
                          (sig.side == "SHORT" and cur_price > sig.entry * 1.008)
                if against:
                    drift = abs(cur_price - sig.entry) / sig.entry * 100
                    log.info(f"{sig.symbol}: цена ушла против сигнала на {drift:.2f}% — пропуск")
                    await self._notify(
                        f"⏭ <b>{sig.symbol}</b> пропущен\n"
                        f"Цена ушла против сигнала: {drift:.1f}%\n"
                        f"Сигнал: <code>{sig.entry:.6f}</code> → Сейчас: <code>{cur_price:.6f}</code>"
                    )
                    return
                # Use current price as actual entry
                sig.entry = cur_price
            except Exception as e:
                log.warning(f"{sig.symbol}: не удалось получить текущую цену перед входом — используется цена сигнала: {e}")

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
                            break
            except Exception as pe:
                log.warning(f"Не удалось получить реальный qty {sig.symbol}: {pe}")

            sl_order = await self.ex.place_stop_loss(sig.symbol, side, qty, sig.sl)
            sl_id    = str(sl_order.get("data", {}).get("orderId", ""))
            if sl_order.get("code") != 0:
                err_code = sl_order.get("code", "?")
                err_msg  = sl_order.get("msg", str(sl_order))
                log.error(f"SL не выставился {sig.symbol}: код {err_code} — аварийное закрытие")
                await self._notify(
                    f"⚠️ SL не выставился <b>{sig.symbol}</b> (код {err_code}) — закрываем"
                )
                try:
                    await self.ex.close_position(sig.symbol, qty, sig.side)
                except Exception as ce:
                    log.error(f"emergency close {sig.symbol}: {ce}")
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
                    # Set cooldown if closed at a loss (likely SL hit)
                    if pnl <= 0:
                        self._sl_cooldown[symbol] = datetime.utcnow()
                    sign = "+" if pnl >= 0 else ""
                    await self._notify(
                        f"{'✅ WIN' if pnl > 0 else '❌ LOSS'} | {symbol} {pos.side}\n"
                        f"Закрыто на бирже | Цена: <code>{price:.4f}</code>\n"
                        f"PnL: <code>{sign}{pnl:.2f} USDT</code>"
                    )
        except Exception as e:
            log.error(f"monitor sync: {e}")

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
                be_price = round(pos.entry + buffer, 8)
            else:
                be_price = round(pos.entry - buffer, 8)

            if pos.sl_order_id:
                await self.ex.cancel_order(pos.symbol, pos.sl_order_id)
            side = "BUY" if pos.side == "LONG" else "SELL"
            r = await self.ex.place_stop_loss(pos.symbol, side, pos.qty, be_price)
            pos.sl_order_id = str(r.get("data", {}).get("orderId", ""))
            pos.sl          = be_price
            pos.be_moved    = True
            pos.trail_price = be_price

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
                new_sl = round(price * (1 - cfg.TRAIL_PCT / 100), 8)
            else:
                if price >= pos.trail_price and pos.trail_price != 0:
                    return
                pos.trail_price = price
                new_sl = round(price * (1 + cfg.TRAIL_PCT / 100), 8)

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
            pos.sl_order_id = str(r.get("data", {}).get("orderId", ""))
            pos.sl = new_sl
            log.info(f"Trail SL {pos.symbol} → {new_sl:.4f}")
        except Exception as e:
            log.error(f"trail_sl {pos.symbol}: {e}")

    async def _partial_close(self, pos: Position, pct: float, label: str):
        try:
            qty = round(pos.qty * pct, 3)
            if qty <= 0:
                return
            await self.ex.close_position(pos.symbol, qty, pos.side)
            pos.qty    -= qty
            pos.tp2_hit = True
            # Re-place SL for remaining qty
            if pos.sl_order_id:
                await self.ex.cancel_order(pos.symbol, pos.sl_order_id)
                side = "BUY" if pos.side == "LONG" else "SELL"
                r = await self.ex.place_stop_loss(pos.symbol, side, pos.qty, pos.sl)
                pos.sl_order_id = str(r.get("data", {}).get("orderId", ""))
            await self._notify(
                f"💚 <b>{label}</b> | {pos.symbol}\n"
                f"Закрыто {int(pct * 100)}% позиции по <code>{pos.tp2:.4f}</code>"
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
            from datetime import timedelta
            state.day.paused_until = datetime.utcnow() + timedelta(minutes=cfg.PAUSE_AFTER_LOSS_MIN)

        # Save to DB
        try:
            db.save_trade(pos, price, pnl, result)
        except Exception as e:
            log.error(f"db.save_trade: {e}")

        del state.positions[pos.symbol]

        # Cooldown after SL hit — don't re-enter same symbol for 1 hour
        if sl_hit:
            self._sl_cooldown[pos.symbol] = datetime.utcnow()

        sign = "+" if pnl >= 0 else ""
        icon = "✅ WIN" if pnl > 0 else "❌ LOSS"
        reason = "TP3 🎯" if tp3_hit else "SL 🛑"
        await self._notify(
            f"{icon} | {pos.symbol} {pos.side}\n"
            f"{reason} | Цена: <code>{price:.4f}</code>\n"
            f"PnL: <code>{sign}{pnl:.2f} USDT</code>\n"
            f"Итого: <code>{sign}{state.total_pnl:.2f} USDT</code>"
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
