"""
Политика провалов: автоматическая остановка Системы после N (по умолчанию 2)
проваленных ежедневных испытаний ПОДРЯД.

Провалом считается и просрочка по таймауту, и "Сдаться" - оба пути вызывают
register_failure() ровно один раз на испытание (оба защищены условными UPDATE
в Database.expire_challenge / Database.give_up_challenge). Выполненное
испытание обнуляет счётчик (handlers/challenge.py::on_finish_challenge).

Остановка - это тот же флаг users.is_stopped, что и у ручной команды /stop:
ежедневные испытания не выдаются, пока игрок не наберёт /start.
"""
import logging

from aiogram import Bot

from bot import messages as tx
from bot.config import settings
from bot.database import db
from bot.utils import call_with_retries

logger = logging.getLogger(__name__)


async def register_failure(user_id: int) -> bool:
    """
    Засчитывает провал испытания. Если подряд проваленных стало
    settings.auto_stop_after_failures или больше - останавливает Систему.
    Возвращает True, если Система остановлена этим вызовом.

    Вызывать ДО обновления профиля, чтобы строка об остановке попала в него.
    """
    count = await db.register_failure(user_id)
    if count < settings.auto_stop_after_failures:
        return False

    user = await db.get_user(user_id)
    if user and user.get("is_stopped"):
        return False  # уже остановлена (например, вручную) - ничего не меняем

    await db.set_stopped(user_id, True)
    logger.info(
        "Система автоматически остановлена для %s: провалов подряд %s", user_id, count
    )
    return True


async def post_failure_report(bot: Bot, user_id: int, *, streak_lost: int) -> None:
    """
    Дополнительное сообщение в привязанную группу о провале испытания (таймаут или
    "Сдаться"): игрок потерпел поражение, на него наложен штраф, серия сброшена.
    Личная карточка провала игроку (messages.challenge_failed) приходит отдельно.

    Вызывать ровно из тех мест, где сработал условный UPDATE провала (expire_challenge /
    give_up_challenge) - тогда дубля не будет. streak_lost - серия ДО сброса. Любой сбой
    только логируется: штраф уже наложен, и недоступная группа ничего ломать не должна.
    """
    try:
        user = await db.get_user(user_id)
        if not user or not user.get("group_id"):
            return
        group_id = user["group_id"]
        text = tx.group_failure_report(
            user_id,
            xp_loss=settings.missed_day_xp_penalty,
            penalty_hours=settings.penalty_hours,
            streak_lost=streak_lost,
        )
        await call_with_retries(
            lambda: bot.send_message(group_id, text),
            attempts=3,
            delay=settings.profile_retry_delay_seconds,
            what=f"сообщение о поражении игрока {user_id} в группу",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось отправить сообщение о поражении в группу (игрок %s): %s", user_id, e)
