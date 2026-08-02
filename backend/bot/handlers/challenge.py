from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from bot import challenge_render
from bot import messages as tx
from bot import profile
from bot.config import (
    BONUS_MULTIPLIER,
    BONUS_XP_MULTIPLIER,
    FOCUS_OPTIONS,
    settings,
    xp_reward_for_challenge,
)
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

    # Дисциплины не запечатываются - можно грайндить любую хоть до х2 и дальше
    await db.set_active_focus(challenge_id, focus_key)
    await challenge_render.push_challenge_update(call.bot, challenge_id)
    await call.answer()


@router.message(F.text.regexp(r"^-?\d+$"))
async def on_amount_logged(message: Message):
    challenge = await db.get_active_challenge(message.from_user.id)
    if not challenge or not challenge["active_focus"]:
        # Число не относится ни к одному активному испытанию - это спам, чистим чат.
        try:
            await message.delete()
        except Exception:
            pass
        return

    amount = int(message.text)
    focus_key = challenge["active_focus"]

    # Число — это один подход (или его отмена, если введено с минусом). Сообщение
    # с числом сразу удаляется, весь UX живёт в единственном сообщении испытания.
    try:
        await message.delete()
    except Exception:
        pass

    # Пожизненный счётчик пользователя для /profile - не зависит от целей конкретного дня.
    # Не уходит ниже 0 (см. clamp в database.add_focus_amount).
    await db.add_focus_amount(message.from_user.id, focus_key, amount)

    # Отрицательное число (например, "-20") вычитает повторения - на случай, если
    # игрок вписал их не в ту дисциплину. amount тоже не уходит ниже 0.
    await db.add_progress_amount(challenge["id"], focus_key, amount)
    await db.sync_progress_completed(challenge["id"], focus_key)

    # Личный огонёк 🔥 по этой конкретной дисциплине: чисто визуальная метка на
    # кнопке, когда амплитуда по НЕЙ ОДНОЙ доведена до x2 цели. Никакой награды
    # не даёт и НЕ мешает продолжать вписывать подходы дальше - дисциплина
    # остаётся выбираемой, всё, что вписывается сверху, просто идёт в общий
    # счётчик и в статистику. Синк, а не одностороннее выставление - метка
    # должна и сниматься, если ввод скорректировали вниз ниже x2. Общий
    # секретный бонус (+5 уровней) по-прежнему считается отдельно ниже и
    # требует x2 сразу по ВСЕМ выбранным дисциплинам, и он уже одноразовый
    # (награда), поэтому его самого назад не откатываем.
    await db.sync_progress_bonus(challenge["id"], focus_key, BONUS_MULTIPLIER)

    # Секретный бонус: единоразово за ВСЁ испытание, только когда КАЖДАЯ выбранная
    # дисциплина доведена минимум до x2 своей цели - не за одну отдельную дисциплину.
    if not challenge["bonus_claimed"]:
        rows = await db.get_progress_rows(challenge["id"])
        if rows and all(r["amount"] >= r["target"] * BONUS_MULTIPLIER for r in rows):
            await db.mark_challenge_bonus_claimed(challenge["id"])
            user = await db.get_user(message.from_user.id)
            bonus_xp = xp_reward_for_challenge(user["level"]) * BONUS_XP_MULTIPLIER
            await db.add_xp(message.from_user.id, bonus_xp)

    # Испытание НЕ завершается автоматически: как только все цели закрыты, в сообщении
    # появляется кнопка "Завершить испытание". Пока её не нажали, можно продолжать
    # копить подходы на любой дисциплине (в том числе ради секретного бонуса).
    await challenge_render.push_challenge_update(message.bot, challenge["id"])
    await profile.sync_profile_message(message.bot, message.from_user.id)


@router.message(F.photo)
async def on_photo_received(message: Message, bot):
    """
    Фото запрашивается СРАЗУ, как только закрыты все физические цели дня -
    не дожидаясь интеллектуальных и не дожидаясь нажатия "Завершить испытание".
    Для чисто интеллектуальных испытаний это сообщение просто ни на что не среагирует.
    """
    challenge = await db.get_active_challenge(message.from_user.id)
    if not challenge or challenge["physical_photo_done"]:
        return

    progress_rows = await db.get_progress_rows(challenge["id"])
    physical_rows = [r for r in progress_rows if FOCUS_OPTIONS[r["focus"]]["kind"] == "physical"]
    if not physical_rows or not all(r["completed"] for r in physical_rows):
        return  # фото сейчас не ожидается

    user = await db.get_user(message.from_user.id)
    posted = False
    if user["group_id"]:
        # Секретная строка в отчёте оценивается ТОЛЬКО по физическим дисциплинам
        # (см. Database.physical_bonus_achieved) - специально не завязана на
        # общий challenges.bonus_claimed (который требует x2 ещё и по
        # интеллектуальным дисциплинам): иначе пришлось бы тянуть с отчётом до
        # закрытия шахмат/чтения, а по мышцам прогресс тренировки оценить уже
        # не получится, памп пройдёт. Полный бонус (x2 XP) от этого никак не
        # зависит и по-прежнему даётся только при закрытии ВСЕГО испытания.
        physical_bonus = await db.physical_bonus_achieved(challenge["id"])
        caption = tx.photo_caption(
            user["streak"],
            message.from_user.id,
            physical_bonus_claimed=physical_bonus,
        )
        try:
            await bot.send_photo(user["group_id"], message.photo[-1].file_id, caption=caption)
            posted = True
        except Exception:
            posted = False  # тихо - итоговый статус виден в самом сообщении испытания

    await db.mark_physical_photo(challenge["id"], posted=posted)

    try:
        await message.delete()
    except Exception:
        pass

    await challenge_render.push_challenge_update(bot, challenge["id"])


@router.callback_query(F.data.startswith("skipphysicalphoto:"))
async def on_skip_physical_photo(call: CallbackQuery):
    challenge_id = int(call.data.split(":")[1])
    challenge = await db.get_challenge(challenge_id)
    if not challenge or challenge["user_id"] != call.from_user.id or challenge["status"] != "awaiting_action":
        await call.answer()
        return

    await db.mark_physical_photo(challenge_id, posted=False)
    await challenge_render.push_challenge_update(call.bot, challenge_id)
    await call.answer()


@router.callback_query(F.data.startswith("finish:"))
async def on_finish_challenge(call: CallbackQuery):
    challenge_id = int(call.data.split(":")[1])
    challenge = await db.get_challenge(challenge_id)
    if not challenge or challenge["user_id"] != call.from_user.id or challenge["status"] != "awaiting_action":
        await call.answer("Испытание уже неактуально.", show_alert=True)
        return

    if not await db.all_progress_completed(challenge_id):
        await call.answer("Ещё не все цели дня выполнены.", show_alert=True)
        return

    user = await db.get_user(call.from_user.id)
    reward = xp_reward_for_challenge(user["level"])

    await db.set_active_focus(challenge_id, None)
    await db.add_xp(call.from_user.id, reward)
    await db.increment_streak(call.from_user.id)
    await db.complete_challenge(challenge_id, with_photo=bool(challenge["physical_photo_posted"]))
    await db.record_daily_stats(challenge_id)

    await challenge_render.push_challenge_update(call.bot, challenge_id)
    await profile.sync_profile_message(call.bot, call.from_user.id)
    await call.answer()


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
    await db.record_daily_stats(challenge_id)
    await db.reset_streak(call.from_user.id)
    # "Сдался" - это тоже пропущенный день: та же XP-плата и то же ограничение,
    # что и при обычной просрочке (см. scheduler.expire_stale_challenges).
    await db.add_xp(call.from_user.id, -settings.missed_day_xp_penalty)
    await db.set_penalty(call.from_user.id, settings.penalty_hours)

    await challenge_render.close_failed_challenge(
        call.bot,
        challenge_id,
        tx.give_up(xp_loss=settings.missed_day_xp_penalty, penalty_hours=settings.penalty_hours),
    )
    await profile.sync_profile_message(call.bot, call.from_user.id)
    await call.answer()
