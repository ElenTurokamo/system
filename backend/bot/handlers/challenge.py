from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from bot import challenge_render
from bot import messages as tx
from bot import profile
from bot.config import BONUS_LEVELS, BONUS_MULTIPLIER, FOCUS_OPTIONS, XP_PER_CHALLENGE, XP_PER_LEVEL, settings
from bot.database import db

router = Router(name="challenge")


@router.callback_query(F.data.startswith("focus:"))
async def on_focus_picked(call: CallbackQuery):
    _, challenge_id, focus_key = call.data.split(":")
    challenge_id = int(challenge_id)

    challenge = await db.get_challenge(challenge_id)
    if not challenge or challenge["user_id"] != call.from_user.id or challenge["status"] != "awaiting_action":
        await call.answer("Это испытание уже неактуально.", show_alert=True)
        return

    progress = await db.get_progress(challenge_id, focus_key)
    if not progress:
        await call.answer()
        return

    if progress["bonus_claimed"]:
        await call.answer("Эта дисциплина запечатана — максимум уже получен сегодня.", show_alert=True)
        return

    await db.set_active_focus(challenge_id, focus_key)
    await challenge_render.push_challenge_update(call.bot, challenge_id)
    await call.answer()


@router.message(F.text.regexp(r"^\d+$"))
async def on_amount_logged(message: Message):
    challenge = await db.get_active_challenge(message.from_user.id)
    if not challenge or challenge["status"] != "awaiting_action" or not challenge["active_focus"]:
        return  # число не относится ни к одному активному испытанию - не трогаем сообщение

    amount = int(message.text)
    focus_key = challenge["active_focus"]
    opt = FOCUS_OPTIONS[focus_key]

    # Число — это один подход. Сообщение с числом сразу удаляется, весь UX живёт
    # в единственном сообщении испытания.
    try:
        await message.delete()
    except Exception:
        pass

    # Пожизненный счётчик пользователя для /profile - не зависит от целей конкретного дня
    await db.add_focus_amount(message.from_user.id, focus_key, amount)

    progress = await db.add_progress_amount(challenge["id"], focus_key, amount)

    if not progress["completed"] and progress["amount"] >= progress["target"]:
        await db.mark_progress_completed(challenge["id"], focus_key)

    # Секретный бонус: довёл дисциплину до x2 цели - мгновенные уровни и печать на дисциплину
    if not progress["bonus_claimed"] and progress["amount"] >= progress["target"] * BONUS_MULTIPLIER:
        await db.mark_progress_bonus_claimed(challenge["id"], focus_key)
        await db.set_active_focus(challenge["id"], None)
        await db.add_xp(message.from_user.id, XP_PER_LEVEL * BONUS_LEVELS)
        try:
            await message.answer(tx.secret_bonus(opt["label"], BONUS_LEVELS))
        except Exception:
            pass

    # Испытание дня засчитано только когда закрыты ВСЕ цели
    if await db.all_progress_completed(challenge["id"]):
        await db.set_active_focus(challenge["id"], None)
        await db.set_status(challenge["id"], "awaiting_photo")

        xp, level, leveled_up = await db.add_xp(message.from_user.id, XP_PER_CHALLENGE)
        streak = await db.increment_streak(message.from_user.id)

        try:
            await message.answer(tx.success(streak, XP_PER_CHALLENGE, xp, level))
            if leveled_up:
                await message.answer(tx.level_up(level))
            if streak % 7 == 0:
                await message.answer(tx.streak_milestone(streak))
        except Exception:
            pass

    await challenge_render.push_challenge_update(message.bot, challenge["id"])
    await profile.sync_profile_message(message.bot, message.from_user.id)


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

    await challenge_render.push_challenge_update(bot, challenge["id"])


@router.callback_query(F.data.startswith("skipphoto:"))
async def on_skip_photo(call: CallbackQuery):
    challenge_id = int(call.data.split(":")[1])
    challenge = await db.get_challenge(challenge_id)
    if not challenge or challenge["user_id"] != call.from_user.id:
        await call.answer()
        return

    await db.complete_challenge(challenge_id, with_photo=False)
    await challenge_render.push_challenge_update(call.bot, challenge_id)
    await call.answer("Испытание завершено без фото ✅")


@router.callback_query(F.data.startswith("giveup:"))
async def on_give_up(call: CallbackQuery):
    challenge_id = int(call.data.split(":")[1])
    challenge = await db.get_challenge(challenge_id)
    if not challenge or challenge["user_id"] != call.from_user.id:
        await call.answer()
        return
    if challenge["status"] != "awaiting_action":
        await call.answer("Испытание уже закрыто.", show_alert=True)
        return

    await db.give_up_challenge(challenge_id)
    await db.reset_streak(call.from_user.id)
    await db.set_penalty(call.from_user.id, settings.penalty_hours)

    await challenge_render.push_challenge_update(call.bot, challenge_id)
    try:
        await call.message.answer(tx.give_up(settings.penalty_hours))
    except Exception:
        pass

    await profile.sync_profile_message(call.bot, call.from_user.id)
    await call.answer()
