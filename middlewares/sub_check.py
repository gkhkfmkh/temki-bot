"""Optional mandatory subscription middleware."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import is_admin, settings
from database.db import get_setting

logger = logging.getLogger(__name__)


async def is_subscribed(bot: Any, user_id: int, channel_id: str) -> bool:
    try:
        member = await bot.get_chat_member(channel_id, user_id)
        status = getattr(member.status, "value", member.status)
        return status in {"creator", "administrator", "member"} or (
            status == "restricted" and bool(getattr(member, "is_member", False))
        )
    except Exception:
        logger.exception("Subscription check failed for user %s", user_id)
        return False


class SubscriptionMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Any, dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: dict[str, Any],
    ) -> Any:
        user = getattr(event, "from_user", None)
        if user is None or is_admin(user.id):
            return await handler(event, data)
        if isinstance(event, Message) and (event.text or "").startswith("/start"):
            return await handler(event, data)
        if await get_setting("maintenance", "0") == "1":
            notice = "🛠 <b>Бот временно находится на техническом обслуживании.</b>\n\nПопробуй немного позже."
            if isinstance(event, CallbackQuery):
                await event.answer("Бот на техническом обслуживании.", show_alert=True)
                if event.message:
                    await event.message.answer(notice)
            elif isinstance(event, Message):
                await event.answer(notice)
            return None

        if isinstance(event, CallbackQuery) and event.data == "sub:check":
            return await handler(event, data)

        required = (await get_setting("require_subscription", "1" if settings.subscription_required else "0")) == "1"
        channel_id = (await get_setting("main_channel_id", settings.main_channel_id)).strip()
        if not required or not channel_id:
            return await handler(event, data)
        if await is_subscribed(data["bot"], user.id, channel_id):
            return await handler(event, data)

        channel_url = (await get_setting("main_channel_url", settings.main_channel_url)).strip()
        if not channel_url and channel_id.startswith("@"):
            channel_url = f"https://t.me/{channel_id[1:]}"
        rows = []
        if channel_url:
            rows.append([InlineKeyboardButton(text="📢 Подписаться", url=channel_url)])
        rows.append([InlineKeyboardButton(text="✅ Проверить подписку", callback_data="sub:check")])
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
        notice = "🔒 <b>Для продолжения подпишись на наш канал.</b>\n\nПосле подписки нажми «Проверить подписку»."
        if isinstance(event, CallbackQuery):
            await event.answer("Сначала подпишись на канал.", show_alert=True)
            if event.message:
                await event.message.answer(notice, reply_markup=markup)
        elif isinstance(event, Message):
            await event.answer(notice, reply_markup=markup)
        return None
