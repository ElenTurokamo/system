from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, Message

from bot import keyboards as kb
from bot import messages as tx
from bot.config import FOCUS_OPTIONS, TIME_OF_DAY_LABELS
from bot.database import db
from bot.states import Registration

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message):
    user = await db.create_user_if_missing(message.from_user.id, message.from_user.username or "")

    if user["reg_state"] == "done":
        await message.answer(
            "Ты уже зарегистрирован, Игрок. Используй /profile, чтобы посмотреть статистику."
        )
        return

    await message.answer(tx.registration_welcome())
    await message.answer(
        "Во сколько ты хочешь, чтобы Система активировала ежедневное испытание?",
        reply_markup=kb.time_of_day_kb(),
    )
    await db.set_reg_state(message.from_user.id, "time_of_day")


@router.callback_query(F.data.startswith("time:"))
async def on_time_chosen(call: CallbackQuery):
    time_key = call.data.split(":", 1)[1]
    await db.set_time_of_day(call.from_user.id, time_key)
    await db.set_reg_state(call.from_user.id, "focus")

    await call.message.edit_text(f"Время испытаний: {TIME_OF_DAY_LABELS[time_key]} ✅")
    await call.message.answer(
        "Выбери направления, которые Система будет тебе предлагать "
        "(можно выбрать несколько). Когда закончишь — нажми «Готово».",
        reply_markup=kb.focus_select_kb([]),
    )
    await call.answer()


@router.callback_query(F.data.startswith("focus_toggle:"))
async def on_focus_toggle(call: CallbackQuery):
    focus_key = call.data.split(":", 1)[1]
    selected = await db.toggle_focus(call.from_user.id, focus_key)
    await call.message.edit_reply_markup(reply_markup=kb.focus_select_kb(selected))
    await call.answer()


@router.callback_query(F.data == "focus_done")
async def on_focus_done(call: CallbackQuery):
    selected = await db.get_focuses(call.from_user.id)
    if not selected:
        await call.answer("Выбери хотя бы одно направление!", show_alert=True)
        return

    labels = ", ".join(FOCUS_OPTIONS[f]["label"] for f in selected)
    await call.message.edit_text(f"Направления сохранены: {labels} ✅")

    await db.set_reg_state(call.from_user.id, "group")
    await call.message.answer(
        "Последний шаг: привяжи бота к группе, чтобы твой прогресс (фото завершённых "
        "испытаний) публиковался туда и вся команда видела твой рост.\n\n"
        "Можно сделать это сейчас или позже командой /bind_group внутри нужной группы.",
        reply_markup=kb.group_binding_kb(),
    )
    await call.answer()


@router.callback_query(F.data == "group_instructions")
async def group_instructions(call: CallbackQuery):
    text = (
        "Как привязать группу:\n\n"
        "1. Создай группу в Telegram (или используй существующую).\n"
        "2. Добавь этого бота в группу как участника.\n"
        "3. Бот попытается определить группу автоматически. Если этого не произошло — "
        "напиши в группе команду /bind_group, и я привяжу её к твоему профилю.\n"
        "4. Убедись, что у бота есть право отправлять сообщения (и фото) в группе — "
        "обычно этого достаточно без прав администратора.\n\n"
        "После этого сюда, в личные сообщения, ничего дополнительно делать не нужно."
    )
    await call.message.answer(text, reply_markup=kb.group_binding_kb())
    await call.answer()


@router.callback_query(F.data == "group_skip")
async def group_skip(call: CallbackQuery):
    await finish_registration(call)


async def finish_registration(call: CallbackQuery):
    await db.finish_registration(call.from_user.id)
    await call.message.edit_text("Группа: пропущено (можно привязать позже через /bind_group)")
    await call.message.answer(tx.registration_done())
    await call.answer()
