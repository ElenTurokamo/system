from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from bot import profile
from bot.database import db

router = Router(name="misc")


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user or user["reg_state"] != "done":
        await message.answer("Ты ещё не зарегистрирован. Отправь /start.")
        return

    # /profile просто показывает актуальную закреплённую сводку (создаст, если её нет)
    await profile.sync_profile_message(message.bot, message.from_user.id)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "/start — регистрация\n"
        "/profile — обновить и показать закреплённую сводку профиля\n"
        "/bind_group — привязать текущую группу (вызывать внутри группы)\n"
        "/group_id — узнать ID текущей группы"
    )
