"""Referral registration and one-time rewards."""

from __future__ import annotations

from aiogram.types import User

from config import settings
from database.db import connection, execute, fetchone, log_transaction
from utils.helpers import dt_to_db


async def register_user(user: User, referrer_id: int | None = None) -> bool:
    """Create or refresh a user record without duplicate-registration races."""
    now = dt_to_db()
    if referrer_id == user.id:
        referrer_id = None
    async with connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            "INSERT OR IGNORE INTO users(user_id,username,first_name,referrer,joined_at,last_seen,last_active) VALUES(?,?,?,?,?,?,?)",
            (user.id, user.username or "", user.first_name or "", None, now, now, now),
        )
        is_new = cursor.rowcount == 1
        if is_new and referrer_id is not None:
            valid = await (await db.execute("SELECT user_id FROM users WHERE user_id=?", (referrer_id,))).fetchone()
            if valid:
                await db.execute("UPDATE users SET referrer=? WHERE user_id=?", (referrer_id, user.id))
                await db.execute(
                    "INSERT OR IGNORE INTO referrals(referrer_id,referred_id,rewarded,created_at) VALUES(?,?,0,?)",
                    (referrer_id, user.id, now),
                )
        elif not is_new:
            await db.execute(
                "UPDATE users SET username=?,first_name=?,last_seen=?,last_active=? WHERE user_id=?",
                (user.username or "", user.first_name or "", now, now, user.id),
            )
        await db.commit()
    return is_new


async def reward_referrer(referred_id: int) -> bool:
    if settings.referral_bonus_points <= 0:
        return False
    async with connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT referrer_id,rewarded FROM referrals WHERE referred_id=?", (referred_id,)
        )).fetchone()
        if not row or row["rewarded"]:
            await db.rollback()
            return False
        referrer_id = int(row["referrer_id"])
        await db.execute("UPDATE users SET points=points+? WHERE user_id=?", (settings.referral_bonus_points, referrer_id))
        await db.execute(
            "UPDATE referrals SET rewarded=1,rewarded_at=? WHERE referred_id=? AND rewarded=0",
            (dt_to_db(), referred_id),
        )
        await db.execute(
            "INSERT INTO transactions_log(user_id,transaction_type,amount,currency,description,created_at) VALUES(?,?,?,?,?,?)",
            (referrer_id, "referral_reward", settings.referral_bonus_points, "FP", f"Награда за приглашение пользователя {referred_id}", dt_to_db()),
        )
        await db.commit()
    return True
