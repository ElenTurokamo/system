import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot import profile
from bot.config import settings
from bot.database import db
from bot.handlers import challenge, friends, group, misc, start, stats
from bot.middlewares import DeleteCommandsMiddleware
from bot.scheduler import setup_scheduler
from bot.state_store import state
from bot.utils import resume_pending_deletes

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)


async def backfill_group_links(bot: Bot) -> None:
    """Запрашивает ссылки для групп, у которых их ещё нет, и обновляет профили. Сбои только логируются."""
    try:
        users = await db.get_users_without_group_link()
    except Exception as e:  # noqa: BLE001
        logger.warning("Дозаполнение ссылок на группы: не удалось прочитать игроков: %s", e)
        return
    for row in users:
        try:
            link = await profile.refresh_group_link(bot, row["user_id"], row["group_id"])
            if link:
                await profile.sync_profile_message(bot, row["user_id"])
        except Exception as e:  # noqa: BLE001
            logger.warning("Дозаполнение ссылки на группу (игрок %s): %s", row["user_id"], e)


async def main():
    # Хранилище состояния подключается ДО основной БД: при её подключении ключи сообщений
    # сверяются с ним (Database.reconcile_message_keys) - после отката db.sql из бэкапа
    # профиль игрока снова можно редактировать. Бот работает и без него (только логирует).
    if await state.connect():
        logger.info("Хранилище состояния подключено: %s", settings.state_db_path)
    await db.connect()
    logger.info("База данных подключена: %s", settings.db_path)

    # link_preview_is_disabled: в профиле теперь есть ссылка на группу - без этого Telegram
    # прикреплял бы к закреплённому сообщению огромную карточку-превью группы.
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = Dispatcher()
    dp.message.middleware(DeleteCommandsMiddleware())

    dp.include_router(start.router)
    dp.include_router(group.router)
    dp.include_router(challenge.router)
    dp.include_router(stats.router)
    dp.include_router(friends.router)
    dp.include_router(misc.router)

    # Автоудаления, запланированные до перезапуска (ответы на команды, /help с гайдом и т.п.).
    await resume_pending_deletes(bot)

    scheduler = setup_scheduler(bot)
    scheduler.start()
    logger.info("Планировщик запущен (TZ=%s)", settings.tz)

    # Разовое дозаполнение ссылок на группы у игроков, привязавших группу до появления
    # строки «Группа: ссылка» в профиле. Фоновой задачей: сетевые сбои не задерживают старт.
    backfill_task = asyncio.create_task(backfill_group_links(bot))

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        backfill_task.cancel()
        await db.close()
        await state.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
