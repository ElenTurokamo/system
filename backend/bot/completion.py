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

Отчёт в привязанную группу (включая фото/видео/кружок) отправляется ТОЛЬКО здесь, после
завершения испытания, - ни при получении медиа, ни раньше. Медиа, присланное по
физическим целям, до этого момента лежит в БД (challenges.physical_photo_file_id +
physical_media_type: photo / video / video_note):
  * есть фото или видео - в группу уходит оно с подписью-отчётом (messages.photo_caption);
  * есть кружок - у кружков (video_note) подписей не бывает, поэтому в группу уходит кружок,
    а сразу за ним - сообщение с отчётом;
  * медиа нет, завершение автоматическое - тот же отчёт уходит текстом;
  * медиа нет, завершение ручное - отчёта нет (игрок сам пропустил отчёт).
Проваленное, сданное или остановленное испытание отчёта о выполнении не даёт: медиа
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


async def _send_media_report(
    bot: Bot, group_id: int, file_id: str, media_type: str | None, text: str, challenge_id: int
) -> None:
    """
    Отправляет в группу медиа-отчёт нужного типа (с повторами). Исключение при сбое
    поднимается наружу - вызывающий код решает, слать ли отчёт текстом.

    Фото и видео уходят с подписью-отчётом. У кружка (video_note) подписи нет в Bot API,
    поэтому отчёт идёт отдельным сообщением сразу за ним; если сам кружок ушёл, а текст -
    нет, исключение наружу не поднимается (кружок уже опубликован, дублировать нельзя).
    Неизвестный/пустой тип (старые строки БД) трактуется как фото.
    """
    delay = settings.profile_retry_delay_seconds
    if media_type == "video_note":
        await call_with_retries(
            lambda: bot.send_video_note(group_id, file_id),
            attempts=3,
            delay=delay,
            what=f"кружок-отчёт о завершении испытания {challenge_id}",
        )
        await db.mark_physical_photo_posted(challenge_id)
        try:
            await call_with_retries(
                lambda: bot.send_message(group_id, text),
                attempts=3,
                delay=delay,
                what=f"подпись к кружку-отчёту испытания {challenge_id}",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Кружок отправлен, а подпись нет (испытание %s): %s", challenge_id, e)
        return

    if media_type == "video":
        await call_with_retries(
            lambda: bot.send_video(group_id, file_id, caption=text),
            attempts=3,
            delay=delay,
            what=f"видео-отчёт о завершении испытания {challenge_id}",
        )
    else:
        await call_with_retries(
            lambda: bot.send_photo(group_id, file_id, caption=text),
            attempts=3,
            delay=delay,
            what=f"фото-отчёт о завершении испытания {challenge_id}",
        )
    await db.mark_physical_photo_posted(challenge_id)


async def _post_group_report(
    bot: Bot, challenge_id: int, user_id: int, report_day: int, *, auto: bool
) -> None:
    """
    Отчёт о завершении в привязанную группу: фото/видео/кружок с подписью (если игрок прислал
    медиа) либо текст (только при авто-завершении). Любой сбой только логируется: испытание уже
    засчитано, и из-за недоступной группы (бот удалён/нет прав) игрок терять ничего не должен.
    Вызывается один раз - только тем вызовом complete_challenge_flow, который закрыл испытание.
    """
    try:
        challenge = await db.get_challenge(challenge_id)
        if not challenge:
            return
        file_id = challenge.get("physical_photo_file_id")
        if not file_id and not auto:
            return  # ручное завершение без медиа - отчёта нет (как и раньше)

        user = await db.get_user(user_id)
        if not user or not user.get("group_id"):
            return  # группа не привязана (или отвязана после отправки медиа) - отправлять некуда
        group_id = user["group_id"]

        # Строка про секретное испытание: личный x2 по всем физическим дисциплинам
        # либо общий секретный бонус - x2 по всем дисциплинам, включая интеллектуальные
        # (его же показывает профиль). Второе нужно для испытаний только из шахмат/чтения:
        # у них физических дисциплин нет. Оценивается на момент завершения, а не получения медиа.
        # При сниженной сложности секретного испытания нет (флаги сброшены), строка заменяется пометкой.
        eased = bool(challenge.get("eased"))
        secret = not eased and (
            bool(challenge.get("bonus_claimed")) or await db.physical_bonus_achieved(challenge_id)
        )
        text = tx.photo_caption(report_day, user_id, physical_bonus_claimed=secret, eased=eased)

        if file_id:
            try:
                await _send_media_report(
                    bot, group_id, file_id, challenge.get("physical_media_type"), text, challenge_id
                )
                return
            except TelegramForbiddenError:
                raise  # бота выгнали из группы - текст тоже не дойдёт
            except Exception as e:  # noqa: BLE001
                # Медиа не ушло - друзья всё равно должны узнать о завершении: шлём тот же отчёт текстом.
                logger.warning(
                    "Не удалось отправить медиа-отчёт (испытание %s), шлём текстом: %s", challenge_id, e
                )

        await call_with_retries(
            lambda: bot.send_message(group_id, text),
            attempts=3,
            delay=settings.profile_retry_delay_seconds,
            what=f"отчёт о завершении испытания {challenge_id}",
        )
    except Exception as e:  # noqa: BLE001 - сбой группы не должен ломать завершение
        logger.warning("Не удалось отправить отчёт в группу (испытание %s): %s", challenge_id, e)
