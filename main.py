import asyncio, logging
from aiogram import Bot, Dispatcher
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from core.config import cfg
from telegram.handlers import router
from strategy.scanner import Scanner
from exchange.bingx import BingXClient
from core.state import state

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("main")

async def main():
    log.info("Запуск Герчик Бота v2...")
    bot = Bot(token=cfg.TELEGRAM_TOKEN)
    dp = Dispatcher()
    dp.include_router(router)
    exchange = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    scanner = Scanner(exchange, bot)
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(scanner.scan_all, "cron", minute=f"*/{cfg.SCAN_H1_INTERVAL_MIN}", id="scan_h1")
    scheduler.add_job(scanner.update_pairs, "cron", minute="0", id="update_pairs")
    scheduler.add_job(scanner.monitor_positions, "interval", seconds=30, id="monitor")
    scheduler.add_job(scanner.daily_report, "cron", hour="23", minute="55", id="daily")
    scheduler.start()
    balance = await exchange.get_balance()
    await bot.send_message(cfg.TELEGRAM_CHAT_ID,
        f"✅ Герчик Бот v2 запущен\n\nРежим: {cfg.MODE}\nТФ: H1+H4+D1\nБаланс: {balance:.2f} USDT\nРиск: {cfg.RISK_PER_TRADE}%\n\n/help — команды",
        parse_mode="HTML")
    await scanner.update_pairs()
    await scanner.scan_all()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
