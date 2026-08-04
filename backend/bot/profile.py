"""
Закреплённая сводка профиля игрока.

Идея: одно сообщение в личке с ботом, которое создаётся один раз (в конце
регистрации) и затем просто редактируется — при завершении испытания,
при просрочке, при повторной привязке группы и т.п. Никакого мусора в чате.
"""
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from bot.config import FOCUS_OPTIONS, level_from_total_xp, settings, time_of_day_label
from bot.database import db

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

    until = datetime.fromisoformat(penalty_until)
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


def render_profile_text(user: dict, quest_done_today: bool = False, bonus_claimed_today: bool = False) -> str:
    level, xp_into_level, xp_needed = level_from_total_xp(user["xp"])
    time_label = time_of_day_label(user["time_of_day"]) if user["time_of_day"] else "—"
    group_line = "да ✅" if user["group_id"] else "нет (напиши /bind_group в группе)"
    streak = user["streak"]

    header = (
        "『Профиль Игрока』\n\n"
        f"🆔 {display_name(user)} · #<code>{player_code(user)}</code>\n"
        f"🏆 Уровень: {level} ({xp_into_level}/{xp_needed} XP)\n"
        f"🔥 Серия: {streak} {ru_days(streak)}\n\n"
        f"⏰ Время испытаний: {time_label}\n"
        f"👥 Группа привязана: {group_line}"
    )

    stats = (
        f"{FOCUS_OPTIONS['pushups']['label']}: {user['daily_pushups']}\n"
        f"{FOCUS_OPTIONS['squats']['label']}: {user['daily_squats']}\n"
        f"{FOCUS_OPTIONS['abs']['label']}: {user['daily_abs']}\n"
        f"{FOCUS_OPTIONS['pullups']['label']}: {user['daily_pullups']}\n"
        f"{FOCUS_OPTIONS['running']['label']} (мин): {user['daily_running']}\n"
        f"{FOCUS_OPTIONS['chess']['label']} (партий): {user['daily_chess']}\n"
        f"{FOCUS_OPTIONS['reading']['label']} (страниц): {user['daily_reading']}"
    )

    text = f"{header}\n\n{stats}"

    # "Хвост" профиля: отметка о выполнении сегодняшнего квеста (пропадает сама,
    # как только диспетчер создаст новое испытание на следующий день - см.
    # sync/resend_profile_message) и блок штрафа за провал требования Системы.
    # Блок штрафа всегда идёт САМЫМ ПОСЛЕДНИМ, чтобы взгляд цеплялся за него
    # в первую очередь, даже если квест на сегодня уже выполнен.
    tail: list[str] = []
    if quest_done_today:
        tail.append("✅ Вы выполнили сегодняшний квест.")
        if bonus_claimed_today:
            tail.append("✅ Вы завершили секретное испытание.")

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


async def sync_profile_message(bot: Bot, user_id: int):
    """
    Обновляет закреплённую сводку профиля пользователя.
    Используется для автоматических обновлений (испытание завершено/просрочено/группа
    привязана) - лёгкий edit без пересоздания сообщения, чтобы не спамить.
    Если сообщения ещё нет (первая регистрация) или его удалили НАВСЕГДА (ошибка API) -
    создаёт новое и закрепляет.
    """
    user = await db.get_user(user_id)
    if not user:
        return

    latest_challenge = await db.get_latest_challenge(user_id)
    quest_done_today = bool(
        latest_challenge and latest_challenge["status"] in ("completed", "completed_with_photo")
    )
    bonus_claimed_today = bool(quest_done_today and latest_challenge.get("bonus_claimed"))
    text = render_profile_text(user, quest_done_today, bonus_claimed_today)

    if user.get("profile_message_id"):
        try:
            await bot.edit_message_text(
                text,
                chat_id=user["profile_chat_id"],
                message_id=user["profile_message_id"],
            )
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                # Текст не изменился с прошлого раза - это нормально, ничего не делаем.
                return
            logger.info(
                "Не удалось обновить сводку профиля %s (пересоздаю): %s", user_id, e
            )
        except Exception as e:
            logger.info(
                "Не удалось обновить сводку профиля %s (пересоздаю): %s", user_id, e
            )

    await _send_and_pin(bot, user_id, text)


async def resend_profile_message(bot: Bot, user_id: int):
    """
    Принудительно пересоздаёт сводку профиля: удаляет старое сообщение (если получится)
    и присылает + закрепляет новое.

    Используется командой /profile - Telegram не даёт боту способа узнать, очистил ли
    пользователь историю чата локально (edit_message_text в этом случае "успешно"
    редактирует невидимое сообщение), поэтому единственный надёжный способ гарантировать
    видимость профиля по явному запросу - переслать его заново.
    """
    user = await db.get_user(user_id)
    if not user:
        return

    latest_challenge = await db.get_latest_challenge(user_id)
    quest_done_today = bool(
        latest_challenge and latest_challenge["status"] in ("completed", "completed_with_photo")
    )
    bonus_claimed_today = bool(quest_done_today and latest_challenge.get("bonus_claimed"))
    text = render_profile_text(user, quest_done_today, bonus_claimed_today)

    if user.get("profile_message_id"):
        try:
            await bot.delete_message(user["profile_chat_id"], user["profile_message_id"])
        except Exception:
            pass  # уже удалено/недоступно - не страшно, всё равно создадим новое

    await _send_and_pin(bot, user_id, text)


async def _send_and_pin(bot: Bot, user_id: int, text: str):
    try:
        msg = await bot.send_message(user_id, text)
        await db.set_profile_message(user_id, user_id, msg.message_id)
        try:
            await bot.pin_chat_message(user_id, msg.message_id, disable_notification=True)
        except Exception as e:
            logger.info("Не удалось закрепить сводку профиля %s: %s", user_id, e)
    except Exception as e:
        logger.warning("Не удалось отправить сводку профиля %s: %s", user_id, e)
