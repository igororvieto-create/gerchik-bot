import asyncio, logging
from aiogram import Bot, Dispatcher
from aiogram.utils import executor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from core.config import cfg
from core.state import state
from strategy.scanner import Scanner
from exchange.bingx import BingXClient

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("main")

bot = Bot(token=cfg.TELEGRAM_TOKEN)
dp  = Dispatcher(bot)

async def on_startup(dp):
    exchange = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    scanner  = Scanner(exchange, bot)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(scanner.scan_all, "cron", minute=f"*/{cfg.SCAN_H1_INTERVAL_MIN}")
    scheduler.add_job(scanner.update_pairs, "cron", minute="0")
    scheduler.add_job(scanner.monitor_positions, "interval", seconds=30)
    scheduler.add_job(scanner.daily_report, "cron", hour="23", minute="55")
    scheduler.start()
    dp["scanner"] = scanner
    dp["exchange"] = exchange
    balance = await exchange.get_balance()
    await bot.send_message(cfg.TELEGRAM_CHAT_ID,
        f"✅ Герчик Бот запущен\n\nРежим: {cfg.MODE}\nБаланс: {balance:.2f} USDT\nРиск: {cfg.RISK_PER_TRADE}%\n\n/help — команды")
    await scanner.update_pairs()
    await scanner.scan_all()

from telegram.handlers import register_handlers
register_handlers(dp)

if __name__ == "__main__":
    executor.start_polling(dp, on_startup=on_startup, skip_updates=True)

