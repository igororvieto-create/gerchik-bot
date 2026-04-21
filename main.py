import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from core import db
from core.config import cfg
from core.state import state, Position
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


async def main():
    from aiogram import Bot, Dispatcher

    log.info(f"TOKEN: {'OK' if cfg.TELEGRAM_TOKEN else 'ПУСТО!'}")
    log.info(f"CHATID: {cfg.TELEGRAM_CHAT_ID!r}")
    if not cfg.TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN не задан — бот не запустится")
        return

    # Init SQLite and restore state
    db.init_db()
    state.total_pnl = db.load_total_pnl()
    log.info(f"Восстановлен total_pnl из БД: {state.total_pnl:.2f} USDT")
    state.paused = db.get_kv("paused", "0") == "1"
    if state.paused:
        log.info("Бот восстановлен на паузе (из БД)")

    # Restore open positions from DB (survive restarts)
    saved_positions = db.load_positions()
    for row in saved_positions:
        try:
            pos = Position(
                symbol=row["symbol"], side=row["side"],
                entry=row["entry"], sl=row["sl"],
                tp1=row["tp1"], tp2=row["tp2"], tp3=row["tp3"],
                qty=row["qty"], risk_usdt=row["risk_usdt"],
                order_id=row["order_id"], sl_order_id=row["sl_order_id"],
                tp_order_id=row["tp_order_id"],
                be_moved=bool(row["be_moved"]), tp2_hit=bool(row["tp2_hit"]),
                trail_price=row["trail_price"],
                opened_at=datetime.fromisoformat(row["opened_at"]),
                pattern=row["pattern"], tf=row["tf"],
                rr=row["rr"], score=row["score"],
            )
            state.positions[pos.symbol] = pos
        except Exception as e:
            log.error(f"Ошибка восстановления позиции {row.get('symbol')}: {e}")
    if saved_positions:
        log.info(f"Восстановлено {len(saved_positions)} позиций из БД")

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
    scheduler.add_job(scanner.scan_all,         "cron",     minute="*/15")
    scheduler.add_job(scanner.update_pairs,      "cron",     minute="0")
    scheduler.add_job(scanner.monitor_positions, "interval", seconds=30)
    scheduler.add_job(scanner.watchdog,          "interval", hours=1)
    scheduler.add_job(scanner.daily_report,      "cron",     hour="9",  minute="0")
    scheduler.add_job(scanner.weekly_report,     "cron",     day_of_week="mon", hour="9", minute="5")
    scheduler.add_job(scanner.monthly_report,    "cron",     day="1",   hour="9", minute="10")
    scheduler.start()

    async def startup_tasks():
        await asyncio.sleep(2)
        try:
            balance = await exchange.get_balance()
            state.current_balance = balance

            # Sync with exchange: add any positions open on exchange but not in DB
            live = await exchange.get_open_positions()
            synced = 0
            for p in live:
                sym   = p.get("symbol", "")
                side  = p.get("positionSide", "LONG")
                amt   = abs(float(p.get("positionAmt", 0)))
                entry = float(p.get("entryPrice", 0))
                if sym and amt > 0 and sym not in state.positions:
                    state.positions[sym] = Position(
                        symbol=sym, side=side, entry=entry, sl=0.0,
                        tp1=0.0, tp2=0.0, tp3=0.0, qty=amt, risk_usdt=0.0,
                    )
                    synced += 1
            if synced:
                log.info(f"Добавлено {synced} новых позиций с биржи (не было в БД)")

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

    asyncio.create_task(startup_tasks())

    while True:
        try:
            await dp.start_polling(bot)
            break
        except Exception as e:
            log.error(f"Polling error: {e} — retry in 10s")
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
