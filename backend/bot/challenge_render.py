"""
Единая точка правды для отображения сообщения-испытания.

Сообщение испытания живёт с момента отправки до полного завершения дня
(все цели выполнены / сдался / истекло время) и всегда РЕДАКТИРУЕТСЯ,
а не пересоздаётся — это и есть "единая UX архитектура", которую попросили.
"""
import logging

from aiogram import Bot

from bot import keyboards as kb
from bot.config import FOCUS_OPTIONS
from bot.database import db

logger = logging.getLogger(__name__)


def _build_footer(challenge: dict) -> str:
    status = challenge["status"]

    if status == "awaiting_action":
        if challenge["active_focus"]:
            opt = FOCUS_OPTIONS[challenge["active_focus"]]
            return f"\n\n➜ {opt['label']}: жду цифру"
        return ""

    if status == "awaiting_photo":
        return "\n\n✅ Все цели дня выполнены! Пришли фото или нажми «Пропустить»."

    if status == "gave_up":
        return "\n\n🏳 Испытание прервано."

    if status == "expired":
        return "\n\n⌛ Время испытания истекло."

    if status in ("completed", "completed_with_photo"):
        return "\n\n✅ Испытание завершено."

    return ""


def _build_keyboard(challenge: dict, progress_rows: list[dict]):
    status = challenge["status"]
    if status == "awaiting_action":
        return kb.challenge_kb(challenge["id"], progress_rows, challenge["active_focus"])
    if status == "awaiting_photo":
        return kb.photo_prompt_kb(challenge["id"])
    return None  # финальные статусы - клавиатура убирается


async def push_challenge_update(bot: Bot, challenge_id: int):
    """Перерисовывает сообщение испытания по актуальному состоянию из БД."""
    challenge = await db.get_challenge(challenge_id)
    if not challenge or not challenge.get("message_id"):
        return

    progress_rows = await db.get_progress_rows(challenge_id)
    text = (challenge["quest_text"] or "") + _build_footer(challenge)
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
