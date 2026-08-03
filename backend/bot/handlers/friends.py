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
from bot.utils import schedule_delete

logger = logging.getLogger(__name__)
router = Router(name="friends")

# Если игрок не ответил на запрос кода друга - не держим состояние "ждём код"
# вечно (иначе следующее случайное текстовое сообщение улетело бы в поиск по
# коду). Подобрано равным общему таймауту ephemeral-сообщений бота.
FRIEND_CODE_PROMPT_TIMEOUT = 60


async def _is_awaiting_friend_code(message: Message) -> bool:
    if not message.text:
        return False
    user = await db.get_user(message.from_user.id)
    return bool(user and user.get("awaiting_friend_code"))


async def _expire_friend_prompt(bot: Bot, user_id: int, prompt_message_id: int) -> None:
    await asyncio.sleep(FRIEND_CODE_PROMPT_TIMEOUT)
    user = await db.get_user(user_id)
    if not user or not user.get("awaiting_friend_code"):
        return  # уже ответили - обработчик кода сам всё убрал
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
    ответ игрока), тоже будет удалено - см. on_friend_code_entered.
    """
    user = await db.get_user(message.from_user.id)
    if not user or user["reg_state"] != "done":
        sent = await message.answer("Ты ещё не зарегистрирован. Отправь /start.")
        schedule_delete(bot, sent.chat.id, sent.message_id)
        return

    prompt = await message.answer(
        "Пришли код игрока друга (он есть у него в /profile, например "
        "<code>260728-987654321</code>) - отправлю ему запрос в друзья."
    )
    await db.set_awaiting_friend_code(message.from_user.id, prompt.message_id)
    asyncio.create_task(_expire_friend_prompt(bot, message.from_user.id, prompt.message_id))


@router.message(_is_awaiting_friend_code)
async def on_friend_code_entered(message: Message, bot: Bot):
    user = await db.get_user(message.from_user.id)
    code = (message.text or "").strip()

    # Чистим и код, и подсказку сразу - "всё, связанное с этой командой,
    # удалится" (см. постановку задачи), независимо от исхода поиска ниже.
    try:
        await message.delete()
    except Exception:
        pass
    prompt_message_id = user.get("friend_prompt_message_id")
    if prompt_message_id:
        try:
            await bot.delete_message(message.chat.id, prompt_message_id)
        except Exception:
            pass
    await db.set_awaiting_friend_code(message.from_user.id, None)

    target = await db.find_user_by_player_code(code)
    error = None
    if not target:
        error = "Код не найден. Проверь его в /profile у друга и попробуй /add_friend ещё раз."
    elif target["user_id"] == message.from_user.id:
        error = "Нельзя добавить в друзья самого себя."
    else:
        existing = await db.find_friendship(message.from_user.id, target["user_id"])
        if existing and existing["status"] == "accepted":
            error = "Вы уже в друзьях."
        elif existing and existing["status"] == "pending":
            error = "Запрос уже отправлен, ждём ответа."

    if error:
        sent = await message.answer(error)
        schedule_delete(bot, sent.chat.id, sent.message_id)
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
        sent = await message.answer(
            "Не удалось отправить запрос - похоже, друг ещё не писал этому боту."
        )
        schedule_delete(bot, sent.chat.id, sent.message_id)
        return

    sent = await message.answer(f"Запрос в друзья отправлен игроку {display_name(target)}.")
    schedule_delete(bot, sent.chat.id, sent.message_id)


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
        sent = await message.answer("Ты ещё не зарегистрирован. Отправь /start.")
        schedule_delete(bot, sent.chat.id, sent.message_id)
        return

    friend_rows = await friends.build_friend_rows(message.from_user.id)
    text, markup = friends.render_list(friend_rows, page=0)
    sent = await message.answer(text, reply_markup=markup)
    schedule_delete(bot, sent.chat.id, sent.message_id)


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
    # Любое взаимодействие со списком продлевает ему жизнь ещё на минуту -
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
