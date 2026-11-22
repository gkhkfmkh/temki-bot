"""Block banned users and update activity timestamps."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from database.db import execute, fetchone
from utils.helpers import dt_to_db


class BanCheckMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is not None:
            stamp = dt_to_db()
            await execute(
                "UPDATE users SET last_active=?,last_seen=? WHERE user_id=?",
                (stamp, stamp, user.id),
            )
            row = await fetchone("SELECT banned FROM users WHERE user_id=?", (user.id,))
            if row and row["banned"]:
                if isinstance(event, CallbackQuery):
                    await event.answer("⛔ Доступ к боту ограничен.", show_alert=True)
                elif isinstance(event, Message):
                    await event.answer("⛔ Доступ к боту ограничен. Если это ошибка, свяжись с администрацией.")
                return None
        return await handler(event, data)
