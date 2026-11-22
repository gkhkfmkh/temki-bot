"""VIP access granting, invite links and delivery."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from aiogram import Bot

from config import settings
from database.db import connection, execute, fetchone, get_currency, get_setting, log_transaction
from utils.helpers import dt_to_db, parse_datetime, utcnow

logger = logging.getLogger(__name__)


async def paid_until(user_id: int):
    row = await fetchone("SELECT paid_until,vip_forever FROM users WHERE user_id=?", (user_id,))
    if not row:
        return None
    if row["vip_forever"]:
        return None
    return parse_datetime(row["paid_until"])


async def has_vip(user_id: int) -> bool:
    row = await fetchone("SELECT paid_until,vip_forever FROM users WHERE user_id=?", (user_id,))
    if not row:
        return False
    return bool(row["vip_forever"] or (parse_datetime(row["paid_until"]) and parse_datetime(row["paid_until"]) > utcnow()))


async def grant_vip(user_id: int, days: int, forever: bool = False) -> str | None:
    if forever or days >= 36500:
        await execute(
            "UPDATE users SET vip_forever=1,paid_until=NULL,vip_reminded_at=NULL,expired_notice_at=NULL,vip_removed_at=NULL WHERE user_id=?",
            (user_id,),
        )
        return None
    if days <= 0:
        raise ValueError("VIP duration must be positive")
    row = await fetchone("SELECT paid_until,vip_forever FROM users WHERE user_id=?", (user_id,))
    current = parse_datetime(row["paid_until"]) if row else None
    start = current if current and current > utcnow() else utcnow()
    expiry = start + timedelta(days=days)
    await execute(
        "UPDATE users SET paid_until=?,vip_forever=0,vip_reminded_at=NULL,expired_notice_at=NULL,vip_removed_at=NULL WHERE user_id=?",
        (dt_to_db(expiry), user_id),
    )
    return dt_to_db(expiry)


async def create_private_invite(bot: Bot, user_id: int) -> str | None:
    channel_id = (await get_setting("private_channel_id", settings.private_channel_id)).strip()
    if not channel_id:
        return None
    try:
        invite = await bot.create_chat_invite_link(
            chat_id=channel_id,
            member_limit=1,
            expire_date=utcnow() + timedelta(hours=24),
            name=f"PF user {user_id}",
        )
        return invite.invite_link
    except Exception:
        logger.exception("Could not create private invite for %s", user_id)
        return None


async def deliver_vip(bot: Bot, user_id: int, days: int, order_id: int | None = None) -> str | None:
    expiry = await grant_vip(user_id, days, forever=days >= 36500)
    invite_link = await create_private_invite(bot, user_id)
    if order_id is not None:
        await execute("UPDATE orders SET invite_link=? WHERE id=?", (invite_link, order_id))
    if days >= 36500:
        expiry_text = "Навсегда"
    else:
        expiry_text = parse_datetime(expiry).strftime("%d.%m.%Y %H:%M UTC") if expiry else "—"
    text = (
        "✅ <b>Доступ в ПРИВАТ активирован!</b>\n\n"
        f"Срок: <b>{'Навсегда' if days >= 36500 else f'{days} дн.'}</b>\n"
        f"Действует до: <b>{expiry_text}</b>"
    )
    if invite_link:
        text += f"\n\n🔐 Твоя персональная ссылка для входа:\n{invite_link}\n\n<i>Ссылка одноразовая и действует 24 часа.</i>"
    elif (await get_setting("private_channel_id", settings.private_channel_id)).strip():
        text += "\n\n⚠️ Не удалось автоматически создать ссылку. Напиши в поддержку, и администрация поможет с входом."
    await bot.send_message(user_id, text)
    return invite_link


async def get_notification_targets() -> tuple[int | str, ...]:
    targets: list[int | str] = list(settings.admin_ids)
    group_id = (await get_setting("support_chat_id", settings.support_chat_id)).strip()
    if group_id and group_id not in {str(item) for item in targets}:
        targets.append(group_id)
    return tuple(targets)


async def notify_admins(bot: Bot, text: str) -> None:
    for admin_id in await get_notification_targets():
        try:
            await bot.send_message(admin_id, text)
        except Exception:
            logger.exception("Could not notify admin %s", admin_id)


async def send_user(bot: Bot, user_id: int, text: str, **kwargs: Any) -> bool:
    try:
        await bot.send_message(user_id, text, **kwargs)
        return True
    except Exception:
        logger.exception("Could not send message to user %s", user_id)
        return False


async def apply_referral_cashback(
    bot: Bot,
    user_id: int,
    purchase_amount: float,
    percent: float,
    order_id: int | None = None,
) -> None:
    """Credit referral cashback once for an approved order.

    When order_id is supplied, the order row is the idempotency guard. This lets
    the background scheduler safely retry a cashback if the process stops after
    payment approval but before the notification is sent.
    """
    if order_id is not None:
        cashback = 0.0
        referrer_id: int | None = None
        async with connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            order = await (await db.execute(
                "SELECT user_id,amount,status,referral_cashback_paid FROM orders WHERE id=?",
                (order_id,),
            )).fetchone()
            if not order or order["status"] != "approved" or order["referral_cashback_paid"]:
                await db.rollback()
                return
            referred_user_id = int(order["user_id"])
            referral = await (await db.execute(
                "SELECT referrer_id FROM referrals WHERE referred_id=?",
                (referred_user_id,),
            )).fetchone()
            if referral and percent > 0 and float(order["amount"] or 0) > 0:
                referrer_id = int(referral["referrer_id"])
                cashback = round(float(order["amount"]) * percent / 100, 2)
                if cashback > 0:
                    await db.execute(
                        "UPDATE users SET balance=balance+? WHERE user_id=?",
                        (cashback, referrer_id),
                    )
                    await db.execute(
                        "INSERT INTO transactions_log(user_id,transaction_type,amount,currency,description,created_at) VALUES(?,?,?,?,?,?)",
                        (referrer_id, "referral_cashback", cashback, "RUB", f"Кэшбэк за покупку приглашённого пользователя {referred_user_id}", dt_to_db()),
                    )
            await db.execute(
                "UPDATE orders SET referral_cashback_paid=1 WHERE id=? AND referral_cashback_paid=0",
                (order_id,),
            )
            await db.commit()
        if referrer_id is not None and cashback > 0:
            currency = await get_currency()
            try:
                await bot.send_message(
                    referrer_id,
                    f"🎉 Тебе начислен реферальный кэшбэк: <b>{cashback:.2f} {currency}</b> за покупку приглашённого пользователя.",
                )
            except Exception:
                logger.exception("Could not send referral cashback notice to %s", referrer_id)
        return

    # Compatibility path for callers that do not have an order identifier.
    if percent <= 0 or purchase_amount <= 0:
        return
    row = await fetchone("SELECT referrer_id FROM referrals WHERE referred_id=?", (user_id,))
    if not row:
        return
    referrer_id = int(row["referrer_id"])
    cashback = round(purchase_amount * percent / 100, 2)
    if cashback <= 0:
        return
    await execute("UPDATE users SET balance=balance+? WHERE user_id=?", (cashback, referrer_id))
    await log_transaction(referrer_id, "referral_cashback", cashback, f"Кэшбэк за покупку приглашённого пользователя {user_id}")
    currency = await get_currency()
    try:
        await bot.send_message(
            referrer_id,
            f"🎉 Тебе начислен реферальный кэшбэк: <b>{cashback:.2f} {currency}</b> за покупку приглашённого пользователя.",
        )
    except Exception:
        logger.exception("Could not send referral cashback notice to %s", referrer_id)
