from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from bot import keyboards as kb
from bot import messages as tx
from bot.config import FOCUS_OPTIONS, XP_PER_CHALLENGE, settings
from bot.database import db

router = Router(name="challenge")


@router.callback_query(F.data.startswith("focus:"))
async def on_focus_picked(call: CallbackQuery):
    _, challenge_id, focus_key = call.data.split(":")
    challenge_id = int(challenge_id)

    challenge = await db.get_challenge(challenge_id)
    if not challenge or challenge["user_id"] != call.from_user.id or challenge["status"] != "awaiting_focus":
        await call.answer("Это испытание уже неактуально.", show_alert=True)
        return

    await db.set_challenge_focus(challenge_id, focus_key)
    opt = FOCUS_OPTIONS[focus_key]

    new_text = call.message.text + f"\n\n— Фокус: {opt['label']} —"
    await call.message.edit_text(new_text)
    await call.message.answer(tx.focus_selected(opt["label"], opt["unit"]))
    await call.answer()


@router.message(F.text.regexp(r"^\d+$"))
async def on_amount_logged(message: Message):
    challenge = await db.get_active_challenge(message.from_user.id)
    if not challenge or challenge["status"] != "in_progress":
        return  # не относится к испытанию - просто число в чате

    amount = int(message.text)
    focus_key = challenge["focus"]
    opt = FOCUS_OPTIONS[focus_key]

    await db.add_focus_amount(message.from_user.id, focus_key, amount)
    await db.set_challenge_amount(challenge["id"], amount)

    await message.answer(tx.amount_logged(amount, opt["unit"], opt["label"]))

    # Начисление опыта и стрика сразу по факту выполнения (фото - опционально)
    xp, level, leveled_up = await db.add_xp(message.from_user.id, XP_PER_CHALLENGE)
    streak = await db.increment_streak(message.from_user.id)

    await message.answer(tx.success(streak, XP_PER_CHALLENGE, xp, level))
    if leveled_up:
        await message.answer(tx.level_up(level))
    if streak % 7 == 0:
        await message.answer(tx.streak_milestone(streak))

    await message.answer(
        "Отправь фото, чтобы закрепить прогресс в группе, или нажми «Пропустить фото».",
        reply_markup=kb.photo_prompt_kb(challenge["id"]),
    )


@router.message(F.photo)
async def on_photo_received(message: Message, bot):
    challenge = await db.get_active_challenge(message.from_user.id)
    if not challenge or challenge["status"] != "awaiting_photo":
        return

    user = await db.get_user(message.from_user.id)
    await db.complete_challenge(challenge["id"], with_photo=True)

    if user["group_id"]:
        caption = tx.photo_caption(user["streak"], message.from_user.id)
        try:
            await bot.send_photo(user["group_id"], message.photo[-1].file_id, caption=caption)
            await message.answer("Фото опубликовано в группе ✅")
        except Exception:
            await message.answer(
                "Не получилось отправить фото в группу (возможно, бот был удалён из неё). "
                "Но твой прогресс уже сохранён."
            )
    else:
        await message.answer(
            "Прогресс сохранён, но группа не привязана — фото никуда не опубликовано. "
            "Привязать группу можно командой /bind_group внутри неё."
        )


@router.callback_query(F.data.startswith("skipphoto:"))
async def on_skip_photo(call: CallbackQuery):
    challenge_id = int(call.data.split(":")[1])
    challenge = await db.get_challenge(challenge_id)
    if not challenge or challenge["user_id"] != call.from_user.id:
        await call.answer()
        return

    await db.complete_challenge(challenge_id, with_photo=False)
    await call.message.edit_text("Испытание завершено без фото. До встречи на следующем ✅")
    await call.answer()


@router.callback_query(F.data.startswith("giveup:"))
async def on_give_up(call: CallbackQuery):
    challenge_id = int(call.data.split(":")[1])
    challenge = await db.get_challenge(challenge_id)
    if not challenge or challenge["user_id"] != call.from_user.id:
        await call.answer()
        return
    if challenge["status"] not in ("awaiting_focus", "in_progress"):
        await call.answer("Испытание уже закрыто.", show_alert=True)
        return

    await db.give_up_challenge(challenge_id)
    await db.reset_streak(call.from_user.id)
    await db.set_penalty(call.from_user.id, settings.penalty_hours)

    await call.message.edit_text(call.message.text + "\n\n— 🏳 Испытание прервано —")
    await call.message.answer(tx.give_up(settings.penalty_hours))
    await call.answer()
