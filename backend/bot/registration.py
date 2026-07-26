from aiogram import Bot

from bot import profile
from bot.database import db


async def finish_registration(bot: Bot, user_id: int):
    """
    Завершает регистрацию: убирает последний экран-шаг регистрации (если есть)
    и создаёт закреплённую сводку профиля.
    """
    await db.finish_registration(user_id)

    user = await db.get_user(user_id)
    if user and user.get("last_reg_message_id"):
        try:
            await bot.delete_message(user_id, user["last_reg_message_id"])
        except Exception:
            pass
        await db.set_last_reg_message(user_id, None)

    await profile.sync_profile_message(bot, user_id)
