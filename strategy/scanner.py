import asyncio, logging
from datetime import datetime, timedelta
from aiogram import Bot
from core.config import cfg
from core.state import Position, state
from exchange.bingx import BingXClient
from strategy.gerchik import Signal, analyze, parse_klines


log = logging.getLogger("scanner")

class Scanner:
    def __init__(self, exchange: BingXClient, bot: Bot):
        self.ex = exchange
        self.bot = bot

    async def _notify(self, text):
        try:
            await self.bot.send_message(cfg.TELEGRAM_CHAT_ID, text, parse_mode="HTML")
        except Exception as e:
            log.error(f"TG: {e}")

    async def update_pairs(self):
        try:
            if cfg.WHITELIST:
                state.pairs = cfg.WHITELIST
            else:
                symbols = await self.ex.get_top_symbols(cfg.TOP_N_PAIRS)
                state.pairs = [s for s in symbols if s not in cfg.BLACKLIST]
            log.info(f"Пар: {len(state.pairs)}")
            await self._notify(f"🔄 Пары обновлены: <b>{len(state.pairs)}</b> | Режим: <code>{cfg.MODE}</code>")
        except Exception as e:
            log.error(f"update_pairs: {e}")

    async def scan_all(self):
        if not state.pairs:
            await self.update_pairs()
        can, reason = state.can_trade(cfg.MAX_DAILY_LOSS, cfg.MAX_POSITIONS, cfg.MAX_DAILY_TRADES)
        if not can:
            log.info(f"Пропуск: {reason}"); return
        log.info(f"Сканирую {len(state.pairs)} пар...")
        signals = []
        for i in range(0, len(state.pairs), cfg.SCAN_BATCH_SIZE):
            batch = state.pairs[i:i+cfg.SCAN_BATCH_SIZE]
            tasks = [self._analyze(s) for s in batch if s not in state.positions and s not in state.pending]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Signal):
                    signals.append(r)
            if i+cfg.SCAN_BATCH_SIZE < len(state.pairs):
                await asyncio.sleep(cfg.SCAN_BATCH_DELAY)
        if not signals:
            log.info("Сигналов нет"); return
        signals.sort(key=lambda s: s.score, reverse=True)
        for sig in signals:
            can, _ = state.can_trade(cfg.MAX_DAILY_LOSS, cfg.MAX_POSITIONS, cfg.MAX_DAILY_TRADES)
            if not can: break
            if sig.score < cfg.MIN_SCORE: continue
            await self._handle(sig)

    async def _analyze(self, symbol):
        try:
            d1 = parse_klines(await self.ex.get_klines(symbol, cfg.TREND_TF, limit=250))
            h4 = parse_klines(await self.ex.get_klines(symbol, cfg.H4_TF, limit=150))
            h1 = parse_klines(await self.ex.get_klines(symbol, cfg.SIGNAL_TF, limit=100))
            funding = await self.ex.get_funding_rate(symbol)
            return analyze(symbol, d1, h4, h1, funding, cfg)
        except Exception as e:
            log.error(f"analyze {symbol}: {e}")
            return None

    async def _handle(self, sig):
        if cfg.MODE == "auto":
            await self._enter(sig)
        else:
            state.pending[sig.symbol] = {"signal": sig, "expires": datetime.utcnow()+timedelta(seconds=cfg.CONFIRM_TIMEOUT_SEC)}
            await self._notify(f"🔔 <b>СЕТАП</b>\n\n{sig.reason}\n\n/confirm_{sig.symbol.replace('-','_')} — войти\n/skip_{sig.symbol.replace('-','_')} — пропустить")

    async def _enter(self, sig, confirmed=False):
        try:
            balance = await self.ex.get_balance()
            if balance <= 0: return
            state.current_balance = balance
            risk_usdt = balance * cfg.RISK_PER_TRADE / 100
            sl_pct = abs(sig.entry - sig.sl) / sig.entry
            if sl_pct == 0: return
            qty = round((risk_usdt / sl_pct) / sig.entry, 3)
            if qty <= 0: return
            await self.ex.set_leverage(sig.symbol, cfg.LEVERAGE)
            side = "BUY" if sig.side == "LONG" else "SELL"
            order = await self.ex.place_order(sig.symbol, side, qty, position_side=sig.side)
            order_id = str(order.get("data",{}).get("orderId",""))
            sl_order = await self.ex.place_stop_loss(sig.symbol, side, qty, sig.sl)
            sl_id = str(sl_order.get("data",{}).get("orderId",""))
            tp_order = await self.ex.place_take_profit(sig.symbol, side, qty, sig.tp3)
            tp_id = str(tp_order.get("data",{}).get("orderId",""))
            pos = Position(symbol=sig.symbol, side=sig.side, entry=sig.entry, sl=sig.sl, tp1=sig.tp1, tp2=sig.tp2, tp3=sig.tp3, qty=qty, risk_usdt=risk_usdt, order_id=order_id, sl_order_id=sl_id, tp_order_id=tp_id, pattern=sig.pattern, tf=sig.tf, rr=sig.rr, score=sig.score)
            state.positions[sig.symbol] = pos
            state.day.trades += 1
            state.pending.pop(sig.symbol, None)
            await self._notify(f"{'🤖' if not confirmed else '✅'} <b>ВХОД</b> | {sig.symbol} {sig.side}\n🕯 {sig.pattern} | ⭐{sig.score}/100\n🟡 <code>{sig.entry:.4f}</code> 🔴 SL: <code>{sig.sl:.4f}</code>\n🟢 TP2: <code>{sig.tp2:.4f}</code> TP3: <code>{sig.tp3:.4f}</code>\n💰 Риск: <code>{risk_usdt:.2f} USDT</code> x{cfg.LEVERAGE}")
        except Exception as e:
            log.error(f"enter {sig.symbol}: {e}")
            await self._notify(f"❌ Ошибка входа {sig.symbol}: {e}")

    async def monitor_positions(self):
        for symbol, pos in list(state.positions.items()):
            try:
                ticker = await self.ex.get_ticker(symbol)
                if not ticker: continue
                price = float(ticker.get("lastPrice", pos.entry))
                if not pos.be_moved:
                    if (pos.side=="LONG" and price>=pos.tp1) or (pos.side=="SHORT" and price<=pos.tp1):
                        await self._move_be(pos)
                if pos.be_moved and not pos.tp2_hit:
                    if (pos.side=="LONG" and price>=pos.tp2) or (pos.side=="SHORT" and price<=pos.tp2):
                        await self._partial_close(pos, cfg.TP2_CLOSE_PCT, "TP2")
                await self._check_closed(pos, price)
            except Exception as e:
                log.error(f"monitor {symbol}: {e}")

    async def _move_be(self, pos):
        try:
            if pos.sl_order_id:
                await self.ex.cancel_order(pos.symbol, pos.sl_order_id)
            side = "BUY" if pos.side=="LONG" else "SELL"
            r = await self.ex.place_stop_loss(pos.symbol, side, pos.qty, pos.entry)
            pos.sl_order_id = str(r.get("data",{}).get("orderId",""))
            pos.sl = pos.entry; pos.be_moved = True
            await self._notify(f"🔄 <b>БЕЗУБЫТОК</b> | {pos.symbol}\nSL → <code>{pos.entry:.4f}</code>")
        except Exception as e:
            log.error(f"move_be {pos.symbol}: {e}")

    async def _partial_close(self, pos, pct, label):
        try:
            qty = round(pos.qty*pct, 3)
            if qty <= 0: return
            await self.ex.close_position(pos.symbol, qty, pos.side)
            pos.qty -= qty; pos.tp2_hit = True
            await self._notify(f"✅ <b>{label}</b> | {pos.symbol} | Закрыто {int(pct*100)}%")
        except Exception as e:
            log.error(f"partial_close {pos.symbol}: {e}")

    async def _check_closed(self, pos, price):
        sl_hit  = (pos.side=="LONG" and price<=pos.sl)  or (pos.side=="SHORT" and price>=pos.sl)
        tp3_hit = (pos.side=="LONG" and price>=pos.tp3) or (pos.side=="SHORT" and price<=pos.tp3)
        if not sl_hit and not tp3_hit: return
        pnl = (price-pos.entry)*pos.qty if pos.side=="LONG" else (pos.entry-price)*pos.qty
        state.total_pnl += pnl; state.day.pnl_usdt += pnl
        if pnl > 0:
            state.day.wins += 1; state.day.loss_streak = 0
        else:
            state.day.losses += 1; state.day.loss_streak += 1
            state.day.paused_until = datetime.utcnow()+timedelta(minutes=cfg.PAUSE_AFTER_LOSS_MIN)
        del state.positions[pos.symbol]
        await self._notify(f"{'✅ WIN' if pnl>0 else '❌ LOSS'} | {pos.symbol} {pos.side}\n{'TP3 🎯' if tp3_hit else 'SL 🛑'} | Цена: <code>{price:.4f}</code>\nPnL: <code>{'+' if pnl>0 else ''}{pnl:.2f} USDT</code>")

    async def daily_report(self):
        d = state.day
        wr = round(d.wins/d.trades*100) if d.trades else 0
        await self._notify(f"📋 <b>Отчёт</b> {d.date}\nСделок: {d.trades} | WR: {wr}%\nPnL: {'+' if d.pnl_usdt>0 else ''}{d.pnl_usdt:.2f} USDT\nИтого: {'+' if state.total_pnl>0 else ''}{state.total_pnl:.2f} USDT")
