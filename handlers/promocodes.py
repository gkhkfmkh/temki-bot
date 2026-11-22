"""General-purpose promo code redemption."""

from __future__ import annotations

from datetime import timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from database.db import connection, get_currency, get_setting, get_text
from services.access import create_private_invite
from keyboards.inline import back_keyboard
from states.user_states import PromoStates
from utils.helpers import dt_to_db, format_datetime, parse_datetime, utcnow

router = Router(name="promocodes")


async def redeem_promo_code(bot, user_id: int, raw_code: str) -> str:
    code = raw_code.strip().upper()
    if not code:
        return "❌ Введи промокод без пробелов."
    currency_symbol = await get_currency()
    async with connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        promo = await (await db.execute("SELECT * FROM promo_codes WHERE code=? AND active=1", (code,))).fetchone()
        if not promo:
            await db.rollback()
            return "❌ Промокод не найден или отключён. Проверь написание."
        if promo["max_uses"] > 0 and promo["uses_count"] >= promo["max_uses"]:
            await db.rollback()
            return "❌ Лимит активаций этого промокода исчерпан."
        expiry = parse_datetime(promo["expires_at"])
        if expiry and expiry <= utcnow():
            await db.rollback()
            return "❌ Срок действия промокода закончился."
        used = await (await db.execute(
            "SELECT 1 FROM promo_activations WHERE code=? AND user_id=?", (code, user_id)
        )).fetchone()
        if used:
            await db.rollback()
            return "❌ Ты уже активировал этот промокод."
        user = await (await db.execute("SELECT * FROM users WHERE user_id=?", (user_id,))).fetchone()
        if not user:
            await db.rollback()
            return "Сначала отправь команду /start, чтобы создать профиль."
        reward_type = promo["reward_type"]
        value = float(promo["value"])
        result_text = ""
        if reward_type == "balance":
            await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (value, user_id))
            result_text = f"💰 На баланс начислено <b>{value:g} {currency_symbol}</b>."
            currency = "RUB"
        elif reward_type == "points":
            points = int(value)
            await db.execute("UPDATE users SET points=points+? WHERE user_id=?", (points, user_id))
            result_text = f"🪙 Начислено <b>{points} FP-коинов</b>."
            currency = "FP"
        elif reward_type == "vip_days":
            days = int(value)
            if user["vip_forever"] and days < 36500:
                await db.rollback()
                return "🔐 У тебя уже активирован бессрочный доступ. Этот промокод не был использован."
            if days >= 36500:
                await db.execute("UPDATE users SET vip_forever=1,paid_until=NULL,vip_reminded_at=NULL,expired_notice_at=NULL,vip_removed_at=NULL WHERE user_id=?", (user_id,))
                result_text = "🔐 Доступ в ПРИВАТ активирован <b>навсегда</b>."
            else:
                current = parse_datetime(user["paid_until"])
                start = current if current and current > utcnow() else utcnow()
                new_expiry = start + timedelta(days=days)
                await db.execute("UPDATE users SET paid_until=?,vip_forever=0,vip_reminded_at=NULL,expired_notice_at=NULL,vip_removed_at=NULL WHERE user_id=?", (dt_to_db(new_expiry), user_id))
                result_text = f"🔐 Доступ продлён на <b>{days} дн.</b>\nДействует до: <b>{format_datetime(new_expiry, True)}</b>."
            currency = "DAY"
        elif reward_type == "discount":
            if not 1 <= value <= 100:
                await db.rollback()
                return "❌ У промокода некорректный размер скидки. Обратись к администрации."
            if user["discount_reserved_order_id"]:
                await db.rollback()
                return "❌ Сначала заверши или отмени текущий заказ, для которого уже зарезервирована скидка."
            discount_expiry = utcnow() + timedelta(days=30)
            await db.execute(
                "UPDATE users SET discount_percent=?,discount_expires_at=?,discount_reserved_order_id=NULL WHERE user_id=?",
                (value, dt_to_db(discount_expiry), user_id),
            )
            result_text = f"🏷 На следующую покупку начислена персональная скидка <b>{value:g}%</b> (действует 30 дней)."
            currency = "%"
        else:
            await db.rollback()
            return "❌ Неизвестный тип награды промокода."
        await db.execute(
            "INSERT INTO promo_activations(code,user_id,reward_type,value,activated_at) VALUES(?,?,?,?,?)",
            (code, user_id, reward_type, value, dt_to_db()),
        )
        await db.execute("UPDATE promo_codes SET uses_count=uses_count+1 WHERE code=?", (code,))
        await db.execute(
            "INSERT INTO transactions_log(user_id,transaction_type,amount,currency,description,created_at) VALUES(?,?,?,?,?,?)",
            (user_id, "promo_" + reward_type, value, currency, f"Активация промокода {code}", dt_to_db()),
        )
        await db.commit()
    if reward_type == "vip_days":
        invite_link = await create_private_invite(bot, user_id)
        if invite_link:
            result_text += f"\n\n🔐 Ссылка для входа в ПРИВАТ (одноразовая, 24 часа):\n{invite_link}"
        elif (await get_setting("private_channel_id", "")).strip():
            result_text += "\n\n⚠️ Не удалось создать ссылку автоматически. Напиши в поддержку."
    return f"✅ <b>Промокод {code} активирован!</b>\n\n{result_text}"


@router.callback_query(F.data == "menu:promo")
async def prompt_promo(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoStates.waiting_code)
    prompt = await get_text("promo_prompt", "<b>🎟 Активация промокода</b>\n\nОтправь промокод одним сообщением.")
    await call.message.answer(prompt + "\n\nДля отмены используй /cancel.", reply_markup=back_keyboard())
    await call.answer()


@router.message(PromoStates.waiting_code, F.text)
async def process_promo(message: Message, state: FSMContext) -> None:
    result = await redeem_promo_code(message.bot, message.from_user.id, message.text or "")
    await state.clear()
    await message.answer(result, reply_markup=back_keyboard())
