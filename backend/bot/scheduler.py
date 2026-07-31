import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot import challenge_render
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

        # Новый ежедневный квест выдаётся - удаляем отчёт о вчерашнем провале
        # (если он был), чтобы он не копился в чате.
        await profile.clear_failure_message(bot, user)

        quest_text = tx.challenge_start(
            streak=user["streak"],
            level=user["level"],
            xp=user["xp"],
            timeout=settings.challenge_timeout_hours,
        )
        challenge_id = await db.create_challenge(user["user_id"], quest_text, focuses)

        try:
            await challenge_render.send_challenge_message(bot, challenge_id)
        except Exception as e:
            logger.warning("Не удалось отправить испытание пользователю %s: %s", user["user_id"], e)

        # Новое испытание создано (status снова awaiting_action) - отметка
        # "Вы выполнили сегодняшний квест" в профиле должна исчезнуть сама.
        await profile.sync_profile_message(bot, user["user_id"])


async def expire_stale_challenges(bot: Bot):
    stale = await db.get_expirable_challenges(settings.challenge_timeout_hours)
    for challenge in stale:
        user_id = challenge["user_id"]

        await db.expire_challenge(challenge["id"])
        await db.reset_streak(user_id)

        # Пропущенный день - штраф: -300 XP (с понижением уровня, если сгорает
        # весь запас XP текущего уровня) и ограничение на PENALTY_HOURS часов,
        # которое отображается в профиле (см. bot/profile.py::penalty_block)
        # и само обновляется там каждую минуту (см. refresh_penalty_profiles).
        await db.add_xp(user_id, -settings.missed_day_xp_penalty)
        await db.set_penalty(user_id, settings.penalty_hours)

        await challenge_render.close_failed_challenge(
            bot,
            challenge["id"],
            tx.expired(xp_loss=settings.missed_day_xp_penalty, penalty_hours=settings.penalty_hours),
        )

        await profile.sync_profile_message(bot, user_id)


async def refresh_penalty_profiles(bot: Bot):
    """
    Перерисовывает профиль каждому игроку с активным (или только что истёкшим)
    ограничением, чтобы таймер "До снятия ограничений осталось..." шёл в
    реальном времени, а не замирал до следующего произвольного действия игрока.
    """
    users = await db.get_users_with_active_penalty()
    for user in users:
        await profile.sync_profile_message(bot, user["user_id"])


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

    # Обновление таймера "до снятия ограничений" в профиле - раз в минуту
    scheduler.add_job(
        refresh_penalty_profiles,
        "interval",
        minutes=1,
        args=[bot],
        id="penalty_refresh",
        replace_existing=True,
    )

    return scheduler
