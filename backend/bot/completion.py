"""
Завершение ежедневного испытания - единая точка начисления награды и отчёта в группу.

Испытание закрывается как выполненное двумя путями:
  * вручную - игрок нажал "🏁 Завершить испытание" (handlers/challenge.py);
  * автоматически - игрок закрыл все цели, но забыл нажать кнопку, и время
    испытания (CHALLENGE_TIMEOUT_HOURS, 18 ч) вышло (scheduler._expire_one).
    Вместо провала такое испытание засчитывается как выполненное.

Оба пути идут через complete_challenge_flow, поэтому награда (XP, серия, сброс
счётчика провалов, строка статистики, обновление карточки и профиля) выглядит
одинаково, а повторное/параллельное срабатывание (двойное нажатие кнопки, кнопка
в ту же секунду, что и таймаут) не может начислить её дважды: решающий шаг -
атомарный условный UPDATE в Database.complete_challenge_if_ready ("Сдаться"
после закрытия всех целей невозможно - см. Database.give_up_challenge).

Отчёт в привязанную группу (включая фото) отправляется ТОЛЬКО здесь, после
завершения испытания, - ни при получении фото, ни раньше. Фото, присланное по
физическим целям, до этого момента лежит в БД (challenges.physical_photo_file_id):
  * есть фото - в группу уходит фото с подписью-отчётом (messages.photo_caption);
  * фото нет, завершение автоматическое - тот же отчёт уходит текстом;
  * фото нет, завершение ручное - отчёта нет (игрок сам пропустил фото).
Проваленное, сданное или остановленное испытание отчёта о выполнении не даёт: фото
в группу не уходит никогда (о провале в группу пишет failure_policy.post_failure_report).
"""
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError

from bot import challenge_render, profile
from bot import messages as tx
from bot.config import eased_reward, settings, xp_reward_for_challenge
from bot.database import db
from bot.utils import call_with_retries

logger = logging.getLogger(__name__)


async def complete_challenge_flow(bot: Bot, challenge: dict, *, auto: bool = False) -> bool:
    """
    Закрывает испытание как выполненное и начисляет награду.

    auto=True - авто-завершение по таймауту: если фото нет, отчёт в группу всё равно уходит
    текстом (см. _post_group_report). Возвращает False, если закрывать нечего: испытание
    уже закрыто другим путём (завершено/сдано/просрочено/остановлено) или не все цели
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
    # Номер дня в отчёте группы - серия С УЧЁТОМ этого дня: первый день - "день 1".
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

    # Отчёт в группу (с фото, если оно было) идёт ДО обновления профиля: профиль при
    # сбоях edit ждёт повторных попыток по 30 с, и друзья не должны ждать отчёта из-за этого.
    await _post_group_report(bot, challenge_id, user_id, report_day, auto=auto)

    await profile.sync_profile_message(bot, user_id)
    return True


async def _post_group_report(
    bot: Bot, challenge_id: int, user_id: int, report_day: int, *, auto: bool
) -> None:
    """
    Отчёт о завершении в привязанную группу: фото с подписью (если игрок прислал фото) либо
    текст (только при авто-завершении). Любой сбой только логируется: испытание уже
    засчитано, и из-за недоступной группы (бот удалён/нет прав) игрок терять ничего не должен.
    Вызывается один раз - только тем вызовом complete_challenge_flow, который закрыл испытание.
    """
    try:
        challenge = await db.get_challenge(challenge_id)
        if not challenge:
            return
        file_id = challenge.get("physical_photo_file_id")
        if not file_id and not auto:
            return  # ручное завершение без фото - отчёта нет (как и раньше)

        user = await db.get_user(user_id)
        if not user or not user.get("group_id"):
            return  # группа не привязана (или отвязана после фото) - отправлять некуда
        group_id = user["group_id"]

        # Строка про секретное испытание: личный x2 по всем физическим дисциплинам
        # либо общий секретный бонус - x2 по всем дисциплинам, включая интеллектуальные
        # (его же показывает профиль). Второе нужно для испытаний только из шахмат/чтения:
        # у них физических дисциплин нет. Оценивается на момент завершения, а не получения фото.
        # При сниженной сложности секретного испытания нет (флаги сброшены), строка заменяется пометкой.
        eased = bool(challenge.get("eased"))
        secret = not eased and (
            bool(challenge.get("bonus_claimed")) or await db.physical_bonus_achieved(challenge_id)
        )
        text = tx.photo_caption(report_day, user_id, physical_bonus_claimed=secret, eased=eased)

        if file_id:
            try:
                await call_with_retries(
                    lambda: bot.send_photo(group_id, file_id, caption=text),
                    attempts=3,
                    delay=settings.profile_retry_delay_seconds,
                    what=f"фото-отчёт о завершении испытания {challenge_id}",
                )
                await db.mark_physical_photo_posted(challenge_id)
                return
            except TelegramForbiddenError:
                raise  # бота выгнали из группы - текст тоже не дойдёт
            except Exception as e:  # noqa: BLE001
                # Фото не ушло - друзья всё равно должны узнать о завершении: шлём тот же отчёт текстом.
                logger.warning(
                    "Не удалось отправить фото-отчёт (испытание %s), шлём текстом: %s", challenge_id, e
                )

        await call_with_retries(
            lambda: bot.send_message(group_id, text),
            attempts=3,
            delay=settings.profile_retry_delay_seconds,
            what=f"отчёт о завершении испытания {challenge_id}",
        )
    except Exception as e:  # noqa: BLE001 - сбой группы не должен ломать завершение
        logger.warning("Не удалось отправить отчёт в группу (испытание %s): %s", challenge_id, e)
