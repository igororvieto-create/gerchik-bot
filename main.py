import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core import db
from core.config import cfg
from core.state import state
from strategy.scanner import Scanner
from exchange.bingx import BingXClient
from telegram.handlers import register_handlers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("main")

# Module-level scheduler — prevents garbage collection
scheduler = AsyncIOScheduler(timezone="UTC")


def _validate_config() -> bool:
    errors = []
    if not cfg.TELEGRAM_TOKEN:
        errors.append("TELEGRAM_TOKEN не задан")
    if not cfg.TELEGRAM_CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID не задан")
    if not cfg.BINGX_API_KEY:
        errors.append("BINGX_API_KEY не задан")
    if not cfg.BINGX_SECRET:
        errors.append("BINGX_SECRET не задан")
    if cfg.MIN_RR < 1.0:
        errors.append(f"MIN_RR={cfg.MIN_RR} должен быть ≥ 1.0")
    if cfg.MAX_DAILY_LOSS <= 0:
        errors.append(f"MAX_DAILY_LOSS={cfg.MAX_DAILY_LOSS} должен быть > 0")
    if not (0 < cfg.RISK_PER_TRADE <= 5):
        errors.append(f"RISK_PER_TRADE={cfg.RISK_PER_TRADE} должен быть в диапазоне 0–5%")
    for msg in errors:
        log.error(f"[config] {msg}")
    critical = not cfg.TELEGRAM_TOKEN or not cfg.BINGX_API_KEY or not cfg.BINGX_SECRET
    return not critical


async def main():
    from aiogram import Bot, Dispatcher

    log.info(f"TOKEN: {'OK' if cfg.TELEGRAM_TOKEN else 'ПУСТО!'}")
    log.info(f"CHATID: {cfg.TELEGRAM_CHAT_ID!r}")
    if not _validate_config():
        log.error("Критические параметры не заданы — бот не запустится")
        return

    # Init SQLite and restore state
    db.init_db()

    # Restore runtime-changed config values (setrisk, setlev, etc.)
    _saved_cfg = db.load_cfg_values()
    for _k, _v in _saved_cfg.items():
        if hasattr(cfg, _k):
            try:
                _cur = getattr(cfg, _k)
                if isinstance(_cur, bool):
                    setattr(cfg, _k, _v.lower() == "true")
                elif isinstance(_cur, int):
                    setattr(cfg, _k, int(float(_v)))
                elif isinstance(_cur, float):
                    setattr(cfg, _k, float(_v))
                elif isinstance(_cur, list):
                    setattr(cfg, _k, [s.strip() for s in _v.split(",") if s.strip()])
                else:
                    setattr(cfg, _k, _v)
                log.info(f"Восстановлена настройка {_k}={_v}")
            except Exception as _e:
                log.warning(f"Не удалось восстановить настройку {_k}: {_e}")

    state.total_pnl = db.load_total_pnl()
    log.info(f"Восстановлен total_pnl из БД: {state.total_pnl:.2f} USDT")
    _peak_str = db.get_kv("peak_balance", "0")
    try:
        state.peak_balance = float(_peak_str)
        if state.peak_balance > 0:
            log.info(f"Восстановлен peak_balance: {state.peak_balance:.2f} USDT")
    except Exception:
        pass
    # Restore today's stats so daily limits and /report are correct after restart
    today = db.get_today_stats()
    state.day.trades    = today["total"]
    state.day.wins      = today["wins"]
    state.day.losses    = today["losses"]
    state.day.pnl_usdt  = today["pnl"]
    log.info(f"Восстановлена дневная статистика: {today['total']} сделок, PnL {today['pnl']:.2f} USDT")
    state.paused = db.get_kv("paused", "0") == "1"
    if state.paused:
        log.info("Бот восстановлен на паузе (из БД)")
    _paused_until_str = db.get_kv("paused_until", "")
    if _paused_until_str:
        try:
            _pu = datetime.fromisoformat(_paused_until_str)
            if _pu > datetime.utcnow():
                state.day.paused_until = _pu
                log.info(f"Восстановлена авто-пауза до {_pu.strftime('%H:%M UTC')}")
        except Exception:
            pass

    bot = Bot(token=cfg.TELEGRAM_TOKEN)
    dp  = Dispatcher()
    register_handlers(dp)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning(f"delete_webhook: {e}")

    exchange = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    scanner  = Scanner(exchange, bot)

    # Scheduler jobs
    scheduler.add_job(scanner.scan_all,          "cron",     minute=f"*/{cfg.SCAN_H1_INTERVAL_MIN}")
    scheduler.add_job(scanner.update_pairs,       "cron",     minute="0")
    scheduler.add_job(scanner.monitor_positions,  "interval", seconds=30)
    scheduler.add_job(scanner.health_check,       "cron",     minute="*/15")
    scheduler.add_job(scanner.btc_weekly_alert,   "cron",     hour="*/6", minute="30")  # 4x/day
    scheduler.add_job(scanner.funding_scan,        "cron",     hour="0,8,16", minute="5")
    scheduler.add_job(scanner.periodic_report,     "cron",     hour="*/3", minute="0")
    scheduler.add_job(scanner.daily_report,       "cron",     hour="9",  minute="0")
    scheduler.add_job(scanner.weekly_report,      "cron",     day_of_week="mon", hour="9", minute="5")
    scheduler.add_job(scanner.monthly_report,     "cron",     day="1",   hour="9", minute="10")
    scheduler.start()

    async def startup_tasks():
        await asyncio.sleep(2)
        try:
            balance = await exchange.get_balance()
            state.current_balance = balance

            # Get live positions from exchange (ground truth)
            live = await exchange.get_open_positions()
            live_map = {
                p.get("symbol"): p for p in live
                if abs(float(p.get("positionAmt", 0))) > 0
            }

            # Restore positions from DB (full data: SL, TP, pattern, BE state, etc.)
            from core.state import Position
            saved = db.load_open_positions()
            restored = 0
            downtime_closed = []
            for d in saved:
                sym = d.get("symbol", "")
                if not sym:
                    continue
                if sym not in live_map:
                    # Closed during downtime — PnL cannot be recorded without the close price.
                    # Notify the user so they can check exchange history manually.
                    log.warning(
                        f"Позиция {sym} закрыта в downtime — PnL не записан (нет цены закрытия)"
                    )
                    downtime_closed.append(sym)
                    db.delete_open_position(sym)
                    continue
                if sym not in state.positions:
                    state.positions[sym] = Position(
                        symbol=sym, side=d["side"],
                        entry=float(d["entry"]), sl=float(d["sl"]),
                        tp1=float(d["tp1"]), tp2=float(d["tp2"]), tp3=float(d["tp3"]),
                        qty=float(d["qty"]), risk_usdt=float(d.get("risk_usdt", 0)),
                        order_id=d.get("order_id", ""),
                        sl_order_id=d.get("sl_order_id", ""),
                        tp_order_id=d.get("tp_order_id", ""),
                        be_moved=bool(d.get("be_moved", False)),
                        tp1_hit=bool(d.get("tp1_hit", False)),
                        tp2_hit=bool(d.get("tp2_hit", False)),
                        trail_price=float(d.get("trail_price", 0.0)),
                        partial_pnl_taken=float(d.get("partial_pnl_taken", 0.0)),
                        opened_at=datetime.fromisoformat(
                            d.get("opened_at") or datetime.utcnow().isoformat()
                        ) if d.get("opened_at") else datetime.utcnow(),
                        pattern=d.get("pattern", ""),
                        tf=d.get("tf", "H1+H4"),
                        rr=float(d.get("rr", 0.0)),
                        score=int(d.get("score", 0)),
                    )
                    restored += 1
            if restored:
                log.info(f"Восстановлено {restored} позиций из БД с полными данными (SL/TP/BE)")
            if downtime_closed:
                syms_str = ", ".join(downtime_closed)
                try:
                    await bot.send_message(
                        cfg.TELEGRAM_CHAT_ID,
                        f"⚠️ <b>Позиции закрыты в downtime</b>\n"
                        f"Символы: <code>{syms_str}</code>\n"
                        f"PnL не записан — проверь историю на бирже вручную",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

            # Any live exchange position not in DB → add with sl=0 (manual / unknown)
            # Try multiple field names for entry price (BingX API inconsistency)
            for sym, lp in live_map.items():
                if sym not in state.positions:
                    raw_entry = (
                        float(lp.get("entryPrice") or 0) or
                        float(lp.get("avgPrice")   or 0) or
                        float(lp.get("markPrice")  or 0)
                    )
                    state.positions[sym] = Position(
                        symbol=sym, side=lp.get("positionSide", "LONG"),
                        entry=raw_entry, sl=0.0,
                        tp1=0.0, tp2=0.0, tp3=0.0,
                        qty=abs(float(lp.get("positionAmt", 0))), risk_usdt=0.0,
                    )
                    log.info(f"Внешняя позиция {sym} добавлена без SL/TP (вход={raw_entry})")

            await bot.send_message(
                cfg.TELEGRAM_CHAT_ID,
                f"✅ <b>Герчик Бот запущен</b>\n\n"
                f"Режим: <code>{cfg.MODE}</code>\n"
                f"Баланс: <code>{balance:.2f} USDT</code>\n"
                f"Открытых позиций: <code>{len(state.positions)}</code>\n"
                f"PnL всего: <code>{'+' if state.total_pnl >= 0 else ''}{state.total_pnl:.2f} USDT</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            log.error(f"startup notify: {e}")
        try:
            await scanner.update_pairs()
            await scanner.scan_all()
        except Exception as e:
            log.error(f"startup scan: {e}")

    _startup_task = asyncio.create_task(startup_tasks())  # noqa: F841 — keep reference

    while True:
        try:
            await dp.start_polling(bot)
            break
        except Exception as e:
            log.error(f"Polling error: {e} — retry in 10s")
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
