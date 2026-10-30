from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery, Message

from bot import challenge_render
from bot import completion
from bot import failure_policy
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
    # При сниженной сложности ("📉 Снизить сложность") секретного испытания сегодня нет:
    # ни огоньков 🔥, ни бонуса - даже если повторений набрано вдвое больше новой цели.
    eased = bool(challenge.get("eased"))
    if not eased:
        await db.sync_progress_bonus(challenge["id"], focus_key, BONUS_MULTIPLIER)

    # Секретный бонус: единоразово за ВСЁ испытание, только когда КАЖДАЯ выбранная
    # дисциплина доведена минимум до x2 своей цели - не за одну отдельную дисциплину.
    if not eased and not challenge["bonus_claimed"]:
        rows = await db.get_progress_rows(challenge["id"])
        if rows and all(r["amount"] >= r["target"] * BONUS_MULTIPLIER for r in rows):
            await db.mark_challenge_bonus_claimed(challenge["id"])
            user = await db.get_user(message.from_user.id)
            bonus_xp = xp_reward_for_challenge(user["level"]) * BONUS_XP_MULTIPLIER
            await db.add_xp(message.from_user.id, bonus_xp)

    # Закрыта последняя цель - уже висящее предупреждение о штрафе ("осталось менее N
    # минут") теряет смысл: провал не грозит. Убираем его сразу; если игрок потом
    # откатит прогресс ниже цели, предупреждения вернутся (см.
    # challenge_render.maybe_send_failure_warning).
    if await db.goals_ready(challenge["id"]):
        await challenge_render.clear_warning_message(message.bot, challenge["id"])

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

    В группу фото при этом НЕ отправляется: оно только сохраняется (file_id в БД) и уходит
    вместе с отчётом лишь после завершения испытания - вручную или автоматически
    (см. bot/completion.py). Сдача/провал фото в группу не отправляют вовсе.
    """
    challenge = await db.get_active_challenge(message.from_user.id)
    if not challenge or challenge["physical_photo_done"]:
        return

    progress_rows = await db.get_progress_rows(challenge["id"])
    physical_rows = [r for r in progress_rows if FOCUS_OPTIONS[r["focus"]]["kind"] == "physical"]
    if not physical_rows or not all(r["completed"] for r in physical_rows):
        return  # фото сейчас не ожидается

    # Условный UPDATE: если два фото пришли разом (альбом) или фото опоздало за
    # "Пропустить фото"/завершением - сохранится только первое, остальные игнорируются.
    if not await db.store_physical_photo(challenge["id"], message.photo[-1].file_id):
        return

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

    await db.skip_physical_photo(challenge_id)
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

    # Награда и закрытие - в общем модуле (его же использует авто-завершение по
    # таймауту): атомарный условный UPDATE не даст начислить её дважды, если
    # кнопку нажали в ту же секунду, что сработал таймаут/"Сдаться".
    if not await completion.complete_challenge_flow(call.bot, challenge, auto=False):
        await call.answer("Испытание уже неактуально.", show_alert=True)
        return
    await call.answer()


GIVEUP_LOCKED_TEXT = "Все цели дня выполнены - сдаться уже нельзя. Заверши испытание."


async def _execute_give_up(bot: Bot, challenge: dict) -> bool:
    """
    Собственно сдача испытания - вызывается только после подтверждения "Да".
    Возвращает False, если сдавать нечего: испытание уже закрыто или все цели дня закрыты.
    """
    challenge_id = challenge["id"]
    user_id = challenge["user_id"]

    # Условный UPDATE: если испытание уже закрыто (двойное нажатие "Да" или
    # планировщик успел просрочить его раньше) - второй штраф/провал не засчитываем.
    # Он же не даёт сдать испытание, у которого закрыты все цели (кнопки "Сдаться" в
    # этом случае нет - см. Database.give_up_challenge).
    if not await db.give_up_challenge(challenge_id):
        return False

    user = await db.get_user(user_id)
    streak_lost = (user or {}).get("streak") or 0

    await db.record_daily_stats(challenge_id)
    await db.reset_streak(user_id)
    # "Сдался" - это тоже пропущенный день: та же XP-плата и то же ограничение,
    # что и при обычной просрочке (см. scheduler.expire_stale_challenges).
    await db.add_xp(user_id, -settings.missed_day_xp_penalty)
    await db.set_penalty(user_id, settings.penalty_hours)

    # Второй провал подряд - Система останавливается сама (см. failure_policy).
    await failure_policy.register_failure(user_id)

    await challenge_render.close_failed_challenge(
        bot,
        challenge_id,
        tx.challenge_failed(
            "gave_up", xp_loss=settings.missed_day_xp_penalty, penalty_hours=settings.penalty_hours
        ),
    )
    # Сообщение о поражении в группу - до профиля: профиль при сбоях edit ждёт повторов.
    await failure_policy.post_failure_report(bot, user_id, streak_lost=streak_lost)
    await profile.sync_profile_message(bot, user_id)
    return True


async def _ease_unavailable_reason(call: CallbackQuery, challenge: dict | None) -> str | None:
    """Почему снижение сложности сейчас недоступно (None - доступно). Единая проверка для запроса и подтверждения."""
    if not challenge or challenge["user_id"] != call.from_user.id:
        return "Это испытание уже неактуально."
    if challenge["status"] != "awaiting_action":
        return "Испытание уже закрыто."
    if challenge.get("eased"):
        return "Сложность уже снижена."
    user = await db.get_user(call.from_user.id)
    rows = await db.get_progress_rows(challenge["id"])
    if not challenge_render.ease_offered(challenge, rows, user):
        return "Сейчас снизить сложность нельзя: все цели дня уже закрыты."
    return None


@router.callback_query(F.data.startswith("ease:"))
async def on_ease_request(call: CallbackQuery):
    """"📉 Снизить сложность": карточка подменяется экраном подтверждения Да/Нет (ничего не меняется до "Да")."""
    challenge_id = int(call.data.split(":")[1])
    challenge = await db.get_challenge(challenge_id)
    reason = await _ease_unavailable_reason(call, challenge)
    if reason:
        await call.answer(reason, show_alert=True)
        # Кнопка на карточке устарела (цели закрыли, уже снижено) - перерисовываем актуальный вид.
        if challenge and challenge["user_id"] == call.from_user.id and challenge["status"] == "awaiting_action":
            await challenge_render.push_challenge_update(call.bot, challenge_id)
        return

    await challenge_render.push_ease_confirm(call.bot, challenge_id)
    await call.answer()


@router.callback_query(F.data.startswith("ease_yes:"))
async def on_ease_confirmed(call: CallbackQuery):
    """
    "Да": цели всех сегодняшних дисциплин делятся пополам, награда становится половинной,
    секретное испытание отключается, серия не страдает (см. Database.ease_challenge и
    completion.complete_challenge_flow). Прогресс остаётся: если набрано уже не меньше новой
    цели, дисциплина сразу закрыта (а при всех закрытых - появляется "Завершить испытание").
    """
    challenge_id = int(call.data.split(":")[1])
    challenge = await db.get_challenge(challenge_id)
    reason = await _ease_unavailable_reason(call, challenge)
    if reason:
        await call.answer(reason, show_alert=True)
        if challenge and challenge["user_id"] == call.from_user.id and challenge["status"] == "awaiting_action":
            await challenge_render.push_challenge_update(call.bot, challenge_id)
        return

    # Условный UPDATE: двойное "Да" или "Да" вместе с завершением/просрочкой не поделят цели дважды.
    if not await db.ease_challenge(challenge_id):
        await call.answer("Сейчас снизить сложность нельзя.", show_alert=True)
        await challenge_render.push_challenge_update(call.bot, challenge_id)
        return

    # Если после пересчёта все цели закрыты - предупреждение о штрафе теряет смысл.
    if await db.goals_ready(challenge_id):
        await challenge_render.clear_warning_message(call.bot, challenge_id)

    await challenge_render.push_challenge_update(call.bot, challenge_id)
    await profile.sync_profile_message(call.bot, call.from_user.id)
    await call.answer("📉 Сложность снижена: цели и награда вдвое меньше.")


@router.callback_query(F.data.startswith("ease_no:"))
async def on_ease_declined(call: CallbackQuery):
    """"Нет": карточка возвращается к обычному виду, кнопка снижения сложности остаётся."""
    challenge_id = int(call.data.split(":")[1])
    challenge = await db.get_challenge(challenge_id)
    if challenge and challenge["user_id"] == call.from_user.id and challenge["status"] == "awaiting_action":
        await challenge_render.push_challenge_update(call.bot, challenge_id)
    await call.answer()


@router.callback_query(F.data.startswith("giveup:"))
async def on_give_up_request(call: CallbackQuery):
    """
    Первый клик по "🏳 Сдаться" больше не сдаёт испытание сразу - он полностью
    подменяет карточку испытания на экран подтверждения "Сдаться?" с кнопками
    "Да"/"Нет" (см. challenge_render.push_giveup_confirm). Собственно сдача
    происходит только после явного "Да" - см. on_give_up_confirmed.

    Если все цели дня уже закрыты, кнопки "Сдаться" на карточке нет; устаревший клик по ней
    (клиент ещё показывал старую клавиатуру) отклоняется, а карточка перерисовывается.
    """
    challenge_id = int(call.data.split(":")[1])
    challenge = await db.get_challenge(challenge_id)
    if not challenge or challenge["user_id"] != call.from_user.id:
        await call.answer()
        return
    if challenge["status"] != "awaiting_action":
        await call.answer("Испытание уже закрыто.", show_alert=True)
        return
    if await db.goals_ready(challenge_id):
        await call.answer(GIVEUP_LOCKED_TEXT, show_alert=True)
        await challenge_render.push_challenge_update(call.bot, challenge_id)
        return

    await challenge_render.push_giveup_confirm(call.bot, challenge_id)
    await call.answer()


@router.callback_query(F.data.startswith("giveup_yes:"))
async def on_give_up_confirmed(call: CallbackQuery):
    challenge_id = int(call.data.split(":")[1])

    challenge = await db.get_challenge(challenge_id)
    if not challenge or challenge["user_id"] != call.from_user.id:
        await call.answer()
        return
    if challenge["status"] != "awaiting_action":
        await call.answer("Испытание уже закрыто.", show_alert=True)
        return

    if not await _execute_give_up(call.bot, challenge):
        # Пока висел экран подтверждения, игрок закрыл последнюю цель (или испытание закрылось
        # само) - сдача не прошла. Возвращаем карточку в актуальный вид, если она ещё жива.
        fresh = await db.get_challenge(challenge_id)
        if fresh and fresh["status"] == "awaiting_action":
            await call.answer(GIVEUP_LOCKED_TEXT, show_alert=True)
            await challenge_render.push_challenge_update(call.bot, challenge_id)
        else:
            await call.answer("Испытание уже закрыто.", show_alert=True)
        return
    await call.answer()


@router.callback_query(F.data.startswith("giveup_no:"))
async def on_give_up_declined(call: CallbackQuery):
    challenge_id = int(call.data.split(":")[1])

    # Возвращаем карточку испытания к обычному виду - если она к этому моменту
    # ещё жива (испытание не истекло/не изменилось иным путём).
    challenge = await db.get_challenge(challenge_id)
    if challenge and challenge["status"] == "awaiting_action":
        await challenge_render.push_challenge_update(call.bot, challenge_id)

    await call.answer()
