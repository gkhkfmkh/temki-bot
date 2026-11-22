"""Async SQLite access and idempotent database initialization."""

from __future__ import annotations

import json
import logging
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

import aiosqlite

from config import settings
from database.models import SCHEMA

logger = logging.getLogger(__name__)


def database_file_path() -> str:
    configured = settings.database_path.strip() or "playfunpay.db"
    if configured == ":memory:":
        raise RuntimeError("DATABASE_PATH=:memory: is not supported because this bot opens short-lived SQLite connections. Use a persistent file path.")
    path = Path(configured).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@asynccontextmanager
async def connection() -> AsyncIterator[aiosqlite.Connection]:
    db = await aiosqlite.connect(database_file_path(), timeout=30)
    db.row_factory = aiosqlite.Row
    try:
        await db.execute("PRAGMA foreign_keys=ON")
        await db.execute("PRAGMA busy_timeout=30000")
        yield db
    finally:
        await db.close()


async def execute(query: str, params: tuple[Any, ...] = ()) -> int:
    async with connection() as db:
        cursor = await db.execute(query, params)
        await db.commit()
        return int(cursor.lastrowid or 0)


async def fetchone(query: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
    async with connection() as db:
        cursor = await db.execute(query, params)
        return await cursor.fetchone()


async def fetchall(query: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
    async with connection() as db:
        cursor = await db.execute(query, params)
        return await cursor.fetchall()


async def get_setting(key: str, default: str = "") -> str:
    row = await fetchone("SELECT value FROM bot_settings WHERE key=?", (key,))
    if row and str(row["value"]).strip():
        return str(row["value"])
    legacy = await fetchone("SELECT value FROM settings WHERE key=?", (key,))
    if legacy and str(legacy["value"]).strip():
        return str(legacy["value"])
    return default


async def set_setting(key: str, value: str) -> None:
    now = utc_now()
    async with connection() as db:
        await db.execute(
            "INSERT INTO bot_settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
            (key, value, now),
        )
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def get_button_title(key: str, default: str = "") -> str:
    row = await fetchone("SELECT title FROM buttons WHERE button_id=?", (key,))
    return str(row["title"]) if row else default


async def get_currency() -> str:
    return (await get_setting("currency", settings.currency)).strip() or settings.currency


async def get_text(key: str, default: str = "") -> str:
    row = await fetchone("SELECT body FROM texts WHERE text_key=?", (key,))
    return str(row["body"]) if row else default


async def set_text(key: str, body: str) -> None:
    await execute(
        "INSERT INTO texts(text_key,body) VALUES(?,?) "
        "ON CONFLICT(text_key) DO UPDATE SET body=excluded.body",
        (key, body),
    )


async def init_database() -> None:
    db_path = Path(database_file_path())
    backup_path = db_path.with_suffix(db_path.suffix + ".pre-v2.bak")
    async with connection() as db:
        # Checkpoint any old WAL before creating the one-time pre-migration copy.
        try:
            await db.execute("PRAGMA wal_checkpoint(FULL)")
        except Exception:
            logger.debug("No WAL checkpoint was needed", exc_info=True)
        if db_path.exists() and db_path.stat().st_size > 0 and not backup_path.exists():
            try:
                shutil.copy2(db_path, backup_path)
                logger.info("Created one-time pre-upgrade database backup: %s", backup_path)
            except OSError:
                logger.exception("Could not create the one-time database backup; continuing with the existing database")
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        await db.executescript(SCHEMA)

        # Upgrade the original PlayFunpay database without dropping user data.
        columns = {row["name"] for row in await (await db.execute("PRAGMA table_info(users)")).fetchall()}
        additions = {
            "balance": "REAL NOT NULL DEFAULT 0",
            "points": "INTEGER NOT NULL DEFAULT 0",
            "total_purchases": "INTEGER NOT NULL DEFAULT 0",
            "daily_streak": "INTEGER NOT NULL DEFAULT 0",
            "last_daily_at": "TEXT",
            "vip_forever": "INTEGER NOT NULL DEFAULT 0",
            "last_active": "TEXT",
            "vip_reminded_at": "TEXT",
            "expired_notice_at": "TEXT",
            "vip_removed_at": "TEXT",
            "discount_percent": "REAL NOT NULL DEFAULT 0",
            "discount_expires_at": "TEXT",
            "discount_reserved_order_id": "INTEGER",
        }
        for name, definition in additions.items():
            if name not in columns:
                await db.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
        await db.execute("UPDATE users SET last_active=COALESCE(last_active,last_seen)")
        order_columns = {row["name"] for row in await (await db.execute("PRAGMA table_info(orders)")).fetchall()}
        if "referral_cashback_paid" not in order_columns:
            await db.execute("ALTER TABLE orders ADD COLUMN referral_cashback_paid INTEGER NOT NULL DEFAULT 0")
            # Do not retroactively pay referral cashback for historical purchases.
            await db.execute("UPDATE orders SET referral_cashback_paid=1")
        quest_columns = {row["name"] for row in await (await db.execute("PRAGMA table_info(quest_completions)")).fetchall()}
        for column, definition in {"proof_text": "TEXT", "proof_file_id": "TEXT", "proof_file_type": "TEXT"}.items():
            if column not in quest_columns:
                await db.execute(f"ALTER TABLE quest_completions ADD COLUMN {column} {definition}")
        giveaway_columns = {row["name"] for row in await (await db.execute("PRAGMA table_info(giveaways)")).fetchall()}
        if "min_referrals" not in giveaway_columns:
            await db.execute("ALTER TABLE giveaways ADD COLUMN min_referrals INTEGER NOT NULL DEFAULT 0")
        if "min_balance" not in giveaway_columns:
            await db.execute("ALTER TABLE giveaways ADD COLUMN min_balance REAL NOT NULL DEFAULT 0")

        # Keep old tariffs and expose them through the new catalog table.
        await db.execute(
            "INSERT OR IGNORE INTO products_tariffs(legacy_tariff_id,title,price,days,active) "
            "SELECT id,title,price,days,active FROM tariffs"
        )
        # Convert legacy day-only promo codes into the generalized reward model.
        await db.execute(
            "INSERT OR IGNORE INTO promo_codes(code,reward_type,value,max_uses,uses_count,active,created_at) "
            "SELECT p.code,'vip_days',p.days,CASE WHEN p.uses_left=0 THEN (SELECT COUNT(*) FROM promo_uses pu WHERE pu.code=p.code) "
            "ELSE p.uses_left+(SELECT COUNT(*) FROM promo_uses pu WHERE pu.code=p.code) END,"
            "(SELECT COUNT(*) FROM promo_uses pu WHERE pu.code=p.code),CASE WHEN p.uses_left=0 THEN 0 ELSE p.active END,p.created_at FROM promos p"
        )
        # Preserve already redeemed legacy codes so users cannot redeem them twice.
        await db.execute(
            "INSERT OR IGNORE INTO promo_activations(code,user_id,reward_type,value,activated_at) "
            "SELECT pu.code,pu.user_id,'vip_days',p.days,pu.used_at FROM promo_uses pu JOIN promos p ON p.code=pu.code"
        )
        await db.execute(
            "UPDATE promo_codes SET uses_count=(SELECT COUNT(*) FROM promo_activations pa WHERE pa.code=promo_codes.code)"
        )
        # Map legacy setting keys to the expanded configuration names.
        await db.execute("INSERT OR IGNORE INTO settings(key,value) SELECT 'main_channel_url',value FROM settings WHERE key='channel_url'")
        await db.execute("INSERT OR IGNORE INTO settings(key,value) SELECT 'main_channel_id',value FROM settings WHERE key='channel_chat_id'")
        # Copy legacy settings into the new settings table without overriding existing values.
        await db.execute("INSERT OR IGNORE INTO bot_settings(key,value) SELECT key,value FROM settings")
        # Migrate old manual payment requests while retaining their IDs and receipt file IDs.
        await db.execute(
            "INSERT OR IGNORE INTO orders(id,user_id,tariff_id,tariff_title,amount,original_amount,days,payment_method,status,receipt_file_id,receipt_type,referral_cashback_paid,created_at,reviewed_at,reviewed_by,admin_comment) "
            "SELECT id,user_id,tariff_id,tariff_title,amount,amount,days,'manual',"
            "CASE WHEN status='pending' THEN 'pending_review' WHEN status='awaiting_receipt' THEN 'awaiting_receipt' "
            "WHEN status='approved' THEN 'approved' WHEN status='rejected' THEN 'rejected' ELSE status END, "
            "receipt_file_id,receipt_type,1,created_at,reviewed_at,reviewed_by,admin_comment FROM payment_requests"
        )
        await db.execute(
            "UPDATE users SET total_purchases=MAX(total_purchases,(SELECT COUNT(*) FROM payment_requests pr WHERE pr.user_id=users.user_id AND pr.status='approved'))"
        )
        # Migrate old support messages into the unified ticket inbox.
        await db.execute(
            "INSERT OR IGNORE INTO tickets_suggestions(id,user_id,category,message,file_id,file_type,status,created_at,answered_at) "
            "SELECT id,user_id,'support',message,file_id,file_type,status,created_at,answered_at FROM support_messages"
        )
        # Restore legacy giveaway winners as well as their participants.
        await db.execute(
            "INSERT OR IGNORE INTO giveaway_winners(giveaway_id,user_id,place,selected_at) "
            "SELECT raffle_id,user_id,place,selected_at FROM raffle_winners"
        )
        await db.execute(
            "INSERT OR IGNORE INTO giveaway_winner_history(giveaway_id,user_id,place,selected_at) "
            "SELECT giveaway_id,user_id,place,selected_at FROM giveaway_winners"
        )
        # Restore referral relationships from the old users table.
        await db.execute(
            "INSERT OR IGNORE INTO referrals(referrer_id,referred_id,rewarded,created_at) "
            "SELECT referrer,user_id,0,joined_at FROM users WHERE referrer IS NOT NULL AND referrer<>user_id"
        )
        # Migrate old raffle records into the expanded giveaway engine once.
        old_giveaways = await (await db.execute("SELECT COUNT(*) AS n FROM giveaways")).fetchone()
        if old_giveaways["n"] == 0:
            legacy = await (await db.execute("SELECT * FROM raffles")).fetchall()
            for item in legacy:
                sponsor_list = json.dumps([item["channel_id"]] if item["channel_id"] else [])
                await db.execute(
                    "INSERT INTO giveaways(id,title,prize,description,sponsor_channels,ends_at,winners_count,status,created_by,created_at,finished_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (item["id"], item["title"], item["prize"], item["description"], sponsor_list,
                     item["ends_at"], item["winners_count"], "completed" if item["status"] == "finished" else item["status"], item["created_by"],
                     item["created_at"], item["finished_at"]),
                )
                participants = await (await db.execute(
                    "SELECT user_id,joined_at FROM raffle_participants WHERE raffle_id=?", (item["id"],)
                )).fetchall()
                for participant in participants:
                    await db.execute(
                        "INSERT OR IGNORE INTO giveaway_participants(giveaway_id,user_id,joined_at) VALUES(?,?,?)",
                        (item["id"], participant["user_id"], participant["joined_at"]),
                    )
        await db.execute("UPDATE giveaways SET status='completed' WHERE status='finished'")
        default_buttons = {
            "channel": "📢 Наш канал", "ads": "💎 Реклама и сотрудничество",
            "about": "🛒 Что в ПРИВАТЕ?", "buy": "🔐 Купить доступ",
            "free": "📚 Бесплатные материалы", "daily": "⚡ Бонусы и задания",
            "games": "🎮 Игры и гайды", "referral": "👥 Реферальная система",
            "promo": "🎟 Ввести промокод", "support": "🤖💬 Поддержка", "payments": "💳 Способы оплаты",
            "raffle": "🎁 Розыгрыши", "back": "⬅️ Назад в меню",
            "profile": "👤 Личный кабинет", "faq": "❓ FAQ и отзывы",
            "balance": "💰 Баланс", "leaderboard": "🏆 Топ рефоводов",
            "daily_bonus": "⚡ Забрать ежедневный бонус", "tasks": "🎯 Задания",
            "roblox": "🤖 Roblox", "standoff": "⚡ Standoff 2",
            "brawl": "🌵 Brawl Stars", "pubg": "🔫 PUBG Mobile",
            "clash": "🏰 Clash of Clans", "safety": "📜 Безопасность",
        }
        default_texts = {
            "welcome": "<b>PlayFunpay Team | Официальный бот ⚡</b>\n\nПривет, {first_name}! Здесь собраны материалы проекта, закрытый раздел, бонусы и полезные возможности.\n\nТвой статус: <b>{status}</b>\nБаланс: <b>{balance}</b>\nFP-коины: <b>{points}</b>",
            "about": "<b>🛒 Что входит в ПРИВАТ</b>\n\nЗакрытые материалы, схемы, инструкции, софт и боты, а также чат участников. Листай разделы кнопками ниже.",
            "faq": "<b>❓ Частые вопросы</b>\n\nОплата проверяется администрацией. Доступ выдаётся после подтверждения. Если возник вопрос, напиши в поддержку.\n\n<i>Отзывы и подробности можно добавить через админ-панель.</i>",
            "private_page_0": "<b>🔐 О ПРИВАТЕ</b>\n\nЗакрытое пространство PlayFunpay Team с дополнительными материалами и поддержкой участников.",
            "private_page_1": "<b>📚 Схемы и материалы</b>\n\nПодборки инструкций, схем и полезных материалов проекта.",
            "private_page_2": "<b>🤖 Софт и боты</b>\n\nИнструменты, боты и разборы их возможностей.",
            "private_page_3": "<b>💬 Чат участников</b>\n\nОбщение с участниками и возможность задать вопросы по материалам.",
            "private_page_4": "<b>⭐ Отзывы</b>\n\nОтзывы и результаты участников будут публиковаться в этом разделе.",
            "support_prompt": "<b>🤖💬 Поддержка</b>\n\nОпиши вопрос одним сообщением. Можно приложить фото или скриншот. Администратор получит обращение.",
            "paid_locked": "🔒 <b>Доступ закрыт</b>\n\nЧтобы открыть закрытый раздел, выбери тариф и оплати доступ.",
            "paid_open": "🔐 <b>ПРИВАТ АКТИВЕН</b>\n\nТвой доступ активен до: <b>{paid_until}</b>\n\nДобро пожаловать в закрытый раздел!",
            "free": "🆓 <b>Бесплатное меню</b>\n\nЗдесь находятся открытые материалы и полезная информация.",
            "daily_empty": "💰 <b>Способы заработка</b>\n\nНовые публикации появятся здесь позже. Заглядывай!",
            "games": "🎮 <b>Игры и гайды</b>\n\nВыбери интересующий раздел ниже.",
            "safety": "📜 <b>Безопасность</b>\n\nПроверяй ссылки и адресата перед оплатой. Не передавай пароли, коды подтверждения и данные банковской карты.",
            "payment_intro": "💳 <b>Оплата доступа</b>\n\nВыбери подходящий тариф ниже. После оплаты отправь чек в этот чат.",
            "promo_prompt": "🎟 <b>Активация промокода</b>\n\nОтправь промокод ответным сообщением.",
            "support_received": "✅ Обращение отправлено. Администратор рассмотрит его и ответит при первой возможности.",
            "referral": "👥 <b>Реферальная система</b>\n\nТвоя ссылка:\n<code>{ref_link}</code>\n\nПриглашено пользователей: <b>{count}</b>",
            "payment_instructions": "🔐 <b>Оплата доступа</b>\n\nТариф: <b>{tariff}</b>\nСтоимость: <b>{price} {currency}</b>\nСрок: <b>{days} дн.</b>\n\n{details}\n\nПосле оплаты отправь сюда скриншот или фото чека.",
        }
        default_settings = {
            "main_channel_id": settings.main_channel_id, "main_channel_url": settings.main_channel_url, "private_channel_id": settings.private_channel_id,
            "support_chat_id": settings.support_chat_id, "ads_url": "", "payment_details": "Перевод на карту / СБП.\nРеквизиты уточняй у администрации.",
            "require_subscription": "1" if settings.subscription_required else "0",
            "currency": settings.currency, "maintenance": "0",
        }
        legacy_about = await (await db.execute("SELECT body FROM texts WHERE text_key='about'")).fetchone()
        if legacy_about:
            default_texts["private_page_0"] = str(legacy_about["body"])
        for key, value in default_buttons.items():
            await db.execute("INSERT OR IGNORE INTO buttons(button_id,title) VALUES(?,?)", (key, value))
        await db.execute("UPDATE buttons SET title='⚡ Бонусы и задания' WHERE button_id='daily' AND title='💰 Способы заработка'")
        for key, value in default_texts.items():
            await db.execute("INSERT OR IGNORE INTO texts(text_key,body) VALUES(?,?)", (key, value))
        for key, value in default_settings.items():
            await db.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, value))
            saved = await (await db.execute("SELECT value FROM settings WHERE key=?", (key,))).fetchone()
            await db.execute("INSERT OR IGNORE INTO bot_settings(key,value) VALUES(?,?)", (key, saved["value"] if saved else value))
        tariff_count = await (await db.execute("SELECT COUNT(*) AS n FROM tariffs")).fetchone()
        if tariff_count["n"] == 0:
            defaults = [("7 дней", 49.0, 7, 1), ("30 дней", 149.0, 30, 1), ("90 дней", 349.0, 90, 1), ("Навсегда", 999.0, 36500, 1)]
            await db.executemany("INSERT INTO tariffs(title,price,days,active) VALUES(?,?,?,?)", defaults)
        await db.execute(
            "INSERT OR IGNORE INTO products_tariffs(legacy_tariff_id,title,price,days,active) "
            "SELECT id,title,price,days,active FROM tariffs"
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_cashback_pending ON orders(status,referral_cashback_paid)")
        await db.execute("PRAGMA user_version=2")
        await db.commit()
        check = await (await db.execute("PRAGMA quick_check")).fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise RuntimeError(f"SQLite integrity check failed: {check[0] if check else 'no result'}")
        users_table = await (await db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'")).fetchone()
        if not users_table:
            raise RuntimeError("Database initialization finished without the required users table")
    logger.info("Database initialized and legacy schema upgraded: %s", db_path)


async def log_transaction(user_id: int, kind: str, amount: float, description: str, currency: str = "RUB") -> None:
    await execute(
        "INSERT INTO transactions_log(user_id,transaction_type,amount,currency,description,created_at) VALUES(?,?,?,?,?,?)",
        (user_id, kind, amount, currency, description[:500], utc_now()),
    )


async def audit(admin_id: int, action: str, details: str = "") -> None:
    await execute(
        "INSERT INTO audit_log(admin_id,action,details,created_at) VALUES(?,?,?,?)",
        (admin_id, action, details[:1000], utc_now()),
    )
