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


def schedule_delete(bot: Bot, chat_id: int, message_id: int, delay: int = AUTO_DELETE_SECONDS) -> None:
    """
    Планирует автоудаление сообщения через `delay` секунд после того, как оно
    было получено пользователем, не блокируя вызывающий хендлер (фоновая
    asyncio-задача). Если сообщение к этому моменту уже удалено пользователем
    вручную - просто тихо ничего не делает.

    Задача живёт только в памяти процесса: если бот перезапустится раньше, чем
    истекут AUTO_DELETE_SECONDS, конкретное сообщение останется в чате - это не критично,
    это чисто уборка чата, а не часть игровой логики.
    """

    async def _delayed_delete():
        await asyncio.sleep(delay)
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception as e:
            logger.info(
                "Не удалось автоудалить сообщение %s в чате %s: %s", message_id, chat_id, e
            )

    asyncio.create_task(_delayed_delete())
