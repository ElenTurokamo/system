import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot import backup
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

        # Профиль обновляется ПЕРЕД отправкой карточки испытания, а не после -
        # так в чате профиль всегда оказывается выше нового квеста, а не
        # наоборот (испытание создано в БД строкой выше, поэтому отметка
        # "Вы выполнили сегодняшний квест" в профиле уже корректно исчезает).
        await profile.sync_profile_message(bot, user["user_id"])

        try:
            await challenge_render.send_challenge_message(bot, challenge_id)
        except Exception as e:
            logger.warning("Не удалось отправить испытание пользователю %s: %s", user["user_id"], e)


async def expire_stale_challenges(bot: Bot):
    stale = await db.get_expirable_challenges(settings.challenge_timeout_hours)
    for challenge in stale:
        user_id = challenge["user_id"]

        await db.expire_challenge(challenge["id"])
        await db.record_daily_stats(challenge["id"])
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
            tx.challenge_failed(
                "timeout", xp_loss=settings.missed_day_xp_penalty, penalty_hours=settings.penalty_hours
            ),
        )

        await profile.sync_profile_message(bot, user_id)


async def refresh_challenge_timers(bot: Bot):
    """
    Перерисовывает карточку испытания каждому игроку с активным (awaiting_action)
    испытанием, чтобы таймер "До провала испытания осталось..." шёл в реальном
    времени, а не замирал до следующего действия игрока в карточке.
    """
    challenges = await db.get_awaiting_challenges()
    for challenge in challenges:
        await challenge_render.push_challenge_update(bot, challenge["id"])


async def refresh_stale_profiles(bot: Bot):
    """
    Превентивно пересоздаёт сообщение профиля, если оно приближается к
    границе, после которой Telegram Bot API перестаёт разрешать его
    редактирование (см. settings.profile_edit_window_hours/profile_refresh_buffer_hours).

    Без этого sync_profile_message продолжал бы молча editMessageText, пока
    Telegram не начнёт отвечать ошибкой - и только тогда (реактивно, с
    заметной задержкой до следующего апдейта) пересоздал бы сообщение. Здесь
    же пересоздание происходит заранее и планово, так что игрок никогда не
    видит ни ошибки, ни "залипший" старый профиль.
    """
    users = await db.get_users_with_stale_profile(
        settings.profile_edit_window_hours, settings.profile_refresh_buffer_hours
    )
    for user in users:
        await profile.resend_profile_message(bot, user["user_id"])


async def cleanup_failure_messages(bot: Bot):
    """
    Удаляет пуш-уведомления о провале испытания, которые висят в чате уже
    дольше settings.failure_message_ttl_minutes (по умолчанию - час), чтобы
    чат оставался чистым, даже если игрок не откроет бота до выдачи
    следующего ежедневного квеста (при котором это сообщение тоже удаляется -
    см. profile.clear_failure_message).
    """
    users = await db.get_users_with_expired_failure_message(settings.failure_message_ttl_minutes)
    for user in users:
        await profile.clear_failure_message(bot, user)


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

    # Превентивное пересоздание "постаревшего" сообщения профиля, пока Telegram
    # ещё разрешает его отредактировать в последний раз (см. docstring выше) -
    # раз в 30 минут этого достаточно с запасом (buffer по умолчанию - 2 часа).
    scheduler.add_job(
        refresh_stale_profiles,
        "interval",
        minutes=30,
        args=[bot],
        id="profile_freshness_check",
        replace_existing=True,
    )

    # Удаление пуш-уведомлений о провале испытания через час после отправки -
    # раз в 5 минут, чтобы сообщение не задерживалось в чате надолго сверх TTL.
    scheduler.add_job(
        cleanup_failure_messages,
        "interval",
        minutes=5,
        args=[bot],
        id="failure_message_cleanup",
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

    # Обновление таймера "до провала испытания" в карточке испытания - раз в минуту
    scheduler.add_job(
        refresh_challenge_timers,
        "interval",
        minutes=1,
        args=[bot],
        id="challenge_timer_refresh",
        replace_existing=True,
    )

    # Автобэкап БД в приватный GitHub-репозиторий - дважды в неделю, в дни,
    # максимально равноудалённые друг от друга (по умолчанию пн/чт). Если
    # BACKUP_REPO_URL не задан - job просто не регистрируется.
    if settings.backup_repo_url:
        b_hour, b_minute = map(int, settings.backup_time.split(":"))
        scheduler.add_job(
            backup.run_backup,
            CronTrigger(
                day_of_week=f"{settings.backup_day_1},{settings.backup_day_2}",
                hour=b_hour,
                minute=b_minute,
            ),
            args=[bot],
            id="db_backup",
            replace_existing=True,
        )
    else:
        logger.info("BACKUP_REPO_URL не задан - автобэкап БД в git отключён.")

    return scheduler
