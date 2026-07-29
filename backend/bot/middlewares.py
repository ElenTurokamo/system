"""
Общие middleware бота.
"""
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message


class DeleteCommandsMiddleware(BaseMiddleware):
    """
    Автоматически удаляет сообщение пользователя, если это команда (/start, /profile и т.п.),
    сразу после того как её обработал соответствующий хендлер. Работает для ВСЕХ команд
    сразу, включая будущие - не нужно дублировать удаление в каждом хендлере отдельно.

    В группах может не сработать, если у бота нет прав на удаление чужих сообщений -
    это ожидаемо и просто тихо игнорируется.
    """

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any],
    ) -> Any:
        result = await handler(event, data)

        if event.text and event.text.startswith("/"):
            try:
                await event.delete()
            except Exception:
                pass

        return result
