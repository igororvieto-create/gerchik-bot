
import asyncio
import logging
from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from core.config import cfg
from core.state import state
from strategy.scanner import Scanner
from exchange.bingx import BingXClient
from telegram.handlers import register_handlers

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("main")

bot = Bot(token=cfg.TELEGRAM_TOKEN)
dp = Dispatcher()

async def on_startup():
    exchange = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    scanner = Scanner(exchange, bot)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(scanner.scan_all, "cron", minute="*/15")
    scheduler.add_job(scanner.update_pairs, "cron", minute="0")
    scheduler.add_job(scanner.monitor_positions, "interval", seconds=30)
    scheduler.add_job(scanner.daily_report, "cron", hour="9")
    scheduler.start()

    dp["scanner"] = scanner
    dp["exchange"] = exchange

    try:
        balance = await exchange.get_balance()
        await bot.send_message(cfg.TELEGRAM_CHAT_ID,
            f"✅ Герчик Бот запущен\n\nРежим: {cfg.MODE}\nБаланс: {balance}")
    except Exception as e:
        log.error(f"Startup notify error: {e}")

    try:
        await scanner.update_pairs()
        await scanner.scan_all()
    except Exception as e:
        log.error(f"Startup scan error: {e}")

async def main():
    register_handlers(dp)
    try:
        await on_startup()
    except Exception as e:
        log.error(f"on_startup failed: {e}", exc_info=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
