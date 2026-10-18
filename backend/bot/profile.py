"""
Закреплённая сводка профиля игрока.

Идея: одно сообщение в личке с ботом, которое создаётся один раз (в конце
регистрации) и затем просто редактируется — при завершении испытания,
при просрочке, при повторной привязке группы и т.п. Никакого мусора в чате.

Как обновляется сообщение (см. sync_profile_message):
  1. Обычное обновление - edit на месте. Прежде чем признать, что edit не
     удался, делается МИНИМУМ 3 попытки (settings.profile_retry_attempts, не
     меньше 3; flood-control учитывается по RetryAfter).
  2. Переотправка (удалить старое + прислать и закрепить новое) происходит
     автоматически, только если ВСЕ попытки edit провалились именно потому,
     что сообщения больше нет / оно не редактируется (удалено игроком, старше
     лимита Telegram и т.п.). Попытки идут с шагом
     settings.profile_edit_retry_interval_seconds (30 с). На временные сбои (сеть, 5xx, flood-control) и
     на блокировку бота переотправка НЕ срабатывает - иначе в чате копились бы
     дубликаты (это и была причина прежнего запрета на пересоздание из sync).
  3. Помимо этого профиль пересоздаётся: явной командой /profile (и /start),
     а также диспетчером квестов, когда сообщение "стареет" (см.
     ensure_fresh_profile_message).

Любая отправка нового сообщения профиля (_replace_profile) выглядит одинаково:
  открепить ВСЕ закреплённые сообщения чата -> отправить новое -> закрепить ->
  удалить старое. Служебные записи "бот закрепил сообщение" удаляет хендлер
  handlers/misc.py::on_pin_service_message (Telegram при ОТкреплении служебного
  сообщения в чате не создаёт - убирать там нечего). Все шаги - с повторными
  попытками (utils.call_with_retries), а операции над профилем одного игрока
  сериализуются блокировкой, чтобы параллельные обновления (джоба таймера
  штрафа + ввод подхода игроком) не создали два профиля одновременно.
"""
import asyncio
import html
import json
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from bot.config import FOCUS_OPTIONS, level_from_total_xp, settings, time_of_day_label
from bot.database import db
from bot.utils import RETRY_AFTER_CAP_SECONDS, call_with_retries

logger = logging.getLogger(__name__)


def ru_days(n: int) -> str:
    """Правильное склонение слова 'день' для русского языка: 1 день, 2 дня, 5 дней и т.п."""
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return "день"
    if n_abs % 10 in (2, 3, 4) and n_abs % 100 not in (12, 13, 14):
        return "дня"
    return "дней"


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return one
    if n_abs % 10 in (2, 3, 4) and n_abs % 100 not in (12, 13, 14):
        return few
    return many


def penalty_block(user: dict) -> str:
    """
    Блок-уведомление о провале требования Системы: показывается, пока у игрока
    активно ограничение (penalty_until в будущем). Всегда встаёт в самый низ
    профиля - см. render_profile_text - чтобы цепляться взглядом в первую
    очередь. Обратный отсчёт пересчитывается при каждом вызове, а сам профиль
    перерисовывается раз в минуту планировщиком (см. scheduler.refresh_penalty_profiles).
    """
    penalty_until = user.get("penalty_until")
    if not penalty_until:
        return ""

    try:
        until = datetime.fromisoformat(penalty_until)
    except (TypeError, ValueError):
        return ""  # испорченное значение в БД не должно ронять отрисовку всего профиля
    remaining = until - datetime.utcnow()
    if remaining.total_seconds() <= 0:
        return ""

    total_minutes = int(remaining.total_seconds() // 60)
    hours, minutes = divmod(total_minutes, 60)
    hours_label = _ru_plural(hours, "час", "часа", "часов")
    minutes_label = _ru_plural(minutes, "минута", "минуты", "минут")

    return (
        "🚫 Вы не выполнили требование системы. На вас были наложены ограничения. \n"
        f"В течении следующих {settings.penalty_hours} часов вы не можете:\n\n"
        "* Смотреть контент 18+\n"
        "* Играть в любые видеоигры.\n\n"
        f"⏱️ До снятия ограничений осталось {hours} {hours_label} и {minutes} {minutes_label}."
    )


def player_code(user: dict) -> str:
    """
    Уникальный короткий код игрока: дата регистрации (ГГММДД) + полный user_id.
    Например, зарегистрировался 28.07.2026, ID 987654321 -> 260728-987654321.

    ВАЖНО: раньше вторая часть обрезалась до первых 5 цифр user_id - это было
    нормально, пока код был чисто декоративным. Теперь он служит ключом поиска
    для /add_friend (см. Database.find_user_by_player_code), а первые 5 цифр
    telegram user_id совсем не гарантируют уникальность (у двух разных ID может
    начинаться одинаково) - обрезка могла привести к тому, что заявка в друзья
    случайно уйдёт не тому человеку. Полный user_id уникален по определению
    (это PRIMARY KEY таблицы users), поэтому здесь он не обрезается.
    """
    try:
        registered = datetime.fromisoformat(user["registered_at"])
        date_part = registered.strftime("%y%m%d")
    except (TypeError, ValueError):
        date_part = "000000"
    return f"{date_part}-{user['user_id']}"


def display_name(user: dict) -> str:
    return user.get("first_name") or user.get("username") or f"Игрок {user['user_id']}"


def get_tracked_focus_keys(user: dict) -> set[str]:
    """Дисциплины, которые игрок сейчас отслеживает (поле users.focuses)."""
    raw = user.get("focuses")
    if not raw:
        return set()
    try:
        return set(json.loads(raw))
    except (TypeError, ValueError):
        return set()


def focus_stat_line(focus_key: str, user: dict, bold: bool = False) -> str:
    """
    Одна строка статистики по дисциплине - вынесено отдельно, чтобы бриф
    друга (bot/friends.py) форматировал числа ТОЧНО так же, как личный
    профиль, вместо дублирования f-строк в двух местах.

    bold=True оборачивает строку в <b>...</b> (сообщения бота отправляются с
    ParseMode.HTML) - так в личном профиле выделяются дисциплины, которые
    игрок отслеживает ПРЯМО СЕЙЧАС.
    """
    opt = FOCUS_OPTIONS[focus_key]
    unit_suffix = f" ({opt['unit']})" if opt["unit"] != "раз" else ""
    line = html.escape(f"{opt['label']}{unit_suffix}: {user[opt['counter_field']]}", quote=False)
    return f"<b>{line}</b>" if bold else line


def render_profile_text(
    user: dict,
    quest_done_today: bool = False,
    bonus_claimed_today: bool = False,
    history_focus_keys: set[str] | None = None,
) -> str:
    level, xp_into_level, xp_needed = level_from_total_xp(user["xp"])
    time_label = time_of_day_label(user["time_of_day"]) if user["time_of_day"] else "—"
    group_line = "да ✅" if user["group_id"] else "нет (напиши /bind_group в группе)"
    streak = user["streak"]

    header = (
        "『Профиль Игрока』\n\n"
        f"🆔 {html.escape(display_name(user))} · #<code>{player_code(user)}</code>\n"
        f"🏆 Уровень: {level} ({xp_into_level}/{xp_needed} XP)\n"
        f"🔥 Серия: {streak} {ru_days(streak)}\n\n"
        f"⏰ Время испытаний: {time_label}\n"
        f"👥 Группа привязана: {group_line}"
    )

    # Дисциплины в профиле показываются в два приоритета (см. описание задачи):
    # Показываем только "нужные" дисциплины: 1) отслеживаемые сейчас (всегда,
    # даже с 0) и 2) не отслеживаемые сейчас, но с историей прогресса (см.
    # Database.get_focus_keys_with_history). Порядок при этом НЕ группируется
    # по приоритету - используется естественный порядок FOCUS_OPTIONS
    # (сначала физические дисциплины, потом интеллектуальные), как и было
    # раньше в профиле. Дисциплины без отслеживания и без единой активности
    # не показываются вовсе.
    tracked_keys = get_tracked_focus_keys(user)
    history_keys = set(history_focus_keys or ())
    visible_keys = [k for k in FOCUS_OPTIONS if k in tracked_keys or k in history_keys]

    # Отслеживаемые сейчас дисциплины выделяются жирным - так сразу видно, какие
    # из показанных строк "живые", а какие остались только как история прогресса
    # по дисциплинам, которые игрок уже убрал через /change_focus.
    stat_lines: list[str] = [focus_stat_line(k, user, bold=(k in tracked_keys)) for k in visible_keys]

    stats = (
        "\n".join(stat_lines)
        if stat_lines
        else "Пока нет отслеживаемых дисциплин - выбери их через /change_focus."
    )

    text = f"{header}\n\n{stats}"

    # "Хвост" профиля: отметка о выполнении сегодняшнего квеста (пропадает сама,
    # как только диспетчер создаст новое испытание на следующий день - см.
    # sync/resend_profile_message), отметка об остановке Системы (см. /stop) и
    # блок штрафа за провал требования Системы. Блок штрафа всегда идёт САМЫМ
    # ПОСЛЕДНИМ, чтобы взгляд цеплялся за него в первую очередь, даже если
    # квест на сегодня уже выполнен.
    tail: list[str] = []
    if quest_done_today:
        tail.append("✅ Вы выполнили сегодняшний квест.")
        if bonus_claimed_today:
            tail.append("✅ Вы завершили секретное испытание.")

    if user.get("is_stopped"):
        # Держится, пока игрок не наберёт /start (см. handlers/start.py::cmd_start,
        # там же снимается is_stopped) - до этого момента ежедневные испытания
        # ему не создаются (см. Database.get_users_by_time).
        tail.append("⏸ Система остановлена. Ежедневные испытания не приходят. Наберите /start, чтобы возобновить.")

    penalty_text = penalty_block(user)
    if penalty_text:
        tail.append(penalty_text)

    if tail:
        text += "\n\n" + "\n".join(tail)
    return text


async def clear_failure_message(bot: Bot, user: dict):
    """
    Удаляет предыдущее сообщение-отчёт о провале испытания (если оно есть) -
    вызывается диспетчером при выдаче нового ежедневного квеста (см.
    scheduler.dispatch_daily_challenges), чтобы отчёты о провалах не копились
    в чате. Подпись о штрафе в самом профиле (penalty_block) при этом НЕ
    трогается - она держится по penalty_until независимо от смены испытаний.
    """
    if not user.get("failure_message_id"):
        return
    chat_id = user.get("failure_chat_id") or user["user_id"]
    try:
        await bot.delete_message(chat_id, user["failure_message_id"])
    except Exception as e:
        logger.info("Не удалось удалить отчёт о провале %s: %s", user["user_id"], e)
    await db.clear_failure_message(user["user_id"])


def _is_profile_stale(user: dict) -> bool:
    """
    True, если текущему сообщению профиля уже >= (profile_edit_window_hours -
    profile_refresh_buffer_hours) часов - т.е. оно приближается к границе,
    после которой Telegram Bot API перестаёт разрешать editMessageText.
    Используется ТОЛЬКО диспетчером ежедневных квестов (см.
    ensure_fresh_profile_message), чтобы решить, пересоздавать профиль в
    момент выдачи квеста или достаточно обычного edit'а.
    """
    created_at = user.get("profile_created_at")
    if not created_at:
        return False
    try:
        created = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return False
    threshold_hours = settings.profile_edit_window_hours - settings.profile_refresh_buffer_hours
    return datetime.utcnow() - created >= timedelta(hours=threshold_hours)


# Блокировки на игрока: все операции, меняющие/пересоздающие сообщение профиля,
# выполняются строго по очереди. Без этого, например, минутная джоба таймера
# штрафа и ввод подхода игроком (оба зовут sync_profile_message) могли бы
# одновременно прийти к выводу "профиля нет/он пропал" и создать два профиля.
_profile_locks: dict[int, asyncio.Lock] = {}


def _profile_lock(user_id: int) -> asyncio.Lock:
    lock = _profile_locks.get(user_id)
    if lock is None:
        lock = _profile_locks[user_id] = asyncio.Lock()
    return lock


async def _build_profile_text(user: dict) -> str:
    user_id = user["user_id"]
    latest_challenge = await db.get_latest_challenge(user_id)
    quest_done_today = bool(
        latest_challenge and latest_challenge["status"] in ("completed", "completed_with_photo")
    )
    bonus_claimed_today = bool(quest_done_today and latest_challenge.get("bonus_claimed"))
    history_focus_keys = await db.get_focus_keys_with_history(user_id)
    return render_profile_text(user, quest_done_today, bonus_claimed_today, history_focus_keys)


def _is_message_gone(error_text: str) -> bool:
    """
    True, если текст ошибки Telegram означает, что сообщение профиля больше не
    существует или не может быть отредактировано (удалено игроком, слишком
    старое и т.п.) - то есть единственный выход - прислать профиль заново.
    """
    t = error_text.lower()
    return (
        "message to edit not found" in t
        or "message can't be edited" in t
        or "message_id_invalid" in t
        or "message identifier is not specified" in t
    )


# Исход попыток отредактировать профиль на месте.
_EDIT_OK = "ok"  # отредактировано (или текст не изменился)
_EDIT_GONE = "gone"  # все попытки провалились, сообщения больше нет/не редактируется
_EDIT_FAILED = "failed"  # временный сбой/прочее - переотправлять НЕ нужно
_EDIT_FORBIDDEN = "forbidden"  # бот заблокирован игроком - ни edit, ни resend не помогут


async def _edit_in_place(bot: Bot, user: dict, text: str) -> str:
    """
    Редактирует сообщение профиля на месте, делая не меньше
    settings.profile_retry_attempts (минимум 3) попыток, и возвращает один из
    _EDIT_*. Исход определяется ПОСЛЕДНЕЙ попыткой: если сообщение пропало
    только на третьей попытке, а на первых двух был сетевой сбой - всё равно
    "gone", потому что именно это состояние актуально сейчас.
    """
    attempts = max(3, settings.profile_retry_attempts)
    outcome = _EDIT_FAILED
    for attempt in range(1, attempts + 1):
        try:
            await bot.edit_message_text(
                text,
                chat_id=user["profile_chat_id"],
                message_id=user["profile_message_id"],
            )
            return _EDIT_OK
        except TelegramForbiddenError:
            return _EDIT_FORBIDDEN
        except TelegramRetryAfter as e:
            outcome = _EDIT_FAILED
            logger.info("Профиль %s: flood-control, попытка %s/%s", user["user_id"], attempt, attempts)
            if attempt < attempts:
                await asyncio.sleep(
                    max(settings.profile_edit_retry_interval_seconds,
                        min(float(e.retry_after) + 0.5, RETRY_AFTER_CAP_SECONDS))
                )
            continue
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return _EDIT_OK  # текст не изменился с прошлого раза - это нормально
            outcome = _EDIT_GONE if _is_message_gone(str(e)) else _EDIT_FAILED
            logger.info("Профиль %s: edit, попытка %s/%s не удалась: %s", user["user_id"], attempt, attempts, e)
        except Exception as e:
            outcome = _EDIT_FAILED
            logger.info("Профиль %s: edit, попытка %s/%s не удалась: %s", user["user_id"], attempt, attempts, e)

        if attempt < attempts:
            await asyncio.sleep(settings.profile_edit_retry_interval_seconds)
    return outcome


async def _unpin_all(bot: Bot, chat_id: int, fallback_message_id: int | None = None) -> None:
    """
    Открепляет ВСЕ закреплённые сообщения в чате (а не только прошлый профиль):
    перед отправкой нового профиля в закрепе не должно оставаться ничего
    лишнего. Если массовое открепление не удалось даже с повторами - пробуем
    хотя бы открепить прошлый профиль по id. Ошибки только логируются: на
    отправку нового профиля это не должно влиять.
    """
    attempts = max(3, settings.profile_retry_attempts)
    try:
        await call_with_retries(
            lambda: bot.unpin_all_chat_messages(chat_id),
            attempts=attempts,
            delay=settings.profile_retry_delay_seconds,
            what=f"открепление всех сообщений {chat_id}",
        )
        return
    except Exception as e:
        logger.warning("Не удалось открепить все сообщения в чате %s: %s", chat_id, e)

    if fallback_message_id:
        try:
            await call_with_retries(
                lambda: bot.unpin_chat_message(chat_id, message_id=fallback_message_id),
                attempts=attempts,
                delay=settings.profile_retry_delay_seconds,
                what=f"открепление профиля {chat_id}",
            )
        except Exception as e:
            logger.warning("Не удалось открепить прежний профиль %s: %s", chat_id, e)


async def _delete_message_with_retries(bot: Bot, chat_id: int, message_id: int) -> bool:
    """Удаляет сообщение с повторами. 'Уже удалено' считается успехом. True - сообщения больше нет."""

    async def _delete():
        try:
            await bot.delete_message(chat_id, message_id)
        except TelegramBadRequest as e:
            if "message to delete not found" in str(e).lower():
                return
            raise

    try:
        await call_with_retries(
            _delete,
            attempts=max(3, settings.profile_retry_attempts),
            delay=settings.profile_retry_delay_seconds,
            what=f"удаление сообщения {message_id}",
        )
        return True
    except Exception as e:
        logger.warning("Не удалось удалить сообщение %s в чате %s: %s", message_id, chat_id, e)
        return False


async def _replace_profile(bot: Bot, user: dict, text: str) -> bool:
    """
    Единственная точка, где отправляется НОВОЕ сообщение профиля. Вызывать
    только под _profile_lock(user_id). Порядок:

      1. открепить все закреплённые сообщения чата;
      2. отправить новый профиль (с повторами; без звука - пересоздание
         профиля само по себе не повод для пуша);
      3. записать его id в БД и закрепить (служебное "бот закрепил сообщение"
         удаляет хендлер on_pin_service_message);
      4. удалить прежнее сообщение профиля.

    Если отправка нового сообщения не удалась даже с повторами - прежнее
    сообщение НЕ удаляется (у игрока остаётся хоть какой-то профиль), его
    закреп восстанавливается по возможности, возвращается False.
    """
    user_id = user["user_id"]
    chat_id = user_id  # профиль всегда живёт в личке с игроком
    old_chat_id = user.get("profile_chat_id") or chat_id
    old_message_id = user.get("profile_message_id")
    attempts = max(3, settings.profile_retry_attempts)

    await _unpin_all(bot, chat_id, fallback_message_id=old_message_id)

    try:
        msg = await call_with_retries(
            lambda: bot.send_message(chat_id, text, disable_notification=True),
            attempts=attempts,
            delay=settings.profile_retry_delay_seconds,
            what=f"отправка профиля {user_id}",
        )
    except Exception as e:
        logger.warning("Не удалось отправить сводку профиля %s: %s", user_id, e)
        if old_message_id:
            try:
                await bot.pin_chat_message(old_chat_id, old_message_id, disable_notification=True)
            except Exception:
                pass
        return False

    await db.set_profile_message(user_id, chat_id, msg.message_id)

    try:
        await call_with_retries(
            lambda: bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True),
            attempts=attempts,
            delay=settings.profile_retry_delay_seconds,
            what=f"закрепление профиля {user_id}",
        )
    except Exception as e:
        logger.info("Не удалось закрепить сводку профиля %s: %s", user_id, e)

    if old_message_id and (old_chat_id, old_message_id) != (chat_id, msg.message_id):
        await _delete_message_with_retries(bot, old_chat_id, old_message_id)
    return True


async def sync_profile_message(bot: Bot, user_id: int):
    """
    Обновляет закреплённую сводку профиля лёгким edit'ом НА МЕСТЕ. Используется
    для всех автоматических обновлений (испытание завершено/просрочено/группа
    привязана, таймер штрафа и т.п.).

    Не меньше 3 попыток edit (см. _edit_in_place). Если ВСЕ они провалились
    потому, что сообщения профиля больше нет или оно не редактируется - профиль
    переотправляется (см. _replace_profile). При временных сбоях и блокировке
    бота ничего не пересоздаётся: актуальный текст подтянется при следующем
    вызове.

    Если сообщения профиля ещё нет вообще (первая регистрация) - создаёт его.
    """
    async with _profile_lock(user_id):
        user = await db.get_user(user_id)
        if not user:
            return
        text = await _build_profile_text(user)

        if not user.get("profile_message_id"):
            await _replace_profile(bot, user, text)
            return

        result = await _edit_in_place(bot, user, text)
        if result == _EDIT_GONE:
            logger.warning(
                "Профиль %s: сообщения больше нет или оно не редактируется "
                "(после всех попыток) - переотправляю.", user_id
            )
            await _replace_profile(bot, user, text)
        elif result == _EDIT_FAILED:
            logger.info("Профиль %s: не удалось обновить (НЕ пересоздаю, попробую в следующий раз)", user_id)
        elif result == _EDIT_FORBIDDEN:
            logger.info("Профиль %s: бот заблокирован игроком, обновление пропущено", user_id)


async def ensure_fresh_profile_message(bot: Bot, user_id: int):
    """
    Используется ИСКЛЮЧИТЕЛЬНО ежедневным диспетчером квестов
    (см. scheduler.dispatch_daily_challenges), вызывается один раз в день - в
    момент выдачи испытания на собственный временной слот игрока (а также в
    слот, пропущенный из-за действующего ограничения - чтобы профиль не
    "состарился" за время паузы).

    Если сообщение профиля приближается к границе, после которой Telegram
    перестанет давать его редактировать (см. _is_profile_stale) - пересоздаёт
    его. Иначе - обычный лёгкий edit (sync_profile_message).
    """
    async with _profile_lock(user_id):
        user = await db.get_user(user_id)
        if not user:
            return
        if user.get("profile_message_id") and _is_profile_stale(user):
            text = await _build_profile_text(user)
            await _replace_profile(bot, user, text)
            return

    await sync_profile_message(bot, user_id)


async def resend_profile_message(bot: Bot, user_id: int):
    """
    Принудительно пересоздаёт сводку профиля: открепляет всё закреплённое,
    присылает и закрепляет новое сообщение, удаляет старое.

    Используется командами /profile и /start - Telegram не даёт боту способа
    узнать, очистил ли пользователь историю чата локально (edit_message_text в
    этом случае "успешно" редактирует невидимое сообщение), поэтому единственный
    надёжный способ гарантировать видимость профиля по явному запросу -
    переслать его заново.
    """
    async with _profile_lock(user_id):
        user = await db.get_user(user_id)
        if not user:
            return
        text = await _build_profile_text(user)
        await _replace_profile(bot, user, text)
