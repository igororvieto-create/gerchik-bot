import os
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s: %(message)s"
)
log = logging.getLogger("bot")

TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

async def main():
    log.info("=== СТАРТ ===")
    log.info(f"TOKEN  : {'OK len=' + str(len(TOKEN)) if TOKEN else 'ПУСТО!'}")
    log.info(f"CHAT_ID: {CHAT_ID!r}")

    if not TOKEN:
        log.error("TELEGRAM_TOKEN не задан — выход")
        return

    from aiogram import Bot, Dispatcher
    from aiogram.types import Message

    bot = Bot(token=TOKEN)
    dp  = Dispatcher()

    @dp.message()
    async def handle_all(msg: Message):
        log.info(f"Сообщение от chat_id={msg.chat.id}: {msg.text!r}")
        await msg.reply(
            f"✅ Бот работает!\n"
            f"Твой chat_id: <code>{msg.chat.id}</code>\n"
            f"Настроенный: <code>{CHAT_ID}</code>",
            parse_mode="HTML"
        )

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Вебхук удалён, очередь очищена")
    except Exception as e:
        log.warning(f"delete_webhook: {e}")

    log.info("Polling запущен — жду сообщений...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
