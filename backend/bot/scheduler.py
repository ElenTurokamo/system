import asyncio
import logging

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from bot import backup
from bot import challenge_render
from bot import failure_policy
from bot import messages as tx
from bot import profile
from bot.config import settings
from bot.database import db

logger = logging.getLogger(__name__)


async def dispatch_daily_challenges(bot: Bot, time_of_day: str):
    users = await db.get_users_by_time(time_of_day)
    for user in users:
        # Ошибка на одном игроке (заблокировал бота, сбой БД и т.п.) не должна
        # обрывать рассылку всем остальным в этом слоте.
        try:
            await _dispatch_for_user(bot, user)
        except Exception:
            logger.exception("Сбой выдачи испытания пользователю %s", user.get("user_id"))


async def _dispatch_for_user(bot: Bot, user: dict):
    # Не создаём новое испытание, если предыдущее ещё не закрыто
    active = await db.get_active_challenge(user["user_id"])
    if active:
        return

    # После провала испытания (таймаут или "Сдаться") на игрока накладывается
    # ограничение на penalty_hours часов (см. таймер в профиле). Всё это время
    # новые испытания не выдаются - слоты, попавшие в этот срок, просто
    # пропускаются. Данные берутся из БД заново, а не из списка users, чтобы
    # учесть штраф, наложенный уже после того, как список был получен.
    fresh_user = await db.get_user(user["user_id"]) or user

    # Система могла быть остановлена уже после получения списка users (второй
    # провал подряд / /stop в эту же секунду) - тогда ничего не выдаём.
    if fresh_user.get("is_stopped"):
        return

    if db.is_penalty_active(fresh_user):
        logger.info(
            "Испытание не выдано пользователю %s: действует ограничение до %s (UTC)",
            user["user_id"], fresh_user.get("penalty_until"),
        )
        # Профиль всё равно обслуживаем: иначе за 48 часов паузы, пока
        # диспетчер ничего не трогает, сообщение профиля могло бы "состариться"
        # настолько, что Telegram перестанет давать его редактировать.
        try:
            await profile.ensure_fresh_profile_message(bot, user["user_id"])
        except Exception:
            logger.exception("Не удалось обновить профиль пользователя %s", user["user_id"])
        return
    user = fresh_user

    focuses = await db.get_focuses(user["user_id"])
    if not focuses:
        return

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
    #
    # Именно здесь (а не отдельной фоновой джобой) профиль ещё и
    # ПЕРЕСОЗДАЁТСЯ, если это нужно (см. profile.ensure_fresh_profile_message) -
    # так пересоздание не может случиться чаще раза в день и происходит
    # только в момент выдачи квеста, во время, которое сам игрок задал.
    try:
        await profile.ensure_fresh_profile_message(bot, user["user_id"])
    except Exception:
        # Сбой профиля не должен лишать игрока карточки уже созданного квеста.
        logger.exception("Не удалось обновить профиль перед выдачей испытания %s", user["user_id"])

    try:
        await challenge_render.send_challenge_message(bot, challenge_id)
    except Exception as e:
        logger.warning("Не удалось отправить испытание пользователю %s: %s", user["user_id"], e)


async def expire_stale_challenges(bot: Bot):
    stale = await db.get_expirable_challenges(settings.challenge_timeout_hours)
    for challenge in stale:
        try:
            await _expire_one(bot, challenge)
        except Exception:
            logger.exception("Сбой при провале испытания %s", challenge.get("id"))


async def _expire_one(bot: Bot, challenge: dict):
    user_id = challenge["user_id"]

    # Условный UPDATE: если игрок успел завершить/сдать испытание, пока мы
    # разбирали список, - ничего не штрафуем (см. Database.expire_challenge).
    if not await db.expire_challenge(challenge["id"]):
        return

    await db.record_daily_stats(challenge["id"])
    await db.reset_streak(user_id)

    # Пропущенный день - штраф: -300 XP (с понижением уровня, если сгорает
    # весь запас XP текущего уровня) и ограничение на PENALTY_HOURS часов,
    # которое отображается в профиле (см. bot/profile.py::penalty_block)
    # и само обновляется там каждую минуту (см. refresh_penalty_profiles).
    # Пока ограничение действует, новые испытания не выдаются (см.
    # _dispatch_for_user).
    await db.add_xp(user_id, -settings.missed_day_xp_penalty)
    await db.set_penalty(user_id, settings.penalty_hours)

    # Второй провал подряд - Система останавливается сама (см. failure_policy).
    # Должно быть до sync_profile_message ниже, чтобы профиль уже показал паузу.
    await failure_policy.register_failure(user_id)

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

    Раз в минуту обновляются только карточки, до провала которых больше 15
    минут. Последние 15 минут (таймер в формате ММ:СС) обновляет отдельная,
    более частая джоба - refresh_final_countdown.
    """
    challenges = await db.get_awaiting_challenges()
    for challenge in challenges:
        if challenge_render.in_final_countdown(challenge):
            continue
        try:
            await challenge_render.push_challenge_update(bot, challenge["id"], from_timer=True)
        except Exception:
            logger.exception("Сбой обновления таймера испытания %s", challenge.get("id"))


# Сколько карточек одновременно редактируем в быстрой джобе: потолок, чтобы при
# большом числе игроков в последних 15 минутах не устроить Telegram шквал
# запросов за один тик (общий лимит бота - порядка 30 сообщений/сек).
_COUNTDOWN_CONCURRENCY = 10


async def refresh_final_countdown(bot: Bot):
    """
    Частая (settings.countdown_refresh_seconds) перерисовка карточек, у которых
    до провала осталось не больше 15 минут: там таймер показывается как ММ:СС.
    """
    challenges = await db.get_awaiting_challenges()
    due = [c for c in challenges if challenge_render.in_final_countdown(c)]
    if not due:
        return

    sem = asyncio.Semaphore(_COUNTDOWN_CONCURRENCY)

    async def _one(challenge: dict):
        async with sem:
            try:
                await challenge_render.push_challenge_update(bot, challenge["id"], from_timer=True)
            except Exception:
                logger.exception("Сбой обновления ММ:СС-таймера испытания %s", challenge.get("id"))

    await asyncio.gather(*(_one(c) for c in due))


async def send_failure_warnings(bot: Bot):
    """
    Предупреждения о скором провале: за 60, 30 и 15 минут до дедлайна
    (см. config.FAILURE_WARNING_MINUTES и challenge_render.maybe_send_failure_warning).
    Состояние (какое предупреждение уже отправлено) хранится в БД, поэтому
    перезапуск бота не приводит ни к дублям, ни к потере уже отправленных.
    """
    challenges = await db.get_awaiting_challenges()
    for challenge in challenges:
        try:
            await challenge_render.maybe_send_failure_warning(bot, challenge)
        except Exception:
            logger.exception("Сбой отправки предупреждения о провале %s", challenge.get("id"))


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
        try:
            await profile.sync_profile_message(bot, user["user_id"])
        except Exception:
            logger.exception("Сбой обновления профиля %s", user.get("user_id"))


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

    # Проверка просроченных испытаний - раз в минуту (раньше было раз в 15
    # минут). Теперь таймер в карточке доходит до 00:00, и игрок видит это
    # вживую - провал должен фиксироваться сразу, а не через до 15 минут после
    # нулевой отметки. Запрос дешёвый (индексированный отбор по статусу).
    scheduler.add_job(
        expire_stale_challenges,
        "interval",
        minutes=1,
        args=[bot],
        id="expire_check",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Превентивное пересоздание "постаревшего" сообщения профиля (пока Telegram
    # ещё разрешает его отредактировать в последний раз) теперь НЕ отдельная
    # фоновая джоба, а часть dispatch_daily_challenges (см. её docstring выше и
    # profile.ensure_fresh_profile_message) - так пересоздание происходит не
    # чаще раза в день и всегда в момент выдачи ежедневного квеста, в то время,
    # которое сам игрок задал, а не в случайный момент отдельного расписания.

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

    # Обновление таймера "до провала испытания" в карточке испытания - раз в
    # минуту (пока до провала больше 15 минут)
    scheduler.add_job(
        refresh_challenge_timers,
        "interval",
        minutes=1,
        args=[bot],
        id="challenge_timer_refresh",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Последние 15 минут до провала: таймер ММ:СС, обновление каждые
    # CHALLENGE_COUNTDOWN_REFRESH_SECONDS секунд (по умолчанию 5).
    scheduler.add_job(
        refresh_final_countdown,
        "interval",
        seconds=settings.countdown_refresh_seconds,
        args=[bot],
        id="challenge_final_countdown",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )

    # Предупреждения о провале за 60/30/15 минут - проверка каждые 15 секунд,
    # чтобы пуш приходил практически ровно на отметке, а не с опозданием до минуты.
    scheduler.add_job(
        send_failure_warnings,
        "interval",
        seconds=15,
        args=[bot],
        id="failure_warnings",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
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
