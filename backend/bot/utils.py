"""
Общие мелкие утилиты бота.
"""
import asyncio
import logging

from aiogram import Bot

logger = logging.getLogger(__name__)

# 1 минута - автоудаление служебных сообщений (справка /help, файл со
# статистикой), чтобы они не копились в чате, но пользователь успел их
# прочитать/скачать.
AUTO_DELETE_SECONDS = 60

# Активные задачи автоудаления по (chat_id, message_id) - нужны, чтобы уметь
# СБРАСЫВАТЬ обратный отсчёт (см. schedule_delete), когда пользователь
# продолжает взаимодействовать с сообщением (например, кликает по другу в
# /friendlist) - иначе оно может исчезнуть прямо у него из-под пальцев.
_delete_tasks: dict[tuple[int, int], asyncio.Task] = {}


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
