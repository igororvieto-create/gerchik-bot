import asyncio
import logging
from datetime import datetime
from aiogram import Dispatcher
from aiogram.filters import Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from core.config import cfg
from core.state import state

log = logging.getLogger("handlers")

def _auth(msg: Message) -> bool:
    result = str(msg.chat.id) == str(cfg.TELEGRAM_CHAT_ID)
    if not result:
        log.warning(f"Auth failed: msg.chat.id={msg.chat.id}, cfg={cfg.TELEGRAM_CHAT_ID!r}")
    return result

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статус"),    KeyboardButton(text="💰 Баланс")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="📋 Пары")],
            [KeyboardButton(text="🔍 Скан"),       KeyboardButton(text="📈 Отчёт")],
            [KeyboardButton(text="⏸ Пауза"),      KeyboardButton(text="▶️ Продолжить")],
            [KeyboardButton(text="🤖 Авто"),       KeyboardButton(text="✋ Ручной")],
            [KeyboardButton(text="❌ Закрыть всё")],
        ],
        resize_keyboard=True,
        persistent=True
    )

async def cmd_ping(msg: Message):
    await msg.answer(
        f"🏓 Pong!\nВаш chat ID: <code>{msg.chat.id}</code>\nНастроенный: <code>{cfg.TELEGRAM_CHAT_ID}</code>",
        parse_mode="HTML"
    )

async def cmd_start(msg: Message):
    if not _auth(msg): return
    await msg.answer(
        "👋 <b>Герчик Бот</b> — используй кнопки ниже",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

async def cmd_status(msg: Message):
    if not _auth(msg): return
    from exchange.bingx import BingXClient
    ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    try:
        positions = await ex.get_open_positions()
    except Exception as e:
        positions = []
        log.error(f"get_open_positions: {e}")
    finally:
        await ex.close()

    if not positions:
        await msg.answer("📭 Нет открытых позиций на BingX")
        return

    text = "📊 <b>Позиции на BingX:</b>\n\n"
    for p in positions:
        sym   = p.get("symbol", "?")
        side  = p.get("positionSide", "?")
        amt   = float(p.get("positionAmt", 0))
        entry = float(p.get("avgPrice", 0))
        pnl   = float(p.get("unrealizedProfit", 0))
        margin= float(p.get("initialMargin", 0))
        lev   = p.get("leverage", cfg.LEVERAGE)
        text += (f"<b>{sym}</b> {side}\n"
                 f"Объём: <code>{amt}</code> | Вход: <code>{entry:.4f}</code>\n"
                 f"Маржа: <code>{margin:.2f} USDT</code> x{lev}\n"
                 f"PnL: <code>{'+'if pnl>=0 else ''}{pnl:.2f} USDT</code>\n\n")
    await msg.answer(text, parse_mode="HTML")

async def cmd_balance(msg: Message):
    if not _auth(msg): return
    from exchange.bingx import BingXClient
    ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    bal = await ex.get_balance()
    await ex.close()
    state.current_balance = bal
    d = state.day
    wr = round(d.wins/d.trades*100) if d.trades else 0
    await msg.answer(
        f"💰 <b>{bal:.2f} USDT</b>\nСделок: {d.trades} | WR: {wr}%\nPnL: {'+' if d.pnl_usdt>0 else ''}{d.pnl_usdt:.2f} USDT\nИтого: {'+' if state.total_pnl>0 else ''}{state.total_pnl:.2f} USDT",
        parse_mode="HTML")

async def cmd_settings(msg: Message):
    if not _auth(msg): return
    paused_str = ""
    if state.day.paused_until:
        remaining = max(0, int((state.day.paused_until - datetime.utcnow()).total_seconds() // 60))
        paused_str = f"\n⏸ Пауза ещё <b>{remaining} мин</b>"
    await msg.answer(
        f"⚙️ <b>Настройки бота</b>\n\n"
        f"🔧 Режим: <code>{cfg.MODE}</code>\n"
        f"📊 Пар в списке: <code>{len(state.pairs)}</code>\n\n"
        f"💰 Риск на сделку: <code>{cfg.RISK_PER_TRADE}%</code>\n"
        f"⚡ Плечо: <code>x{cfg.LEVERAGE}</code>\n"
        f"🎯 Мин. R/R: <code>1:{cfg.MIN_RR}</code>\n"
        f"⭐ Мин. Score: <code>{cfg.MIN_SCORE}/100</code>\n\n"
        f"🛡 Макс. потеря/день: <code>{cfg.MAX_DAILY_LOSS}%</code>\n"
        f"📋 Макс. позиций: <code>{cfg.MAX_POSITIONS}</code>\n"
        f"🔄 Макс. сделок/день: <code>{cfg.MAX_DAILY_TRADES}</code>\n\n"
        f"📈 EMA D1: <code>{cfg.TREND_EMA_D1}</code> | H4: <code>{cfg.TREND_EMA_H4}</code>\n"
        f"📦 Мин. объём: <code>{cfg.VOLUME_MULT}×</code> MA{cfg.VOLUME_MA_PERIOD}\n"
        f"🔴 SL буфер: <code>{cfg.SL_BUFFER_PCT}%</code>"
        f"{paused_str}",
        parse_mode="HTML"
    )

async def cmd_pairs(msg: Message):
    if not _auth(msg): return
    n = len(state.pairs)
    await msg.answer(f"📋 Пар: <b>{n}</b>\n{' | '.join(state.pairs[:15])}{'...' if n>15 else ''}", parse_mode="HTML")

async def cmd_pause(msg: Message):
    if not _auth(msg): return
    state.paused = True
    await msg.answer("⏸ Пауза. Нажми ▶️ Продолжить для возобновления.")

async def cmd_resume(msg: Message):
    if not _auth(msg): return
    state.paused = False; state.day.paused_until = None
    await msg.answer("▶️ Торговля возобновлена")

async def cmd_setmode(msg: Message):
    if not _auth(msg): return
    args = msg.text.split() if msg.text.startswith("/") else []
    if len(args) >= 2:
        mode = args[1]
    elif "авто" in (msg.text or "").lower() or "auto" in (msg.text or "").lower():
        mode = "auto"
    elif "ручной" in (msg.text or "").lower() or "manual" in (msg.text or "").lower():
        mode = "manual"
    else:
        await msg.answer("/setmode auto | /setmode manual"); return
    if mode not in ("auto", "manual"):
        await msg.answer("/setmode auto | /setmode manual"); return
    cfg.MODE = mode
    await msg.answer(f"✅ Режим: <code>{cfg.MODE}</code>", parse_mode="HTML")

async def cmd_setrisk(msg: Message):
    if not _auth(msg): return
    args = msg.text.split()
    if len(args)<2:
        await msg.answer(f"Риск: {cfg.RISK_PER_TRADE}%\n/setrisk 0.5"); return
    try:
        v = float(args[1])
        if not 0.1<=v<=3.0: raise ValueError
        cfg.RISK_PER_TRADE = v
        await msg.answer(f"✅ Риск: <code>{v}%</code>", parse_mode="HTML")
    except: await msg.answer("Введи число 0.1–3.0")

async def cmd_setlev(msg: Message):
    if not _auth(msg): return
    args = msg.text.split()
    if len(args)<2:
        await msg.answer(f"Плечо: x{cfg.LEVERAGE}\n/setlev 10"); return
    try:
        v = int(args[1])
        if not 1<=v<=50: raise ValueError
        cfg.LEVERAGE = v
        await msg.answer(f"✅ Плечо: <code>x{v}</code>", parse_mode="HTML")
    except: await msg.answer("Введи число 1–50")

async def cmd_scan(msg: Message):
    if not _auth(msg): return
    n = len(state.pairs)
    await msg.answer(f"🔍 Сканирую {n} пар... (результат придёт отдельным сообщением)")

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

async def cmd_daily_report(msg: Message):
    if not _auth(msg): return
    d = state.day
    wr = round(d.wins/d.trades*100) if d.trades else 0
    await msg.answer(
        f"📈 <b>Отчёт за день</b> {d.date}\n\n"
        f"Сделок: {d.trades} | WR: {wr}%\n"
        f"Победы: {d.wins} | Убытки: {d.losses}\n"
        f"PnL: {'+' if d.pnl_usdt>=0 else ''}{d.pnl_usdt:.2f} USDT\n"
        f"Всего PnL: {'+' if state.total_pnl>=0 else ''}{state.total_pnl:.2f} USDT\n"
        f"Баланс: {state.current_balance:.2f} USDT",
        parse_mode="HTML"
    )

async def cmd_closeall(msg: Message):
    if not _auth(msg): return
    from exchange.bingx import BingXClient
    ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    try:
        positions = await ex.get_open_positions()
    except Exception as e:
        await msg.answer(f"❌ Ошибка получения позиций: {e}")
        await ex.close()
        return
    if not positions:
        await msg.answer("Нет открытых позиций")
        await ex.close()
        return
    closed = []
    for p in positions:
        sym  = p.get("symbol")
        amt  = float(p.get("positionAmt", 0))
        side = p.get("positionSide", "LONG")
        try:
            await ex.close_position(sym, abs(amt), side)
            closed.append(sym)
            state.positions.pop(sym, None)
        except Exception as e:
            log.error(f"closeall {sym}: {e}")
    await ex.close()
    await msg.answer(f"✅ Закрыто: {', '.join(closed) or 'ничего'}")

async def cmd_help(msg: Message):
    if not _auth(msg): return
    await msg.answer(
        "<b>Команды:</b>\n"
        "/status — открытые позиции\n"
        "/balance — баланс и статистика\n"
        "/settings — текущие настройки\n"
        "/pairs — список пар\n"
        "/scan — ручной скан\n"
        "/pause /resume — пауза/продолжить\n"
        "/setmode auto|manual — режим\n"
        "/setrisk 1.0 — риск %\n"
        "/setlev 5 — плечо\n"
        "/closeall — закрыть все позиции",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )

async def handle_misc(msg: Message):
    if not _auth(msg): return
    text = msg.text or ""

    # Button handlers
    if text == "📊 Статус":
        return await cmd_status(msg)
    if text == "💰 Баланс":
        return await cmd_balance(msg)
    if text == "⚙️ Настройки":
        return await cmd_settings(msg)
    if text == "📋 Пары":
        return await cmd_pairs(msg)
    if text == "🔍 Скан":
        return await cmd_scan(msg)
    if text == "📈 Отчёт":
        return await cmd_daily_report(msg)
    if text == "⏸ Пауза":
        return await cmd_pause(msg)
    if text == "▶️ Продолжить":
        return await cmd_resume(msg)
    if text == "🤖 Авто":
        cfg.MODE = "auto"
        await msg.answer("✅ Режим: <code>auto</code>", parse_mode="HTML"); return
    if text == "✋ Ручной":
        cfg.MODE = "manual"
        await msg.answer("✅ Режим: <code>manual</code>", parse_mode="HTML"); return
    if text == "❌ Закрыть всё":
        return await cmd_closeall(msg)

    # Confirm/skip signal handlers
    if text.startswith("/confirm_"):
        sym = text.replace("/confirm_","").replace("_","-").upper()
        if sym not in state.pending: await msg.answer(f"Сигнал {sym} не найден"); return
        pend = state.pending[sym]
        if datetime.utcnow() > pend["expires"]:
            state.pending.pop(sym,None); await msg.answer("⏰ Истёк"); return
        from exchange.bingx import BingXClient
        from strategy.scanner import Scanner
        ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
        await Scanner(ex, msg.bot)._enter(pend["signal"], confirmed=True); await ex.close()
    elif text.startswith("/skip_"):
        sym = text.replace("/skip_","").replace("_","-").upper()
        state.pending.pop(sym,None); await msg.answer(f"⏭ Пропущен: {sym}")

def register_handlers(dp: Dispatcher):
    dp.message.register(cmd_ping,         Command("ping"))
    dp.message.register(cmd_start,        Command("start"))
    dp.message.register(cmd_status,       Command("status"))
    dp.message.register(cmd_balance,      Command("balance"))
    dp.message.register(cmd_settings,     Command("settings"))
    dp.message.register(cmd_pairs,        Command("pairs"))
    dp.message.register(cmd_pause,        Command("pause"))
    dp.message.register(cmd_resume,       Command("resume"))
    dp.message.register(cmd_setmode,      Command("setmode"))
    dp.message.register(cmd_setrisk,      Command("setrisk"))
    dp.message.register(cmd_setlev,       Command("setlev"))
    dp.message.register(cmd_scan,         Command("scan"))
    dp.message.register(cmd_daily_report, Command("report"))
    dp.message.register(cmd_closeall,     Command("closeall"))
    dp.message.register(cmd_help,         Command("help"))
    dp.message.register(handle_misc)
