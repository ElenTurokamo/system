"""
Общие мелкие утилиты бота.
"""
import asyncio
import logging

from aiogram import Bot

logger = logging.getLogger(__name__)

# 30 секунд - автоудаление служебных ephemeral-сообщений (справка /help,
# список друзей, запрос кода друга, файл со статистикой и т.п.), чтобы они не
# копились в чате, но пользователь успел их прочитать/скачать.
AUTO_DELETE_SECONDS = 30

# Активные задачи автоудаления по (chat_id, message_id) - нужны, чтобы уметь
# СБРАСЫВАТЬ обратный отсчёт (см. schedule_delete), когда пользователь
# продолжает взаимодействовать с сообщением (например, кликает по другу в
# /friendlist) - иначе оно может исчезнуть прямо у него из-под пальцев.
_delete_tasks: dict[tuple[int, int], asyncio.Task] = {}

# Текущее ephemeral-сообщение конкретной команды в конкретном чате:
# (chat_id, command_key) -> message_id. Обеспечивает правило "одно сообщение
# на команду одновременно" - если игрок вызывает ту же команду ещё раз, пока
# предыдущий ответ на неё ещё не пропал (не истёк таймер и не был убран
# вручную), старое сообщение удаляется перед показом нового, вместо того
# чтобы копиться рядом с ним. См. replace_command_message / send_command_message.
_command_messages: dict[tuple[int, str], int] = {}


def schedule_delete(bot: Bot, chat_id: int, message_id: int, delay: int = AUTO_DELETE_SECONDS) -> None:
    """
    Планирует автоудаление сообщения через `delay` секунд после того, как оно
    было получено пользователем, не блокируя вызывающий хендлер (фоновая
    asyncio-задача). Если сообщение к этому моменту уже удалено пользователем
    вручную - просто тихо ничего не делает.

    Повторный вызов для ТОГО ЖЕ (chat_id, message_id) отменяет предыдущий
    отсчёт и запускает новый с нуля - так продолжающееся взаимодействие с
    сообщением (например, открытие брифа друга) сбрасывает таймер удаления,
    а не соревнуется со старой задачей.

    Задача живёт только в памяти процесса: если бот перезапустится раньше, чем
    истекут AUTO_DELETE_SECONDS, конкретное сообщение останется в чате - это не критично,
    это чисто уборка чата, а не часть игровой логики.
    """
    key = (chat_id, message_id)

    existing = _delete_tasks.get(key)
    if existing and not existing.done():
        existing.cancel()

    async def _delayed_delete():
        try:
            await asyncio.sleep(delay)
            await bot.delete_message(chat_id, message_id)
        except asyncio.CancelledError:
            pass  # таймер сброшен новым взаимодействием - это ожидаемо
        except Exception as e:
            logger.info(
                "Не удалось автоудалить сообщение %s в чате %s: %s", message_id, chat_id, e
            )
        finally:
            if _delete_tasks.get(key) is asyncio.current_task():
                _delete_tasks.pop(key, None)

    _delete_tasks[key] = asyncio.create_task(_delayed_delete())


def cancel_scheduled_delete(chat_id: int, message_id: int) -> None:
    """Отменяет запланированное автоудаление совсем (без повторного запуска)."""
    existing = _delete_tasks.pop((chat_id, message_id), None)
    if existing and not existing.done():
        existing.cancel()


async def replace_command_message(bot: Bot, chat_id: int, command_key: str) -> None:
    """
    Убирает предыдущее ephemeral-сообщение той же команды (command_key) в этом
    чате, если оно ещё не пропало - см. _command_messages. Вызывается ПЕРЕД
    отправкой нового ответа на ту же команду, чтобы гарантировать, что одновременно
    существует не больше одного сообщения на команду.
    """
    prev_message_id = _command_messages.pop((chat_id, command_key), None)
    if prev_message_id is None:
        return
    cancel_scheduled_delete(chat_id, prev_message_id)
    try:
        await bot.delete_message(chat_id, prev_message_id)
    except Exception:
        pass  # уже удалено пользователем/раньше - не критично


def track_command_message(
    bot: Bot, chat_id: int, command_key: str, message_id: int, delay: int = AUTO_DELETE_SECONDS
) -> None:
    """Запоминает сообщение как текущее для (chat_id, command_key) и планирует его автоудаление."""
    _command_messages[(chat_id, command_key)] = message_id
    schedule_delete(bot, chat_id, message_id, delay)


async def send_command_message(
    bot: Bot, chat_id: int, command_key: str, text: str, delay: int = AUTO_DELETE_SECONDS, **kwargs
):
    """
    Основной способ ответить на команду текстом с соблюдением правила "одно
    сообщение на команду": убирает предыдущий ответ этой же команды (если он
    ещё жив), отправляет новый и планирует его автоудаление.
    """
    await replace_command_message(bot, chat_id, command_key)
    sent = await bot.send_message(chat_id, text, **kwargs)
    track_command_message(bot, chat_id, command_key, sent.message_id, delay)
    return sent
