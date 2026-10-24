"""
PDF-гайд игрока (data/guide.pdf) - отправка вместе с /help и при первой регистрации.

Файл лежит в data/ (эта папка копируется в Docker-образ - см. Dockerfile). Чтобы
обновить гайд, достаточно заменить data/guide.pdf и перезапустить бота.

Отправка:
  * первый раз файл загружается в Telegram с диска, дальше используется file_id
    из ответа (кэш в памяти процесса): повторная загрузка ~0,5 МБ при каждом
    /help не нужна. Если Telegram отверг кэшированный file_id - один раз
    загружаем файл заново;
  * /help - ОДНО сообщение: PDF с подписью-списком команд (лимит подписи 1024
    знака). Как и прочие ответы на команды, оно живёт ограниченное время, но
    дольше обычных (GUIDE_TTL_SECONDS): за 30 секунд гайд не успевают открыть;
  * регистрация - отдельное сообщение, которое остаётся в чате; приходит один раз
    за всё время (флаг users.guide_sent), ВЫШЕ первого шага регистрации;
  * если файла нет или Telegram недоступен - /help отвечает обычным текстом
    (как раньше), регистрация идёт дальше без гайда. Гайд - не критичная часть.
"""
import logging
from pathlib import Path
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import FSInputFile, Message

from bot.database import db
from bot.utils import replace_command_message, send_command_message, track_command_message

logger = logging.getLogger(__name__)

GUIDE_PATH = Path(__file__).resolve().parent.parent / "data" / "guide.pdf"
GUIDE_FILENAME = "Гайд по системе.pdf"

# Сколько секунд сообщение /help с гайдом остаётся в чате (обычные ответы - 30 с).
GUIDE_TTL_SECONDS = 300

HELP_TEXT = (
    "/start — регистрация\n"
    "/profile — обновить и показать закреплённую сводку профиля\n"
    "/change_time — сменить время ежедневного испытания\n"
    "/change_focus — сменить дисциплины испытаний\n"
    "/stop — остановить Систему (ежедневные испытания перестанут приходить)\n"
    "/get_stats_excel — скачать статистику прокачки в Excel\n"
    "/add_friend — добавить друга по его коду игрока\n"
    "/friendlist — список друзей и их прогресс\n"
    "/bind_group — привязать текущую группу (вызывать внутри группы)\n"
    "/group_id — узнать ID текущей группы"
)

REGISTRATION_CAPTION = (
    "📘 Гайд по Системе: как устроены квесты, XP и серия.\n"
    "Загляни, пока настраиваешь профиль. Он всегда доступен по команде /help."
)

# file_id уже загруженного гайда (только для этого токена бота; живёт до перезапуска).
_file_id: Optional[str] = None


def guide_available() -> bool:
    return GUIDE_PATH.is_file()


async def send_guide(bot: Bot, chat_id: int, caption: str, **kwargs) -> Message:
    """
    Отправляет PDF-гайд. Бросает исключение, если файла нет или отправка не
    удалась - вызывающая сторона сама решает, что делать (fallback).
    """
    global _file_id

    if _file_id:
        try:
            msg = await bot.send_document(chat_id, _file_id, caption=caption, **kwargs)
            return msg
        except TelegramBadRequest as e:
            # Кэшированный file_id мог перестать подходить - один раз пробуем с диска.
            logger.info("Кэшированный file_id гайда отвергнут (%s) - загружаю файл заново", e)
            _file_id = None

    document = FSInputFile(GUIDE_PATH, filename=GUIDE_FILENAME)
    msg = await bot.send_document(chat_id, document, caption=caption, **kwargs)
    file_id = getattr(getattr(msg, "document", None), "file_id", None)
    if file_id:
        _file_id = file_id
    return msg


async def send_help(bot: Bot, chat_id: int) -> None:
    """
    Ответ на /help: PDF-гайд с подписью-списком команд одним сообщением. При любом
    сбое (нет файла, ошибка Telegram) - прежний текстовый ответ, чтобы /help
    всегда что-то отвечал. Правило "одно сообщение на команду" сохраняется.
    """
    if guide_available():
        try:
            await replace_command_message(bot, chat_id, "help")
            msg = await send_guide(bot, chat_id, HELP_TEXT)
            track_command_message(bot, chat_id, "help", msg.message_id, GUIDE_TTL_SECONDS)
            return
        except Exception as e:  # noqa: BLE001 - гайд не критичен, отвечаем текстом
            logger.warning("Не удалось отправить гайд в /help (чат %s): %s - отвечаю текстом", chat_id, e)
    else:
        logger.warning("Файл гайда не найден: %s - /help отвечает текстом", GUIDE_PATH)

    await send_command_message(bot, chat_id, "help", HELP_TEXT)


async def send_registration_guide(bot: Bot, user_id: int) -> bool:
    """
    Отправляет гайд игроку один раз - при первом запуске, в начале регистрации.
    Возвращает True, если гайд отправлен этим вызовом. Никогда не бросает
    исключений: сбой гайда не должен мешать регистрации (флаг тогда
    возвращается, и гайд придёт при следующем /start до конца регистрации).
    """
    if not guide_available():
        logger.warning("Файл гайда не найден: %s - регистрация без гайда", GUIDE_PATH)
        return False

    # Право на отправку занимается ДО отправки: два быстрых /start подряд не дадут дубль.
    if not await db.claim_guide(user_id):
        return False
    try:
        await send_guide(bot, user_id, REGISTRATION_CAPTION)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("Не удалось отправить гайд при регистрации игроку %s: %s", user_id, e)
        try:
            await db.release_guide(user_id)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось вернуть право на отправку гайда игроку %s", user_id)
        return False
