"""Per-user update rate limiting."""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from config import is_admin


class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, interval: float = 0.65) -> None:
        self.interval = interval
        self.last_seen: dict[int, float] = defaultdict(float)

    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None:
            return await handler(event, data)
        now = time.monotonic()
        previous = self.last_seen[user.id]
        if now - previous < self.interval:
            if isinstance(event, CallbackQuery):
                await event.answer("Не так быстро 🙂", show_alert=False)
            elif isinstance(event, Message):
                await event.answer("Не так быстро 🙂 Попробуй через секунду.")
            return None
        self.last_seen[user.id] = now
        if len(self.last_seen) > 20000:
            cutoff = now - 600
            self.last_seen = defaultdict(float, {uid: ts for uid, ts in self.last_seen.items() if ts > cutoff})
        return await handler(event, data)
