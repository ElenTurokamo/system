"""
Политика провалов: заморозка серии на первом провале и полная кара на втором подряд.

Провалом считается и просрочка по таймауту, и "Сдаться" - оба пути вызывают
apply_failure() ровно один раз на испытание (оба защищены условными UPDATE в
Database.expire_challenge / Database.give_up_challenge). Выполненное испытание обнуляет
счётчик провалов подряд (completion.complete_challenge_flow).

Что происходит при провале (решает счётчик users.consecutive_failures ПОСЛЕ этого провала):

  * ПЕРВЫЙ подряд (config.failure_is_forgiven) - заморозка серии. Игрок не штрафуется:
    XP не снимается, ограничений нет, серия сохраняется, а в профиле вместо 🔥 показывается
    🧊 (users.streak_frozen). Личная карточка и сообщение в группу сообщают о провале и о
    том, что штраф не наложен. Выполнил следующее испытание - заморозка снимается, серия
    растёт дальше.
  * ВТОРОЙ подряд (и далее) - прежняя полная кара: -XP (MISSED_DAY_XP_PENALTY), серия
    обнулена, ограничение доступа на PENALTY_HOURS (новые квесты не выдаются), а по достижении
    AUTO_STOP_AFTER_FAILURES Система останавливается (тот же флаг users.is_stopped, что и у
    ручной /stop: квесты не выдаются, пока игрок не наберёт /start).

При AUTO_STOP_AFTER_FAILURES=1 прощения нет - каждый провал полный (см. failure_is_forgiven).
"""
import logging
from dataclasses import dataclass

from aiogram import Bot

from bot import challenge_render, profile
from bot import messages as tx
from bot.config import failure_is_forgiven, settings
from bot.database import db
from bot.utils import call_with_retries

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FailureOutcome:
    """Итог применения провала (для логов, тестов и вызывающего кода)."""

    forgiven: bool      # True - первый провал подряд: серия заморожена, штрафа нет
    streak: int         # серия: сохранённая (forgiven) либо потерянная (полная кара)
    auto_stopped: bool  # Система остановлена этим провалом


async def _auto_stop_if_needed(user_id: int, count: int) -> bool:
    """Останавливает Систему, если проваленных подряд стало порог или больше. True - остановлена этим вызовом."""
    if count < settings.auto_stop_after_failures:
        return False

    user = await db.get_user(user_id)
    if user and user.get("is_stopped"):
        return False  # уже остановлена (например, вручную) - ничего не меняем

    await db.set_stopped(user_id, True)
    logger.info(
        "Система автоматически остановлена для %s: провалов подряд %s", user_id, count
    )
    return True


async def register_failure(user_id: int) -> bool:
    """
    Засчитывает провал в счётчик подряд проваленных. Если их стало
    settings.auto_stop_after_failures или больше - останавливает Систему.
    Возвращает True, если Система остановлена этим вызовом.

    Саму кару/заморозку применяет apply_failure; эта функция - только счётчик и автостоп.
    """
    count = await db.register_failure(user_id)
    return await _auto_stop_if_needed(user_id, count)


async def apply_failure(
    bot: Bot, challenge_id: int, user_id: int, reason: str
) -> FailureOutcome:
    """
    Единая точка применения провала испытания (reason: "timeout" | "gave_up"). Вызывать ровно
    из тех мест, где сработал условный UPDATE провала (expire_challenge / give_up_challenge) -
    тогда дубля не будет.

    Порядок: статистика дня -> счётчик подряд проваленных (атомарный +1) -> по его значению
    либо заморозка серии (первый провал подряд), либо полная кара (второй и далее) ->
    личная карточка игроку -> сообщение в группу -> профиль. Сообщение в группу идёт до
    профиля: профиль при сбоях edit ждёт повторов, и друзья не должны ждать из-за этого.
    """
    user = await db.get_user(user_id)
    streak_before = int((user or {}).get("streak") or 0)

    await db.record_daily_stats(challenge_id)
    count = await db.register_failure(user_id)

    if failure_is_forgiven(count):
        # Заморозка: серия остаётся как есть, XP и доступ не трогаются.
        await db.set_streak_frozen(user_id, True)
        outcome = FailureOutcome(forgiven=True, streak=streak_before, auto_stopped=False)
        card_text = tx.challenge_failed_frozen(
            reason,
            streak=streak_before,
            xp_loss=settings.missed_day_xp_penalty,
            penalty_hours=settings.penalty_hours,
        )
        logger.info(
            "Провал испытания %s игрока %s прощён: серия %s заморожена (подряд: %s)",
            challenge_id, user_id, streak_before, count,
        )
    else:
        # Полная кара: -XP (с понижением уровня, если сгорает весь запас XP текущего уровня),
        # обнуление серии (заодно снимает заморозку) и ограничение на PENALTY_HOURS часов -
        # оно показывается в профиле (profile.penalty_block) и обновляется там каждую минуту
        # (scheduler.refresh_penalty_profiles). Пока ограничение действует, новые испытания не
        # выдаются (dispatch._process_locked).
        await db.reset_streak(user_id)
        await db.add_xp(user_id, -settings.missed_day_xp_penalty)
        await db.set_penalty(user_id, settings.penalty_hours)
        # Автостоп - до профиля, чтобы профиль уже показал паузу.
        auto_stopped = await _auto_stop_if_needed(user_id, count)
        outcome = FailureOutcome(forgiven=False, streak=streak_before, auto_stopped=auto_stopped)
        card_text = tx.challenge_failed(
            reason, xp_loss=settings.missed_day_xp_penalty, penalty_hours=settings.penalty_hours
        )

    await challenge_render.close_failed_challenge(bot, challenge_id, card_text)
    await post_failure_report(
        bot,
        user_id,
        streak_lost=0 if outcome.forgiven else outcome.streak,
        forgiven=outcome.forgiven,
        streak_saved=outcome.streak if outcome.forgiven else 0,
    )
    await profile.sync_profile_message(bot, user_id)
    return outcome


async def post_failure_report(
    bot: Bot,
    user_id: int,
    *,
    streak_lost: int = 0,
    forgiven: bool = False,
    streak_saved: int = 0,
) -> None:
    """
    Дополнительное сообщение в привязанную группу о провале испытания (таймаут или
    "Сдаться"). Личная карточка провала игроку (messages.challenge_failed /
    challenge_failed_frozen) приходит отдельно.

      * forgiven=False - игрок потерпел поражение, наложен штраф, серия сброшена;
        streak_lost - серия ДО сброса (messages.group_failure_report);
      * forgiven=True - первый провал подряд: штрафа нет, серия заморожена;
        streak_saved - сохранённая серия (messages.group_failure_frozen_report).

    Вызывать ровно из тех мест, где сработал условный UPDATE провала (apply_failure) - тогда
    дубля не будет. Любой сбой только логируется: кара/заморозка уже применены, и
    недоступная группа ничего ломать не должна.
    """
    try:
        user = await db.get_user(user_id)
        if not user or not user.get("group_id"):
            return
        group_id = user["group_id"]
        if forgiven:
            text = tx.group_failure_frozen_report(user_id, streak_saved=streak_saved)
        else:
            text = tx.group_failure_report(
                user_id,
                xp_loss=settings.missed_day_xp_penalty,
                penalty_hours=settings.penalty_hours,
                streak_lost=streak_lost,
            )
        await call_with_retries(
            lambda: bot.send_message(group_id, text),
            attempts=3,
            delay=settings.profile_retry_delay_seconds,
            what=f"сообщение о поражении игрока {user_id} в группу",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось отправить сообщение о поражении в группу (игрок %s): %s", user_id, e)
