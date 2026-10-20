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

from bot.config import settings
from bot.database import db

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
