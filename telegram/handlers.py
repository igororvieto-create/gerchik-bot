import asyncio
import logging
from datetime import datetime
from aiogram import Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from core.config import cfg
from core.state import state

log = logging.getLogger("handlers")

def _auth(msg: Message) -> bool:
    return str(msg.chat.id) == str(cfg.TELEGRAM_CHAT_ID)

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статус"),     KeyboardButton(text="💰 Баланс")],
            [KeyboardButton(text="⚙️ Настройки"),  KeyboardButton(text="📋 Пары")],
            [KeyboardButton(text="🔍 Скан"),        KeyboardButton(text="📈 Отчёт")],
            [KeyboardButton(text="⏸ Пауза"),       KeyboardButton(text="▶️ Продолжить")],
            [KeyboardButton(text="🤖 Авто"),        KeyboardButton(text="✋ Ручной")],
            [KeyboardButton(text="❌ Закрыть всё")],
        ],
        resize_keyboard=True,
        persistent=True,
    )

async def cmd_ping(msg: Message):
    await msg.answer("🟢 Бот работает", reply_markup=main_keyboard())

async def cmd_help(msg: Message):
    if not _auth(msg): return
    await msg.answer(
        "<b>Команды:</b>\n"
        "/status — открытые позиции\n"
        "/balance — баланс и статистика\n"
        "/pairs — торговые пары\n"
        "/scan — запустить сканирование\n"
        "/settings — все настройки\n"
        "/pause — пауза\n"
        "/resume — возобновить\n"
        "/setmode auto|manual — режим\n"
        "/setrisk 1.0 — риск %\n"
        "/setlev 5 — плечо\n"
        "/closeall — закрыть все позиции",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

async def cmd_status(msg: Message):
    if not _auth(msg): return
    from exchange.bingx import BingXClient
    ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    try:
        live = await ex.get_open_positions()
    except Exception as e:
        log.error(f"cmd_status get_open_positions: {e}")
        live = []
    finally:
        await ex.close()

    if not live and not state.positions:
        await msg.answer("📭 Нет открытых позиций", reply_markup=main_keyboard())
        return

    text = "📊 <b>Открытые позиции:</b>\n\n"
    if live:
        for p in live:
            sym  = p.get("symbol", "?")
            side = p.get("positionSide", "?")
            amt  = float(p.get("positionAmt", 0))
            eprice = float(p.get("avgPrice", 0))
            upnl = float(p.get("unrealizedProfit", 0))
            sign = "+" if upnl >= 0 else ""
            text += f"<b>{sym}</b> {side}\nКол-во: {abs(amt)}\nВход: <code>{eprice:.4f}</code>\nPnL: {sign}{upnl:.2f} USDT\n\n"
    else:
        for sym, p in state.positions.items():
            text += f"<b>{sym}</b> {p.side} {'✅BE' if p.be_moved else '⏳'}\nВход: <code>{p.entry:.4f}</code> SL: <code>{p.sl:.4f}</code>\nTP3: <code>{p.tp3:.4f}</code>\n\n"

    await msg.answer(text, parse_mode="HTML", reply_markup=main_keyboard())

async def cmd_balance(msg: Message):
    if not _auth(msg): return
    from exchange.bingx import BingXClient
    ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    bal = await ex.get_balance()
    await ex.close()
    state.current_balance = bal
    d = state.day
    wr = round(d.wins / d.trades * 100) if d.trades else 0
    await msg.answer(
        f"💰 <b>{bal:.2f} USDT</b>\n"
        f"Сделок: {d.trades} | WR: {wr}%\n"
        f"PnL сегодня: {'+' if d.pnl_usdt >= 0 else ''}{d.pnl_usdt:.2f} USDT\n"
        f"Итого: {'+' if state.total_pnl >= 0 else ''}{state.total_pnl:.2f} USDT",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

async def cmd_pairs(msg: Message):
    if not _auth(msg): return
    n = len(state.pairs)
    await msg.answer(
        f"📋 Пар: <b>{n}</b>\n{' | '.join(state.pairs[:15])}{'...' if n > 15 else ''}",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

async def cmd_settings(msg: Message):
    if not _auth(msg): return
    status = "⏸ ПАУЗА" if state.paused else "▶️ Работает"
    await msg.answer(
        f"⚙️ <b>Настройки бота:</b>\n\n"
        f"Статус: {status}\n"
        f"Режим: <code>{cfg.MODE}</code>\n"
        f"Риск на сделку: <code>{cfg.RISK_PER_TRADE}%</code>\n"
        f"Плечо: <code>x{cfg.LEVERAGE}</code>\n"
        f"Мин. R/R: <code>{cfg.MIN_RR}</code>\n"
        f"Мин. оценка: <code>{cfg.MIN_SCORE}</code>\n"
        f"Макс. позиций: <code>{cfg.MAX_POSITIONS}</code>\n"
        f"Макс. сделок/день: <code>{cfg.MAX_DAILY_TRADES}</code>\n"
        f"Макс. убыток/день: <code>{cfg.MAX_DAILY_LOSS}%</code>\n"
        f"Объём (мульт.): <code>{cfg.VOLUME_MULT}x</code>\n"
        f"SL буфер: <code>{cfg.SL_BUFFER_PCT}%</code>\n"
        f"Фандинг макс LONG: <code>{cfg.FUNDING_MAX_LONG}%</code>\n"
        f"Фандинг макс SHORT: <code>{cfg.FUNDING_MAX_SHORT}%</code>\n\n"
        f"<i>/setmode auto|manual\n/setrisk 1.0\n/setlev 5</i>",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

async def cmd_report(msg: Message):
    if not _auth(msg): return
    d = state.day
    wr = round(d.wins / d.trades * 100) if d.trades else 0
    losses = d.trades - d.wins
    await msg.answer(
        f"📈 <b>Статистика за сегодня:</b>\n\n"
        f"Сделок: {d.trades}\n"
        f"Прибыльных: {d.wins} | Убыточных: {losses}\n"
        f"Винрейт: {wr}%\n"
        f"PnL: {'+' if d.pnl_usdt >= 0 else ''}{d.pnl_usdt:.2f} USDT\n"
        f"Серия потерь: {state.loss_streak if hasattr(state, 'loss_streak') else 0}",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )

async def cmd_pause(msg: Message):
    if not _auth(msg): return
    state.paused = True
    await msg.answer("⏸ Торговля на паузе.\n/resume — возобновить", reply_markup=main_keyboard())

async def cmd_resume(msg: Message):
    if not _auth(msg): return
    state.paused = False
    state.day.paused_until = None
    await msg.answer("▶️ Торговля возобновлена", reply_markup=main_keyboard())

async def cmd_setmode(msg: Message):
    if not _auth(msg): return
    args = msg.text.split()
    if len(args) < 2 or args[1] not in ("auto", "manual"):
        await msg.answer("/setmode auto | /setmode manual"); return
    cfg.MODE = args[1]
    await msg.answer(f"✅ Режим: <code>{cfg.MODE}</code>", parse_mode="HTML", reply_markup=main_keyboard())

async def cmd_setrisk(msg: Message):
    if not _auth(msg): return
    args = msg.text.split()
    if len(args) < 2:
        await msg.answer(f"Текущий риск: {cfg.RISK_PER_TRADE}%\nПример: /setrisk 0.5"); return
    try:
        v = float(args[1])
        if not 0.1 <= v <= 3.0: raise ValueError
        cfg.RISK_PER_TRADE = v
        await msg.answer(f"✅ Риск: <code>{v}%</code>", parse_mode="HTML", reply_markup=main_keyboard())
    except:
        await msg.answer("Введи число от 0.1 до 3.0")

async def cmd_setlev(msg: Message):
    if not _auth(msg): return
    args = msg.text.split()
    if len(args) < 2:
        await msg.answer(f"Текущее плечо: x{cfg.LEVERAGE}\nПример: /setlev 10"); return
    try:
        v = int(args[1])
        if not 1 <= v <= 50: raise ValueError
        cfg.LEVERAGE = v
        await msg.answer(f"✅ Плечо: <code>x{v}</code>", parse_mode="HTML", reply_markup=main_keyboard())
    except:
        await msg.answer("Введи число от 1 до 50")

async def cmd_scan(msg: Message):
    if not _auth(msg): return
    await msg.answer(f"🔍 Сканирую {len(state.pairs)} пар... (результат придёт отдельным сообщением)",
                     reply_markup=main_keyboard())

    async def _do_scan():
        try:
            from exchange.bingx import BingXClient
            from strategy.scanner import Scanner
            ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
            await Scanner(ex, msg.bot).scan_all()
            await ex.close()
        except Exception as e:
            log.error(f"cmd_scan bg error: {e}")

    asyncio.create_task(_do_scan())

async def cmd_closeall(msg: Message):
    if not _auth(msg): return
    from exchange.bingx import BingXClient
    ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    live = await ex.get_open_positions()
    if not live and not state.positions:
        await ex.close()
        await msg.answer("Нет открытых позиций", reply_markup=main_keyboard())
        return
    closed = []
    if live:
        for p in live:
            sym  = p.get("symbol")
            side = p.get("positionSide", "LONG")
            amt  = abs(float(p.get("positionAmt", 0)))
            if amt == 0: continue
            try:
                await ex.close_position(sym, amt, side)
                state.positions.pop(sym, None)
                closed.append(sym)
            except Exception as e:
                log.error(f"closeall {sym}: {e}")
    else:
        for sym, p in list(state.positions.items()):
            try:
                await ex.close_position(sym, p.qty, p.side)
                del state.positions[sym]
                closed.append(sym)
            except Exception as e:
                log.error(f"closeall {sym}: {e}")
    await ex.close()
    await msg.answer(f"✅ Закрыто: {', '.join(closed) or 'ничего'}", reply_markup=main_keyboard())

async def handle_misc(msg: Message):
    if not _auth(msg): return
    text = msg.text or ""

    # Handle keyboard button presses
    btn_map = {
        "📊 Статус":      cmd_status,
        "💰 Баланс":      cmd_balance,
        "⚙️ Настройки":  cmd_settings,
        "📋 Пары":        cmd_pairs,
        "🔍 Скан":        cmd_scan,
        "📈 Отчёт":       cmd_report,
        "⏸ Пауза":       cmd_pause,
        "▶️ Продолжить": cmd_resume,
        "🤖 Авто":        None,
        "✋ Ручной":      None,
        "❌ Закрыть всё": cmd_closeall,
    }
    if text in btn_map:
        fn = btn_map[text]
        if fn:
            await fn(msg)
        elif text == "🤖 Авто":
            cfg.MODE = "auto"
            await msg.answer("✅ Режим: <code>auto</code>", parse_mode="HTML", reply_markup=main_keyboard())
        elif text == "✋ Ручной":
            cfg.MODE = "manual"
            await msg.answer("✅ Режим: <code>manual</code>", parse_mode="HTML", reply_markup=main_keyboard())
        return

    # Handle confirm/skip commands
    if text.startswith("/confirm_"):
        sym = text.replace("/confirm_", "").replace("_", "-").upper()
        if sym not in state.pending:
            await msg.answer(f"Сигнал {sym} не найден"); return
        pend = state.pending[sym]
        if datetime.utcnow() > pend["expires"]:
            state.pending.pop(sym, None)
            await msg.answer("⏰ Время подтверждения истекло"); return
        from exchange.bingx import BingXClient
        from strategy.scanner import Scanner
        ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
        await Scanner(ex, msg.bot)._enter(pend["signal"], confirmed=True)
        await ex.close()
    elif text.startswith("/skip_"):
        sym = text.replace("/skip_", "").replace("_", "-").upper()
        state.pending.pop(sym, None)
        await msg.answer(f"⏭ Пропущен: {sym}")

def register_handlers(dp: Dispatcher):
    dp.message.register(cmd_ping,     Command("ping"))
    dp.message.register(cmd_help,     Command("help", "start"))
    dp.message.register(cmd_status,   Command("status"))
    dp.message.register(cmd_balance,  Command("balance"))
    dp.message.register(cmd_pairs,    Command("pairs"))
    dp.message.register(cmd_settings, Command("settings"))
    dp.message.register(cmd_report,   Command("report"))
    dp.message.register(cmd_pause,    Command("pause"))
    dp.message.register(cmd_resume,   Command("resume"))
    dp.message.register(cmd_setmode,  Command("setmode"))
    dp.message.register(cmd_setrisk,  Command("setrisk"))
    dp.message.register(cmd_setlev,   Command("setlev"))
    dp.message.register(cmd_scan,     Command("scan"))
    dp.message.register(cmd_closeall, Command("closeall"))
    dp.message.register(handle_misc)
