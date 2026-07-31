import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import settings
from bot.database import db
from bot.handlers import challenge, group, misc, start, stats
from bot.middlewares import DeleteCommandsMiddleware
from bot.scheduler import setup_scheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)


async def main():
    await db.connect()
    logger.info("База данных подключена: %s", settings.db_path)

    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.message.middleware(DeleteCommandsMiddleware())

    dp.include_router(start.router)
    dp.include_router(group.router)
    dp.include_router(challenge.router)
    dp.include_router(stats.router)
    dp.include_router(misc.router)

    scheduler = setup_scheduler(bot)
    scheduler.start()
    logger.info("Планировщик запущен (TZ=%s)", settings.tz)

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
