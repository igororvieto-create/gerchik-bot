import asyncio
import logging
import sys
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from core.config import cfg
from strategy.scanner import Scanner
from exchange.bingx import BingXClient
from telegram.handlers import register_handlers

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("main")

scheduler = AsyncIOScheduler(timezone="UTC")

async def main():
    from aiogram import Bot, Dispatcher

    log.info("=== Герчик Бот стартует ===")
    log.info(f"TOKEN  set: {bool(cfg.TELEGRAM_TOKEN)} len={len(cfg.TELEGRAM_TOKEN)}")
    log.info(f"CHATID set: {cfg.TELEGRAM_CHAT_ID!r}")
    log.info(f"APIKEY set: {bool(cfg.BINGX_API_KEY)}")
    log.info(f"SECRET set: {bool(cfg.BINGX_SECRET)}")
    log.info(f"MODE: {cfg.MODE}")

    bot = Bot(token=cfg.TELEGRAM_TOKEN)
    dp  = Dispatcher()

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Вебхук удалён")
    except Exception as e:
        log.warning(f"delete_webhook (non-fatal): {e}")

    register_handlers(dp)

    exchange = BingXClient(cfg.BINGX_API_KEY, cfg.BINGX_SECRET)
    scanner  = Scanner(exchange, bot)

    scheduler.add_job(scanner.scan_all,          "cron",     minute="*/15")
    scheduler.add_job(scanner.update_pairs,      "cron",     minute="0")
    scheduler.add_job(scanner.monitor_positions, "interval", seconds=30)
    scheduler.add_job(scanner.daily_report,      "cron",     hour="9")
    scheduler.start()
    log.info("Планировщик запущен")

    # Startup tasks run in background so polling starts immediately
    async def startup_tasks():
        try:
            balance = await exchange.get_balance()
            await bot.send_message(
                cfg.TELEGRAM_CHAT_ID,
                f"✅ Герчик Бот запущен\n\nРежим: {cfg.MODE}\nБаланс: {balance:.2f} USDT"
            )
            log.info(f"Стартовое сообщение отправлено. Баланс: {balance:.2f} USDT")
        except Exception as e:
            log.error(f"Startup notify error: {e}")
        try:
            await scanner.update_pairs()
            await scanner.scan_all()
        except Exception as e:
            log.error(f"Startup scan error: {e}")

    asyncio.create_task(startup_tasks())

    log.info("Запускаем polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    log.info("python main.py запущен")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Остановлен вручную")
    except Exception as e:
        log.critical(f"FATAL: {e}", exc_info=True)
        sys.exit(1)
