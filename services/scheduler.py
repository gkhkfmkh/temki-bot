"""Periodic maintenance jobs for VIP access, giveaways and payment checks."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from aiogram import Bot

from config import settings
from database.db import execute, fetchall, get_setting
from services.access import apply_referral_cashback
from handlers.giveaways import process_due_giveaways
from handlers.private_shop import check_crypto_orders
from utils.helpers import dt_to_db, format_datetime, parse_datetime, utcnow

logger = logging.getLogger(__name__)


async def process_vip_expirations(bot: Bot) -> None:
    now = utcnow()
    reminder_limit = dt_to_db(now + timedelta(hours=24))
    reminders = await fetchall(
        "SELECT user_id,paid_until FROM users WHERE vip_forever=0 AND paid_until>? AND paid_until<=? AND vip_reminded_at IS NULL",
        (dt_to_db(now), reminder_limit),
    )
    for user in reminders:
        try:
            await bot.send_message(
                user["user_id"],
                f"⏰ <b>Напоминание: доступ в ПРИВАТ скоро закончится.</b>\n\nТекущий срок истекает: <b>{format_datetime(user['paid_until'], True)}</b>. Продли доступ заранее, чтобы не потерять доступ к закрытому разделу.",
            )
            await execute("UPDATE users SET vip_reminded_at=? WHERE user_id=? AND vip_reminded_at IS NULL", (dt_to_db(), user["user_id"]))
        except Exception:
            logger.exception("Could not send VIP reminder to %s", user["user_id"])

    expired = await fetchall(
        "SELECT user_id,paid_until FROM users WHERE vip_forever=0 AND paid_until IS NOT NULL AND paid_until<=? AND vip_removed_at IS NULL",
        (dt_to_db(now),),
    )
    private_channel = (await get_setting("private_channel_id", settings.private_channel_id)).strip()
    for user in expired:
        removed = not bool(private_channel)
        if private_channel:
            try:
                await bot.ban_chat_member(private_channel, int(user["user_id"]))
                await bot.unban_chat_member(private_channel, int(user["user_id"]), only_if_banned=True)
                removed = True
            except Exception:
                logger.exception("Could not remove expired VIP user %s from private channel", user["user_id"])
        if removed:
            await execute("UPDATE users SET vip_removed_at=?,expired_notice_at=COALESCE(expired_notice_at,?) WHERE user_id=?", (dt_to_db(), dt_to_db(), user["user_id"]))
            try:
                await bot.send_message(
                    user["user_id"],
                    "🔒 <b>Срок доступа в ПРИВАТ завершился.</b>\n\nЧтобы вернуться в закрытый раздел, выбери новый тариф в главном меню.",
                )
            except Exception:
                logger.exception("Could not notify expired VIP user %s", user["user_id"])


async def expire_unfinished_orders(bot: Bot) -> None:
    cutoff = dt_to_db(utcnow() - timedelta(hours=24))
    expired = await fetchall(
        "SELECT id,user_id FROM orders WHERE status IN ('created','awaiting_receipt','pending_crypto') AND created_at<=? LIMIT 100",
        (cutoff,),
    )
    for order in expired:
        await execute("UPDATE orders SET status='expired' WHERE id=? AND status IN ('created','awaiting_receipt','pending_crypto')", (order["id"],))
        await execute("UPDATE users SET discount_reserved_order_id=NULL WHERE user_id=? AND discount_reserved_order_id=?", (order["user_id"], order["id"]))
        try:
            await bot.send_message(order["user_id"], f"⌛ Срок оформления заказа #{order['id']} истёк. Если хочешь продолжить покупку, выбери тариф заново.")
        except Exception:
            logger.debug("Could not notify user about expired order %s", order["id"], exc_info=True)


async def process_pending_referral_cashbacks(bot: Bot) -> None:
    """Retry cashback delivery for approved orders if a previous run was interrupted."""
    rows = await fetchall(
        "SELECT id,user_id,amount FROM orders WHERE status='approved' AND referral_cashback_paid=0 ORDER BY id LIMIT 100"
    )
    for row in rows:
        try:
            await apply_referral_cashback(
                bot,
                int(row["user_id"]),
                float(row["amount"]),
                settings.referral_cashback_percent,
                order_id=int(row["id"]),
            )
        except Exception:
            logger.exception("Could not process referral cashback for order %s", row["id"])


async def scheduler_loop(bot: Bot) -> None:
    while True:
        try:
            await process_vip_expirations(bot)
            await process_due_giveaways(bot)
            await check_crypto_orders(bot)
            await expire_unfinished_orders(bot)
            await process_pending_referral_cashbacks(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Background scheduler iteration failed")
        await asyncio.sleep(300)
