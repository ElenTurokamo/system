from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import ChatMemberUpdated, Message

from bot import profile
from bot.database import db
from bot.registration import finish_registration

router = Router(name="group")


async def _bind(bot: Bot, user_id: int, group_id: int) -> bool:
    """Привязывает группу к пользователю, если он зарегистрирован. Возвращает успех."""
    user = await db.get_user(user_id)
    if not user:
        return False

    await db.set_group(user_id, group_id)

    if user["reg_state"] != "done":
        await finish_registration(bot, user_id)
    else:
        # Регистрация уже была завершена ранее - просто обновляем закреплённую сводку
        await profile.sync_profile_message(bot, user_id)

    return True


@router.my_chat_member(F.chat.type.in_({"group", "supergroup"}))
async def on_bot_added_to_group(event: ChatMemberUpdated, bot: Bot):
    """
    Срабатывает, когда меняется статус бота в группе.
    Если бота только что добавили (был left/kicked -> стал member/administrator),
    привязываем группу к пользователю, который это сделал.
    """
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status
    just_added = old_status in ("left", "kicked") and new_status in ("member", "administrator")
    if not just_added:
        return

    adder_id = event.from_user.id
    ok = await _bind(bot, adder_id, event.chat.id)

    if ok:
        await bot.send_message(
            event.chat.id,
            "『Система』 Группа привязана к профилю Игрока. Здесь будет публиковаться прогресс.",
        )
    else:
        await bot.send_message(
            event.chat.id,
            "Не удалось привязать группу автоматически: сначала пройдите регистрацию "
            "у бота в личных сообщениях (/start), затем повторите добавление в группу "
            "или используйте команду /bind_group здесь.",
        )


@router.message(Command("bind_group"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_bind_group(message: Message, bot: Bot):
    ok = await _bind(bot, message.from_user.id, message.chat.id)
    if ok:
        await message.answer(
            "『Система』 Группа привязана к твоему профилю. Прогресс будет публиковаться здесь."
        )
    else:
        await message.answer(
            "Сначала пройди регистрацию в личных сообщениях с ботом (/start), "
            "затем повтори команду здесь."
        )


@router.message(Command("group_id"), F.chat.type.in_({"group", "supergroup"}))
async def cmd_group_id(message: Message):
    await message.answer(f"ID этой группы: <code>{message.chat.id}</code>")
