from datetime import datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot.config import TIME_OF_DAY_LABELS, XP_PER_LEVEL
from bot.database import db

router = Router(name="misc")


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user or user["reg_state"] != "done":
        await message.answer("Ты ещё не зарегистрирован. Отправь /start.")
        return

    penalty_line = ""
    if user["penalty_until"]:
        until = datetime.fromisoformat(user["penalty_until"])
        if until > datetime.utcnow():
            penalty_line = f"\n🚫 Штраф активен до {until.strftime('%Y-%m-%d %H:%M UTC')}"

    xp_into_level = user["xp"] % XP_PER_LEVEL
    text = (
        f"『Профиль Игрока』\n\n"
        f"Уровень: {user['level']} ({xp_into_level}/{XP_PER_LEVEL} XP)\n"
        f"Всего опыта: {user['xp']} XP\n"
        f"Стрик: {user['streak']} 🔥\n"
        f"Время испытаний: {TIME_OF_DAY_LABELS.get(user['time_of_day'], '—')}\n"
        f"Группа привязана: {'да ✅' if user['group_id'] else 'нет'}\n\n"
        f"Отжимания: {user['daily_pushups']}\n"
        f"Приседания: {user['daily_squats']}\n"
        f"Пресс: {user['daily_abs']}\n"
        f"Шахматы (партий): {user['daily_chess']}\n"
        f"Чтение (страниц): {user['daily_reading']}"
        f"{penalty_line}"
    )
    await message.answer(text)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "/start — регистрация\n"
        "/profile — статистика и уровень\n"
        "/bind_group — привязать текущую группу (вызывать внутри группы)\n"
        "/group_id — узнать ID текущей группы"
    )
