import asyncio
import logging
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from core.config import cfg
from strategy.scanner import Scanner
from exchange.bingx import BingXClient
from telegram.handlers import register_handlers

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("main")

# Module-level scheduler to prevent garbage collection
scheduler = AsyncIOScheduler(timezone="UTC")

async def main():
    from aiogram import Bot, Dispatcher
    log.info(f"TOKEN: {'OK' if cfg.TELEGRAM_TOKEN else 'ПУСТО!'}")
    log.info(f"CHATID: {cfg.TELEGRAM_CHAT_ID!r}")
    if not cfg.TELEGRAM_TOKEN:
        log.error("TELEGRAM_TOKEN не задан — бот не запустится"); return

    bot = Bot(token=cfg.TELEGRAM_TOKEN)
    dp = Dispatcher()
    register_handlers(dp)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning(f"delete_webhook: {e}")

    exchange = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    scanner = Scanner(exchange, bot)

    scheduler.add_job(scanner.scan_all,          "cron",     minute="*/15")
    scheduler.add_job(scanner.update_pairs,       "cron",     minute="0")
    scheduler.add_job(scanner.monitor_positions,  "interval", seconds=30)
    scheduler.add_job(scanner.daily_report,       "cron",     hour="9")
    scheduler.start()

    async def startup_tasks():
        await asyncio.sleep(2)
        try:
            balance = await exchange.get_balance()
            await bot.send_message(cfg.TELEGRAM_CHAT_ID,
                f"✅ Герчик Бот запущен\nРежим: {cfg.MODE}\nБаланс: {balance:.2f} USDT")
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
