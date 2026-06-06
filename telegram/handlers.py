import asyncio
import html as _html
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


def _smc_shadow() -> bool:
    try:
        from strategy.smc_filters import SHADOW_MODE
        return SHADOW_MODE
    except Exception:
        return True


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
            [KeyboardButton(text="💸 Фандинг"),     KeyboardButton(text="❌ Закрыть всё")],
        ],
        resize_keyboard=True,
        persistent=True,
    )


# ------------------------------------------------------------------ /ping

async def cmd_ping(msg: Message):
    if not _auth(msg):
        return
    await msg.answer("🟢 Бот работает", reply_markup=main_keyboard())


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
        text = f"❌ Ошибка API: <code>{_html.escape(str(e))}</code>"
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
        "/close_BTC_USDT — закрыть конкретную позицию\n\n"
        "<b>Настройки риска:</b>\n"
        "/setmaxloss 3.0 — макс. дневной убыток %\n"
        "/setmaxdaily 5 — макс. сделок в день\n"
        "/setminscore 70 — мин. оценка сигнала\n"
        "/setautolev on|off — авто-плечо по балансу",
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

    # Build live map for PnL lookup
    live_map = {p.get("symbol"): p for p in live if abs(float(p.get("positionAmt", 0))) > 0}

    text = "📊 <b>Открытые позиции:</b>\n\n"
    positions_to_show = state.positions if state.positions else {
        p.get("symbol"): p for p in live if abs(float(p.get("positionAmt", 0))) > 0
    }

    for sym, pos in positions_to_show.items():
        lp    = live_map.get(sym, {})
        upnl  = float(lp.get("unrealizedProfit", 0))
        cur   = float(lp.get("markPrice", 0))
        sign  = "+" if upnl >= 0 else ""
        emoji = "🟢" if upnl >= 0 else "🔴"

        if hasattr(pos, "entry"):
            age_h = int((datetime.utcnow() - pos.opened_at).total_seconds() / 3600)
            sl_dist = f"{abs(cur - pos.sl) / pos.entry * 100:.1f}%" if cur > 0 and pos.sl > 0 and pos.entry > 0 else "?"
            tp_dist = f"{abs(pos.tp2 - cur) / pos.entry * 100:.1f}%" if cur > 0 and pos.tp2 > 0 and pos.entry > 0 else "?"
            be_tag  = " ✅BE" if pos.be_moved else ""
            t1_tag  = " 🎯TP1" if pos.tp1_hit else ""
            text += (
                f"<b>{sym}</b> {pos.side}{be_tag}{t1_tag} | ⭐{pos.score} | {pos.pattern}\n"
                f"Вход: <code>{pos.entry:.4f}</code> | {age_h}ч\n"
                f"🔴 SL: <code>{pos.sl:.4f}</code> ({sl_dist} до стопа)\n"
                f"🟡 TP2: <code>{pos.tp2:.4f}</code> ({tp_dist} до TP2)\n"
                f"PnL: {emoji} <code>{sign}{upnl:.2f} USDT</code>\n\n"
            )
        else:
            # Exchange-synced position without bot data
            ep   = float(lp.get("avgPrice", 0))
            amt  = abs(float(lp.get("positionAmt", 0)))
            side = lp.get("positionSide", "?")
            text += (
                f"<b>{sym}</b> {side} (внешняя)\n"
                f"Вход: <code>{ep:.4f}</code> | Кол-во: {amt}\n"
                f"PnL: {emoji} <code>{sign}{upnl:.2f} USDT</code>\n\n"
            )

    await msg.answer(text, parse_mode="HTML", reply_markup=main_keyboard())


# ------------------------------------------------------------------ /balance

async def cmd_balance(msg: Message):
    if not _auth(msg):
        return
    from exchange.bingx import BingXClient
    ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    try:
        bal = await ex.get_balance()
        state.current_balance = bal
    except Exception as e:
        log.error(f"cmd_balance: {e}")
        await msg.answer("❌ Ошибка получения баланса", reply_markup=main_keyboard())
        return
    finally:
        await ex.close()
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
        f"  до 100$ → x3 | 100$–2000$ → x5 | от 2000$ → x3\n"
        f"Безубыток: <code>{be_mode}</code> (буфер +{cfg.BE_BUFFER_PCT}%)\n"
        f"Трейлинг стоп: <code>{cfg.TRAIL_PCT}%</code>\n"
        f"Фандинг LONG макс: <code>{cfg.FUNDING_MAX_LONG}%</code>\n"
        f"Фандинг SHORT макс: <code>{cfg.FUNDING_MAX_SHORT}%</code>\n"
        f"Макс. время позиции: <code>{cfg.MAX_POSITION_HOURS}ч</code>\n"
        f"SMC фильтр: <code>{'тень (лог)' if _smc_shadow() else 'АКТИВЕН'}</code>\n\n"
        f"<i>/setrisk | /setlev | /setbe | /settrail | /setautolev</i>\n"
        f"<i>/setmaxloss | /setmaxdaily | /setminscore</i>"
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
        icon = "✅" if pnl >= 0 else "❌"
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
        from core import db; db.save_cfg_value("MIN_POSITION_USDT", v)
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
        from core import db; db.save_cfg_value("MAX_POSITIONS", v)
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
        from core import db; db.save_cfg_value("TRAIL_PCT", v)
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
        from core import db; db.save_cfg_value("BE_TRIGGER_PCT", v)
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
        from core import db; db.save_cfg_value("MAX_RISK_USDT", v)
        await msg.answer(
            f"✅ Макс. риск: <code>{v} USDT</code> на сделку",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    except Exception:
        await msg.answer("Введи число от 1 до 10000 (например: /setmaxrisk 20)")


async def cmd_setminscore(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.answer(
            f"⭐ <b>Мин. оценка сигнала:</b> <code>{cfg.MIN_SCORE}</code>\n\n"
            f"Сигналы ниже этого порога игнорируются.\n"
            f"Изменить: <code>/setminscore 70</code>",
            parse_mode="HTML",
        )
        return
    try:
        v = int(args[1])
        if v < 40 or v > 95:
            raise ValueError
        cfg.MIN_SCORE = v
        from core import db; db.save_cfg_value("MIN_SCORE", v)
        await msg.answer(
            f"✅ Мин. оценка: <code>{v}</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    except Exception:
        await msg.answer("Введи число от 40 до 95 (например: /setminscore 70)")


async def cmd_setmaxloss(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.answer(
            f"🛑 <b>Макс. дневной убыток:</b> <code>{cfg.MAX_DAILY_LOSS}%</code>\n\n"
            f"При достижении торговля останавливается до следующего дня.\n"
            f"Изменить: <code>/setmaxloss 3.0</code>",
            parse_mode="HTML",
        )
        return
    try:
        v = float(args[1])
        if v < 0.5 or v > 20:
            raise ValueError
        cfg.MAX_DAILY_LOSS = v
        from core import db; db.save_cfg_value("MAX_DAILY_LOSS", v)
        await msg.answer(
            f"✅ Макс. дневной убыток: <code>{v}%</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    except Exception:
        await msg.answer("Введи число от 0.5 до 20 (например: /setmaxloss 3.0)")


async def cmd_setmaxdaily(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        await msg.answer(
            f"📅 <b>Макс. сделок в день:</b> <code>{cfg.MAX_DAILY_TRADES}</code>\n\n"
            f"Изменить: <code>/setmaxdaily 5</code>",
            parse_mode="HTML",
        )
        return
    try:
        v = int(args[1])
        if v < 1 or v > 50:
            raise ValueError
        cfg.MAX_DAILY_TRADES = v
        from core import db; db.save_cfg_value("MAX_DAILY_TRADES", v)
        await msg.answer(
            f"✅ Макс. сделок/день: <code>{v}</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    except Exception:
        await msg.answer("Введи число от 1 до 50 (например: /setmaxdaily 5)")


async def cmd_setmaxsl(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        current = f"{cfg.MAX_SL_PCT}%" if cfg.MAX_SL_PCT > 0 else "выкл"
        await msg.answer(
            f"🛑 <b>Макс. ширина SL от входа:</b> <code>{current}</code>\n\n"
            f"Сигналы с SL шире этого порога игнорируются.\n"
            f"Изменить: <code>/setmaxsl 10</code>\n"
            f"Отключить: <code>/setmaxsl 0</code>",
            parse_mode="HTML",
        )
        return
    try:
        v = float(args[1])
        if v < 0 or v > 100:
            raise ValueError
        cfg.MAX_SL_PCT = v
        from core import db; db.save_cfg_value("MAX_SL_PCT", v)
        label = f"{v}%" if v > 0 else "выкл"
        await msg.answer(
            f"✅ Макс. SL: <code>{label}</code> от входа",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    except Exception:
        await msg.answer("Введи число от 0 до 100 (например: /setmaxsl 10)")


async def cmd_setautolev(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2:
        status = "✅ вкл" if cfg.AUTO_LEVERAGE else "❌ выкл"
        await msg.answer(
            f"⚡ <b>Авто-плечо:</b> {status}\n\n"
            f"При включении плечо выбирается по балансу:\n"
            f"• &lt; $100 → x3 (защита малого счёта)\n"
            f"• $100 – $2000 → x5\n"
            f"• ≥ $2000 → x3\n\n"
            f"Включить: <code>/setautolev on</code>\n"
            f"Выключить: <code>/setautolev off</code>",
            parse_mode="HTML",
        )
        return
    v = args[1].lower()
    if v in ("on", "1", "true", "вкл"):
        cfg.AUTO_LEVERAGE = True
        from core import db; db.save_cfg_value("AUTO_LEVERAGE", "true")
        await msg.answer("✅ Авто-плечо включено", reply_markup=main_keyboard())
    elif v in ("off", "0", "false", "выкл"):
        cfg.AUTO_LEVERAGE = False
        from core import db; db.save_cfg_value("AUTO_LEVERAGE", "false")
        await msg.answer(
            f"❌ Авто-плечо выключено. Используется фиксированное плечо x{cfg.LEVERAGE}.\n"
            f"Изменить: <code>/setlev 5</code>",
            parse_mode="HTML",
            reply_markup=main_keyboard(),
        )
    else:
        await msg.answer("Используй: /setautolev on или /setautolev off")


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
        from strategy.scanner import _global_scanner
        if _global_scanner:
            await _global_scanner.update_pairs()
        else:
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
    db.save_kv("paused_until", "")
    await msg.answer("▶️ Торговля возобновлена", reply_markup=main_keyboard())


async def cmd_setmode(msg: Message):
    if not _auth(msg):
        return
    args = msg.text.split()
    if len(args) < 2 or args[1] not in ("auto", "manual"):
        await msg.answer("/setmode auto | /setmode manual")
        return
    cfg.MODE = args[1]
    from core import db; db.save_cfg_value("MODE", cfg.MODE)
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
        from core import db; db.save_cfg_value("RISK_PER_TRADE", v)
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
        from core import db; db.save_cfg_value("LEVERAGE", v)
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
            from strategy.scanner import _global_scanner
            if _global_scanner:
                await _global_scanner.scan_all()
            else:
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
    live_syms = set()
    for p in live:
        sym  = p.get("symbol")
        side = p.get("positionSide", "LONG")
        amt  = abs(float(p.get("positionAmt", 0)))
        if amt == 0:
            continue
        live_syms.add(sym)
        try:
            # Cancel SL/TP orders if tracked in state
            tracked = state.positions.get(sym)
            if tracked:
                for oid in (tracked.sl_order_id, tracked.tp_order_id):
                    if oid:
                        try:
                            await ex.cancel_order(sym, oid)
                        except Exception:
                            pass
            await ex.close_position(sym, amt, side)
            state.positions.pop(sym, None)
            from core import db as _db; _db.delete_open_position(sym)
            closed.append(sym)
        except Exception as e:
            log.error(f"closeall {sym}: {e}")
            errors.append(sym)

    # Close positions tracked in state but absent from exchange (ghost state or API returned none)
    ghost_syms = [sym for sym in list(state.positions.keys()) if sym not in live_syms]
    for sym in ghost_syms:
        p = state.positions.get(sym)
        if not p:
            continue
        try:
            if p.sl_order_id:
                try:
                    await ex.cancel_order(sym, p.sl_order_id)
                except Exception:
                    pass
            if p.tp_order_id:
                try:
                    await ex.cancel_order(sym, p.tp_order_id)
                except Exception:
                    pass
            if not live:
                # Exchange returned nothing — try to close anyway (position may exist)
                await ex.close_position(sym, p.qty, p.side)
            state.positions.pop(sym, None)
            from core import db as _db; _db.delete_open_position(sym)
            closed.append(sym)
        except Exception as e:
            log.error(f"closeall ghost {sym}: {e}")
            errors.append(sym)
    await ex.close()
    text = f"✅ Закрыто: {', '.join(closed) or 'ничего'}"
    if errors:
        text += f"\n❌ Ошибка закрытия: {', '.join(errors)}"
    await msg.answer(text, reply_markup=main_keyboard())


# ------------------------------------------------------------------ /close_SYMBOL

async def cmd_close_symbol(msg: Message):
    if not _auth(msg):
        return
    # Accept /close_BTC_USDT or /close BTC-USDT
    text = msg.text.strip()
    if text.startswith("/close_"):
        raw = text[len("/close_"):]
    elif " " in text:
        raw = text.split(None, 1)[1]
    else:
        await msg.answer("Укажи символ: /close_BTC_USDT", reply_markup=main_keyboard())
        return
    symbol = raw.replace("_", "-").upper()
    pos = state.positions.get(symbol)
    if not pos:
        await msg.answer(f"Позиция {symbol} не найдена в памяти бота", reply_markup=main_keyboard())
        return
    from exchange.bingx import BingXClient
    ex = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    try:
        for oid in (pos.sl_order_id, pos.tp_order_id):
            if oid:
                try:
                    await ex.cancel_order(symbol, oid)
                except Exception:
                    pass
        await ex.close_position(symbol, pos.qty, pos.side)
        state.positions.pop(symbol, None)
        from core import db as _db; _db.delete_open_position(symbol)
        await msg.answer(f"✅ Позиция {symbol} закрыта", reply_markup=main_keyboard())
    except Exception as e:
        log.error(f"close_symbol {symbol}: {e}")
        await msg.answer(f"❌ Ошибка закрытия {symbol}: {_html.escape(str(e))}", parse_mode="HTML", reply_markup=main_keyboard())
    finally:
        await ex.close()


# ------------------------------------------------------------------ inline callbacks (manual mode)

async def handle_signal_callback(cb: CallbackQuery):
    if not _auth_cb(cb):
        await cb.answer("Нет доступа", show_alert=True)
        return
    parts = cb.data.split(":", 1)
    if len(parts) != 2:
        await cb.answer("Некорректные данные", show_alert=True)
        return
    action, symbol = parts
    if action == "skip":
        state.pending.pop(symbol, None)
        try:
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
        except Exception:
            pass
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

    try:
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
    except Exception:
        pass
    await cb.answer("Входим...")

    try:
        from strategy.scanner import _global_scanner
        if _global_scanner:
            await _global_scanner._enter(pend["signal"], confirmed=True)
        else:
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
        "💸 Фандинг":    cmd_funding,
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
        from core import db as _db; _db.save_cfg_value("MODE", "auto")
        await msg.answer("✅ Режим: <code>auto</code>", parse_mode="HTML",
                         reply_markup=main_keyboard())
        return
    if text == "✋ Ручной":
        cfg.MODE = "manual"
        from core import db as _db; _db.save_cfg_value("MODE", "manual")
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
        from strategy.scanner import _global_scanner
        if _global_scanner:
            await _global_scanner._enter(pend["signal"], confirmed=True)
        else:
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
        return

    if text.startswith("/close_"):
        await cmd_close_symbol(msg)


# ------------------------------------------------------------------ /funding

async def cmd_funding(msg: Message):
    if not _auth(msg):
        return
    from strategy.scanner import _global_scanner
    if _global_scanner is None:
        await msg.answer("⚠️ Сканер не запущен", reply_markup=main_keyboard())
        return
    await msg.answer("⏳ Сканирую фандинг по всем парам...", reply_markup=main_keyboard())
    try:
        await _global_scanner.funding_scan()
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {_html.escape(str(e))}", parse_mode="HTML")


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
    dp.message.register(cmd_funding,  Command("funding"))
    dp.message.register(cmd_setminpos,  Command("setminpos"))
    dp.message.register(cmd_setmaxpos,  Command("setmaxpos"))
    dp.message.register(cmd_setmaxrisk, Command("setmaxrisk"))
    dp.message.register(cmd_setbe,       Command("setbe"))
    dp.message.register(cmd_settrail,   Command("settrail"))
    dp.message.register(cmd_setminscore, Command("setminscore"))
    dp.message.register(cmd_setmaxloss,  Command("setmaxloss"))
    dp.message.register(cmd_setmaxdaily, Command("setmaxdaily"))
    dp.message.register(cmd_setmaxsl,    Command("setmaxsl"))
    dp.message.register(cmd_setautolev,  Command("setautolev"))
    dp.message.register(cmd_setpairs, Command("setpairs"))
    dp.message.register(cmd_pause,    Command("pause"))
    dp.message.register(cmd_resume,   Command("resume"))
    dp.message.register(cmd_setmode,  Command("setmode"))
    dp.message.register(cmd_setrisk,  Command("setrisk"))
    dp.message.register(cmd_setlev,   Command("setlev"))
    dp.message.register(cmd_scan,     Command("scan"))
    dp.message.register(cmd_closeall,     Command("closeall"))
    dp.message.register(cmd_close_symbol, Command("close"))
    dp.message.register(handle_misc)
    dp.callback_query.register(
        handle_signal_callback,
        F.data.startswith("confirm:") | F.data.startswith("skip:"),
    )
