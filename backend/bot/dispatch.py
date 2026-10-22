"""
Отказоустойчивая ежедневная выдача квестов.

Что делает модуль
-----------------
Раньше выдача была "выстрелом" cron-задачи: создали испытание в БД, один раз
попытались отправить карточку и забыли. Если отправка не удалась (сеть, 5xx,
flood-control) или бот в момент слота был выключен, игрок квеста не получал, а
созданная в БД строка через CHALLENGE_TIMEOUT_HOURS сгорала со штрафом - за то,
чего игрок не видел.

Теперь по каждому игроку, слоту и локальному дню ведётся запись в таблице
dispatch_log (см. Database.save_dispatch), и её статус отвечает на вопрос "было ли
сообщение с квестом реально отправлено":

    delivered   - карточка доставлена
    skipped     - выдача не требовалась (Система остановлена, действует штраф,
                  не выбраны дисциплины, прошлый квест ещё активен)
    pending     - доставка не удалась; следующая попытка в next_attempt_at
    failed      - попытки исчерпаны; игрок пропущен до следующего дня
    unreachable - игрок заблокировал бота / чат недоступен; повторять бессмысленно

Политика повторов
-----------------
Первая попытка - в момент слота (cron), затем до DISPATCH_RETRY_ATTEMPTS (3)
повторов с шагом DISPATCH_RETRY_INTERVAL_MINUTES (60): слот, +1ч, +2ч, +3ч. После
последней неудачной попытки недоставленная карточка удаляется из БД, а игрок
пропускается до следующего слота (то есть до следующего дня).

Повторы не делаются, если игрок сам "отключил" бота:
  * /stop или автостоп (is_stopped) - в том числе между попытками: ожидающая
    запись закрывается как skipped;
  * бот заблокирован / чат недоступен (TelegramForbiddenError и т.п.) - сразу
    unreachable, без часовых повторов;
  * игрок сменил время испытаний (/change_time) - повторы старого слота отменяются.

Проверка по таймеру
-------------------
Задача reconcile_dispatches (раз в минуту, см. scheduler.setup_scheduler):
  1) доводит до конца записи pending, у которых подошло время попытки;
  2) ловит "пропущенные" слоты: если cron в момент слота не отработал (бот был
     выключен/перезапускался/процесс убили посреди рассылки) - по журналу
     dispatch_runs слот не отмечен обойдённым - выдача досылается, но только в
     течение DISPATCH_CATCHUP_WINDOW_MINUTES с момента слота и только игрокам,
     зарегистрированным ДО слота (позже зарегистрировавшиеся/сменившие время
     ждут следующего дня, как и раньше).

Конкурентность
--------------
cron-задача слота и reconcile могут добраться до одного игрока одновременно;
поэтому вся обработка игрока идёт под asyncio.Lock этого игрока, а решения
принимаются по записи журнала, прочитанной уже внутри блокировки.
"""
import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from bot import challenge_render, profile
from bot import messages as tx
from bot.config import settings
from bot.database import db
from bot.utils import call_with_retries

logger = logging.getLogger(__name__)

_UTC = ZoneInfo("UTC")

# Сколько секунд после слота reconcile не считает слот пропущенным: cron должен
# успеть стартовать, и мы не хотим лезть к нему под руку.
CATCHUP_GRACE_SECONDS = 120

# Признаки "чат недоступен" в тексте TelegramBadRequest (кроме явного Forbidden).
_UNREACHABLE_MARKERS = ("chat not found", "user is deactivated", "peer_id_invalid", "bot was blocked")

_locks: dict[int, asyncio.Lock] = {}


# ---------- время ----------


def utcnow() -> datetime:
    """Наивное UTC-время, как везде в проекте. Вынесено в функцию ради тестов."""
    return datetime.utcnow()


def local_today() -> date:
    """Сегодняшняя дата по TZ из настроек."""
    return utcnow().replace(tzinfo=_UTC).astimezone(ZoneInfo(settings.tz)).date()


def slot_start_utc(day: date, hhmm: str) -> datetime:
    """Момент (наивный UTC), когда на локальную дату `day` срабатывает слот HH:MM."""
    hour, minute = map(int, hhmm.split(":"))
    local = datetime(day.year, day.month, day.day, hour, minute, tzinfo=ZoneInfo(settings.tz))
    return local.astimezone(_UTC).replace(tzinfo=None)


def _parse_iso(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def _max_attempts() -> int:
    """Всего попыток доставки за день: первая + повторы."""
    return 1 + settings.dispatch_retry_attempts


def _is_unreachable(exc: BaseException) -> bool:
    """True, если игрок недоступен для бота и повторять отправку бессмысленно."""
    if isinstance(exc, TelegramForbiddenError):
        return True
    if isinstance(exc, TelegramBadRequest):
        text = str(exc).lower()
        return any(marker in text for marker in _UNREACHABLE_MARKERS)
    return False


def _lock_for(user_id: int) -> asyncio.Lock:
    lock = _locks.get(user_id)
    if lock is None:
        lock = _locks[user_id] = asyncio.Lock()
    return lock


# ---------- обработка одного игрока ----------


async def process_dispatch(bot: Bot, user_id: int, day: str, slot: str) -> str:
    """
    Одна "попытка выдачи" игроку за (день, слот): решает, нужна ли выдача, и если
    нужна - создаёт/отправляет карточку и обновляет журнал. Безопасна для
    повторного и параллельного вызова. Возвращает короткий код исхода (для
    логов и тестов): delivered / skipped:<причина> / retry_scheduled / failed /
    unreachable / not_due / already_final / ...
    """
    async with _lock_for(user_id):
        return await _process_locked(bot, user_id, day, slot)


async def _save(
    rec: Optional[dict],
    user_id: int,
    day: str,
    slot: str,
    status: str,
    reason: Optional[str],
    attempts: int,
    *,
    last_attempt_at: Optional[str] = None,
    next_attempt_at: Optional[str] = None,
    error: Optional[str] = None,
):
    await db.save_dispatch(
        user_id,
        day,
        slot,
        status,
        reason=reason,
        attempts=attempts,
        last_attempt_at=last_attempt_at or (rec or {}).get("last_attempt_at"),
        next_attempt_at=next_attempt_at,
        last_error=error if error is not None else (rec or {}).get("last_error"),
    )


async def _drop_undelivered(user_id: int):
    """Удаляет недоставленную карточку игрока (если есть), чтобы она не висела в БД."""
    active = await db.get_active_challenge(user_id)
    if active and not active.get("message_id"):
        await db.delete_undelivered_challenge(active["id"])


async def _process_locked(bot: Bot, user_id: int, day: str, slot: str) -> str:
    if slot not in settings.time_slots:
        return "bad_slot"

    user = await db.get_user(user_id)
    if not user or user.get("reg_state") != "done":
        return "no_user"

    rec = await db.get_dispatch(user_id, day, slot)
    if rec and rec["status"] != "pending":
        return "already_final"

    now = utcnow()
    if rec:
        next_at = _parse_iso(rec.get("next_attempt_at"))
        if next_at is not None and next_at > now:
            return "not_due"

    attempts_done = int((rec or {}).get("attempts") or 0)

    # Игрок сменил время испытаний: этот слот его больше не касается. Новый слот
    # обработает собственный cron (если ещё не прошёл) - старые повторы отменяем.
    if user.get("time_of_day") != slot:
        if rec:
            await _drop_undelivered(user_id)
            await _save(rec, user_id, day, slot, "skipped", "slot_changed", attempts_done)
            logger.info("Выдача %s/%s/%s отменена: игрок сменил время испытаний", user_id, day, slot)
        return "slot_changed"

    # Процесс могли убить посреди попытки: запись-маркер уже учла её в attempts.
    if rec and attempts_done >= _max_attempts():
        await _drop_undelivered(user_id)
        await _save(rec, user_id, day, slot, "failed", "attempts_exhausted", attempts_done)
        logger.warning(
            "Выдача %s/%s/%s: попытки исчерпаны, игрок пропущен до следующего дня", user_id, day, slot
        )
        return "failed"

    active = await db.get_active_challenge(user_id)
    undelivered = active if active and not active.get("message_id") else None
    delivered_active = active if active and active.get("message_id") else None

    # Миграция/страховка: квест за этот слот уже был выдан (например, обновление
    # кода произошло уже после рассылки, или запись в журнал потерялась) - просто
    # фиксируем это, ничего не отправляя.
    if rec is None and not undelivered:
        slot_start = slot_start_utc(date.fromisoformat(day), settings.time_slots[slot])
        if await db.has_challenge_since(user_id, slot_start.isoformat()):
            await _save(None, user_id, day, slot, "delivered", "pre_existing", 0)
            return "already_delivered"

    # Есть доставленное активное испытание - новое не выдаётся (как и раньше).
    if delivered_active:
        if rec:
            # Карточка дошла другим путём (например, игрок набрал /start, и
            # resend_challenge_message её отправил) - попытки больше не нужны.
            await _save(rec, user_id, day, slot, "delivered", "delivered_elsewhere", attempts_done)
            return "delivered"
        await _save(None, user_id, day, slot, "skipped", "active_challenge", 0)
        return "skipped:active_challenge"

    # Игрок сам остановил Систему (/stop) или она остановилась автоматически -
    # квест не нужен, повторы (если были запланированы) отменяются.
    if user.get("is_stopped"):
        await _drop_undelivered(user_id)
        await _save(rec, user_id, day, slot, "skipped", "stopped", attempts_done)
        return "skipped:stopped"

    # Штраф: слоты, попавшие в срок ограничения, просто пропускаются.
    if db.is_penalty_active(user):
        await _drop_undelivered(user_id)
        await _save(rec, user_id, day, slot, "skipped", "penalty", attempts_done)
        logger.info(
            "Испытание не выдано пользователю %s: действует ограничение до %s (UTC)",
            user_id, user.get("penalty_until"),
        )
        if rec is None:
            # Профиль всё равно обслуживаем: иначе за время паузы его сообщение
            # могло бы "состариться" настолько, что Telegram перестанет давать его
            # редактировать.
            try:
                await profile.ensure_fresh_profile_message(bot, user_id)
            except Exception:
                logger.exception("Не удалось обновить профиль пользователя %s", user_id)
        return "skipped:penalty"

    focuses = await db.get_focuses(user_id)
    if not focuses:
        await _drop_undelivered(user_id)
        await _save(rec, user_id, day, slot, "skipped", "no_focuses", attempts_done)
        return "skipped:no_focuses"

    # ---------- сама попытка ----------
    attempt_no = attempts_done + 1
    attempt_at = utcnow()
    interval = timedelta(minutes=settings.dispatch_retry_interval_minutes)

    # Маркер "попытка начата": если процесс умрёт посреди отправки, reconcile
    # через интервал увидит pending-запись и повторит (или закроет как failed).
    await _save(
        rec, user_id, day, slot, "pending", "attempt_in_progress", attempt_no,
        last_attempt_at=attempt_at.isoformat(),
        next_attempt_at=(attempt_at + interval).isoformat(),
    )
    # Дальнейшие сохранения (успех/сбой/остановка) наследуют last_attempt_at и
    # прочие поля именно от этой записи.
    rec = await db.get_dispatch(user_id, day, slot)

    challenge_id: Optional[int] = undelivered["id"] if undelivered else None
    try:
        if challenge_id is None:
            # Новый ежедневный квест выдаётся - удаляем отчёт о вчерашнем провале
            # (если он был), чтобы он не копился в чате.
            try:
                await profile.clear_failure_message(bot, user)
            except Exception:
                logger.exception("Не удалось удалить отчёт о провале пользователя %s", user_id)

            quest_text = tx.challenge_start(
                streak=user["streak"],
                level=user["level"],
                xp=user["xp"],
                timeout=settings.challenge_timeout_hours,
            )
            challenge_id = await db.create_challenge(user_id, quest_text, focuses)

        # Профиль обновляется ПЕРЕД отправкой карточки - так в чате профиль всегда
        # оказывается выше нового квеста. Сбой профиля не должен лишать игрока
        # карточки (все его ошибки проглатываются и логируются).
        try:
            await profile.ensure_fresh_profile_message(bot, user_id)
        except Exception:
            logger.exception("Не удалось обновить профиль перед выдачей испытания %s", user_id)

        # Игрок мог нажать /stop, пока мы создавали испытание и обновляли профиль.
        fresh_user = await db.get_user(user_id)
        if fresh_user and fresh_user.get("is_stopped"):
            await db.delete_undelivered_challenge(challenge_id)
            await _save(rec, user_id, day, slot, "skipped", "stopped", attempt_no)
            return "skipped:stopped"

        await call_with_retries(
            lambda: challenge_render.send_challenge_message(bot, challenge_id),
            attempts=3,
            delay=settings.profile_retry_delay_seconds,
            what=f"выдача испытания {challenge_id}",
        )
    except Exception as e:  # noqa: BLE001 - любой сбой попытки учитывается политикой повторов
        return await _register_failed_attempt(user_id, day, slot, rec, attempt_no, challenge_id, e)

    # Пока карточка отправлялась, игрок мог нажать /stop (карточка уже создана в
    # БД, поэтому /stop её закрыл, но удалить сообщение с ещё пустым message_id
    # не смог) - убираем свежую карточку, чтобы не висела с мёртвыми кнопками.
    fresh = await db.get_challenge(challenge_id)
    if not fresh or fresh["status"] != "awaiting_action":
        if fresh and fresh.get("message_id"):
            try:
                await bot.delete_message(user_id, fresh["message_id"])
            except Exception as e:  # noqa: BLE001
                logger.info("Не удалось удалить карточку закрытого испытания %s: %s", challenge_id, e)
        await _save(rec, user_id, day, slot, "skipped", "closed_during_send", attempt_no)
        return "skipped:closed_during_send"

    await _save(
        rec, user_id, day, slot, "delivered", None, attempt_no,
        last_attempt_at=attempt_at.isoformat(), error="",
    )
    if attempt_no > 1:
        logger.info("Квест пользователю %s доставлен с попытки %s (%s/%s)", user_id, attempt_no, day, slot)
    return "delivered"


async def _register_failed_attempt(
    user_id: int,
    day: str,
    slot: str,
    rec: Optional[dict],
    attempt_no: int,
    challenge_id: Optional[int],
    error: BaseException,
) -> str:
    err_text = f"{type(error).__name__}: {error}"

    # Игрок заблокировал бота / чат недоступен - часовые повторы бессмысленны.
    if _is_unreachable(error):
        if challenge_id is not None:
            await db.delete_undelivered_challenge(challenge_id)
        await _save(rec, user_id, day, slot, "unreachable", type(error).__name__, attempt_no, error=err_text)
        logger.info("Квест не выдан пользователю %s: бот недоступен для чата (%s)", user_id, err_text)
        return "unreachable"

    if attempt_no >= _max_attempts():
        if challenge_id is not None:
            await db.delete_undelivered_challenge(challenge_id)
        await _save(rec, user_id, day, slot, "failed", "attempts_exhausted", attempt_no, error=err_text)
        logger.warning(
            "Квест пользователю %s не доставлен за %s попыток (%s/%s): %s. Пропускаем до следующего дня.",
            user_id, attempt_no, day, slot, err_text,
        )
        return "failed"

    next_at = utcnow() + timedelta(minutes=settings.dispatch_retry_interval_minutes)
    await _save(
        rec, user_id, day, slot, "pending", "send_failed", attempt_no,
        next_attempt_at=next_at.isoformat(), error=err_text,
    )
    logger.warning(
        "Квест пользователю %s не доставлен (попытка %s/%s, %s/%s): %s. Следующая попытка в %s UTC.",
        user_id, attempt_no, _max_attempts(), day, slot, err_text, next_at.isoformat(timespec="seconds"),
    )
    return "retry_scheduled"


# ---------- задачи планировщика ----------

# (день, слот), по которым обход игроков идёт прямо сейчас. Только в памяти: после
# падения процесса набор пуст, и reconcile сможет доделать недообойдённый слот.
_slot_runs_in_progress: set[tuple[str, str]] = set()


def resolve_slot_day(time_of_day: str) -> str:
    """Локальный день, к которому относится текущий запуск слота (допуск на ранний старт - 30 с)."""
    now = utcnow()
    today = local_today()
    start = slot_start_utc(today, settings.time_slots[time_of_day])
    day = today if start - timedelta(seconds=30) <= now else today - timedelta(days=1)
    return day.isoformat()


async def dispatch_slot(
    bot: Bot,
    time_of_day: str,
    day: Optional[str] = None,
    registered_before: Optional[datetime] = None,
):
    """
    Обход игроков слота: первая попытка выдачи каждому (cron-задача слота; она же
    используется для досылки при пропущенном cron - см. catch_up_missed_dispatches).

    registered_before - при досылке не трогаем тех, кто зарегистрировался уже после
    момента слота: им квест "задним числом" никто не обещал.
    """
    day = day or resolve_slot_day(time_of_day)
    key = (day, time_of_day)
    _slot_runs_in_progress.add(key)
    had_errors = False
    try:
        users = await db.get_users_in_slot(time_of_day)
        for user in users:
            if registered_before is not None:
                registered_at = _parse_iso(user.get("registered_at"))
                if registered_at is not None and registered_at >= registered_before:
                    continue
            # Ошибка на одном игроке (сбой БД и т.п.) не должна обрывать рассылку
            # всем остальным в этом слоте.
            try:
                await process_dispatch(bot, user["user_id"], day, time_of_day)
            except Exception:
                had_errors = True
                logger.exception("Сбой выдачи испытания пользователю %s", user.get("user_id"))
        # Слот отмечается обойдённым только при чистом проходе. Если на ком-то
        # случилась неожиданная ошибка (сбой БД и т.п.) и записи в журнале у него
        # нет - reconcile повторит обход (те, у кого запись уже есть, пропускаются).
        if not had_errors:
            await db.mark_slot_run_finished(day, time_of_day)
    finally:
        _slot_runs_in_progress.discard(key)


async def retry_pending_dispatches(bot: Bot):
    """Повторные попытки по записям pending, у которых подошло время."""
    for rec in await db.get_due_pending_dispatches(utcnow().isoformat()):
        try:
            await process_dispatch(bot, rec["user_id"], rec["day"], rec["slot"])
        except Exception:
            logger.exception("Сбой повторной выдачи пользователю %s", rec.get("user_id"))


async def catch_up_missed_dispatches(bot: Bot):
    """
    Досылает квесты за слоты, в которые cron НЕ отработал (бот выключен/перезапускался/
    процесс убили посреди рассылки): по журналу dispatch_runs такой слот не отмечен
    завершённым, и при этом с его момента прошло не меньше CATCHUP_GRACE_SECONDS, но
    не больше DISPATCH_CATCHUP_WINDOW_MINUTES. Дальше окна день пропускается.
    Перебираются сегодняшний и вчерашний локальные дни - окно может перейти через полночь.
    """
    now = utcnow()
    today = local_today()
    window = timedelta(minutes=settings.dispatch_catchup_window_minutes)
    grace = timedelta(seconds=CATCHUP_GRACE_SECONDS)

    for slot, hhmm in settings.time_slots.items():
        for day in (today, today - timedelta(days=1)):
            start = slot_start_utc(day, hhmm)
            if not (start + grace <= now <= start + window):
                continue
            day_iso = day.isoformat()
            if (day_iso, slot) in _slot_runs_in_progress:
                continue  # cron как раз сейчас обходит этот слот
            if await db.is_slot_run_finished(day_iso, slot):
                continue
            logger.warning("Слот %s/%s не был обработан cron-задачей - досылаю квесты", day_iso, slot)
            try:
                await dispatch_slot(bot, slot, day=day_iso, registered_before=start)
            except Exception:
                logger.exception("Сбой досылки пропущенного слота %s/%s", day_iso, slot)


async def reconcile_dispatches(bot: Bot):
    """Проверка по таймеру (раз в минуту): были ли отправлены квесты, и если нет - повтор/досылка."""
    try:
        await retry_pending_dispatches(bot)
    except Exception:
        logger.exception("Сбой retry_pending_dispatches")
    try:
        await catch_up_missed_dispatches(bot)
    except Exception:
        logger.exception("Сбой catch_up_missed_dispatches")
