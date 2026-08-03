"""
Единая точка правды для отображения сообщения-испытания.

Сообщение испытания живёт с момента отправки до полного завершения дня
(все цели выполнены и нажато "Завершить испытание", либо "Сдался", либо
истекло время) и всегда РЕДАКТИРУЕТСЯ, а не пересоздаётся. Новое сообщение
отправляется только при диспетчеризации следующего дня (или при пересылке,
если старое стало пользователю недоступно).

Фото для физических дисциплин запрашивается СРАЗУ, как только все физические
цели закрыты (не дожидаясь интеллектуальных) - см. _physical_photo_pending.
Для чисто интеллектуальных испытаний фото не запрашивается вовсе.
"""
import logging
from datetime import datetime, timedelta

from aiogram import Bot

from bot import keyboards as kb
from bot.config import BONUS_MULTIPLIER, BONUS_XP_MULTIPLIER, FOCUS_OPTIONS, settings
from bot.database import db

logger = logging.getLogger(__name__)

DIVIDER = "=" * 27


def _join(lines: list[str]) -> str:
    return ("\n\n" + "\n\n".join(lines)) if lines else ""


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return one
    if n_abs % 10 in (2, 3, 4) and n_abs % 100 not in (12, 13, 14):
        return few
    return many


def _format_time_left(challenge: dict) -> str:
    """Строка обратного отсчёта до провала испытания по таймауту."""
    try:
        started = datetime.fromisoformat(challenge["started_at"])
    except (TypeError, ValueError):
        return "⏱️ До провала испытания осталось: —"

    deadline = started + timedelta(hours=settings.challenge_timeout_hours)
    remaining = deadline - datetime.utcnow()

    if remaining.total_seconds() <= 0:
        return "⏱️ Время испытания на исходе - вот-вот сгорит."

    total_minutes = int(remaining.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    hours_label = _ru_plural(hours, "час", "часа", "часов")
    minutes_label = _ru_plural(minutes, "минута", "минуты", "минут")
    return f"⏱️ До провала испытания осталось: {hours} {hours_label} и {minutes} {minutes_label}."


def _physical_photo_pending(challenge: dict, progress_rows: list[dict]) -> bool:
    """Все физические цели закрыты, а фото по ним ещё не отправлено/пропущено."""
    if challenge["physical_photo_done"]:
        return False
    physical_rows = [r for r in progress_rows if FOCUS_OPTIONS[r["focus"]]["kind"] == "physical"]
    return bool(physical_rows) and all(r["completed"] for r in physical_rows)


async def _build_cheer_line(challenge_id: int) -> str:
    """
    Компактная строка про поддержку от друзей: "🤝 Верят в тебя: Аня, Игорь" -
    ОДНА строка независимо от числа поддержавших (см. friends_list-концепцию,
    п.6.3), а не по строке на каждого, чтобы не раздувать и без того насыщенную
    карточку испытания (таймер, прогресс, кнопки).
    """
    from bot.profile import display_name  # локальный импорт - избегаем цикла profile <-> challenge_render

    supporter_ids = await db.get_cheer_supporter_ids(challenge_id)
    if not supporter_ids:
        return ""

    names = []
    for supporter_id in supporter_ids:
        supporter = await db.get_user(supporter_id)
        if supporter:
            names.append(display_name(supporter))
    if not names:
        return ""

    return f"🤝 Верят в тебя: {', '.join(names)}"


async def _build_text(challenge: dict, progress_rows: list[dict]) -> str:
    status = challenge["status"]
    quest = challenge["quest_text"] or ""

    if status == "awaiting_action":
        # Карточка активного испытания:
        #   [ Информация о квесте ]      <- статичный заголовок (см. messages.py, не меняется)
        #
        #   (динамическая надпись)       <- случайная строка из _QUEST_TAGLINES
        #
        #   (подсказка про фото)         <- только пока не все физ. цели закрыты/фото не отправлено
        #   (строка поддержки от друзей) <- только если хотя бы один друг нажал "Поддержать"
        #   ===========================
        #   ⏱️ таймер
        # Статус бонуса и выбранная дисциплина текстом не дублируются - они видны
        # прямо на кнопках клавиатуры (🎯, «Пропустить фото», «Завершить испытание»).
        # Подсказка про фото пропадает сама при следующей перерисовке после того,
        # как физ. фото отправлено или пропущено.
        text = quest
        if _physical_photo_pending(challenge, progress_rows):
            text += "\n\n📸 Пришли фото выполнения всех физических активностей."

        cheer_line = await _build_cheer_line(challenge["id"])
        if cheer_line:
            text += f"\n\n{cheer_line}"

        text += f"\n\n{DIVIDER}\n{_format_time_left(challenge)}"
        return text

    lines: list[str] = []
    if challenge.get("bonus_claimed"):
        lines.append(
            f"🎁 Секретный бонус получен: XP испытания x{BONUS_XP_MULTIPLIER} "
            f"(все дисциплины доведены до x{BONUS_MULTIPLIER})."
        )

    if status in ("completed", "completed_with_photo"):
        lines.append("✅ Испытание завершено.")
        user = await db.get_user(challenge["user_id"])
        if user:
            lines.append(f"🔥 Стрик: {user['streak']} · 🏆 Уровень {user['level']} ({user['xp']} XP)")
        if status == "completed_with_photo":
            lines.append("📸 Фото сохранено.")
        return quest + _join(lines)

    if status == "gave_up":
        lines.append(
            f"🏳 Испытание прервано. Штраф на {settings.penalty_hours}ч: "
            "без игр, контента 18+ и спонтанных действий."
        )
        return quest + _join(lines)

    if status == "expired":
        lines.append("⌛ Время испытания истекло. Стрик сброшен.")
        return quest + _join(lines)

    return quest + _join(lines)


def _build_keyboard(challenge: dict, progress_rows: list[dict]):
    status = challenge["status"]
    if status != "awaiting_action":
        return None  # финальные статусы - клавиатура убирается

    show_finish = bool(progress_rows) and all(p["completed"] for p in progress_rows)
    show_skip_photo = _physical_photo_pending(challenge, progress_rows)
    return kb.challenge_kb(
        challenge["id"], progress_rows, challenge["active_focus"], show_finish, show_skip_photo
    )


async def push_challenge_update(bot: Bot, challenge_id: int):
    """Перерисовывает сообщение испытания по актуальному состоянию из БД."""
    challenge = await db.get_challenge(challenge_id)
    if not challenge or not challenge.get("message_id"):
        return

    status = challenge["status"]

    if status in ("completed", "completed_with_photo"):
        # Успешное завершение дня больше не оформляется отдельной "отчётной"
        # карточкой (✅ Испытание завершено / стрик / уровень / фото / бонус) -
        # вся эта информация (включая секретный бонус, если он был получен)
        # теперь постоянно видна в профиле двумя строками в его конце (см.
        # profile.py). Чтобы не дублировать её в чате, карточка испытания
        # просто удаляется.
        try:
            await bot.delete_message(challenge["user_id"], challenge["message_id"])
        except Exception as e:
            logger.info("Не удалось удалить сообщение испытания %s: %s", challenge_id, e)
        return

    progress_rows = await db.get_progress_rows(challenge_id)
    text = await _build_text(challenge, progress_rows)
    markup = _build_keyboard(challenge, progress_rows)

    try:
        await bot.edit_message_text(
            text,
            chat_id=challenge["user_id"],
            message_id=challenge["message_id"],
            reply_markup=markup,
        )
    except Exception as e:
        logger.info("Не удалось обновить сообщение испытания %s: %s", challenge_id, e)


async def close_failed_challenge(bot: Bot, challenge_id: int, failure_text: str):
    """
    Единая точка закрытия ПРОВАЛЕННОГО испытания - неважно, истёк таймаут или
    игрок сдался сам. Вместо того чтобы редактировать карточку испытания в
    "отчёт о провале" и оставлять её висеть в чате навсегда, теперь:
      1) карточка испытания удаляется;
      2) отдельным сообщением приходит отчёт о провале, id которого
         сохраняется в users.failure_message_id - чтобы диспетчер мог
         автоматически удалить именно его, как только выдаст следующее
         ежедневное испытание (см. scheduler.dispatch_daily_challenges /
         profile.clear_failure_message). Подпись о штрафе в самом профиле
         (см. profile.penalty_block) при этом не трогается - она живёт по
         penalty_until независимо от смены испытаний.
    """
    challenge = await db.get_challenge(challenge_id)
    if not challenge:
        return
    user_id = challenge["user_id"]

    if challenge.get("message_id"):
        try:
            await bot.delete_message(user_id, challenge["message_id"])
        except Exception as e:
            logger.info("Не удалось удалить карточку проваленного испытания %s: %s", challenge_id, e)

    try:
        msg = await bot.send_message(user_id, failure_text)
        await db.set_failure_message(user_id, user_id, msg.message_id)
    except Exception as e:
        logger.warning("Не удалось отправить отчёт о провале испытания пользователю %s: %s", user_id, e)


async def send_challenge_message(bot: Bot, challenge_id: int):
    """
    Отправляет актуальное состояние испытания НОВЫМ сообщением и записывает его
    message_id как точку правды, по которой дальше работает push_challenge_update.

    Используется как при первичной отправке испытания дня (диспетчер), так и при
    пересылке, если старое сообщение недоступно пользователю (например, он
    очистил историю чата) - иначе push_challenge_update продолжал бы молча
    редактировать сообщение, которое пользователь уже не видит.
    """
    challenge = await db.get_challenge(challenge_id)
    if not challenge:
        return

    progress_rows = await db.get_progress_rows(challenge_id)
    text = await _build_text(challenge, progress_rows)
    markup = _build_keyboard(challenge, progress_rows)

    msg = await bot.send_message(challenge["user_id"], text, reply_markup=markup)
    await db.set_message_id(challenge_id, msg.message_id)


# Псевдоним для читаемости на месте вызова (см. handlers/start.py) - смысл действия
# там именно "переслать снова", хотя механика идентична первичной отправке.
resend_challenge_message = send_challenge_message
