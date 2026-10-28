"""
Завершение ежедневного испытания - единая точка начисления награды.

Испытание закрывается как выполненное двумя путями:
  * вручную - игрок нажал "🏁 Завершить испытание" (handlers/challenge.py);
  * автоматически - игрок закрыл все цели, но забыл нажать кнопку, и время
    испытания (CHALLENGE_TIMEOUT_HOURS, 18 ч) вышло (scheduler._expire_one).
    Вместо провала такое испытание засчитывается как выполненное.

Оба пути идут через complete_challenge_flow, поэтому награда (XP, серия, сброс
счётчика провалов, строка статистики, обновление карточки и профиля) выглядит
одинаково, а повторное/параллельное срабатывание (двойное нажатие кнопки, кнопка
в ту же секунду, что и таймаут, "Сдаться" рядом с таймаутом) не может начислить
её дважды: решающий шаг - атомарный условный UPDATE в
Database.complete_challenge_if_ready.

Отличие авто-пути: фото игрок уже не пришлёт, поэтому в привязанную группу
текстом уходит тот же отчёт, что обычно стоит под фото
(messages.photo_caption). Если фото-отчёт в группу уже ушёл - второй не шлётся.
"""
import logging

from aiogram import Bot

from bot import challenge_render, profile
from bot import messages as tx
from bot.config import eased_reward, settings, xp_reward_for_challenge
from bot.database import db
from bot.utils import call_with_retries

logger = logging.getLogger(__name__)


async def complete_challenge_flow(bot: Bot, challenge: dict, *, auto: bool = False) -> bool:
    """
    Закрывает испытание как выполненное и начисляет награду.

    auto=True - авто-завершение по таймауту: дополнительно шлёт в группу текстовый
    отчёт (без фото). Возвращает False, если закрывать нечего: испытание уже
    закрыто другим путём (завершено/сдано/просрочено/остановлено) или не все цели
    закрыты - тогда ничего не меняется и ничего не начисляется.
    """
    challenge_id = challenge["id"]
    user_id = challenge["user_id"]

    if not await db.complete_challenge_if_ready(challenge_id):
        return False

    # Дальше испытание закрыто за нами: награда начисляется ровно один раз.
    user = await db.get_user(user_id)
    reward = xp_reward_for_challenge(user["level"])
    # Сложность понижена ("📉 Снизить сложность"): награда - половина, серия продолжается как обычно.
    fresh = await db.get_challenge(challenge_id)
    eased = bool(fresh and fresh.get("eased"))
    if eased:
        reward = eased_reward(reward)
    # Номер дня в отчёте группы - серия С УЧЁТОМ этого дня, как и в фото-отчёте
    # (handlers/challenge.py::on_photo_received): первый день - "день 1".
    report_day = user["streak"] + 1

    await db.add_xp(user_id, reward)
    await db.increment_streak(user_id)
    # Выполненное испытание прерывает серию провалов подряд (см. failure_policy).
    await db.reset_failures(user_id)
    await db.record_daily_stats(challenge_id)

    if auto:
        logger.info(
            "Испытание %s игрока %s завершено автоматически по таймауту: все цели были закрыты",
            challenge_id, user_id,
        )

    # Карточка испытания при завершении удаляется, предупреждение о скором провале
    # убирается вместе с ней (см. challenge_render.push_challenge_update).
    await challenge_render.push_challenge_update(bot, challenge_id)

    # Отчёт в группу идёт ДО обновления профиля: профиль при сбоях edit ждёт
    # повторных попыток по 30 с, и друзья не должны ждать отчёта из-за этого.
    if auto:
        await _post_auto_report(bot, challenge_id, user_id, report_day)
    elif eased and fresh.get("physical_photo_posted"):
        # Фото-отчёт ушёл ДО снижения сложности и пометки в нём нет - дополняем отдельным сообщением.
        await _post_ease_note(bot, user_id, report_day)

    await profile.sync_profile_message(bot, user_id)
    return True


async def _post_auto_report(bot: Bot, challenge_id: int, user_id: int, report_day: int) -> None:
    """
    Текстовый отчёт о завершении в привязанную группу (без фото). Любой сбой
    только логируется: испытание уже засчитано, и из-за недоступной группы
    (бот удалён/нет прав) игрок терять ничего не должен.
    """
    try:
        challenge = await db.get_challenge(challenge_id)
        if not challenge or challenge.get("physical_photo_posted"):
            return  # отчёт с фото в группе уже есть - дубль не нужен

        user = await db.get_user(user_id)
        if not user or not user.get("group_id"):
            return

        # Строка про секретное испытание: личный x2 по всем физическим дисциплинам
        # (как в фото-отчёте) либо общий секретный бонус - x2 по всем дисциплинам,
        # включая интеллектуальные (его же показывает профиль). Второе нужно для
        # испытаний только из шахмат/чтения: у них физических дисциплин нет.
        # При сниженной сложности секретного испытания нет (флаги сброшены), строка заменяется пометкой.
        eased = bool(challenge.get("eased"))
        secret = not eased and (
            bool(challenge.get("bonus_claimed")) or await db.physical_bonus_achieved(challenge_id)
        )

        # Тот же текст, что под фото (messages.photo_caption), и тот же отсчёт дней.
        text = tx.photo_caption(report_day, user_id, physical_bonus_claimed=secret, eased=eased)
        group_id = user["group_id"]
        await call_with_retries(
            lambda: bot.send_message(group_id, text),
            attempts=3,
            delay=settings.profile_retry_delay_seconds,
            what=f"авто-отчёт о завершении испытания {challenge_id}",
        )
    except Exception as e:  # noqa: BLE001 - сбой группы не должен ломать завершение
        logger.warning("Не удалось отправить авто-отчёт в группу (испытание %s): %s", challenge_id, e)


async def _post_ease_note(bot: Bot, user_id: int, report_day: int) -> None:
    """Короткая пометка про сниженную сложность в группу (если фото-отчёт уже был без неё). Сбои только логируются."""
    try:
        user = await db.get_user(user_id)
        if not user or not user.get("group_id"):
            return
        group_id = user["group_id"]
        text = tx.ease_note(report_day, user_id)
        await call_with_retries(
            lambda: bot.send_message(group_id, text),
            attempts=3,
            delay=settings.profile_retry_delay_seconds,
            what=f"пометка о сниженной сложности игрока {user_id}",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось отправить пометку о сниженной сложности в группу (игрок %s): %s", user_id, e)
