from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

from bot import challenge_render
from bot import keyboards as kb
from bot import messages as tx
from bot import profile
from bot.config import FOCUS_OPTIONS, time_of_day_label
from bot.database import db
from bot.registration import finish_registration
from bot.utils import send_command_message

router = Router(name="start")

GROUP_INSTRUCTIONS = (
    "\n\n— Как привязать группу —\n"
    "1. Создай группу в Telegram (или используй существующую).\n"
    "2. Добавь этого бота в группу как участника.\n"
    "3. Бот попытается определить группу автоматически. Если этого не произошло — "
    "напиши в группе команду /bind_group.\n"
    "4. Убедись, что у бота есть право отправлять сообщения (и фото) в группе."
)


async def _advance_step(bot: Bot, user_id: int, text: str, reply_markup=None) -> None:
    """Удаляет предыдущее служебное сообщение (шаг регистрации/настроек) и отправляет следующее."""
    user = await db.get_user(user_id)
    if user and user.get("last_reg_message_id"):
        try:
            await bot.delete_message(user_id, user["last_reg_message_id"])
        except Exception:
            pass

    msg = await bot.send_message(user_id, text, reply_markup=reply_markup)
    await db.set_last_reg_message(user_id, msg.message_id)


async def _close_step(bot: Bot, user_id: int) -> None:
    """Закрывает текущее служебное сообщение без открытия следующего шага."""
    user = await db.get_user(user_id)
    if user and user.get("last_reg_message_id"):
        try:
            await bot.delete_message(user_id, user["last_reg_message_id"])
        except Exception:
            pass
        await db.set_last_reg_message(user_id, None)


@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot):
    user = await db.create_user_if_missing(
        message.from_user.id, message.from_user.username or "", message.from_user.first_name or ""
    )

    if user["reg_state"] == "done":
        # Никаких информационных сообщений - только сама суть: закреплённый профиль
        # и (если сегодняшнее испытание ещё не завершено) трекер испытания дня.
        # Оба пересылаются новым сообщением, а не редактируются на месте, т.к. /start -
        # явный запрос пользователя и старые сообщения могут быть скрыты у него в чате
        # (например, после очистки истории).
        await profile.resend_profile_message(bot, message.from_user.id)

        active_challenge = await db.get_active_challenge(message.from_user.id)
        if active_challenge:
            await challenge_render.send_challenge_message(bot, active_challenge["id"])
        return

    text = (
        f"{tx.registration_welcome()}\n\n"
        "Во сколько ты хочешь, чтобы Система активировала ежедневное испытание?"
    )
    await _advance_step(bot, message.from_user.id, text, kb.time_of_day_kb())
    await db.set_reg_state(message.from_user.id, "time_of_day")


@router.message(Command("change_time"))
async def cmd_change_time(message: Message, bot: Bot):
    user = await db.get_user(message.from_user.id)
    if not user or user["reg_state"] != "done":
        await send_command_message(bot, message.chat.id, "change_time", "Сначала пройди регистрацию: /start")
        return

    text = "Во сколько присылать ежедневное испытание?"
    await _advance_step(bot, message.from_user.id, text, kb.time_of_day_kb())


@router.message(Command("change_focus"))
async def cmd_change_focus(message: Message, bot: Bot):
    user = await db.get_user(message.from_user.id)
    if not user or user["reg_state"] != "done":
        await send_command_message(bot, message.chat.id, "change_focus", "Сначала пройди регистрацию: /start")
        return

    selected = await db.get_focuses(message.from_user.id)
    text = (
        "Выбери дисциплины, которые Система будет тебе предлагать. "
        "Когда закончишь — нажми «Готово»."
    )
    await _advance_step(bot, message.from_user.id, text, kb.focus_select_kb(selected))


@router.callback_query(F.data.startswith("time:"))
async def on_time_chosen(call: CallbackQuery, bot: Bot):
    time_key = call.data.split(":", 1)[1]
    user = await db.get_user(call.from_user.id)
    await db.set_time_of_day(call.from_user.id, time_key)

    if user and user["reg_state"] == "done":
        # Это смена настроек командой /change_time, а не первичная регистрация.
        # Профиль пересобирается сразу же (editing на месте), т.к. строка
        # "Время испытаний" в нём должна отражать актуальное значение, не
        # дожидаясь следующего автоматического обновления профиля.
        await _close_step(bot, call.from_user.id)
        await profile.sync_profile_message(bot, call.from_user.id)
        await call.answer(f"Время обновлено: {time_of_day_label(time_key)} ✅")
        return

    await db.set_reg_state(call.from_user.id, "focus")
    text = (
        f"Время испытаний: {time_of_day_label(time_key)} ✅\n\n"
        "Выбери направления, которые Система будет тебе предлагать "
        "(можно выбрать несколько). Когда закончишь — нажми «Готово»."
    )
    await _advance_step(bot, call.from_user.id, text, kb.focus_select_kb([]))
    await call.answer()


@router.callback_query(F.data.startswith("focus_toggle:"))
async def on_focus_toggle(call: CallbackQuery):
    # Тоггл чекбоксов не переводит на следующий шаг — просто обновляем клавиатуру на месте
    focus_key = call.data.split(":", 1)[1]
    selected = await db.toggle_focus(call.from_user.id, focus_key)
    await call.message.edit_reply_markup(reply_markup=kb.focus_select_kb(selected))
    await call.answer()


@router.callback_query(F.data == "focus_done")
async def on_focus_done(call: CallbackQuery, bot: Bot):
    selected = await db.get_focuses(call.from_user.id)
    if not selected:
        await call.answer("Выбери хотя бы одно направление!", show_alert=True)
        return

    user = await db.get_user(call.from_user.id)
    if user and user["reg_state"] == "done":
        # Это смена настроек командой /change_focus, а не первичная регистрация.
        # Профиль перерисовывается сразу же: новая/снятая дисциплина должна
        # появиться или уйти из списка "первого приоритета" в тот же момент,
        # а не после следующего автоматического обновления.
        await _close_step(bot, call.from_user.id)
        await profile.sync_profile_message(bot, call.from_user.id)
        await call.answer("Дисциплины обновлены ✅")
        return

    labels = ", ".join(FOCUS_OPTIONS[f]["label"] for f in selected)
    await db.set_reg_state(call.from_user.id, "group")

    text = (
        f"Направления сохранены: {labels} ✅\n\n"
        "Последний шаг: привяжи бота к группе, чтобы твой прогресс (фото завершённых "
        "испытаний) публиковался туда и вся команда видела твой рост.\n\n"
        "Можно сделать это сейчас или позже командой /bind_group внутри нужной группы."
    )
    await _advance_step(bot, call.from_user.id, text, kb.group_binding_kb())
    await call.answer()


@router.callback_query(F.data == "group_instructions")
async def group_instructions(call: CallbackQuery):
    # Инструкции дописываются в то же самое сообщение, а не создают новое
    current_text = call.message.text or ""
    if GROUP_INSTRUCTIONS.strip() in current_text:
        await call.answer()
        return
    await call.message.edit_text(current_text + GROUP_INSTRUCTIONS, reply_markup=kb.group_binding_kb())
    await call.answer()


@router.callback_query(F.data == "group_skip")
async def group_skip(call: CallbackQuery, bot: Bot):
    await finish_registration(bot, call.from_user.id)
    await call.answer()
