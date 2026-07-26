"""
Закреплённая сводка профиля игрока.

Идея: одно сообщение в личке с ботом, которое создаётся один раз (в конце
регистрации) и затем просто редактируется — при завершении испытания,
при просрочке, при повторной привязке группы и т.п. Никакого мусора в чате.
"""
import logging
from datetime import datetime

from aiogram import Bot

from bot.config import TIME_OF_DAY_LABELS, XP_PER_LEVEL
from bot.database import db

logger = logging.getLogger(__name__)


def render_profile_text(user: dict) -> str:
    penalty_line = ""
    if user.get("penalty_until"):
        until = datetime.fromisoformat(user["penalty_until"])
        if until > datetime.utcnow():
            penalty_line = f"\n🚫 Штраф активен до {until.strftime('%Y-%m-%d %H:%M UTC')}"

    xp_into_level = user["xp"] % XP_PER_LEVEL
    time_label = TIME_OF_DAY_LABELS.get(user["time_of_day"], "—")
    group_line = "да ✅" if user["group_id"] else "нет (напиши /bind_group в группе)"

    return (
        "『Профиль Игрока』\n\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"🏆 Уровень: {user['level']} ({xp_into_level}/{XP_PER_LEVEL} XP)\n"
        f"✨ Всего опыта: {user['xp']} XP\n"
        f"🔥 Стрик: {user['streak']} дней\n"
        f"⏰ Время испытаний: {time_label}\n"
        f"👥 Группа привязана: {group_line}\n\n"
        f"🥊 Отжимания: {user['daily_pushups']}\n"
        f"🦵 Приседания: {user['daily_squats']}\n"
        f"🔥 Пресс: {user['daily_abs']}\n"
        f"♟ Шахматы (партий): {user['daily_chess']}\n"
        f"📖 Чтение (страниц): {user['daily_reading']}"
        f"{penalty_line}\n\n"
        f"<i>Обновлено: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</i>"
    )


async def sync_profile_message(bot: Bot, user_id: int):
    """
    Обновляет закреплённую сводку профиля пользователя.
    Если сообщения ещё нет (или его удалили) — создаёт новое и закрепляет.
    """
    user = await db.get_user(user_id)
    if not user:
        return

    text = render_profile_text(user)

    if user.get("profile_message_id"):
        try:
            await bot.edit_message_text(
                text,
                chat_id=user["profile_chat_id"],
                message_id=user["profile_message_id"],
            )
            return
        except Exception as e:
            logger.info(
                "Не удалось обновить сводку профиля %s (пересоздаю): %s", user_id, e
            )

    try:
        msg = await bot.send_message(user_id, text)
        await db.set_profile_message(user_id, user_id, msg.message_id)
        try:
            await bot.pin_chat_message(user_id, msg.message_id, disable_notification=True)
        except Exception as e:
            logger.info("Не удалось закрепить сводку профиля %s: %s", user_id, e)
    except Exception as e:
        logger.warning("Не удалось отправить сводку профиля %s: %s", user_id, e)
