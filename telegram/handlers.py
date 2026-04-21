import asyncio
import logging
from datetime import datetime

from aiogram import Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
)

from core.config import cfg
from core.state import state

log = logging.getLogger("handlers")


def _auth(msg: Message) -> bool:
    return str(msg.chat.id) == str(cfg.TELEGRAM_CHAT_ID)


def _auth_cb(cb: CallbackQuery) -> bool:
    return str(cb.message.chat.id) == str(cfg.TELEGRAM_CHAT_ID)


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статус"),     KeyboardButton(text="💰 Баланс")],
            [KeyboardButton(text="⚙️ Настройки"),  KeyboardButton(text="📋 Пары")],
            [KeyboardButton(text="🔍 Скан"),        KeyboardButton(text="📈 Отчёт")],
            [KeyboardButton(text="📜 История"),     KeyboardButton(text="🏆 Топ пары")],
            [KeyboardButton(text="🔄 Безубыток"),    KeyboardButton(text="📉 Трейлинг")],
            [KeyboardButton(text="⏸ Пауза"),       KeyboardButton(text="▶️ Продолжить")],
            [KeyboardButton(text="🤖 Авто"),        KeyboardButton(text="✋ Ручной")],
            [KeyboardButton(text="❌ Закрыть всё")],
        ],
        resize_keyboard=True,
        persistent=True,
    )


# ------------------------------------------------------------------ /ping

async def cmd_ping(msg: Message):
    if not _auth(msg):
        return
    from datetime import datetime as dt
    pos_count = len(state.pairs)
    open_pos  = len(state.positions)
    bal_str   = f"{state.current_balance:.2f}" if state.current_balance else "?"
    can, reason = state.can_trade(
        __import__("core.config", fromlist=["cfg"]).cfg.MAX_DAILY_LOSS,
        __import__("core.config", fromlist=["cfg"]).cfg.MAX_POSITIONS,
        __import__("core.config", fromlist=["cfg"]).cfg.MAX_DAILY_TRADES,
    )
    status = "✅ Торгует" if can else f"⏸ {reason}"
    await msg.answer(
        f"🟢 <b>Бот работает</b>\n\n"
        f"Статус: {status}\n"
        f"Режим: <code>{cfg.MODE}</code>\n"
        f"Баланс: <code>{bal_str} USDT</code>\n"
        f"Позиций открыто: <code>{open_pos}</code>\n"
        f"Пар в мониторинге: <code>{pos_count}</code>\n"
        f"PnL сегодня: <code>{'+' if state.day.pnl_usdt >= 0 else ''}{state.day.pnl_usdt:.2f} USDT</code>",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def cmd_debug(msg: Message):
    if not _auth(msg):
        return
    from exchange.bingx import BingXClient
    import json
    ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    try:
        raw = await ex.get_balance_raw()
        bal = await ex.get_balance()
        text = (
            f"🔧 <b>Диагностика баланса</b>\n\n"
            f"Распознанный баланс: <code>{bal:.4f} USDT</code>\n\n"
            f"Сырой ответ API:\n"
            f"<pre>{json.dumps(raw, ensure_ascii=False, indent=2)[:1000]}</pre>"
        )
    except Exception as e:
        text = f"❌ Ошибка API: <code>{e}</code>"
    finally:
        await ex.close()
    await msg.answer(text, parse_mode="HTML")


# ------------------------------------------------------------------ /start /help

async def cmd_help(msg: Message):
    if not _auth(msg):
        return
    await msg.answer(
        "<b>Команды:</b>\n\n"
        "/status — открытые позиции\n"
        "/balance — баланс и статистика\n"
        "/pairs — торговые пары\n"
        "/settings — все настройки\n"
        "/scan — запустить сканирование\n"
        "/report — отчёт за сегодня\n"
        "/history — история сделок\n"
        "/top — топ пар по объёму\n"
        "/setpairs BTC-USDT,ETH-USDT — задать пары\n"
        "/pause — пауза\n"
        "/resume — возобновить\n"
        "/setmode auto|manual — режим\n"
        "/setrisk 1.0 — риск %\n"
        "/setlev 5 — плечо\n"
        "/closeall — закрыть все позиции\n"
        "/setbbmode on|off — BB пробой режим",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# ------------------------------------------------------------------ /status

async def cmd_status(msg: Message):
    if not _auth(msg):
        return
    from exchange.bingx import BingXClient
    ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    try:
        live = await ex.get_open_positions()
    except Exception as e:
        log.error(f"cmd_status: {e}")
        live = []
    finally:
        await ex.close()

    if not live and not state.positions:
        await msg.answer("📭 Нет открытых позиций", reply_markup=main_keyboard())
        return

    text = "📊 <b>Открытые позиции:</b>\n\n"
    if live:
        for p in live:
            sym   = p.get("symbol", "?")
            side  = p.get("positionSide", "?")
            amt   = abs(float(p.get("positionAmt", 0)))
            ep    = float(p.get("avgPrice", 0))
            upnl  = float(p.get("unrealizedProfit", 0))
            sign  = "+" if upnl >= 0 else ""
            emoji = "🟢" if upnl >= 0 else "🔴"
            text += (
                f"<b>{sym}</b> {side}\n"
                f"Кол-во: {amt}\n"
                f"Вход: <code>{ep:.4f}</code>\n"
                f"PnL: {emoji} <code>{sign}{upnl:.2f} USDT</code>\n\n"
            )
    else:
        for sym, p in state.positions.items():
            text += (
                f"<b>{sym}</b> {p.side} {'✅BE' if p.be_moved else '⏳'}\n"
                f"Вход: <code>{p.entry:.4f}</code>  SL: <code>{p.sl:.4f}</code>\n"
                f"TP3: <code>{p.tp3:.4f}</code>\n\n"
            )

    await msg.answer(text, parse_mode="HTML", reply_markup=main_keyboard())


# ------------------------------------------------------------------ /balance

async def cmd_balance(msg: Message):
    if not _auth(msg):
        return
    from exchange.bingx import BingXClient
    ex  = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    bal = await ex.get_balance()
    await ex.close()
    state.current_balance = bal
    d  = state.day
    wr = round(d.wins / d.trades * 100) if d.trades else 0
    await msg.answer(
        f"💰 <b>{bal:.2f} USDT</b>\n"
        f"Сделок сегодня: {d.trades}  |  WR: {wr}%\n"
        f"PnL сегодня: {'+' if d.pnl_usdt >= 0 else ''}{d.pnl_usdt:.2f} USDT\n"
        f"Итого: {'+' if state.total_pnl >= 0 else ''}{state.total_pnl:.2f} USDT",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# ------------------------------------------------------------------ /pairs

async def cmd_pairs(msg: Message):
    if not _auth(msg):
        return
    n = len(state.pairs)
    await msg.answer(
        f"📋 Пар: <b>{n}</b>\n{' | '.join(state.pairs[:15])}{'...' if n > 15 else ''}",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# ------------------------------------------------------------------ /settings

async def cmd_settings(msg: Message):
    if not _auth(msg):
        return
    status = "⏸ ПАУЗА" if state.paused else "▶️ Работает"
    be_mode = f"+{cfg.BE_TRIGGER_PCT}% от входа" if cfg.BE_TRIGGER_PCT > 0 else "TP1"
    al = "✅ вкл" if cfg.AUTO_LEVERAGE else "❌ выкл"
    bb_mode = "✅ вкл" if cfg.BB_BREAKOUT else "❌ выкл"
    text = (
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
        f"Мин. позиция: <code>{cfg.MIN_POSITION_USDT} USDT</code>\n"
        f"Макс. риск USDT: <code>{cfg.MAX_RISK_USDT} USDT</code>\n"
        f"Авто-плечо: <code>{al}</code>\n"
        f"  до 100$ → x10 | до 500$ → x7 | до 2000$ → x5 | от 2000$ → x3\n"
        f"Безубыток: <code>{be_mode}</code> (буфер +{cfg.BE_BUFFER_PCT}%)\n"
        f"Трейлинг стоп: <code>{cfg.TRAIL_PCT}%</code>\n"
        f"Фандинг LONG макс: <code>{cfg.FUNDING_MAX_LONG}%</code>\n"
        f"Фандинг SHORT макс: <code>{cfg.FUNDING_MAX_SHORT}%</code>\n\n"
        f"📊 <b>BB Пробой:</b> {bb_mode} | период {cfg.BB_PERIOD} | {cfg.BB_STD}σ | мин.score {cfg.BB_MIN_SCORE}\n\n"
        f"<i>/setrisk 1.0 | /setlev 5 | /setbe 0.5 | /settrail 1.0 | /setbbmode on|off</i>"
    )
    await msg.answer(text, parse_mode="HTML", reply_markup=main_keyboard())


# ------------------------------------------------------------------ /report

async def cmd_report(msg: Message):
    if not _auth(msg):
        return
    d  = state.day
    wr = round(d.wins / d.trades * 100) if d.trades else 0
    await msg.answer(
        f"📈 <b>Статистика за сегодня:</b>\n\n"
        f"Сделок: {d.trades}\n"
        f"Прибыльных: {d.wins}  |  Убыточных: {d.losses}\n"
        f"Винрейт: {wr}%\n"
        f"PnL: {'+' if d.pnl_usdt >= 0 else ''}{d.pnl_usdt:.2f} USDT\n"
        f"Серия потерь: {d.loss_streak}",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


# ------------------------------------------------------------------ /history

async def cmd_history(msg: Message):
    if not _auth(msg):
        return
    from core.db import get_history, get_stats
    rows  = get_history(15)
    stats = get_stats()
    if not rows:
        await msg.answer("📜 История сделок пуста", reply_markup=main_keyboard())
        return
    lines = ["📜 <b>Последние сделки:</b>\n"]
    for sym, side, entry, exit_p, pnl, result, closed_at in rows:
        icon = "✅" if result == "WIN" else "❌"
        dt   = closed_at[:16].replace("T", " ") if closed_at else "?"
        sign = "+" if pnl >= 0 else ""
        lines.append(
            f"{icon} <b>{sym}</b> {side}  {sign}{pnl:.2f}$\n"
            f"   {entry:.4f} → {exit_p:.4f}  <i>{dt}</i>"
        )
    wr = stats["wr"]
    lines.append(
        f"\n<b>Всего:</b> {stats['total']} сделок  |  WR: {wr}%\n"
        f"Общий PnL: {'+' if stats['pnl'] >= 0 else ''}{stats['pnl']:.2f} USDT"
    )
    await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_keyboard())


# ------------------------------------------------------------------ /top

async def cmd_top(msg: Message):
    if not _auth(msg):
        return
    await msg.answer("⏳ Получаю топ пары...", reply_markup=main_keyboard())
    from exchange.bingx import BingXClient
    ex   = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    syms = await ex.get_top_symbols(20)
    await ex.close()
    lines = ["🏆 <b>Топ 20 пар по объёму:</b>\n"]
    for i, s in enumerate(syms[:20], 1):
        in_bl = "🚫" if s in cfg.BLACKLIST else ""
        lines.append(f"{i:2}. {s} {in_bl}")
    await msg.answer("\n".join(lines), parse_mode="HTML", reply_markup=main_keyboard())


# ------------------------------------------------------------------ /setpairs

async def cmd_setminpos(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.answer(
            f"📐 <b>Мин. размер позиции:</b> <code>{cfg.MIN_POSITION_USDT} USDT</code>\n\n"
            f"Изменить: <code>/setminpos 20</code>",
            parse_mode="HTML",
        )
        return
    try:
        v = float(args[1])
        if v < 1 or v > 10000:
            raise ValueError
        cfg.MIN_POSITION_USDT = v
        await msg.answer(
            f"✅ Мин. позиция: <code>{v} USDT</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    except Exception:
        await msg.answer("Введи число от 1 до 10000 (например: /setminpos 20)")


async def cmd_setmaxpos(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.answer(
            f"📊 <b>Макс. позиций:</b> <code>{cfg.MAX_POSITIONS}</code>\n\n"
            f"Изменить: <code>/setmaxpos 3</code>",
            parse_mode="HTML",
        )
        return
    try:
        v = int(args[1])
        if v < 1 or v > 20:
            raise ValueError
        cfg.MAX_POSITIONS = v
        await msg.answer(
            f"✅ Макс. позиций: <code>{v}</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    except Exception:
        await msg.answer("Введи число от 1 до 20 (например: /setmaxpos 3)")


async def cmd_settrail(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.answer(
            f"📉 <b>Трейлинг стоп:</b> <code>{cfg.TRAIL_PCT}%</code>\n\n"
            f"SL двигается за ценой на {cfg.TRAIL_PCT}% после безубытка.\n"
            f"Изменить: <code>/settrail 1.5</code>",
            parse_mode="HTML",
        )
        return
    try:
        v = float(args[1])
        if v < 0.1 or v > 10:
            raise ValueError
        cfg.TRAIL_PCT = v
        await msg.answer(
            f"✅ Трейлинг стоп: <code>{v}%</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    except Exception:
        await msg.answer("Введи число от 0.1 до 10 (например: /settrail 1.5)")


async def cmd_setbe(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        mode = f"+{cfg.BE_TRIGGER_PCT}% от входа" if cfg.BE_TRIGGER_PCT > 0 else "TP1 (выкл.)"
        await msg.answer(
            f"🔄 <b>Настройка безубытка:</b>\n\n"
            f"Триггер: <code>{mode}</code>\n"
            f"Буфер: <code>+{cfg.BE_BUFFER_PCT}%</code>\n\n"
            f"Изменить триггер (% от входа):\n"
            f"<code>/setbe 0.5</code> — при +0.5% прибыли\n"
            f"<code>/setbe 1.0</code> — при +1.0% прибыли\n"
            f"<code>/setbe 0</code>   — переносить только на TP1",
            parse_mode="HTML",
        )
        return
    try:
        v = float(args[1])
        if v < 0 or v > 10:
            raise ValueError
        cfg.BE_TRIGGER_PCT = v
        mode = f"+{v}% от входа" if v > 0 else "TP1"
        await msg.answer(
            f"✅ Безубыток теперь переставляется при {mode}",
            reply_markup=main_keyboard(),
        )
    except Exception:
        await msg.answer("Введи число от 0 до 10 (например: /setbe 0.5)")


async def cmd_setmaxrisk(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.answer(
            f"🛡 <b>Макс. риск на сделку:</b> <code>{cfg.MAX_RISK_USDT} USDT</code>\n\n"
            f"Жёсткий лимит убытка за 1 сделку.\n"
            f"Изменить: <code>/setmaxrisk 20</code>",
            parse_mode="HTML",
        )
        return
    try:
        v = float(args[1])
        if v < 1 or v > 10000:
            raise ValueError
        cfg.MAX_RISK_USDT = v
        await msg.answer(
            f"✅ Макс. риск: <code>{v} USDT</code> на сделку",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    except Exception:
        await msg.answer("Введи число от 1 до 10000 (например: /setmaxrisk 20)")


async def cmd_setbbmode(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2 or args[1].lower() not in ("on", "off"):
        status = "✅ вкл" if cfg.BB_BREAKOUT else "❌ выкл"
        await msg.answer(
            f"📊 <b>BB Пробой режим:</b> {status}\n\n"
            f"Период: <code>{cfg.BB_PERIOD}</code> | Отклонение: <code>{cfg.BB_STD}σ</code>\n"
            f"Мин. score: <code>{cfg.BB_MIN_SCORE}</code>\n\n"
            f"Включить: <code>/setbbmode on</code>\n"
            f"Выключить: <code>/setbbmode off</code>",
            parse_mode="HTML",
        )
        return
    cfg.BB_BREAKOUT = args[1].lower() == "on"
    status = "✅ включён" if cfg.BB_BREAKOUT else "❌ выключен"
    await msg.answer(
        f"📊 BB Пробой режим {status}",
        parse_mode="HTML",
        reply_markup=main_keyboard(),
    )


async def cmd_setpairs(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].strip():
        current = ", ".join(state.pairs[:10]) or "нет"
        await msg.answer(
            f"📋 Текущие пары (первые 10): {current}\n\n"
            f"Чтобы задать свои пары:\n"
            f"<code>/setpairs BTC-USDT,ETH-USDT,SOL-USDT</code>\n\n"
            f"Чтобы сбросить к авто:\n"
            f"<code>/setpairs auto</code>",
            parse_mode="HTML",
        )
        return
    raw = args[1].strip()
    if raw.lower() == "auto":
        cfg.WHITELIST = []
        from exchange.bingx import BingXClient
        from strategy.scanner import Scanner
        ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
        await Scanner(ex, msg.bot).update_pairs()
        await ex.close()
        await msg.answer(f"✅ Режим авто, пар: {len(state.pairs)}", reply_markup=main_keyboard())
    else:
        pairs = [p.strip().upper() for p in raw.split(",") if p.strip()]
        cfg.WHITELIST = pairs
        state.pairs   = pairs
        await msg.answer(
            f"✅ Установлено {len(pairs)} пар:\n{', '.join(pairs)}",
            reply_markup=main_keyboard(),
        )


# ------------------------------------------------------------------ controls

async def cmd_pause(msg: Message):
    if not _auth(msg):
        return
    from core import db
    state.paused = True
    db.save_kv("paused", "1")
    await msg.answer("⏸ Торговля на паузе. /resume — возобновить", reply_markup=main_keyboard())


async def cmd_resume(msg: Message):
    if not _auth(msg):
        return
    from core import db
    state.paused           = False
    state.day.paused_until = None
    db.save_kv("paused", "0")
    await msg.answer("▶️ Торговля возобновлена", reply_markup=main_keyboard())


async def cmd_setmode(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2 or args[1] not in ("auto", "manual"):
        await msg.answer("/setmode auto | /setmode manual")
        return
    cfg.MODE = args[1]
    await msg.answer(f"✅ Режим: <code>{cfg.MODE}</code>", parse_mode="HTML",
                     reply_markup=main_keyboard())


async def cmd_setrisk(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.answer(f"Текущий риск: {cfg.RISK_PER_TRADE}%\nПример: /setrisk 0.5")
        return
    try:
        v = float(args[1])
        if not 0.1 <= v <= 3.0:
            raise ValueError
        cfg.RISK_PER_TRADE = v
        await msg.answer(f"✅ Риск: <code>{v}%</code>", parse_mode="HTML",
                         reply_markup=main_keyboard())
    except Exception:
        await msg.answer("Введи число от 0.1 до 3.0")


async def cmd_setlev(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.answer(f"Текущее плечо: x{cfg.LEVERAGE}\nПример: /setlev 10")
        return
    try:
        v = int(args[1])
        if not 1 <= v <= 50:
            raise ValueError
        cfg.LEVERAGE = v
        await msg.answer(f"✅ Плечо: <code>x{v}</code>", parse_mode="HTML",
                         reply_markup=main_keyboard())
    except Exception:
        await msg.answer("Введи число от 1 до 50")


# ------------------------------------------------------------------ /scan

async def cmd_scan(msg: Message):
    if not _auth(msg):
        return
    await msg.answer(
        f"🔍 Сканирую {len(state.pairs)} пар...\n(результат придёт отдельным сообщением)",
        reply_markup=main_keyboard(),
    )

    async def _do():
        try:
            from exchange.bingx import BingXClient
            from strategy.scanner import Scanner
            ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
            await Scanner(ex, msg.bot).scan_all()
            await ex.close()
        except Exception as e:
            log.error(f"cmd_scan bg: {e}")

    asyncio.create_task(_do())


# ------------------------------------------------------------------ /closeall

async def cmd_closeall(msg: Message):
    if not _auth(msg):
        return
    from exchange.bingx import BingXClient
    ex   = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    live = await ex.get_open_positions()
    if not live and not state.positions:
        await ex.close()
        await msg.answer("Нет открытых позиций", reply_markup=main_keyboard())
        return
    closed, errors = [], []
    for p in live:
        sym  = p.get("symbol")
        side = p.get("positionSide", "LONG")
        amt  = abs(float(p.get("positionAmt", 0)))
        if amt == 0:
            continue
        try:
            await ex.close_position(sym, amt, side)
            state.positions.pop(sym, None)
            closed.append(sym)
        except Exception as e:
            log.error(f"closeall {sym}: {e}")
            errors.append(sym)
    if not live:
        for sym, p in list(state.positions.items()):
            try:
                await ex.close_position(sym, p.qty, p.side)
                del state.positions[sym]
                closed.append(sym)
            except Exception as e:
                log.error(f"closeall {sym}: {e}")
                errors.append(sym)
    await ex.close()
    text = f"✅ Закрыто: {', '.join(closed) or 'ничего'}"
    if errors:
        text += f"\n❌ Ошибка закрытия: {', '.join(errors)}"
    await msg.answer(text, reply_markup=main_keyboard())


# ------------------------------------------------------------------ inline callbacks (manual mode)

async def handle_signal_callback(cb: CallbackQuery):
    if not _auth_cb(cb):
        await cb.answer("Нет доступа", show_alert=True)
        return
    action, symbol = cb.data.split(":", 1)
    if action == "skip":
        state.pending.pop(symbol, None)
        if cb.message.photo:
            await cb.message.edit_caption(
                caption=(cb.message.caption or "") + "\n\n⏭ <b>Пропущено</b>",
                parse_mode="HTML",
            )
        else:
            await cb.message.edit_text(
                text=(cb.message.text or "") + "\n\n⏭ <b>Пропущено</b>",
                parse_mode="HTML",
            )
        await cb.answer("Пропущено")
        return

    # confirm
    if symbol not in state.pending:
        await cb.answer("Сигнал уже не актуален", show_alert=True)
        return
    pend = state.pending[symbol]
    if datetime.utcnow() > pend["expires"]:
        state.pending.pop(symbol, None)
        await cb.answer("⏰ Время истекло", show_alert=True)
        return

    if cb.message.photo:
        await cb.message.edit_caption(
            caption=(cb.message.caption or "") + "\n\n⏳ <b>Входим...</b>",
            parse_mode="HTML",
        )
    else:
        await cb.message.edit_text(
            text=(cb.message.text or "") + "\n\n⏳ <b>Входим...</b>",
            parse_mode="HTML",
        )
    await cb.answer("Входим...")

    try:
        from exchange.bingx import BingXClient
        from strategy.scanner import Scanner
        ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
        await Scanner(ex, cb.message.bot)._enter(pend["signal"], confirmed=True)
        await ex.close()
    except Exception as e:
        log.error(f"confirm callback {symbol}: {e}")


# ------------------------------------------------------------------ misc (keyboard buttons + /confirm /skip)

async def handle_misc(msg: Message):
    if not _auth(msg):
        return
    text = msg.text or ""

    btn_map = {
        "📊 Статус":      cmd_status,
        "💰 Баланс":      cmd_balance,
        "⚙️ Настройки":  cmd_settings,
        "📋 Пары":        cmd_pairs,
        "🔍 Скан":        cmd_scan,
        "📈 Отчёт":       cmd_report,
        "📜 История":     cmd_history,
        "🏆 Топ пары":    cmd_top,
        "⏸ Пауза":       cmd_pause,
        "▶️ Продолжить": cmd_resume,
        "❌ Закрыть всё": cmd_closeall,
    }
    if text in btn_map:
        await btn_map[text](msg)
        return

    if text == "🔄 Безубыток":
        mode = f"+{cfg.BE_TRIGGER_PCT}% от входа" if cfg.BE_TRIGGER_PCT > 0 else "TP1"
        await msg.answer(
            f"🔄 <b>Безубыток:</b> <code>{mode}</code>\n"
            f"Буфер: <code>+{cfg.BE_BUFFER_PCT}%</code>\n\n"
            f"Изменить: <code>/setbe 0.5</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    if text == "📉 Трейлинг":
        await msg.answer(
            f"📉 <b>Трейлинг стоп:</b> <code>{cfg.TRAIL_PCT}%</code>\n\n"
            f"SL двигается за ценой на {cfg.TRAIL_PCT}% после безубытка.\n"
            f"Изменить: <code>/settrail 1.5</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
        return

    if text == "🤖 Авто":
        cfg.MODE = "auto"
        await msg.answer("✅ Режим: <code>auto</code>", parse_mode="HTML",
                         reply_markup=main_keyboard())
        return
    if text == "✋ Ручной":
        cfg.MODE = "manual"
        await msg.answer("✅ Режим: <code>manual</code>", parse_mode="HTML",
                         reply_markup=main_keyboard())
        return

    if text.startswith("/confirm_"):
        sym = text.replace("/confirm_", "").replace("_", "-").upper()
        if sym not in state.pending:
            await msg.answer(f"Сигнал {sym} не найден")
            return
        pend = state.pending[sym]
        if datetime.utcnow() > pend["expires"]:
            state.pending.pop(sym, None)
            await msg.answer("⏰ Время подтверждения истекло")
            return
        from exchange.bingx import BingXClient
        from strategy.scanner import Scanner
        ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
        await Scanner(ex, msg.bot)._enter(pend["signal"], confirmed=True)
        await ex.close()
        return

    if text.startswith("/skip_"):
        sym = text.replace("/skip_", "").replace("_", "-").upper()
        state.pending.pop(sym, None)
        await msg.answer(f"⏭ Пропущен: {sym}")


# ------------------------------------------------------------------ register

def register_handlers(dp: Dispatcher):
    dp.message.register(cmd_ping,     Command("ping"))
    dp.message.register(cmd_debug,    Command("debug"))
    dp.message.register(cmd_help,     Command("help", "start"))
    dp.message.register(cmd_status,   Command("status"))
    dp.message.register(cmd_balance,  Command("balance"))
    dp.message.register(cmd_pairs,    Command("pairs"))
    dp.message.register(cmd_settings, Command("settings"))
    dp.message.register(cmd_report,   Command("report"))
    dp.message.register(cmd_history,  Command("history"))
    dp.message.register(cmd_top,      Command("top"))
    dp.message.register(cmd_setminpos,  Command("setminpos"))
    dp.message.register(cmd_setmaxpos,  Command("setmaxpos"))
    dp.message.register(cmd_setmaxrisk, Command("setmaxrisk"))
    dp.message.register(cmd_setbe,      Command("setbe"))
    dp.message.register(cmd_settrail,  Command("settrail"))
    dp.message.register(cmd_setbbmode, Command("setbbmode"))
    dp.message.register(cmd_setpairs,  Command("setpairs"))
    dp.message.register(cmd_pause,    Command("pause"))
    dp.message.register(cmd_resume,   Command("resume"))
    dp.message.register(cmd_setmode,  Command("setmode"))
    dp.message.register(cmd_setrisk,  Command("setrisk"))
    dp.message.register(cmd_setlev,   Command("setlev"))
    dp.message.register(cmd_scan,     Command("scan"))
    dp.message.register(cmd_closeall, Command("closeall"))
    dp.message.register(handle_misc)
    dp.callback_query.register(
        handle_signal_callback,
        F.data.startswith("confirm:") | F.data.startswith("skip:"),
    )
