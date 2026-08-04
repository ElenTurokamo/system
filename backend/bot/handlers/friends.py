"""
/add_friend и /friendlist - "Отряд" (список друзей), см. friends_list-концепцию.

Логика отображения (иконки статуса, рендер списка/брифа) живёт в bot/friends.py -
этот файл только маршрутизирует команды и колбэки, по аналогии с тем, как
challenge.py устроен вокруг challenge_render.py.
"""
import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot import challenge_render
from bot import friends
from bot import keyboards as kb
from bot.config import level_from_total_xp
from bot.database import db
from bot.profile import display_name
from bot.utils import AUTO_DELETE_SECONDS, schedule_delete, send_command_message

logger = logging.getLogger(__name__)
router = Router(name="friends")

NOT_REGISTERED = "Сначала пройди регистрацию: /start"

# Единая формулировка для карточки запроса кода - без пояснений "где искать",
# аудитория не нуждается в подсказках такого уровня; тон - как и везде в
# боте, короткая прямая команда, а не объяснение с примером.
FRIEND_CODE_PROMPT = "Введи код игрока (см. /profile) - отправлю запрос в отряд."


async def _is_awaiting_friend_code(message: Message) -> bool:
    if not message.text:
        return False
    user = await db.get_user(message.from_user.id)
    return bool(user and user.get("awaiting_friend_code"))


async def _expire_friend_prompt(bot: Bot, user_id: int, prompt_message_id: int) -> None:
    """
    Если игрок не ответил на запрос кода за AUTO_DELETE_SECONDS - снимаем режим
    ожидания (иначе следующее случайное текстовое сообщение улетело бы в поиск
    по коду). Сверяемся не с самим фактом ожидания, а с ИМЕННО ЭТИМ message_id:
    если игрок уже успел вызвать /add_friend заново, friend_prompt_message_id
    в базе будет указывать на новый промпт, и эта устаревшая задача должна
    тихо выйти, а не затереть актуальное состояние.
    """
    await asyncio.sleep(AUTO_DELETE_SECONDS)
    user = await db.get_user(user_id)
    if not user or user.get("friend_prompt_message_id") != prompt_message_id:
        return
    await db.set_awaiting_friend_code(user_id, None)
    try:
        await bot.delete_message(user_id, prompt_message_id)
    except Exception:
        pass


@router.message(Command("add_friend"))
async def cmd_add_friend(message: Message, bot: Bot):
    """
    /add_friend сама (как и любая другая команда) удаляется автоматически
    DeleteCommandsMiddleware сразу после обработки - здесь только просим код и
    ждём ответа. Всё, что появится дальше по ходу этой команды (подсказка,
    ответ игрока), живёт под одним и тем же command_key "add_friend" - значит,
    новое сообщение всегда убирает предыдущее (см. send_command_message), в
    том числе сам промпт исчезает, как только придёт итог: успех или ошибка.
    """
    user = await db.get_user(message.from_user.id)
    if not user or user["reg_state"] != "done":
        await send_command_message(bot, message.chat.id, "add_friend", NOT_REGISTERED)
        return

    prompt = await send_command_message(bot, message.chat.id, "add_friend", FRIEND_CODE_PROMPT)
    await db.set_awaiting_friend_code(message.from_user.id, prompt.message_id)
    asyncio.create_task(_expire_friend_prompt(bot, message.from_user.id, prompt.message_id))


@router.message(_is_awaiting_friend_code)
async def on_friend_code_entered(message: Message, bot: Bot):
    code = (message.text or "").strip()

    # Сообщение игрока с кодом само по себе - лишний "мусор" в чате, чистим
    # сразу, независимо от исхода поиска ниже.
    try:
        await message.delete()
    except Exception:
        pass
    await db.set_awaiting_friend_code(message.from_user.id, None)

    target = await db.find_user_by_player_code(code)
    error = None
    if not target:
        error = "Код не найден. Проверь его через /profile и повтори /add_friend."
    elif target["user_id"] == message.from_user.id:
        error = "Нельзя добавить в друзья самого себя."
    else:
        existing = await db.find_friendship(message.from_user.id, target["user_id"])
        if existing and existing["status"] == "accepted":
            error = "Вы уже в друзьях."
        elif existing and existing["status"] == "pending":
            error = "Запрос уже отправлен, ждём ответа."

    if error:
        # Один и тот же command_key "add_friend" - это сообщение заменит
        # собой промпт "Введи код игрока...", он не остаётся висеть рядом.
        await send_command_message(bot, message.chat.id, "add_friend", error)
        return

    friendship_id = await db.create_friend_request(message.from_user.id, target["user_id"])

    requester = await db.get_user(message.from_user.id)
    level, _, _ = level_from_total_xp(requester["xp"])
    notice = (
        "🔔 Обнаружен запрос на объединение.\n\n"
        f"Игрок {display_name(requester)} (уровень {level}) хочет добавить тебя в друзья."
    )
    try:
        await bot.send_message(
            target["user_id"], notice, reply_markup=kb.friend_request_kb(friendship_id)
        )
    except Exception as e:
        logger.info("Не удалось отправить запрос в друзья пользователю %s: %s", target["user_id"], e)
        await db.delete_friendship(friendship_id)
        await send_command_message(
            bot, message.chat.id, "add_friend",
            "Не удалось отправить запрос - похоже, друг ещё не писал этому боту.",
        )
        return

    await send_command_message(
        bot, message.chat.id, "add_friend", f"Запрос в друзья отправлен игроку {display_name(target)}."
    )


@router.callback_query(F.data.startswith("freq:"))
async def on_friend_request_response(call: CallbackQuery, bot: Bot):
    _, friendship_id, action = call.data.split(":")
    friendship_id = int(friendship_id)

    friendship = await db.get_friendship(friendship_id)
    if not friendship or friendship["addressee_id"] != call.from_user.id or friendship["status"] != "pending":
        await call.answer("Запрос уже неактуален.", show_alert=True)
        return

    if action == "accept":
        await db.accept_friendship(friendship_id)
        await call.message.edit_text(
            "✅ Заявка принята. Теперь вы видите прогресс друг друга в /friendlist."
        )

        addressee = await db.get_user(friendship["addressee_id"])
        try:
            notice = await bot.send_message(
                friendship["requester_id"],
                f"✅ {display_name(addressee)} принял(а) твой запрос в друзья.",
            )
            schedule_delete(bot, notice.chat.id, notice.message_id)
        except Exception as e:
            logger.info(
                "Не удалось уведомить инициатора %s о принятой заявке: %s",
                friendship["requester_id"], e,
            )
    else:
        await db.delete_friendship(friendship_id)
        await call.message.edit_text("❌ Заявка отклонена.")

    await call.answer()


@router.message(Command("friendlist"))
async def cmd_friendlist(message: Message, bot: Bot):
    user = await db.get_user(message.from_user.id)
    if not user or user["reg_state"] != "done":
        await send_command_message(bot, message.chat.id, "friendlist", NOT_REGISTERED)
        return

    friend_rows = await friends.build_friend_rows(message.from_user.id)
    text, markup = friends.render_list(friend_rows, page=0)
    await send_command_message(bot, message.chat.id, "friendlist", text, reply_markup=markup)


@router.callback_query(F.data == "flist:noop")
async def on_flist_noop(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("flist:page:"))
async def on_flist_page(call: CallbackQuery, bot: Bot):
    page = int(call.data.split(":")[2])
    friend_rows = await friends.build_friend_rows(call.from_user.id)
    text, markup = friends.render_list(friend_rows, page)

    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    # Любое взаимодействие со списком продлевает ему жизнь ещё на AUTO_DELETE_SECONDS -
    # иначе он мог бы исчезнуть прямо во время листания/чтения брифа.
    schedule_delete(bot, call.message.chat.id, call.message.message_id)
    await call.answer()


@router.callback_query(F.data.startswith("flist:open:"))
async def on_flist_open(call: CallbackQuery, bot: Bot):
    _, _, friend_id, page = call.data.split(":")
    friend_id, page = int(friend_id), int(page)

    friend_ids = await db.get_friend_ids(call.from_user.id)
    if friend_id not in friend_ids:
        await call.answer("Это уже не твой друг.", show_alert=True)
        return

    friend = await db.get_user(friend_id)
    if not friend:
        await call.answer()
        return

    text, markup = await friends.render_brief(friend, call.from_user.id, page)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    schedule_delete(bot, call.message.chat.id, call.message.message_id)
    await call.answer()


@router.callback_query(F.data.startswith("flist:remove:"))
async def on_flist_remove(call: CallbackQuery, bot: Bot):
    """
    Удаление друга из "Отряда". Без подтверждения (нажатие сразу удаляет) и
    БЕЗ каких-либо уведомлений в чат - ни тому, кто удалил, ни удалённому:
    только тихое обновление списка у инициатора. Дружба двусторонняя (см.
    Database.get_friend_ids), поэтому удаляется единственная строка в
    friendships независимо от того, кто изначально отправлял заявку.
    """
    _, _, friend_id, page = call.data.split(":")
    friend_id, page = int(friend_id), int(page)

    friendship = await db.find_friendship(call.from_user.id, friend_id)
    if friendship and friendship["status"] == "accepted":
        await db.delete_friendship(friendship["id"])

    friend_rows = await friends.build_friend_rows(call.from_user.id)
    text, markup = friends.render_list(friend_rows, page)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    schedule_delete(bot, call.message.chat.id, call.message.message_id)
    await call.answer()


@router.callback_query(F.data.startswith("flist:cheer:"))
async def on_flist_cheer(call: CallbackQuery, bot: Bot):
    _, _, friend_id, page = call.data.split(":")
    friend_id, page = int(friend_id), int(page)

    friend_ids = await db.get_friend_ids(call.from_user.id)
    if friend_id not in friend_ids:
        await call.answer("Это уже не твой друг.", show_alert=True)
        return

    active = await db.get_active_challenge(friend_id)
    if not active:
        # Гонка: пока брат смотрел бриф, друг уже закрыл/потерял испытание.
        await call.answer("У друга сейчас нет активного испытания.", show_alert=True)
        friend = await db.get_user(friend_id)
        text, markup = await friends.render_brief(friend, call.from_user.id, page)
        try:
            await call.message.edit_text(text, reply_markup=markup)
        except Exception:
            pass
        schedule_delete(bot, call.message.chat.id, call.message.message_id)
        return

    now_supporting = await db.toggle_cheer(active["id"], call.from_user.id)
    # Обновляем карточку испытания у самого друга - именно там (а не пушем)
    # он увидит строку "🤝 Верят в тебя: ...".
    await challenge_render.push_challenge_update(bot, active["id"])

    friend = await db.get_user(friend_id)
    text, markup = await friends.render_brief(friend, call.from_user.id, page)
    try:
        await call.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    schedule_delete(bot, call.message.chat.id, call.message.message_id)

    await call.answer("Поддержка отправлена!" if now_supporting else "Поддержка снята.")
