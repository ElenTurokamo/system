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
import html
import logging
import math
import time
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from bot import keyboards as kb
from bot import messages as tx
from bot.config import (
    BONUS_MULTIPLIER,
    BONUS_XP_MULTIPLIER,
    CHALLENGE_COUNTDOWN_WINDOW_SECONDS,
    FAILURE_WARNING_MINUTES,
    FOCUS_OPTIONS,
    ease_available,
    eased_target,
    level_from_total_xp,
    settings,
)
from bot.database import db
from bot.utils import call_with_retries

logger = logging.getLogger(__name__)

# Разделитель в карточке испытания. "=" уже, чем "─" (~0,6 em против 1 em), поэтому
# 20 знаков по ширине ≈ 12 знаков SYSTEM_DIVIDER (см. messages.py): визуально обе
# линии одинаковы и целиком помещаются в пузырь на узких экранах (iPhone mini).
DIVIDER = "=" * 20

# Строка на карточке после подтверждённого снижения сложности.
EASED_CARD_NOTE = "📉 Сложность снижена: цели и награда вдвое меньше, секретное испытание сегодня недоступно."


def _join(lines: list[str]) -> str:
    return ("\n\n" + "\n\n".join(lines)) if lines else ""


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return one
    if n_abs % 10 in (2, 3, 4) and n_abs % 100 not in (12, 13, 14):
        return few
    return many


def challenge_deadline(challenge: dict) -> Optional[datetime]:
    """Момент (UTC), когда испытание сгорит по таймауту, либо None, если started_at некорректен."""
    try:
        started = datetime.fromisoformat(challenge["started_at"])
    except (TypeError, ValueError, KeyError):
        return None
    return started + timedelta(hours=settings.challenge_timeout_hours)


def seconds_left(challenge: dict) -> Optional[float]:
    """Сколько секунд осталось до провала (может быть <= 0). None - дедлайн неизвестен."""
    deadline = challenge_deadline(challenge)
    if deadline is None:
        return None
    return (deadline - datetime.utcnow()).total_seconds()


def in_final_countdown(challenge: dict) -> bool:
    """True, если до провала осталось не больше 15 минут (но время ещё не вышло)."""
    left = seconds_left(challenge)
    return left is not None and 0 < left <= CHALLENGE_COUNTDOWN_WINDOW_SECONDS


def _format_time_left(challenge: dict) -> str:
    """
    Строка обратного отсчёта до провала испытания по таймауту.

    Дольше 15 минут - "X часов и Y минут" (обновляется раз в минуту). В
    последние 15 минут - "ММ:СС" (обновляется каждые несколько секунд, см.
    scheduler.refresh_final_countdown). Секунды округляются ВВЕРХ, чтобы таймер
    не показывал 00:00, пока время на самом деле ещё не вышло.
    """
    remaining_seconds = seconds_left(challenge)
    if remaining_seconds is None:
        return "⏱️ До провала испытания осталось: —"

    if remaining_seconds <= 0:
        return "⏱️ Время испытания на исходе - вот-вот сгорит."

    if remaining_seconds <= CHALLENGE_COUNTDOWN_WINDOW_SECONDS:
        total_seconds = math.ceil(remaining_seconds)
        minutes, seconds = divmod(total_seconds, 60)
        return f"⏱️ До провала испытания осталось: {minutes:02d}:{seconds:02d}"

    total_minutes = int(remaining_seconds // 60)
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
            names.append(html.escape(display_name(supporter)))
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
        #   ====================
        #   ⏱️ таймер
        # Статус бонуса и выбранная дисциплина текстом не дублируются - они видны
        # прямо на кнопках клавиатуры (🎯, «Пропустить фото», «Завершить испытание»).
        # Подсказка про фото пропадает сама при следующей перерисовке после того,
        # как физ. фото отправлено или пропущено.
        text = quest
        if challenge.get("eased"):
            text += f"\n\n{EASED_CARD_NOTE}"
        if _physical_photo_pending(challenge, progress_rows):
            text += "\n\n📸 Пришли фото выполнения всех физических активностей."

        cheer_line = await _build_cheer_line(challenge["id"])
        if cheer_line:
            text += f"\n\n{cheer_line}"

        text += f"\n{DIVIDER}\n{_format_time_left(challenge)}"
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


def ease_offered(challenge: dict, progress_rows: list[dict], user: Optional[dict]) -> bool:
    """
    Показывать ли кнопку "📉 Снизить сложность": игрок достиг нужного уровня (см.
    config.EASE_UNLOCK_LEVEL), сложность сегодня ещё не понижена и не все цели дня закрыты
    (при всех закрытых целях кнопка пропадает до конца дня). Уровень считается из XP - так же,
    как цели при создании испытания (Database.create_challenge).
    """
    if challenge["status"] != "awaiting_action" or challenge.get("eased") or not user:
        return False
    if not progress_rows or all(p["completed"] for p in progress_rows):
        return False
    level, _, _ = level_from_total_xp(user["xp"])
    return ease_available(level)


def _build_keyboard(challenge: dict, progress_rows: list[dict], show_ease: bool = False):
    status = challenge["status"]
    if status != "awaiting_action":
        return None  # финальные статусы - клавиатура убирается

    show_finish = bool(progress_rows) and all(p["completed"] for p in progress_rows)
    show_skip_photo = _physical_photo_pending(challenge, progress_rows)
    return kb.challenge_kb(
        challenge["id"], progress_rows, challenge["active_focus"], show_finish, show_skip_photo, show_ease
    )


async def _render_card(challenge: dict) -> tuple[str, object]:
    """Текст и клавиатура карточки по актуальному состоянию - единая сборка для отправки и редактирования."""
    progress_rows = await db.get_progress_rows(challenge["id"])
    text = await _build_text(challenge, progress_rows)
    user = await db.get_user(challenge["user_id"]) if challenge["status"] == "awaiting_action" else None
    markup = _build_keyboard(challenge, progress_rows, ease_offered(challenge, progress_rows, user))
    return text, markup


# Карточки, на которых сейчас показан экран "Сдаться? Да/Нет": challenge_id ->
# момент (time.monotonic), до которого экран считается "живым". Пока он жив,
# таймерные перерисовки (раз в минуту / раз в несколько секунд в последние 15
# минут) его НЕ трогают - иначе они затирали бы подтверждение обратно в обычную
# карточку прямо под пальцем игрока. По истечении CONFIRM_TTL экран без ответа
# откатывается к обычной карточке сам (на ближайшем тике таймера).
_GIVEUP_CONFIRM_TTL_SECONDS = 45.0
_giveup_confirm_until: dict[int, float] = {}

# То же для экрана "Снизить сложность? Да/Нет" (см. push_ease_confirm).
_ease_confirm_until: dict[int, float] = {}

# Chat/user_id -> момент (time.monotonic), до которого Telegram просил не
# редактировать сообщения (RetryAfter). Только для таймерных перерисовок: они
# не критичны и безопасно пропускаются, пока действует flood-control.
_flood_until: dict[int, float] = {}


def _giveup_confirm_active(challenge_id: int) -> bool:
    until = _giveup_confirm_until.get(challenge_id)
    if until is None:
        return False
    if time.monotonic() < until:
        return True
    _giveup_confirm_until.pop(challenge_id, None)
    return False


def _ease_confirm_active(challenge_id: int) -> bool:
    until = _ease_confirm_until.get(challenge_id)
    if until is None:
        return False
    if time.monotonic() < until:
        return True
    _ease_confirm_until.pop(challenge_id, None)
    return False


async def push_challenge_update(bot: Bot, challenge_id: int, *, from_timer: bool = False) -> bool:
    """
    Перерисовывает сообщение испытания по актуальному состоянию из БД.

    from_timer=True - вызов от таймерных джоб планировщика (не от действия
    игрока): в этом режиме перерисовка пропускается, пока на карточке открыт
    экран подтверждения сдачи или действует flood-control Telegram. Возвращает
    True, если сообщение реально отредактировано.
    """
    challenge = await db.get_challenge(challenge_id)
    if not challenge or not challenge.get("message_id"):
        return False

    status = challenge["status"]

    if status in ("completed", "completed_with_photo"):
        # Успешное завершение дня больше не оформляется отдельной "отчётной"
        # карточкой (✅ Испытание завершено / стрик / уровень / фото / бонус) -
        # вся эта информация (включая секретный бонус, если он был получен)
        # теперь постоянно видна в профиле двумя строками в его конце (см.
        # profile.py). Чтобы не дублировать её в чате, карточка испытания
        # просто удаляется.
        _giveup_confirm_until.pop(challenge_id, None)
        _ease_confirm_until.pop(challenge_id, None)
        await clear_warning_message(bot, challenge_id)
        try:
            await bot.delete_message(challenge["user_id"], challenge["message_id"])
        except Exception as e:
            logger.info("Не удалось удалить сообщение испытания %s: %s", challenge_id, e)
        return False

    if from_timer:
        if status != "awaiting_action":
            return False
        if _giveup_confirm_active(challenge_id) or _ease_confirm_active(challenge_id):
            return False
        if time.monotonic() < _flood_until.get(challenge["user_id"], 0.0):
            return False
    else:
        # Любая НЕтаймерная перерисовка (выбор дисциплины, "Нет" на подтверждении
        # и т.п.) возвращает обычный вид карточки - экран подтверждения закрыт.
        _giveup_confirm_until.pop(challenge_id, None)
        _ease_confirm_until.pop(challenge_id, None)

    text, markup = await _render_card(challenge)

    try:
        await bot.edit_message_text(
            text,
            chat_id=challenge["user_id"],
            message_id=challenge["message_id"],
            reply_markup=markup,
        )
        return True
    except TelegramRetryAfter as e:
        _flood_until[challenge["user_id"]] = time.monotonic() + float(e.retry_after) + 0.5
        logger.info("Flood-control при обновлении испытания %s: пауза %s с", challenge_id, e.retry_after)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e).lower():
            logger.info("Не удалось обновить сообщение испытания %s: %s", challenge_id, e)
    except Exception as e:
        logger.info("Не удалось обновить сообщение испытания %s: %s", challenge_id, e)
    return False


async def push_giveup_confirm(bot: Bot, challenge_id: int):
    """
    Полностью подменяет карточку испытания на экран подтверждения сдачи:
    текст - просто "Сдаться?", клавиатура - "Да"/"Нет" (см. keyboards.confirm_giveup_kb).

    Это тот же принцип редактирования единственного сообщения испытания, что и
    у push_challenge_update - никакого нового сообщения не создаётся. Если
    игрок нажмёт "Нет", карточка возвращается к обычному виду через обычный
    push_challenge_update (см. handlers/challenge.py).
    """
    challenge = await db.get_challenge(challenge_id)
    if not challenge or not challenge.get("message_id"):
        return

    try:
        await bot.edit_message_text(
            "Сдаться?",
            chat_id=challenge["user_id"],
            message_id=challenge["message_id"],
            reply_markup=kb.confirm_giveup_kb(challenge_id),
        )
        _giveup_confirm_until[challenge_id] = time.monotonic() + _GIVEUP_CONFIRM_TTL_SECONDS
    except Exception as e:
        logger.info("Не удалось показать подтверждение сдачи %s: %s", challenge_id, e)


def _ease_confirm_text(progress_rows: list[dict]) -> str:
    lines = ["📉 Снизить сложность?", "", "Цели на сегодня станут вдвое меньше:"]
    for p in progress_rows:
        lines.append(f"{FOCUS_OPTIONS[p['focus']]['label']}: {p['target']} → {eased_target(p['target'])}")
    lines += [
        "",
        "• Награда за испытание — половина XP.",
        "• Секретное испытание сегодня станет недоступно.",
        "• Серия сохранится.",
        "",
        "Отменить это уже нельзя.",
    ]
    return "\n".join(lines)


async def push_ease_confirm(bot: Bot, challenge_id: int):
    """
    Подменяет карточку испытания на экран подтверждения снижения сложности: цели "было → станет",
    что изменится, и кнопки "Да"/"Нет" (keyboards.confirm_ease_kb). Как и экран "Сдаться?", это
    правка единственного сообщения испытания - нового сообщения не создаётся; "Нет" возвращает
    обычную карточку (кнопка снижения сложности остаётся), "Да" - см. handlers/challenge.py.
    """
    challenge = await db.get_challenge(challenge_id)
    if not challenge or not challenge.get("message_id"):
        return
    progress_rows = await db.get_progress_rows(challenge_id)
    try:
        await bot.edit_message_text(
            _ease_confirm_text(progress_rows),
            chat_id=challenge["user_id"],
            message_id=challenge["message_id"],
            reply_markup=kb.confirm_ease_kb(challenge_id),
        )
        _ease_confirm_until[challenge_id] = time.monotonic() + _GIVEUP_CONFIRM_TTL_SECONDS
    except Exception as e:
        logger.info("Не удалось показать подтверждение снижения сложности %s: %s", challenge_id, e)


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

    _giveup_confirm_until.pop(challenge_id, None)
    # Предупреждение "осталось менее N минут" после провала уже неактуально.
    await clear_warning_message(bot, challenge_id)

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


# ---------- предупреждения о скором провале ----------


async def clear_warning_message(bot: Bot, challenge_id: int):
    """
    Удаляет из чата текущее предупреждение о скором провале (если оно есть) и
    обнуляет его id в БД. Вызывается при ЛЮБОМ закрытии испытания (завершено,
    сдался, просрочено, /stop), а также перед отправкой следующего
    предупреждения - в чате одновременно висит не больше одного.
    """
    challenge = await db.get_challenge(challenge_id)
    if not challenge or not challenge.get("warning_message_id"):
        return
    message_id = challenge["warning_message_id"]
    # Сначала обнуляем в БД: даже если удаление не удастся, мы не будем
    # безуспешно долбиться в одно и то же сообщение при каждом закрытии.
    await db.set_warning_message(challenge_id, None)
    try:
        await bot.delete_message(challenge["user_id"], message_id)
    except Exception as e:
        logger.info("Не удалось удалить предупреждение о провале %s: %s", challenge_id, e)


def due_warning_threshold(challenge: dict) -> Optional[int]:
    """
    Порог предупреждения (60/30/15 минут), которое пора отправить прямо сейчас,
    либо None. Берётся самый СРОЧНЫЙ из уже пересечённых порогов, о котором ещё
    не предупреждали: если бот был недоступен и вернулся, когда до провала 20
    минут, придёт одно предупреждение "менее 30 минут", а не серия из задним
    числом пропущенных. Пороги, не меньшие всего таймаута испытания, не
    используются (иначе при коротком CHALLENGE_TIMEOUT_HOURS предупреждение
    приходило бы сразу при создании квеста).
    """
    if challenge.get("status") != "awaiting_action":
        return None
    left = seconds_left(challenge)
    if left is None or left <= 0:
        return None  # время вышло - этим займётся expire_stale_challenges

    timeout_minutes = settings.challenge_timeout_hours * 60
    crossed = [m for m in FAILURE_WARNING_MINUTES if m < timeout_minutes and left <= m * 60]
    if not crossed:
        return None

    threshold = min(crossed)
    warned = challenge.get("warned_minutes")
    if warned is not None and warned <= threshold:
        return None
    return threshold


async def maybe_send_failure_warning(bot: Bot, challenge: dict) -> bool:
    """
    Отправляет предупреждение о скором провале, если пора (см.
    due_warning_threshold). Возвращает True, если сообщение отправлено.
    """
    threshold = due_warning_threshold(challenge)
    if threshold is None:
        return False

    # Все цели дня уже закрыты - провал игроку не грозит (испытание всё равно будет
    # засчитано, даже если он забудет нажать "Завершить" - см. bot/completion.py),
    # а текст про штраф был бы ложным. Порог при этом НЕ занимается: если игрок
    # потом откатит прогресс ниже цели, предупреждение придёт как обычно.
    if await db.goals_ready(challenge["id"]):
        return False

    # Порог занимается ДО отправки (атомарный UPDATE с условием): так два
    # параллельных запуска джобы не пришлют дубль. Если отправка потом не
    # удастся - это одно потерянное предупреждение, а не бесконечные повторы
    # каждые полминуты (например, если игрок заблокировал бота).
    if not await db.claim_warning_level(challenge["id"], threshold):
        return False

    user_id = challenge["user_id"]
    await clear_warning_message(bot, challenge["id"])  # предыдущее предупреждение (60 -> 30 -> 15)

    text = tx.challenge_warning(
        threshold,
        xp_loss=settings.missed_day_xp_penalty,
        penalty_hours=settings.penalty_hours,
    )
    try:
        # Обычное уведомление (со звуком) - это именно та ситуация, ради которой
        # игрока стоит отвлечь. 3 попытки: предупреждение пропустить обидно.
        msg = await call_with_retries(
            lambda: bot.send_message(user_id, text),
            attempts=3,
            delay=settings.profile_retry_delay_seconds,
            what=f"предупреждение о провале {challenge['id']}",
        )
    except Exception as e:
        logger.warning("Не удалось отправить предупреждение о провале пользователю %s: %s", user_id, e)
        return False

    await db.set_warning_message(challenge["id"], msg.message_id)

    # Пока шли попытки отправки, игрок мог завершить/сдать испытание - тогда
    # только что отправленное предупреждение уже ложное, убираем его сразу.
    fresh = await db.get_challenge(challenge["id"])
    if (
        not fresh
        or fresh["status"] != "awaiting_action"
        or await db.goals_ready(challenge["id"])  # цели закрыли, пока шла отправка
    ):
        await clear_warning_message(bot, challenge["id"])
        return False
    return True


async def send_challenge_message(bot: Bot, challenge_id: int):
    """
    Отправляет актуальное состояние испытания НОВЫМ сообщением и записывает его
    message_id как точку правды, по которой дальше работает push_challenge_update.

    Используется при первичной отправке испытания дня (диспетчер) - в этот
    момент предыдущего сообщения по определению нет, удалять нечего. Для
    случаев, когда карточка уже могла существовать (см. resend_challenge_message
    ниже), используй именно её - иначе получится дубликат в чате.
    """
    challenge = await db.get_challenge(challenge_id)
    if not challenge:
        return

    # Первая доставка (message_id ещё пуст): таймаут должен идти с момента, когда
    # карточка реально дойдёт до игрока, а не с создания строки в БД - при
    # повторных попытках выдачи (см. bot/dispatch.py) это могут быть часы.
    if not challenge.get("message_id") and challenge["status"] == "awaiting_action":
        if await db.restart_challenge_timer(challenge_id):
            challenge = await db.get_challenge(challenge_id) or challenge

    text, markup = await _render_card(challenge)

    msg = await bot.send_message(challenge["user_id"], text, reply_markup=markup)
    await db.set_message_id(challenge_id, msg.message_id)


async def resend_challenge_message(bot: Bot, challenge_id: int):
    """
    Принудительно ПЕРЕсоздаёт карточку испытания: сначала удаляет предыдущее
    сообщение (если оно есть - удаление просто игнорируется, если сообщение
    уже недоступно), затем отправляет новое как send_challenge_message.

    В отличие от send_challenge_message, здесь мы НЕ предполагаем, что
    предыдущего сообщения не существует - используется там, где карточка
    могла уже быть отправлена ранее (например, /start у игрока с активным
    испытанием, см. handlers/start.py). Без явного удаления здесь оставался
    бы дубликат: старая карточка продолжала бы висеть в чате рядом с новой.
    """
    challenge = await db.get_challenge(challenge_id)
    if not challenge:
        return

    if challenge.get("message_id"):
        try:
            await bot.delete_message(challenge["user_id"], challenge["message_id"])
        except Exception as e:
            logger.info("Не удалось удалить предыдущую карточку испытания %s: %s", challenge_id, e)

    await send_challenge_message(bot, challenge_id)
