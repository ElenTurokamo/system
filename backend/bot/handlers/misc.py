from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot import profile
from bot.database import db

router = Router(name="misc")


@router.message(F.pinned_message, F.chat.type == "private")
async def on_pin_service_message(message: Message):
    """
    Каждое закрепление сообщения Telegram сам добавляет в чат системную запись
    вида "Бот закрепил сообщение" - это отдельное сообщение со своим message_id,
    и оно копится точно так же, как обычный текст. Раз это не несёт пользы,
    удаляем его сразу же, как только оно приходит.

    Ограничено личными чатами: в группах пины могут делать сами админы по своим
    причинам, не связанным с ботом, - трогать их не нужно.
    """
    try:
        await message.delete()
    except Exception:
        pass


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    user = await db.get_user(message.from_user.id)
    if not user or user["reg_state"] != "done":
        await message.answer("Ты ещё не зарегистрирован. Отправь /start.")
        return

    # /profile - явный запрос пользователя, поэтому гарантируем видимость: пересоздаём
    # сообщение, а не полагаемся на edit (который "успешно" редактирует даже то, что
    # пользователь мог локально скрыть/очистить у себя в истории).
    await profile.resend_profile_message(message.bot, message.from_user.id)


@router.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "/start — регистрация\n"
        "/profile — обновить и показать закреплённую сводку профиля\n"
        "/bind_group — привязать текущую группу (вызывать внутри группы)\n"
        "/group_id — узнать ID текущей группы"
    )
