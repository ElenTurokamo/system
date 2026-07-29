"""
Закреплённая сводка профиля игрока.

Идея: одно сообщение в личке с ботом, которое создаётся один раз (в конце
регистрации) и затем просто редактируется — при завершении испытания,
при просрочке, при повторной привязке группы и т.п. Никакого мусора в чате.
"""
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from bot.config import time_of_day_label, XP_PER_LEVEL
from bot.database import db

logger = logging.getLogger(__name__)


def ru_days(n: int) -> str:
    """Правильное склонение слова 'день' для русского языка: 1 день, 2 дня, 5 дней и т.п."""
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return "день"
    if n_abs % 10 in (2, 3, 4) and n_abs % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def player_code(user: dict) -> str:
    """
    Уникальный короткий код игрока: дата регистрации (ГГММДД) + первые 5 цифр ID.
    Например, зарегистрировался 28.07.2026, ID 987654321 -> 260728-98765.
    """
    try:
        registered = datetime.fromisoformat(user["registered_at"])
        date_part = registered.strftime("%y%m%d")
    except (TypeError, ValueError):
        date_part = "000000"
    id_part = str(user["user_id"])[:5]
    return f"{date_part}-{id_part}"


def display_name(user: dict) -> str:
    return user.get("first_name") or user.get("username") or f"Игрок {user['user_id']}"


def render_profile_text(user: dict) -> str:
    penalty_line = ""
    if user.get("penalty_until"):
        until = datetime.fromisoformat(user["penalty_until"])
        if until > datetime.utcnow():
            penalty_line = f"\n🚫 Штраф активен до {until.strftime('%Y-%m-%d %H:%M UTC')}"

    xp_into_level = user["xp"] % XP_PER_LEVEL
    time_label = time_of_day_label(user["time_of_day"]) if user["time_of_day"] else "—"
    group_line = "да ✅" if user["group_id"] else "нет (напиши /bind_group в группе)"
    streak = user["streak"]

    header = (
        "『Профиль Игрока』\n\n"
        f"🆔 {display_name(user)} · #{player_code(user)}\n"
        f"🏆 Уровень: {user['level']} ({xp_into_level}/{XP_PER_LEVEL} XP)\n"
        f"🔥 Серия: {streak} {ru_days(streak)}\n"
        f"⏰ Время испытаний: {time_label}\n"
        f"👥 Группа привязана: {group_line}"
    )

    stats = (
        f"🥊 Отжимания: {user['daily_pushups']}\n"
        f"🦵 Приседания: {user['daily_squats']}\n"
        f"🔥 Пресс: {user['daily_abs']}\n"
        f"♟ Шахматы (партий): {user['daily_chess']}\n"
        f"📖 Чтение (страниц): {user['daily_reading']}"
    )

    return f"{header}\n\n{stats}{penalty_line}"


async def sync_profile_message(bot: Bot, user_id: int):
    """
    Обновляет закреплённую сводку профиля пользователя.
    Используется для автоматических обновлений (испытание завершено/просрочено/группа
    привязана) - лёгкий edit без пересоздания сообщения, чтобы не спамить.
    Если сообщения ещё нет (первая регистрация) или его удалили НАВСЕГДА (ошибка API) -
    создаёт новое и закрепляет.
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
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                # Текст не изменился с прошлого раза - это нормально, ничего не делаем.
                return
            logger.info(
                "Не удалось обновить сводку профиля %s (пересоздаю): %s", user_id, e
            )
        except Exception as e:
            logger.info(
                "Не удалось обновить сводку профиля %s (пересоздаю): %s", user_id, e
            )

    await _send_and_pin(bot, user_id, text)


async def resend_profile_message(bot: Bot, user_id: int):
    """
    Принудительно пересоздаёт сводку профиля: удаляет старое сообщение (если получится)
    и присылает + закрепляет новое.

    Используется командой /profile - Telegram не даёт боту способа узнать, очистил ли
    пользователь историю чата локально (edit_message_text в этом случае "успешно"
    редактирует невидимое сообщение), поэтому единственный надёжный способ гарантировать
    видимость профиля по явному запросу - переслать его заново.
    """
    user = await db.get_user(user_id)
    if not user:
        return

    text = render_profile_text(user)

    if user.get("profile_message_id"):
        try:
            await bot.delete_message(user["profile_chat_id"], user["profile_message_id"])
        except Exception:
            pass  # уже удалено/недоступно - не страшно, всё равно создадим новое

    await _send_and_pin(bot, user_id, text)


async def _send_and_pin(bot: Bot, user_id: int, text: str):
    try:
        msg = await bot.send_message(user_id, text)
        await db.set_profile_message(user_id, user_id, msg.message_id)
        try:
            await bot.pin_chat_message(user_id, msg.message_id, disable_notification=True)
        except Exception as e:
            logger.info("Не удалось закрепить сводку профиля %s: %s", user_id, e)
    except Exception as e:
        logger.warning("Не удалось отправить сводку профиля %s: %s", user_id, e)
