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
from bot.config import BONUS_LEVELS, BONUS_MULTIPLIER, FOCUS_OPTIONS, settings
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


async def _build_text(challenge: dict, progress_rows: list[dict]) -> str:
    status = challenge["status"]
    quest = challenge["quest_text"] or ""

    if status == "awaiting_action":
        # Карточка активного испытания всегда выглядит ровно так: заголовок+строка
        # квеста, разделитель, таймер. Статус бонуса и выбранная дисциплина видны
        # прямо на кнопках клавиатуры (🎯, «Пропустить фото», «Завершить испытание»),
        # текстом не дублируются. Исключение - подсказка про фото физических
        # активностей: она добавляется отдельной строкой сразу под таймером, пока
        # все физические цели закрыты, а фото ещё не отправлено/пропущено, и
        # автоматически пропадает при следующей перерисовке после отправки фото.
        text = f"{quest}\n\n{DIVIDER}\n{_format_time_left(challenge)}"
        if _physical_photo_pending(challenge, progress_rows):
            text += "\n📸 Пришли фото выполнения всех физических активностей."
        return text

    lines: list[str] = []
    if challenge.get("bonus_claimed"):
        lines.append(
            f"🎁 Секретный бонус получен: +{BONUS_LEVELS} уровней "
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
