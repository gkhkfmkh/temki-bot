"""Private showcase, tariff selection, checkout and payment approval."""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import aiohttp
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from config import is_admin, settings
from database.db import connection, execute, fetchall, fetchone, get_currency, get_setting, get_text
from keyboards.inline import back_keyboard, cb, private_pages_keyboard
from services.access import create_private_invite, get_notification_targets
from services.access import apply_referral_cashback
from states.admin_states import AdminStates
from states.user_states import ShopStates
from utils.helpers import dt_to_db, format_datetime, parse_datetime, safe_html, truncate, utcnow

router = Router(name="private_shop")
logger = logging.getLogger(__name__)


async def _discount_for_user(user_id: int) -> float:
    row = await fetchone(
        "SELECT discount_percent,discount_expires_at,discount_reserved_order_id FROM users WHERE user_id=?",
        (user_id,),
    )
    if not row or row["discount_reserved_order_id"]:
        return 0.0
    expiry = parse_datetime(row["discount_expires_at"])
    if expiry and expiry <= utcnow():
        await execute("UPDATE users SET discount_percent=0,discount_expires_at=NULL WHERE user_id=?", (user_id,))
        return 0.0
    return max(0.0, min(100.0, float(row["discount_percent"] or 0)))


async def _create_order(user_id: int, tariff, payment_method: str) -> tuple[int, float, float, float]:
    discount = await _discount_for_user(user_id)
    original = float(tariff["price"])
    amount = round(original * (100 - discount) / 100, 2)
    async with connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        user = await (await db.execute(
            "SELECT discount_percent,discount_expires_at,discount_reserved_order_id FROM users WHERE user_id=?",
            (user_id,),
        )).fetchone()
        if user and user["discount_reserved_order_id"]:
            discount = 0.0
        else:
            expiry = parse_datetime(user["discount_expires_at"] if user else None)
            discount = float(user["discount_percent"] or 0) if user and (not expiry or expiry > utcnow()) else 0.0
        amount = round(original * (100 - discount) / 100, 2)
        cursor = await db.execute(
            "INSERT INTO orders(user_id,tariff_id,tariff_title,amount,original_amount,days,payment_method,status,discount_percent,created_at) "
            "VALUES(?,?,?,?,?,?,?,'created',?,?)",
            (user_id, tariff["id"], tariff["title"], amount, original, tariff["days"], payment_method, discount, dt_to_db()),
        )
        order_id = int(cursor.lastrowid)
        if discount > 0:
            await db.execute("UPDATE users SET discount_reserved_order_id=? WHERE user_id=?", (order_id, user_id))
        await db.commit()
    return order_id, original, amount, discount


async def _activate_order(bot, order_id: int, admin_id: int | None = None) -> tuple[int, int, float, str] | None:
    """Atomically approve a verified external payment and grant VIP access."""
    async with connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        order = await (await db.execute("SELECT * FROM orders WHERE id=?", (order_id,))).fetchone()
        if not order or order["status"] not in {"pending_review", "pending_crypto"}:
            await db.rollback()
            return None
        user_id = int(order["user_id"])
        days = int(order["days"])
        user = await (await db.execute(
            "SELECT paid_until,vip_forever FROM users WHERE user_id=?", (user_id,)
        )).fetchone()
        if not user:
            await db.rollback()
            logger.error("Cannot approve order %s: user %s is missing", order_id, user_id)
            return None
        if user["vip_forever"] and days < 36500:
            await db.rollback()
            logger.warning("Cannot approve finite order %s for lifetime VIP user %s", order_id, user_id)
            return None
        if days >= 36500:
            expiry = None
            await db.execute(
                "UPDATE users SET vip_forever=1,paid_until=NULL,vip_reminded_at=NULL,expired_notice_at=NULL,vip_removed_at=NULL WHERE user_id=?",
                (user_id,),
            )
        else:
            current = parse_datetime(user["paid_until"])
            start = current if current and current > utcnow() else utcnow()
            expiry = dt_to_db(start + timedelta(days=days))
            await db.execute(
                "UPDATE users SET paid_until=?,vip_forever=0,vip_reminded_at=NULL,expired_notice_at=NULL,vip_removed_at=NULL WHERE user_id=?",
                (expiry, user_id),
            )
        await db.execute(
            "UPDATE users SET total_purchases=total_purchases+1,discount_percent=0,discount_expires_at=NULL,discount_reserved_order_id=NULL WHERE user_id=?",
            (user_id,),
        )
        await db.execute(
            "UPDATE orders SET status='approved',reviewed_at=?,reviewed_by=? WHERE id=? AND status IN ('pending_review','pending_crypto')",
            (dt_to_db(), admin_id, order_id),
        )
        await db.execute(
            "INSERT INTO transactions_log(user_id,transaction_type,amount,currency,description,created_at) VALUES(?,?,?,?,?,?)",
            (user_id, "purchase", float(order["amount"]), "RUB", f"Покупка доступа: {order['tariff_title']}", dt_to_db()),
        )
        await db.commit()

    invite = await create_private_invite(bot, user_id)
    await execute("UPDATE orders SET invite_link=? WHERE id=?", (invite, order_id))
    expiry_text = "Навсегда" if days >= 36500 else format_datetime(expiry, True)
    text = (
        "✅ <b>Оплата подтверждена! Доступ активирован.</b>\n\n"
        f"Тариф: <b>{safe_html(order['tariff_title'])}</b>\n"
        f"Срок действия: <b>{expiry_text}</b>"
    )
    if invite:
        text += f"\n\n🔐 Персональная ссылка для входа:\n{invite}\n\n<i>Ссылка одноразовая, действует 24 часа.</i>"
    elif (await get_setting("private_channel_id", settings.private_channel_id)).strip():
        text += "\n\n⚠️ Не удалось создать ссылку автоматически. Напиши в поддержку."
    try:
        await bot.send_message(user_id, text)
    except Exception:
        logger.exception("Could not deliver approved order to user %s", user_id)
    await apply_referral_cashback(
        bot, user_id, float(order["amount"]), settings.referral_cashback_percent, order_id=order_id
    )
    return user_id, days, float(order["amount"]), str(order["tariff_title"])


@router.callback_query(F.data == "menu:about")
async def private_showcase(call: CallbackQuery) -> None:
    text = await get_text("private_page_0", "<b>🔐 О ПРИВАТЕ</b>\n\nЗакрытые материалы PlayFunpay Team.")
    user = await fetchone("SELECT vip_forever,paid_until FROM users WHERE user_id=?", (call.from_user.id,))
    active = bool(user and (user["vip_forever"] or (parse_datetime(user["paid_until"]) and parse_datetime(user["paid_until"]) > utcnow())))
    if active:
        expiry = "Навсегда" if user["vip_forever"] else format_datetime(user["paid_until"], True)
        status_text = await get_text("paid_open", "🔐 Доступ активен до: <b>{paid_until}</b>")
        try:
            text += "\n\n" + status_text.format(paid_until=expiry)
        except (KeyError, ValueError, IndexError):
            text += f"\n\nДоступ активен до: <b>{expiry}</b>"
    else:
        text += "\n\n" + await get_text("paid_locked", "🔒 Чтобы открыть закрытый раздел, выбери тариф.")
    await call.message.answer(text, reply_markup=private_pages_keyboard(0))
    await call.answer()


@router.callback_query(F.data.startswith("private:page:"))
async def private_page(call: CallbackQuery) -> None:
    try:
        page = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        await call.answer("Страница не найдена.", show_alert=True)
        return
    page = max(0, min(4, page))
    text = await get_text(f"private_page_{page}", "Раздел пока не заполнен.")
    try:
        await call.message.edit_text(text, reply_markup=private_pages_keyboard(page))
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            await call.message.answer(text, reply_markup=private_pages_keyboard(page))
    await call.answer()


@router.callback_query(F.data == "private:noop")
async def private_noop(call: CallbackQuery) -> None:
    await call.answer()


@router.callback_query(F.data == "menu:buy")
async def show_tariffs(call: CallbackQuery) -> None:
    user = await fetchone("SELECT vip_forever,paid_until FROM users WHERE user_id=?", (call.from_user.id,))
    if user and user["vip_forever"]:
        await call.message.answer("🔐 <b>У тебя уже бессрочный доступ в ПРИВАТ.</b>\n\nПокупка дополнительного тарифа не требуется.", reply_markup=back_keyboard())
        await call.answer()
        return
    if user and parse_datetime(user["paid_until"]) and parse_datetime(user["paid_until"]) > utcnow():
        expiry = format_datetime(user["paid_until"], True)
        await call.message.answer(f"🔐 <b>ПРИВАТ уже активен</b>\n\nДоступ действует до: <b>{expiry}</b>\n\nМожно продлить его, выбрав тариф ниже.")
    tariffs = await fetchall("SELECT id,title,price,days FROM tariffs WHERE active=1 ORDER BY days")
    if not tariffs:
        await call.message.answer("Сейчас нет доступных тарифов. Напиши в поддержку.", reply_markup=back_keyboard())
        await call.answer()
        return
    discount = await _discount_for_user(call.from_user.id)
    rows = []
    currency = await get_currency()
    for tariff in tariffs:
        price = float(tariff["price"])
        final = round(price * (100 - discount) / 100, 2)
        label = f"🔐 {tariff['title']} — {final:g} {currency}"
        if discount:
            label += f" (−{discount:g}%)"
        rows.append([cb(label, f"shop:tariff:{tariff['id']}")])
    rows.append([cb("⬅️ Назад", "nav:home")])
    await call.message.answer("<b>🔐 Выбери тариф доступа</b>\n\nПосле выбора можно оплатить с баланса, переводом или через CryptoBot (если подключён).", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data == "menu:payments")
async def payment_intro(call: CallbackQuery) -> None:
    intro = await get_text("payment_intro", "💳 <b>Оплата доступа</b>\n\nВыбери подходящий тариф ниже.")
    await call.message.answer(intro, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [cb("🔐 Выбрать тариф", "menu:buy")],
        [cb("⬅️ Назад", "nav:home")],
    ]))
    await call.answer()


@router.callback_query(F.data.startswith("shop:tariff:"))
@router.callback_query(F.data.startswith("tariff:"))
async def select_tariff(call: CallbackQuery, state: FSMContext) -> None:
    try:
        tariff_id = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        await call.answer("Некорректный тариф.", show_alert=True)
        return
    tariff = await fetchone("SELECT id,title,price,days FROM tariffs WHERE id=? AND active=1", (tariff_id,))
    if not tariff:
        await call.answer("Этот тариф больше недоступен.", show_alert=True)
        return
    user = await fetchone("SELECT vip_forever FROM users WHERE user_id=?", (call.from_user.id,))
    if user and user["vip_forever"]:
        await call.answer("У тебя уже бессрочный доступ в ПРИВАТ.", show_alert=True)
        return
    discount = await _discount_for_user(call.from_user.id)
    currency = await get_currency()
    final = round(float(tariff["price"]) * (100 - discount) / 100, 2)
    await state.clear()
    await state.update_data(tariff_id=tariff_id)
    rows = [
        [cb(f"💰 Оплатить с баланса ({final:g} {currency})", "shop:pay:balance")],
        [cb("💳 Банковская карта / СБП", "shop:pay:card")],
    ]
    if settings.crypto_bot_token and settings.crypto_rate_rub_usdt > 0:
        rows.append([cb("₿ CryptoBot", "shop:pay:crypto")])
    rows.append([cb("⬅️ К тарифам", "menu:buy")])
    text = f"<b>{safe_html(tariff['title'])}</b>\nСтоимость: <b>{final:g} {safe_html(currency)}</b>"
    if discount:
        text += f"\nПерсональная скидка: <b>{discount:g}%</b>"
    text += "\n\nВыбери способ оплаты:"
    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data == "shop:pay:balance")
async def pay_from_balance(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    tariff_id = data.get("tariff_id")
    tariff = await fetchone("SELECT id,title,price,days FROM tariffs WHERE id=? AND active=1", (tariff_id,)) if tariff_id else None
    if not tariff:
        await call.answer("Сначала выбери тариф.", show_alert=True)
        return

    order_id, original, amount, discount = await _create_order(call.from_user.id, tariff, "balance")
    currency = await get_currency()
    failure: str | None = None
    expiry_text = ""
    async with connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        order = await (await db.execute("SELECT * FROM orders WHERE id=?", (order_id,))).fetchone()
        user = await (await db.execute(
            "SELECT balance,paid_until,vip_forever FROM users WHERE user_id=?", (call.from_user.id,)
        )).fetchone()
        if not order or order["status"] != "created" or not user:
            await db.rollback()
            failure = "Не удалось подготовить заказ. Попробуй выбрать тариф ещё раз."
        elif user["vip_forever"]:
            await db.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
            await db.execute(
                "UPDATE users SET discount_reserved_order_id=NULL WHERE user_id=? AND discount_reserved_order_id=?",
                (call.from_user.id, order_id),
            )
            await db.commit()
            failure = "У тебя уже бессрочный доступ в ПРИВАТ. Дополнительная покупка не требуется."
        elif float(user["balance"] or 0) < amount:
            await db.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
            await db.execute(
                "UPDATE users SET discount_reserved_order_id=NULL WHERE user_id=? AND discount_reserved_order_id=?",
                (call.from_user.id, order_id),
            )
            balance = float(user["balance"] or 0)
            await db.commit()
            failure = f"Недостаточно средств. На балансе: {balance:.2f} {safe_html(currency)}. Пополни баланс или выбери другой способ оплаты."
        else:
            days = int(tariff["days"])
            if days >= 36500:
                await db.execute(
                    "UPDATE users SET vip_forever=1,paid_until=NULL,vip_reminded_at=NULL,expired_notice_at=NULL,vip_removed_at=NULL WHERE user_id=?",
                    (call.from_user.id,),
                )
                expiry_text = "Навсегда"
            else:
                current = parse_datetime(user["paid_until"])
                expiry = (current if current and current > utcnow() else utcnow()) + timedelta(days=days)
                expiry_db = dt_to_db(expiry)
                await db.execute(
                    "UPDATE users SET paid_until=?,vip_forever=0,vip_reminded_at=NULL,expired_notice_at=NULL,vip_removed_at=NULL WHERE user_id=?",
                    (expiry_db, call.from_user.id),
                )
                expiry_text = format_datetime(expiry, True)
            await db.execute(
                "UPDATE users SET balance=balance-?,total_purchases=total_purchases+1,discount_percent=0,discount_expires_at=NULL,discount_reserved_order_id=NULL WHERE user_id=?",
                (amount, call.from_user.id),
            )
            await db.execute(
                "UPDATE orders SET status='approved',reviewed_at=? WHERE id=? AND status='created'",
                (dt_to_db(), order_id),
            )
            await db.execute(
                "INSERT INTO transactions_log(user_id,transaction_type,amount,currency,description,created_at) VALUES(?,?,?,?,?,?)",
                (call.from_user.id, "purchase_balance", amount, "RUB", f"Оплата тарифа {tariff['title']} с внутреннего баланса", dt_to_db()),
            )
            await db.commit()

    if failure:
        await call.answer(failure, show_alert=True)
        await call.message.answer(failure, reply_markup=back_keyboard("menu:buy"))
        return

    invite = await create_private_invite(call.bot, call.from_user.id)
    await execute("UPDATE orders SET invite_link=? WHERE id=?", (invite, order_id))
    text = (
        "✅ <b>Оплата с баланса прошла!</b>\n\n"
        f"Тариф: <b>{safe_html(tariff['title'])}</b>\nСрок действия: <b>{expiry_text}</b>"
    )
    if invite:
        text += f"\n\n🔐 Ссылка для входа:\n{invite}\n<i>Ссылка одноразовая, действует 24 часа.</i>"
    elif (await get_setting("private_channel_id", settings.private_channel_id)).strip():
        text += "\n\n⚠️ Не удалось создать ссылку автоматически. Напиши в поддержку."
    await call.message.answer(text, reply_markup=back_keyboard())
    await apply_referral_cashback(
        call.bot, call.from_user.id, amount, settings.referral_cashback_percent, order_id=order_id
    )
    await state.clear()
    await call.answer("Оплата выполнена!")


@router.callback_query(F.data == "shop:pay:card")
async def choose_card_payment(call: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    tariff_id = data.get("tariff_id")
    tariff = await fetchone("SELECT id,title,price,days FROM tariffs WHERE id=? AND active=1", (tariff_id,)) if tariff_id else None
    if not tariff:
        await call.answer("Сначала выбери тариф.", show_alert=True)
        return
    order_id, original, amount, discount = await _create_order(call.from_user.id, tariff, "manual")
    currency = await get_currency()
    await execute("UPDATE orders SET status='awaiting_receipt' WHERE id=?", (order_id,))
    await state.set_state(ShopStates.waiting_receipt)
    await state.update_data(order_id=order_id)
    details = await get_setting("payment_details", "Реквизиты уточняй у администрации.")
    text = (
        "<b>💳 Оплата банковской картой / СБП</b>\n\n"
        f"Заказ: <code>#{order_id}</code>\nТариф: <b>{safe_html(tariff['title'])}</b>\n"
        f"К оплате: <b>{amount:g} {safe_html(currency)}</b>\n"
        f"\n{safe_html(details)}\n\n"
        "После перевода отправь сюда фото или файл чека. Заявка поступит администраторам на проверку."
    )
    await call.message.answer(text, reply_markup=back_keyboard())
    await call.answer()


@router.message(ShopStates.waiting_receipt, F.photo | F.document)
async def receive_payment_receipt(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    order_id = data.get("order_id")
    order = await fetchone("SELECT * FROM orders WHERE id=? AND user_id=?", (order_id, message.from_user.id)) if order_id else None
    if not order or order["status"] != "awaiting_receipt":
        await state.clear()
        await message.answer("Не нашёл активный заказ. Открой «Купить доступ» и создай заявку заново.")
        return
    file_id = message.photo[-1].file_id if message.photo else message.document.file_id
    file_type = "photo" if message.photo else "document"
    currency = await get_currency()
    await execute(
        "UPDATE orders SET receipt_file_id=?,receipt_type=?,status='pending_review' WHERE id=? AND status='awaiting_receipt'",
        (file_id, file_type, order_id),
    )
    await state.clear()
    await message.answer("✅ Чек получен! Заявка отправлена администраторам. Доступ будет выдан после проверки.", reply_markup=back_keyboard())
    from keyboards.inline import cb
    for admin_id in await get_notification_targets():
        try:
            caption = (
                f"💳 <b>Новый заказ #{order_id}</b>\nПользователь: <code>{message.from_user.id}</code>\n"
                f"Тариф: <b>{safe_html(order['tariff_title'])}</b>\nСумма: <b>{order['amount']:g} {safe_html(currency)}</b>"
            )
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [cb("✅ Подтвердить оплату", f"order:approve:{order_id}"), cb("❌ Отклонить", f"order:reject:{order_id}")],
                [cb("💬 Написать покупателю", f"order:message:{order_id}")],
            ])
            if file_type == "photo":
                await message.bot.send_photo(admin_id, file_id, caption=caption, reply_markup=markup)
            else:
                await message.bot.send_document(admin_id, file_id, caption=caption, reply_markup=markup)
        except Exception:
            logger.exception("Could not send payment receipt to admin %s", admin_id)


@router.message(ShopStates.waiting_receipt, F.text)
async def receipt_text_reminder(message: Message) -> None:
    await message.answer("Для проверки оплаты отправь именно фото или файл чека. Чтобы отменить оформление, используй /cancel.")


@router.callback_query(F.data == "shop:pay:crypto")
async def choose_crypto_payment(call: CallbackQuery, state: FSMContext) -> None:
    if not settings.crypto_bot_token or settings.crypto_rate_rub_usdt <= 0:
        await call.answer("CryptoBot пока не настроен. Обратись к администрации.", show_alert=True)
        return
    data = await state.get_data()
    tariff_id = data.get("tariff_id")
    tariff = await fetchone("SELECT id,title,price,days FROM tariffs WHERE id=? AND active=1", (tariff_id,)) if tariff_id else None
    if not tariff:
        await call.answer("Сначала выбери тариф.", show_alert=True)
        return
    order_id, original, amount, discount = await _create_order(call.from_user.id, tariff, "cryptobot")
    currency = await get_currency()
    try:
        headers = {"Crypto-Pay-API-Token": settings.crypto_bot_token}
        amount_usdt = round(amount / settings.crypto_rate_rub_usdt, 2)
        if amount_usdt <= 0:
            raise ValueError("The calculated USDT amount is too small")
        payload = {"asset": "USDT", "amount": f"{amount_usdt:.2f}", "description": f"Доступ PlayFunpay, заказ №{order_id}", "payload": str(order_id)}
        async with aiohttp.ClientSession() as session:
            async with session.post("https://pay.crypt.bot/api/createInvoice", headers=headers, json=payload, timeout=20) as response:
                result = await response.json()
        if not result.get("ok"):
            raise RuntimeError(str(result.get("error", "CryptoBot API error")))
        invoice = result["result"]
        await execute("UPDATE orders SET status='pending_crypto',crypto_invoice_id=? WHERE id=?", (str(invoice["invoice_id"]), order_id))
        from aiogram.types import InlineKeyboardButton
        markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="₿ Перейти к оплате", url=invoice["pay_url"])],
            [cb("⬅️ В главное меню", "nav:home")],
        ])
        await call.message.answer(
            f"<b>₿ Счёт CryptoBot создан</b>\n\nЗаказ: <code>#{order_id}</code>\nСумма: <b>{amount_usdt:g} USDT</b>\nЭквивалент: <b>{amount:g} {safe_html(currency)}</b>\n\nПосле оплаты бот автоматически проверит статус счёта.",
            reply_markup=markup,
        )
    except Exception:
        logger.exception("CryptoBot invoice creation failed")
        await execute("UPDATE orders SET status='failed' WHERE id=?", (order_id,))
        await execute("UPDATE users SET discount_reserved_order_id=NULL WHERE user_id=? AND discount_reserved_order_id=?", (call.from_user.id, order_id))
        await call.message.answer("Не удалось создать счёт CryptoBot. Попробуй оплату картой / СБП или обратись в поддержку.")
    await call.answer()


@router.callback_query(F.data.startswith("order:approve:"))
@router.callback_query(F.data.startswith("pay:approve:"))
async def approve_order(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    try:
        order_id = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        await call.answer("Некорректный заказ.", show_alert=True)
        return
    result = await _activate_order(call.bot, order_id, call.from_user.id)
    if not result:
        await call.answer("Заказ уже обработан или не найден.", show_alert=True)
        return
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(f"✅ Заказ #{order_id} подтверждён. Доступ выдан пользователю <code>{result[0]}</code>.")
    await call.answer("Оплата подтверждена.")


@router.callback_query(F.data.startswith("order:reject:"))
@router.callback_query(F.data.startswith("pay:reject:"))
async def reject_order(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    try:
        order_id = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        await call.answer("Некорректный заказ.", show_alert=True)
        return
    row = await fetchone("SELECT user_id,status FROM orders WHERE id=?", (order_id,))
    if not row or row["status"] != "pending_review":
        await call.answer("Заказ уже обработан.", show_alert=True)
        return
    await execute("UPDATE orders SET status='rejected',reviewed_at=?,reviewed_by=? WHERE id=?", (dt_to_db(), call.from_user.id, order_id))
    await execute("UPDATE users SET discount_reserved_order_id=NULL WHERE user_id=? AND discount_reserved_order_id=?", (row["user_id"], order_id))
    try:
        await call.bot.send_message(row["user_id"], "❌ <b>Оплата не подтверждена.</b> Если считаешь, что произошла ошибка, напиши в поддержку.")
    except Exception:
        logger.exception("Could not notify rejected order owner")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.message.answer(f"❌ Заказ #{order_id} отклонён.")
    await call.answer("Заказ отклонён.")


@router.callback_query(F.data.startswith("order:message:"))
async def message_order_user(call: CallbackQuery, state: FSMContext) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    order_id = int(call.data.rsplit(":", 1)[1])
    order = await fetchone("SELECT id FROM orders WHERE id=?", (order_id,))
    if not order:
        await call.answer("Заказ не найден.", show_alert=True)
        return
    await state.set_state(AdminStates.order_message)
    await state.update_data(order_id=order_id)
    await call.message.answer(f"Отправь сообщение покупателю по заказу #{order_id}.")
    await call.answer()


@router.message(AdminStates.order_message, F.text)
async def send_order_message(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    order = await fetchone("SELECT user_id FROM orders WHERE id=?", (data.get("order_id"),))
    await state.clear()
    if not order:
        await message.answer("Заказ не найден.")
        return
    body = message.text or message.caption or ""
    try:
        await message.bot.send_message(order["user_id"], f"💬 <b>Сообщение по заказу</b>\n\n{safe_html(body)}")
        await message.answer("✅ Сообщение отправлено покупателю.")
    except Exception:
        logger.exception("Could not send order message")
        await message.answer("Не удалось отправить сообщение пользователю.")


async def check_crypto_orders(bot) -> None:
    if not settings.crypto_bot_token:
        return
    rows = await fetchall("SELECT id,crypto_invoice_id FROM orders WHERE status='pending_crypto' AND crypto_invoice_id IS NOT NULL LIMIT 100")
    if not rows:
        return
    invoice_ids = ",".join(str(row["crypto_invoice_id"]) for row in rows)
    try:
        headers = {"Crypto-Pay-API-Token": settings.crypto_bot_token}
        async with aiohttp.ClientSession() as session:
            async with session.get("https://pay.crypt.bot/api/getInvoices", headers=headers, params={"invoice_ids": invoice_ids}, timeout=20) as response:
                result = await response.json()
        if not result.get("ok"):
            return
        for invoice in result.get("result", {}).get("items", []):
            if invoice.get("status") == "paid":
                order_id = int(invoice.get("payload") or 0)
                if order_id:
                    await _activate_order(bot, order_id)
    except Exception:
        logger.exception("CryptoBot invoice polling failed")
