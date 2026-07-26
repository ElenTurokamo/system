import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot import keyboards as kb
from bot import messages as tx
from bot import profile
from bot.config import settings
from bot.database import db

logger = logging.getLogger(__name__)


async def dispatch_daily_challenges(bot: Bot, time_of_day: str):
    users = await db.get_users_by_time(time_of_day)
    for user in users:
        # Не создаём новое испытание, если предыдущее ещё не закрыто
        active = await db.get_active_challenge(user["user_id"])
        if active:
            continue

        focuses = await db.get_focuses(user["user_id"])
        if not focuses:
            continue

        challenge_id = await db.create_challenge(user["user_id"])
        text = tx.challenge_start(
            streak=user["streak"],
            level=user["level"],
            xp=user["xp"],
            timeout=settings.challenge_timeout_hours,
        )
        try:
            msg = await bot.send_message(
                user["user_id"], text, reply_markup=kb.challenge_kb(challenge_id, focuses)
            )
            await db.set_message_id(challenge_id, msg.message_id)
        except Exception as e:
            logger.warning("Не удалось отправить испытание пользователю %s: %s", user["user_id"], e)


async def expire_stale_challenges(bot: Bot):
    stale = await db.get_expirable_challenges(settings.challenge_timeout_hours)
    for challenge in stale:
        await db.expire_challenge(challenge["id"])
        await db.reset_streak(challenge["user_id"])
        try:
            await bot.send_message(challenge["user_id"], tx.expired())
        except Exception as e:
            logger.warning("Не удалось уведомить об истечении %s: %s", challenge["user_id"], e)

        await profile.sync_profile_message(bot, challenge["user_id"])


def setup_scheduler(bot: Bot) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=settings.tz)

    for time_of_day, hhmm in settings.time_slots.items():
        hour, minute = map(int, hhmm.split(":"))
        scheduler.add_job(
            dispatch_daily_challenges,
            CronTrigger(hour=hour, minute=minute),
            args=[bot, time_of_day],
            id=f"dispatch_{time_of_day}",
            replace_existing=True,
        )

    # Проверка просроченных испытаний каждые 15 минут
    scheduler.add_job(
        expire_stale_challenges,
        "interval",
        minutes=15,
        args=[bot],
        id="expire_check",
        replace_existing=True,
    )

    return scheduler
