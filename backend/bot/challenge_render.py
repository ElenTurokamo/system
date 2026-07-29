"""
Единая точка правды для отображения сообщения-испытания.

Сообщение испытания живёт с момента отправки до полного завершения дня
(все цели выполнены и нажато "Завершить испытание", либо "Сдался", либо
истекло время) и всегда РЕДАКТИРУЕТСЯ, а не пересоздаётся. Новое сообщение
отправляется только при диспетчеризации следующего дня.

Фото для физических дисциплин запрашивается СРАЗУ, как только все физические
цели закрыты (не дожидаясь интеллектуальных) - см. _physical_photo_pending.
Для чисто интеллектуальных испытаний фото не запрашивается вовсе.
"""
import logging

from aiogram import Bot

from bot import keyboards as kb
from bot.config import BONUS_LEVELS, BONUS_MULTIPLIER, FOCUS_OPTIONS, settings
from bot.database import db

logger = logging.getLogger(__name__)


def _join(lines: list[str]) -> str:
    return ("\n\n" + "\n\n".join(lines)) if lines else ""


def _physical_photo_pending(challenge: dict, progress_rows: list[dict]) -> bool:
    """Все физические цели закрыты, а фото по ним ещё не отправлено/пропущено."""
    if challenge["physical_photo_done"]:
        return False
    physical_rows = [r for r in progress_rows if FOCUS_OPTIONS[r["focus"]]["kind"] == "physical"]
    return bool(physical_rows) and all(r["completed"] for r in physical_rows)


async def _build_footer(challenge: dict, progress_rows: list[dict]) -> str:
    status = challenge["status"]
    lines: list[str] = []

    if challenge.get("bonus_claimed"):
        lines.append(
            f"🎁 Секретный бонус получен: +{BONUS_LEVELS} уровней "
            f"(все дисциплины доведены до x{BONUS_MULTIPLIER})."
        )

    if status == "awaiting_action":
        if _physical_photo_pending(challenge, progress_rows):
            lines.append(
                "💪 Физическая часть выполнена! Пришли фото, чтобы зафиксировать "
                "результат в группе, или нажми «Пропустить фото»."
            )
        if challenge["active_focus"]:
            opt = FOCUS_OPTIONS[challenge["active_focus"]]
            lines.append(f"➜ {opt['label']}: жду цифру")
        return _join(lines)

    if status in ("completed", "completed_with_photo"):
        lines.append("✅ Испытание завершено.")
        user = await db.get_user(challenge["user_id"])
        if user:
            lines.append(f"🔥 Стрик: {user['streak']} · 🏆 Уровень {user['level']} ({user['xp']} XP)")
        if status == "completed_with_photo":
            lines.append("📸 Фото сохранено.")
        return _join(lines)

    if status == "gave_up":
        lines.append(
            f"🏳 Испытание прервано. Штраф на {settings.penalty_hours}ч: "
            "без игр, контента 18+ и спонтанных действий."
        )
        return _join(lines)

    if status == "expired":
        lines.append("⌛ Время испытания истекло. Стрик сброшен.")
        return _join(lines)

    return _join(lines)


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
    footer = await _build_footer(challenge, progress_rows)
    text = (challenge["quest_text"] or "") + footer
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
