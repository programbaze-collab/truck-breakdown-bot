import asyncio
from collections import defaultdict
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User


class PerUserLock(BaseMiddleware):
    """Обрабатывает апдейты одного пользователя строго по очереди.

    aiogram по умолчанию обрабатывает апдейты параллельно. Без блокировки альбом из 5 фото
    или двойное нажатие кнопки могут обработаться одновременно и, например, превысить лимит фото.
    """

    def __init__(self) -> None:
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        async with self._locks[user.id]:
            return await handler(event, data)
