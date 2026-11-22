"""Administrative dashboard, user management, content, campaigns and exports."""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from config import is_admin, settings
from database.db import connection, execute, fetchall, fetchone, get_currency, get_setting, get_text, set_setting, set_text, audit
from keyboards.admin_kb import admin_keyboard
from keyboards.inline import back_keyboard, cb
from services.access import create_private_invite
from states.admin_states import AdminStates
from utils.helpers import csv_bytes, dt_to_db, format_datetime, generate_promo_code, parse_datetime, progress_bar, safe_html, truncate, utcnow, valid_url, xlsx_bytes

router = Router(name="admin_panel")
logger = logging.getLogger(__name__)


def _admin_only(user_id: int) -> bool:
    return is_admin(user_id)


async def _show_user_card(message: Message, user_id: int) -> None:
    user = await fetchone("SELECT * FROM users WHERE user_id=?", (user_id,))
    if not user:
        await message.answer("Пользователь не найден.")
        return
    refs = await fetchone("SELECT COUNT(*) AS n FROM referrals WHERE referrer_id=?", (user_id,))
    vip = "Навсегда" if user["vip_forever"] else (format_datetime(user["paid_until"], True) if parse_datetime(user["paid_until"]) and parse_datetime(user["paid_until"]) > utcnow() else "не активен")
    name = safe_html(user["first_name"] or "Без имени")
    username = "@" + safe_html(user["username"]) if user["username"] else "не указан"
    text = (
        f"<b>👤 Карточка пользователя</b>\n\n"
        f"Имя: {name}\nUsername: {username}\nID: <code>{user_id}</code>\n"
        f"Регистрация: {format_datetime(user['joined_at'])}\nПоследняя активность: {format_datetime(user['last_active'])}\n"
        f"VIP: <b>{vip}</b>\nБаланс: <b>{user['balance']:.2f} {safe_html(await get_currency())}</b>\n"
        f"FP-коины: <b>{user['points']}</b>\nПокупок: <b>{user['total_purchases']}</b>\n"
        f"Рефералов: <b>{refs['n'] if refs else 0}</b>\nБан: <b>{'Да' if user['banned'] else 'Нет'}</b>"
    )
    rows = [
        [cb("➕ Баланс", f"user:action:balance_add:{user_id}"), cb("➖ Баланс", f"user:action:balance_sub:{user_id}")],
        [cb("➕ FP-коины", f"user:action:points_add:{user_id}"), cb("➖ FP-коины", f"user:action:points_sub:{user_id}")],
        [cb("🔐 Выдать VIP-дни", f"user:action:vip_days:{user_id}"), cb("🔒 Снять VIP", f"user:vip:revoke:{user_id}")],
        [cb("✉️ Написать", f"user:action:message:{user_id}"), cb("🚫 Снять бан" if user["banned"] else "⛔ Заблокировать", f"user:ban:{user_id}")],
        [cb("⬅️ К панели", "admin:home")],
    ]
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "admin:home")
async def admin_home(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    await call.message.answer("<b>⚙️ Административная панель PlayFunpay</b>\n\nВыбери нужный раздел:", reply_markup=admin_keyboard())
    await call.answer()


@router.callback_query(F.data == "admin:stats")
async def admin_stats(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    now = utcnow()
    total = await fetchone("SELECT COUNT(*) AS n FROM users")
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = await fetchone("SELECT COUNT(*) AS n FROM users WHERE joined_at>=?", (dt_to_db(today_start),))
    week = await fetchone("SELECT COUNT(*) AS n FROM users WHERE joined_at>=?", (dt_to_db(now - timedelta(days=7)),))
    month = await fetchone("SELECT COUNT(*) AS n FROM users WHERE joined_at>=?", (dt_to_db(now - timedelta(days=30)),))
    vip = await fetchone("SELECT COUNT(*) AS n FROM users WHERE vip_forever=1 OR paid_until>?", (dt_to_db(),))
    revenue = await fetchone("SELECT COALESCE(SUM(amount),0) AS total FROM orders WHERE status='approved'")
    tickets = await fetchone("SELECT COUNT(*) AS n FROM tickets_suggestions WHERE status='open'")
    ads = await fetchone("SELECT COUNT(*) AS n FROM ad_requests WHERE status='pending'")
    giveaways = await fetchone("SELECT COUNT(*) AS n FROM giveaways WHERE status='active'")
    orders = await fetchone("SELECT COUNT(*) AS n FROM orders WHERE status IN ('pending_review','awaiting_receipt')")
    text = (
        "<b>📊 Подробная статистика</b>\n\n"
        f"👥 Всего пользователей: <b>{total['n']}</b>\n"
        f"🆕 Сегодня: <b>{today['n']}</b>\n"
        f"📅 За 7 дней: <b>{week['n']}</b>\n"
        f"🗓 За 30 дней: <b>{month['n']}</b>\n"
        f"💎 Активных VIP: <b>{vip['n']}</b>\n"
        f"💰 Подтверждённая выручка: <b>{float(revenue['total']):.2f} {safe_html(await get_currency())}</b>\n"
        f"🆘 Открытых обращений: <b>{tickets['n']}</b>\n"
        f"💎 Заявок на рекламу: <b>{ads['n']}</b>\n"
        f"🎁 Активных розыгрышей: <b>{giveaways['n']}</b>\n"
        f"💳 Ожидают проверки: <b>{orders['n']}</b>"
    )
    await call.message.answer(text, reply_markup=back_keyboard("admin:home"))
    await call.answer()


@router.callback_query(F.data == "admin:users")
@router.callback_query(F.data == "admin:ban_prompt")
async def admin_users(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(AdminStates.user_search)
    await call.message.answer("👥 <b>Управление пользователями</b>\n\nОтправь Telegram ID или @username пользователя.")
    await call.answer()


@router.message(AdminStates.user_search, F.text)
async def search_user(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    query = (message.text or "").strip()
    if query.startswith("@"):
        row = await fetchone("SELECT user_id FROM users WHERE lower(username)=lower(?)", (query[1:],))
    elif query.isdigit():
        row = await fetchone("SELECT user_id FROM users WHERE user_id=?", (int(query),))
    else:
        await message.answer("Введи числовой Telegram ID или @username.")
        return
    await state.clear()
    if not row:
        await message.answer("Пользователь не найден в базе.", reply_markup=back_keyboard("admin:home"))
        return
    await _show_user_card(message, int(row["user_id"]))


@router.callback_query(F.data.startswith("user:action:"))
async def user_action_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    parts = call.data.split(":")
    action, user_id = parts[2], int(parts[3])
    await state.set_state(AdminStates.user_action_value)
    await state.update_data(user_action=action, target_user_id=user_id)
    prompts = {
        "balance_add": "Введи сумму в рублях для начисления.",
        "balance_sub": "Введи сумму в рублях для списания.",
        "points_add": "Введи количество FP-коинов для начисления.",
        "points_sub": "Введи количество FP-коинов для списания.",
        "vip_days": "Введи количество дней VIP. Для бессрочного доступа отправь 36500.",
        "message": "Отправь текст сообщения пользователю.",
    }
    await call.message.answer(f"Пользователь <code>{user_id}</code>. {prompts.get(action, 'Отправь значение.')}")
    await call.answer()


@router.message(AdminStates.user_action_value)
async def apply_user_action(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    action = data.get("user_action")
    user_id = int(data.get("target_user_id"))
    value = (message.text or "").strip()
    await state.clear()
    if action == "message":
        try:
            await message.bot.send_message(user_id, f"💬 <b>Сообщение от администрации</b>\n\n{safe_html(value)}")
            await message.answer("✅ Сообщение отправлено.")
        except Exception:
            logger.exception("Direct admin message failed")
            await message.answer("Не удалось отправить сообщение пользователю.")
        return
    try:
        amount = float(value.replace(",", "."))
        if amount <= 0:
            raise ValueError
        if action in {"points_add", "points_sub", "vip_days"} and not amount.is_integer():
            raise ValueError
    except ValueError:
        await message.answer("Нужно указать положительное число. Операция отменена.")
        return
    if action == "balance_add":
        async with connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            exists = await (await db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))).fetchone()
            if not exists:
                await db.rollback()
                await message.answer("Пользователь не найден.")
                return
            await db.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (amount, user_id))
            await db.execute("INSERT INTO transactions_log(user_id,transaction_type,amount,currency,description,created_at) VALUES(?,?,?,?,?,?)", (user_id, "admin_balance_add", amount, "RUB", f"Начисление администратором {message.from_user.id}", dt_to_db()))
            await db.commit()
        await message.answer(f"✅ Начислено {amount:g} {safe_html(await get_currency())}.")
        await _send_user_notice(message, user_id, f"💰 На твой баланс начислено <b>{amount:g} {safe_html(await get_currency())}</b> администрацией.")
        await audit(message.from_user.id, "user_balance_add", f"user={user_id}; amount={amount}")
    elif action == "balance_sub":
        async with connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))).fetchone()
            if not row or float(row["balance"]) < amount:
                await db.rollback()
                await message.answer("Недостаточно средств на балансе пользователя.")
                return
            await db.execute("UPDATE users SET balance=balance-? WHERE user_id=?", (amount, user_id))
            await db.execute("INSERT INTO transactions_log(user_id,transaction_type,amount,currency,description,created_at) VALUES(?,?,?,?,?,?)", (user_id, "admin_balance_sub", amount, "RUB", f"Списание администратором {message.from_user.id}", dt_to_db()))
            await db.commit()
        await message.answer("✅ Средства списаны.")
        await audit(message.from_user.id, "user_balance_sub", f"user={user_id}; amount={amount}")
    elif action == "points_add":
        points = int(amount)
        async with connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            exists = await (await db.execute("SELECT 1 FROM users WHERE user_id=?", (user_id,))).fetchone()
            if not exists:
                await db.rollback()
                await message.answer("Пользователь не найден.")
                return
            await db.execute("UPDATE users SET points=points+? WHERE user_id=?", (points, user_id))
            await db.execute("INSERT INTO transactions_log(user_id,transaction_type,amount,currency,description,created_at) VALUES(?,?,?,?,?,?)", (user_id, "admin_points_add", points, "FP", f"Начисление администратором {message.from_user.id}", dt_to_db()))
            await db.commit()
        await message.answer(f"✅ Начислено {points} FP-коинов.")
        await _send_user_notice(message, user_id, f"🪙 Администрация начислила тебе <b>{points} FP-коинов</b>.")
    elif action == "points_sub":
        points = int(amount)
        async with connection() as db:
            await db.execute("BEGIN IMMEDIATE")
            row = await (await db.execute("SELECT points FROM users WHERE user_id=?", (user_id,))).fetchone()
            if not row or int(row["points"]) < points:
                await db.rollback()
                await message.answer("Недостаточно FP-коинов.")
                return
            await db.execute("UPDATE users SET points=points-? WHERE user_id=?", (points, user_id))
            await db.execute("INSERT INTO transactions_log(user_id,transaction_type,amount,currency,description,created_at) VALUES(?,?,?,?,?,?)", (user_id, "admin_points_sub", points, "FP", f"Списание администратором {message.from_user.id}", dt_to_db()))
            await db.commit()
        await message.answer("✅ FP-коины списаны.")
    elif action == "vip_days":
        days = int(amount)
        current_user = await fetchone("SELECT vip_forever FROM users WHERE user_id=?", (user_id,))
        if current_user and current_user["vip_forever"] and days < 36500:
            await state.clear()
            await message.answer("У пользователя уже бессрочный VIP-доступ. Сначала отзови его, если нужно выдать доступ на срок.")
            return
        if days >= 36500:
            await execute("UPDATE users SET vip_forever=1,paid_until=NULL,vip_reminded_at=NULL,expired_notice_at=NULL,vip_removed_at=NULL WHERE user_id=?", (user_id,))
            expiry = "Навсегда"
        else:
            user = await fetchone("SELECT paid_until FROM users WHERE user_id=?", (user_id,))
            current = parse_datetime(user["paid_until"] if user else None)
            from datetime import timedelta
            new_expiry = (current if current and current > utcnow() else utcnow()) + timedelta(days=days)
            await execute("UPDATE users SET vip_forever=0,paid_until=?,vip_reminded_at=NULL,expired_notice_at=NULL,vip_removed_at=NULL WHERE user_id=?", (dt_to_db(new_expiry), user_id))
            expiry = format_datetime(new_expiry, True)
        invite = await create_private_invite(message.bot, user_id)
        notice = f"🔐 Администрация активировала тебе VIP-доступ до <b>{expiry}</b>."
        if invite:
            notice += f"\n\nСсылка для входа (одноразовая, 24 часа):\n{invite}"
        await message.answer(f"✅ VIP-доступ выдан. Действует до: <b>{expiry}</b>.")
        await _send_user_notice(message, user_id, notice)
        await audit(message.from_user.id, "user_vip_granted", f"user={user_id}; days={days}")
    await _show_user_card(message, user_id)


async def _send_user_notice(message: Message, user_id: int, text: str) -> None:
    try:
        await message.bot.send_message(user_id, text)
    except Exception:
        logger.exception("Could not notify user %s", user_id)


@router.callback_query(F.data.startswith("user:ban:"))
async def toggle_user_ban(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    user_id = int(call.data.rsplit(":", 1)[1])
    row = await fetchone("SELECT banned FROM users WHERE user_id=?", (user_id,))
    if not row:
        await call.answer("Пользователь не найден.", show_alert=True)
        return
    new_value = 0 if row["banned"] else 1
    await execute("UPDATE users SET banned=? WHERE user_id=?", (new_value, user_id))
    await audit(call.from_user.id, "user_ban_toggle", f"user={user_id}; banned={new_value}")
    try:
        await call.bot.send_message(user_id, "⛔ Доступ к боту ограничен администрацией." if new_value else "✅ Доступ к боту восстановлен.")
    except Exception:
        logger.debug("Could not send ban-status notice to user %s", user_id, exc_info=True)
    await call.answer("Пользователь заблокирован." if new_value else "Блокировка снята.")
    await _show_user_card(call.message, user_id)


@router.callback_query(F.data.startswith("user:vip:revoke:"))
async def revoke_user_vip(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    user_id = int(call.data.rsplit(":", 1)[1])
    user = await fetchone("SELECT user_id FROM users WHERE user_id=?", (user_id,))
    if not user:
        await call.answer("Пользователь не найден.", show_alert=True)
        return
    await execute("UPDATE users SET paid_until=NULL,vip_forever=0,vip_reminded_at=NULL,expired_notice_at=?,vip_removed_at=NULL WHERE user_id=?", (dt_to_db(), user_id))
    private_channel = (await get_setting("private_channel_id", settings.private_channel_id)).strip()
    if private_channel:
        try:
            await call.bot.ban_chat_member(private_channel, user_id)
            await call.bot.unban_chat_member(private_channel, user_id, only_if_banned=True)
            await execute("UPDATE users SET vip_removed_at=? WHERE user_id=?", (dt_to_db(), user_id))
        except Exception:
            logger.exception("Could not remove revoked VIP user from private channel")
    try:
        await call.bot.send_message(user_id, "🔒 Администрация отозвала твой доступ в ПРИВАТ. Если считаешь, что это ошибка, напиши в поддержку.")
    except Exception:
        logger.debug("Could not send VIP revocation notice to user %s", user_id, exc_info=True)
    await audit(call.from_user.id, "user_vip_revoked", f"user={user_id}")
    await call.answer("VIP-доступ отозван.")
    await _show_user_card(call.message, user_id)


@router.callback_query(F.data == "admin:export")
async def export_menu(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    await call.message.answer("📥 <b>Экспорт базы</b>\n\nВыбери формат и данные для выгрузки:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [cb("👥 Пользователи · CSV", "admin:export:users"), cb("XLSX", "admin:export:xlsx:users")],
        [cb("🛒 Заказы · CSV", "admin:export:orders"), cb("XLSX", "admin:export:xlsx:orders")],
        [cb("⬅️ Назад", "admin:home")],
    ]))
    await call.answer()


@router.callback_query(F.data.startswith("admin:export:"))
async def export_csv(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    parts = call.data.split(":")
    xlsx = "xlsx" in parts
    kind = parts[-1]
    if kind == "users":
        headers = ["user_id", "username", "first_name", "joined_at", "paid_until", "vip_forever", "balance", "points", "total_purchases", "banned", "last_active"]
        rows = await fetchall("SELECT user_id,username,first_name,joined_at,paid_until,vip_forever,balance,points,total_purchases,banned,last_active FROM users ORDER BY joined_at")
        data = [tuple(row) for row in rows]
        filename = "playfunpay_users.xlsx" if xlsx else "playfunpay_users.csv"
        content = xlsx_bytes(headers, data, "Пользователи") if xlsx else csv_bytes(headers, data).getvalue()
    elif kind == "orders":
        headers = ["id", "user_id", "tariff_title", "amount", "original_amount", "days", "payment_method", "status", "created_at", "reviewed_at"]
        rows = await fetchall("SELECT id,user_id,tariff_title,amount,original_amount,days,payment_method,status,created_at,reviewed_at FROM orders ORDER BY id")
        data = [tuple(row) for row in rows]
        filename = "playfunpay_orders.xlsx" if xlsx else "playfunpay_orders.csv"
        content = xlsx_bytes(headers, data, "Заказы") if xlsx else csv_bytes(headers, data).getvalue()
    else:
        await call.answer("Неизвестный тип экспорта.", show_alert=True)
        return
    await call.message.answer_document(BufferedInputFile(content, filename=filename), caption="📥 Экспорт подготовлен.")
    await call.answer()


@router.callback_query(F.data == "admin:tariffs")
async def tariff_list(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    tariffs = await fetchall("SELECT id,title,price,days,active FROM tariffs ORDER BY days")
    lines = ["<b>🛒 Управление тарифами</b>\n"]
    rows = [[cb("➕ Добавить тариф", "admin:tariff:add")]]
    for tariff in tariffs:
        lines.append(f"{'🟢' if tariff['active'] else '⚫'} <b>#{tariff['id']} {safe_html(tariff['title'])}</b> — {tariff['price']:g} {safe_html(await get_currency())}, {tariff['days']} дн.")
        rows.append([cb(f"✏️ {tariff['title'][:28]}", f"admin:tariff:edit:{tariff['id']}"), cb("Вкл/выкл", f"admin:tariff:toggle:{tariff['id']}")])
    rows.append([cb("⬅️ Назад", "admin:home")])
    await call.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data == "admin:tariff:add")
@router.callback_query(F.data == "admin:tariff_add")
async def tariff_add_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(AdminStates.tariff_add)
    await call.message.answer("Отправь данные тарифа в формате:\n<code>Название | Цена | Дни</code>\nДля бессрочного доступа укажи 36500 дней.")
    await call.answer()


@router.message(AdminStates.tariff_add, F.text)
async def tariff_add_save(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    parts = [item.strip() for item in (message.text or "").split("|")]
    if len(parts) != 3:
        await message.answer("Формат: <code>Название | Цена | Дни</code>")
        return
    title, price_raw, days_raw = parts
    try:
        price = float(price_raw.replace(",", "."))
        days = int(days_raw)
        if not title or price < 0 or days <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Проверь данные: цена неотрицательная, срок — целое число больше нуля.")
        return
    async with connection() as db:
        cursor = await db.execute("INSERT INTO tariffs(title,price,days,active) VALUES(?,?,?,1)", (title[:80], price, days))
        tariff_id = int(cursor.lastrowid)
        await db.execute("INSERT INTO products_tariffs(legacy_tariff_id,title,price,days,active) VALUES(?,?,?,?,1)", (tariff_id, title[:80], price, days))
        await db.commit()
    await audit(message.from_user.id, "tariff_created", f"id={tariff_id}; title={title}")
    await state.clear()
    await message.answer("✅ Тариф добавлен.", reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("admin:tariff:toggle:"))
async def tariff_toggle(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    tariff_id = int(call.data.rsplit(":", 1)[1])
    row = await fetchone("SELECT active FROM tariffs WHERE id=?", (tariff_id,))
    if not row:
        await call.answer("Тариф не найден.", show_alert=True)
        return
    active = 0 if row["active"] else 1
    await execute("UPDATE tariffs SET active=? WHERE id=?", (active, tariff_id))
    await execute("UPDATE products_tariffs SET active=? WHERE legacy_tariff_id=?", (active, tariff_id))
    await audit(call.from_user.id, "tariff_toggled", f"id={tariff_id}; active={active}")
    await tariff_list(call)


@router.callback_query(F.data.startswith("admin:tariff:edit:"))
@router.callback_query(F.data.regexp(r"^admin:tariff:\d+$"))
async def tariff_edit_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    tariff_id = int(call.data.rsplit(":", 1)[1])
    tariff = await fetchone("SELECT * FROM tariffs WHERE id=?", (tariff_id,))
    if not tariff:
        await call.answer("Тариф не найден.", show_alert=True)
        return
    await state.set_state(AdminStates.tariff_edit)
    await state.update_data(tariff_id=tariff_id)
    await call.message.answer(
        f"Текущий тариф: <b>{safe_html(tariff['title'])}</b>\n\nОтправь новые данные в формате:\n<code>Название | Цена | Дни | 1 или 0</code>"
    )
    await call.answer()


@router.message(AdminStates.tariff_edit, F.text)
async def tariff_edit_save(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    parts = [item.strip() for item in (message.text or "").split("|")]
    if len(parts) != 4:
        await message.answer("Формат: <code>Название | Цена | Дни | 1 или 0</code>")
        return
    title, price_raw, days_raw, active_raw = parts
    try:
        price, days, active = float(price_raw.replace(",", ".")), int(days_raw), int(active_raw)
        if not title or price < 0 or days <= 0 or active not in (0, 1):
            raise ValueError
    except ValueError:
        await message.answer("Некорректные данные тарифа.")
        return
    tariff_id = int(data["tariff_id"])
    await execute("UPDATE tariffs SET title=?,price=?,days=?,active=? WHERE id=?", (title[:80], price, days, active, tariff_id))
    await execute("UPDATE products_tariffs SET title=?,price=?,days=?,active=? WHERE legacy_tariff_id=?", (title[:80], price, days, active, tariff_id))
    await audit(message.from_user.id, "tariff_updated", f"id={tariff_id}")
    await state.clear()
    await message.answer("✅ Тариф обновлён.", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:promos")
async def promo_list(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    promos = await fetchall("SELECT * FROM promo_codes ORDER BY created_at DESC LIMIT 20")
    lines = ["<b>🎟 Управление промокодами</b>\n"]
    for promo in promos:
        limit = "∞" if promo["max_uses"] == 0 else f"{promo['uses_count']}/{promo['max_uses']}"
        expiry = format_datetime(promo["expires_at"]) if promo["expires_at"] else "без срока"
        reward_label = {"balance": "рубли", "points": "FP-коины", "vip_days": "дни ПРИВАТА", "discount": "скидка"}.get(promo["reward_type"], "награда")
        lines.append(f"{'🟢' if promo['active'] else '⚫'} <code>{safe_html(promo['code'])}</code> — {reward_label}: {promo['value']:g}; активации {limit}; до {expiry}")
    if not promos:
        lines.append("Промокодов пока нет.")
    rows = [[cb("➕ Создать промокод", "admin:promo:create")]]
    for promo in promos[:10]:
        rows.append([cb(f"{'Отключить' if promo['active'] else 'Включить'} {promo['code']}", f"admin:promo:toggle:{promo['code']}")])
    rows.append([cb("⬅️ Назад", "admin:home")])
    await call.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data == "admin:promo:create")
@router.callback_query(F.data == "admin:promo_add")
async def promo_create_start(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(AdminStates.promo_code)
    await call.message.answer("Отправь код промокода (3–32 символа) или напиши <code>AUTO</code> для генерации.")
    await call.answer()


@router.message(AdminStates.promo_code, F.text)
async def promo_code_input(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    code = (message.text or "").strip().upper()
    if code == "AUTO":
        code = generate_promo_code()
    if not re.fullmatch(r"[A-Z0-9_-]{3,32}", code):
        await message.answer("Код должен содержать 3–32 латинских символа, цифры, дефис или подчёркивание.")
        return
    existing = await fetchone("SELECT code FROM promo_codes WHERE code=?", (code,))
    if existing:
        await message.answer("Такой код уже существует. Введи другой.")
        return
    await state.update_data(promo_code=code)
    await state.set_state(AdminStates.promo_type)
    await message.answer("Выбери тип награды:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [cb("💰 Рубли на баланс", "admin:promo:type:balance")],
        [cb("🪙 FP-коины", "admin:promo:type:points")],
        [cb("🔐 Дни VIP", "admin:promo:type:vip_days")],
        [cb("🏷 Скидка на покупку", "admin:promo:type:discount")],
    ]))


@router.callback_query(F.data.startswith("admin:promo:type:"))
async def promo_type_select(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    kind = call.data.rsplit(":", 1)[1]
    if kind not in {"balance", "points", "vip_days", "discount"}:
        await call.answer("Тип не поддерживается.", show_alert=True)
        return
    await state.update_data(promo_type=kind)
    await state.set_state(AdminStates.promo_value)
    label = "рублей" if kind == "balance" else "FP-коинов" if kind == "points" else "дней" if kind == "vip_days" else "процентов (1–100)"
    await call.message.answer(f"Введи размер награды в {label}.")
    await call.answer()


@router.message(AdminStates.promo_value, F.text)
async def promo_value_input(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    try:
        value = float((message.text or "").replace(",", "."))
        if value <= 0 or (data["promo_type"] == "discount" and value > 100):
            raise ValueError
        if data["promo_type"] in {"points", "vip_days"} and not value.is_integer():
            raise ValueError
    except ValueError:
        await message.answer("Укажи корректное положительное число. Для скидки допустимо 1–100%.")
        return
    await state.update_data(promo_value=value)
    await state.set_state(AdminStates.promo_limit)
    await message.answer("Укажи максимальное количество активаций. 0 — без ограничений.")


@router.message(AdminStates.promo_limit, F.text)
async def promo_limit_input(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    try:
        limit = int((message.text or "").strip())
        if limit < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи целое число от 0 и выше.")
        return
    await state.update_data(promo_limit=limit)
    await state.set_state(AdminStates.promo_expiry)
    await message.answer("Укажи срок действия в формате <code>ГГГГ-ММ-ДД ЧЧ:ММ</code> (UTC) или отправь дефис <code>-</code> без срока.")


@router.message(AdminStates.promo_expiry, F.text)
async def promo_expiry_save(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    expiry = None
    if raw != "-":
        try:
            from datetime import datetime, timezone
            expiry = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).isoformat()
            if parse_datetime(expiry) <= utcnow():
                raise ValueError
        except ValueError:
            await message.answer("Неверная дата. Пример: <code>2026-12-01 20:00</code> или дефис.")
            return
    data = await state.get_data()
    await execute(
        "INSERT INTO promo_codes(code,reward_type,value,max_uses,uses_count,expires_at,active,created_at) VALUES(?,?,?,?,0,?,1,?)",
        (data["promo_code"], data["promo_type"], data["promo_value"], data["promo_limit"], expiry, dt_to_db()),
    )
    await audit(message.from_user.id, "promo_created", f"code={data['promo_code']}; type={data['promo_type']}; value={data['promo_value']}")
    await state.clear()
    await message.answer(f"✅ Промокод создан: <code>{safe_html(data['promo_code'])}</code>", reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("admin:promo:toggle:"))
async def promo_toggle(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    code = call.data.split(":", 3)[3]
    row = await fetchone("SELECT active FROM promo_codes WHERE code=?", (code,))
    if not row:
        await call.answer("Промокод не найден.", show_alert=True)
        return
    await execute("UPDATE promo_codes SET active=? WHERE code=?", (0 if row["active"] else 1, code))
    await promo_list(call)


@router.callback_query(F.data == "admin:giveaways")
@router.callback_query(F.data == "admin:raffles")
async def admin_giveaway_list(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    items = await fetchall("SELECT id,title,prize,ends_at,status FROM giveaways ORDER BY id DESC LIMIT 15")
    lines = ["<b>🎁 Управление розыгрышами</b>\n"]
    rows = [[cb("➕ Создать розыгрыш", "admin:giveaway:create")]]
    for item in items:
        lines.append(f"{'🟢' if item['status']=='active' else '⚫'} #{item['id']} <b>{safe_html(item['title'])}</b> — {safe_html(item['prize'])}; {format_datetime(item['ends_at'])}")
        if item["status"] == "active":
            rows.append([cb(f"🏁 Подвести итоги #{item['id']}", f"admin:giveaway:draw:{item['id']}")])
        elif item["status"] == "completed":
            rows.append([cb(f"🔄 Перевыбрать победителей #{item['id']}", f"admin:giveaway:reroll:{item['id']}")])
    if not items:
        lines.append("Розыгрышей пока нет.")
    rows.append([cb("⬅️ Назад", "admin:home")])
    await call.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data == "admin:giveaway:create")
@router.callback_query(F.data == "admin:raffle:create")
async def giveaway_create_start(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.giveaway_title)
    await call.message.answer("Шаг 1/6. Введи название розыгрыша (3–100 символов).")
    await call.answer()


@router.message(AdminStates.giveaway_title, F.text)
async def giveaway_title(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    value = (message.text or "").strip()
    if not 3 <= len(value) <= 100:
        await message.answer("Название должно содержать 3–100 символов.")
        return
    await state.update_data(giveaway_title=value)
    await state.set_state(AdminStates.giveaway_prize)
    await message.answer("Шаг 2/6. Укажи приз.")


@router.message(AdminStates.giveaway_prize, F.text)
async def giveaway_prize(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not 1 <= len(value) <= 200:
        await message.answer("Описание приза должно содержать до 200 символов.")
        return
    await state.update_data(giveaway_prize=value)
    await state.set_state(AdminStates.giveaway_description)
    await message.answer("Шаг 3/6. Отправь описание и условия розыгрыша.")


@router.message(AdminStates.giveaway_description, F.text)
async def giveaway_description(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if len(value) > 1500:
        await message.answer("Описание слишком длинное. Максимум 1500 символов.")
        return
    await state.update_data(giveaway_description=value)
    await state.set_state(AdminStates.giveaway_sponsors)
    await message.answer("Шаг 4/8. Введи ID или @username каналов-спонсоров через запятую. Если проверка подписки не нужна, отправь дефис.")


@router.message(AdminStates.giveaway_sponsors, F.text)
async def giveaway_sponsors(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    channels = [] if raw == "-" else [item.strip() for item in raw.split(",") if item.strip()]
    if len(channels) > 10:
        await message.answer("Можно указать не более 10 каналов.")
        return
    await state.update_data(giveaway_sponsors=channels)
    await state.set_state(AdminStates.giveaway_min_referrals)
    await message.answer("Шаг 5/8. Минимальное число приглашённых друзей для участия (0 — без ограничения).")


@router.message(AdminStates.giveaway_min_referrals, F.text)
async def giveaway_min_referrals(message: Message, state: FSMContext) -> None:
    try:
        value = int((message.text or "").strip())
        if value < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи целое число от 0.")
        return
    await state.update_data(giveaway_min_referrals=value)
    await state.set_state(AdminStates.giveaway_min_balance)
    await message.answer("Шаг 6/8. Минимальный баланс в рублях (0 — без ограничения).")


@router.message(AdminStates.giveaway_min_balance, F.text)
async def giveaway_min_balance(message: Message, state: FSMContext) -> None:
    try:
        value = float((message.text or "").replace(",", "."))
        if value < 0:
            raise ValueError
    except ValueError:
        await message.answer("Введи неотрицательную сумму.")
        return
    await state.update_data(giveaway_min_balance=value)
    await state.set_state(AdminStates.giveaway_end)
    await message.answer("Шаг 7/8. Укажи дату окончания в UTC: <code>ГГГГ-ММ-ДД ЧЧ:ММ</code>.")


@router.message(AdminStates.giveaway_end, F.text)
async def giveaway_end(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        from datetime import datetime, timezone
        end = datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        if end <= utcnow() + timedelta(minutes=2):
            raise ValueError
    except ValueError:
        await message.answer("Дата должна быть минимум на 2 минуты в будущем. Пример: <code>2026-12-01 20:00</code>.")
        return
    await state.update_data(giveaway_end=dt_to_db(end))
    await state.set_state(AdminStates.giveaway_winners)
    await message.answer("Шаг 8/8. Сколько победителей выбрать? Введи число от 1 до 20.")


@router.message(AdminStates.giveaway_winners, F.text)
async def giveaway_winners(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    try:
        winners = int((message.text or "").strip())
        if not 1 <= winners <= 20:
            raise ValueError
    except ValueError:
        await message.answer("Количество победителей должно быть от 1 до 20.")
        return
    data = await state.get_data()
    giveaway_id = await execute(
        "INSERT INTO giveaways(title,prize,description,sponsor_channels,ends_at,winners_count,min_referrals,min_balance,status,created_by,created_at) VALUES(?,?,?,?,?,?,?,?, 'active',?,?)",
        (data["giveaway_title"], data["giveaway_prize"], data["giveaway_description"], json.dumps(data["giveaway_sponsors"], ensure_ascii=False), data["giveaway_end"], winners, int(data.get("giveaway_min_referrals", 0)), float(data.get("giveaway_min_balance", 0)), message.from_user.id, dt_to_db()),
    )
    await state.clear()
    await audit(message.from_user.id, "giveaway_created", f"id={giveaway_id}; title={data['giveaway_title']}")
    await message.answer(
        f"✅ <b>Розыгрыш #{giveaway_id} создан!</b>\n\n{safe_html(data['giveaway_title'])}\nПриз: <b>{safe_html(data['giveaway_prize'])}</b>\nИтоги: {format_datetime(data['giveaway_end'], True)}\nПобедителей: {winners}",
        reply_markup=admin_keyboard(),
    )


@router.callback_query(F.data.startswith("admin:raffle:view:"))
async def admin_giveaway_view(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    try:
        giveaway_id = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        await call.answer("Некорректный ID.", show_alert=True)
        return
    item = await fetchone("SELECT * FROM giveaways WHERE id=?", (giveaway_id,))
    if not item:
        await call.answer("Розыгрыш не найден.", show_alert=True)
        return
    participants = await fetchone("SELECT COUNT(*) AS n FROM giveaway_participants WHERE giveaway_id=?", (giveaway_id,))
    winners = await fetchall("SELECT user_id,place FROM giveaway_winners WHERE giveaway_id=? ORDER BY place", (giveaway_id,))
    lines = [
        f"🎁 <b>Розыгрыш #{giveaway_id}</b>",
        f"Название: {safe_html(item['title'])}",
        f"Приз: {safe_html(item['prize'])}",
        f"Описание: {safe_html(item['description'])}",
        f"Участников: <b>{participants['n'] if participants else 0}</b>",
        f"Победителей: <b>{item['winners_count']}</b>",
        f"Окончание: {format_datetime(item['ends_at'], True)}",
        f"Статус: <b>{'Активен' if item['status'] == 'active' else 'Завершён'}</b>",
    ]
    if winners:
        lines.append("\n🏆 <b>Победители:</b>")
        lines.extend(f"{row['place']}. <code>{row['user_id']}</code>" for row in winners)
    rows = []
    if item["status"] == "active":
        rows.append([cb("🏁 Подвести итоги сейчас", f"admin:giveaway:draw:{giveaway_id}")])
    elif item["status"] == "completed":
        rows.append([cb("🔄 Перевыбрать победителей", f"admin:giveaway:reroll:{giveaway_id}")])
    rows.extend([[cb("⬅️ К розыгрышам", "admin:giveaways")], [cb("⬅️ В админку", "admin:home")]])
    await call.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data == "admin:tasks")
async def admin_task_list(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    tasks = await fetchall("SELECT id,title,task_type,reward_points,active FROM quests_tasks ORDER BY id DESC LIMIT 20")
    pending = await fetchall("SELECT qc.task_id,qc.user_id,qt.title FROM quest_completions qc JOIN quests_tasks qt ON qt.id=qc.task_id WHERE qc.status='pending' ORDER BY qc.id DESC LIMIT 10")
    lines = ["<b>🎯 Управление заданиями</b>\n"]
    rows = [[cb("➕ Добавить задание", "admin:task:create")]]
    for task in tasks:
        task_label = {"channel_sub": "подписка на канал", "referrals": "приглашение друзей", "manual": "ручная проверка"}.get(task["task_type"], "задание")
        lines.append(f"{'🟢' if task['active'] else '⚫'} #{task['id']} {safe_html(task['title'])} — {task_label}, {task['reward_points']} FP")
        rows.append([cb(f"Вкл/выкл #{task['id']}", f"admin:task:toggle:{task['id']}")])
    if pending:
        lines.append("\n<b>Ожидают проверки:</b>")
        for item in pending:
            lines.append(f"#{item['task_id']} · пользователь <code>{item['user_id']}</code> · {safe_html(item['title'])}")
            rows.append([cb(f"Проверить #{item['task_id']} / {item['user_id']}", f"admin:task:review:{item['task_id']}:{item['user_id']}")])
    rows.append([cb("⬅️ Назад", "admin:home")])
    await call.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data == "admin:task:create")
async def task_create_start(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    await state.set_state(AdminStates.task_title)
    await call.message.answer("Введи название задания.")
    await call.answer()


@router.message(AdminStates.task_title, F.text)
async def task_title(message: Message, state: FSMContext) -> None:
    title = (message.text or "").strip()
    if not 3 <= len(title) <= 100:
        await message.answer("Название должно содержать 3–100 символов.")
        return
    await state.update_data(task_title=title)
    await state.set_state(AdminStates.task_description)
    await message.answer("Отправь описание и инструкцию к заданию.")


@router.message(AdminStates.task_description, F.text)
async def task_description(message: Message, state: FSMContext) -> None:
    description = (message.text or "").strip()
    if not description or len(description) > 1000:
        await message.answer("Описание должно содержать 1–1000 символов.")
        return
    await state.update_data(task_description=description)
    await state.set_state(AdminStates.task_type)
    await message.answer("Выбери тип задания:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [cb("Подписка на канал", "admin:task:type:channel_sub")],
        [cb("Пригласить друзей", "admin:task:type:referrals")],
        [cb("Ручная проверка", "admin:task:type:manual")],
    ]))


@router.callback_query(F.data.startswith("admin:task:type:"))
async def task_type_select(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    kind = call.data.rsplit(":", 1)[1]
    if kind not in {"channel_sub", "referrals", "manual"}:
        await call.answer("Тип задания не найден.", show_alert=True)
        return
    await state.update_data(task_type=kind)
    if kind == "channel_sub":
        await state.set_state(AdminStates.task_channel)
        await call.message.answer("Отправь ID или @username канала. Бот должен иметь возможность проверять подписчиков.")
    elif kind == "referrals":
        await state.set_state(AdminStates.task_channel)
        await call.message.answer("Сколько приглашённых пользователей требуется? Введи целое число.")
    else:
        await state.set_state(AdminStates.task_reward)
        await call.message.answer("Сколько FP-коинов начислять за одобренное выполнение?")
    await call.answer()


@router.message(AdminStates.task_channel, F.text)
async def task_channel_or_count(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    value = (message.text or "").strip()
    if data["task_type"] == "channel_sub":
        if not value:
            await message.answer("Укажи ID или @username канала.")
            return
        await state.update_data(task_channel=value)
    else:
        try:
            count = int(value)
            if count <= 0:
                raise ValueError
        except ValueError:
            await message.answer("Введи целое число больше нуля.")
            return
        await state.update_data(task_required_count=count)
    await state.set_state(AdminStates.task_reward)
    await message.answer("Укажи награду в FP-коинах (целое число от 0 и выше).")


@router.message(AdminStates.task_reward, F.text)
async def task_reward_save(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    try:
        reward = int((message.text or "").strip())
        if reward < 0:
            raise ValueError
    except ValueError:
        await message.answer("Награда должна быть целым числом от 0.")
        return
    data = await state.get_data()
    task_type = data["task_type"]
    channel_id = data.get("task_channel") if task_type == "channel_sub" else None
    required = int(data.get("task_required_count", 1)) if task_type == "referrals" else 1
    task_id = await execute(
        "INSERT INTO quests_tasks(title,description,task_type,channel_id,required_count,reward_points,active,created_at) VALUES(?,?,?,?,?,?,1,?)",
        (data["task_title"], data["task_description"], task_type, channel_id, required, reward, dt_to_db()),
    )
    await state.clear()
    await audit(message.from_user.id, "quest_created", f"id={task_id}; type={task_type}")
    await message.answer(f"✅ Задание #{task_id} создано.", reply_markup=admin_keyboard())


@router.callback_query(F.data.startswith("admin:task:toggle:"))
async def task_toggle(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    task_id = int(call.data.rsplit(":", 1)[1])
    row = await fetchone("SELECT active FROM quests_tasks WHERE id=?", (task_id,))
    if not row:
        await call.answer("Задание не найдено.", show_alert=True)
        return
    await execute("UPDATE quests_tasks SET active=? WHERE id=?", (0 if row["active"] else 1, task_id))
    await admin_task_list(call)


@router.callback_query(F.data.startswith("admin:task:review:"))
async def task_review_open(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    try:
        _, _, _, task_id, user_id = call.data.split(":")
    except ValueError:
        await call.answer("Некорректная заявка.", show_alert=True)
        return
    completion = await fetchone("SELECT proof_text,proof_file_id,proof_file_type FROM quest_completions WHERE task_id=? AND user_id=?", (task_id, user_id))
    task = await fetchone("SELECT title FROM quests_tasks WHERE id=?", (task_id,))
    caption = (
        f"🎯 <b>Проверка задания #{task_id}</b>\n"
        f"Задание: {safe_html(task['title'] if task else 'Без названия')}\n"
        f"Пользователь: <code>{user_id}</code>\n\n"
        f"{safe_html(completion['proof_text'] if completion else '')}"
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [cb("✅ Одобрить", f"task:approve:{task_id}:{user_id}"), cb("❌ Отклонить", f"task:reject:{task_id}:{user_id}")]
    ])
    if completion and completion["proof_file_type"] == "photo":
        await call.message.answer_photo(completion["proof_file_id"], caption=caption[:900], reply_markup=markup)
    elif completion and completion["proof_file_type"] == "document":
        await call.message.answer_document(completion["proof_file_id"], caption=caption[:900], reply_markup=markup)
    else:
        await call.message.answer(caption, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data == "admin:tickets")
@router.callback_query(F.data == "admin:support")
async def admin_tickets(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    tickets = await fetchall("SELECT id,user_id,category,message,created_at FROM tickets_suggestions WHERE status='open' ORDER BY id DESC LIMIT 15")
    rows = []
    lines = ["<b>🆘 Открытые обращения</b>\n"]
    if not tickets:
        lines.append("Открытых обращений нет.")
    for ticket in tickets:
        label = "Предложение" if ticket["category"] == "suggestion" else "Поддержка"
        lines.append(f"<b>#{ticket['id']} · {label}</b> от <code>{ticket['user_id']}</code>\n{safe_html(ticket['message'][:180])}")
        rows.append([cb(f"✉️ Ответить #{ticket['id']}", f"ticket:reply:{ticket['id']}")])
    rows.append([cb("⬅️ Назад", "admin:home")])
    await call.message.answer("\n\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("ticket:reply:"))
@router.callback_query(F.data.startswith("support:reply:"))
async def ticket_reply_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    ticket_id = int(call.data.rsplit(":", 1)[1])
    ticket = await fetchone("SELECT id FROM tickets_suggestions WHERE id=?", (ticket_id,))
    if not ticket:
        await call.answer("Обращение не найдено.", show_alert=True)
        return
    await state.set_state(AdminStates.ticket_reply)
    await state.update_data(ticket_id=ticket_id)
    await call.message.answer(f"Отправь ответ для обращения #{ticket_id}. Он будет доставлен пользователю в личные сообщения.")
    await call.answer()


@router.message(AdminStates.ticket_reply, F.text)
async def ticket_reply_send(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    ticket_id = int(data["ticket_id"])
    ticket = await fetchone("SELECT user_id FROM tickets_suggestions WHERE id=?", (ticket_id,))
    await state.clear()
    if not ticket:
        await message.answer("Обращение не найдено.")
        return
    try:
        await message.bot.send_message(ticket["user_id"], f"🆘 <b>Ответ по обращению #{ticket_id}</b>\n\n{safe_html(message.text or '')}")
        await execute("UPDATE tickets_suggestions SET status='answered',answered_at=?,answered_by=? WHERE id=?", (dt_to_db(), message.from_user.id, ticket_id))
        await audit(message.from_user.id, "ticket_answered", str(ticket_id))
        await message.answer("✅ Ответ отправлен пользователю.")
    except Exception:
        logger.exception("Ticket reply failed")
        await message.answer("Не удалось отправить ответ пользователю.")


@router.callback_query(F.data == "admin:ads")
async def admin_ads(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    items = await fetchall("SELECT id,user_id,format,desired_date,brief FROM ad_requests WHERE status='pending' ORDER BY id DESC LIMIT 15")
    lines = ["<b>💎 Заявки на рекламу</b>\n"]
    rows = []
    if not items:
        lines.append("Новых заявок нет.")
    for item in items:
        lines.append(f"<b>#{item['id']} · {safe_html(item['format'])}</b> от <code>{item['user_id']}</code>\nДата: {safe_html(item['desired_date'])}\n{safe_html(item['brief'][:180])}")
        rows.append([cb(f"Открыть #{item['id']}", f"admin:ad:view:{item['id']}")])
    rows.append([cb("⬅️ Назад", "admin:home")])
    await call.message.answer("\n\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("admin:ad:view:"))
async def admin_ad_view(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    ad_id = int(call.data.rsplit(":", 1)[1])
    item = await fetchone("SELECT * FROM ad_requests WHERE id=?", (ad_id,))
    if not item:
        await call.answer("Заявка не найдена.", show_alert=True)
        return
    text = f"💎 <b>Заявка #{ad_id}</b>\nПользователь: <code>{item['user_id']}</code>\nФормат: {safe_html(item['format'])}\nДата: {safe_html(item['desired_date'])}\n\n{safe_html(item['brief'])}"
    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [cb("✅ Принять", f"ad:approve:{ad_id}"), cb("❌ Отклонить", f"ad:reject:{ad_id}")],
        [cb("💬 Ответить", f"ad:reply:{ad_id}")],
        [cb("⬅️ Назад", "admin:ads")],
    ]))
    await call.answer()


@router.callback_query(F.data.startswith("ad:approve:") | F.data.startswith("ad:reject:"))
async def review_ad_request(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    action, ad_id_raw = call.data.split(":")[1:]
    ad_id = int(ad_id_raw)
    item = await fetchone("SELECT user_id,status FROM ad_requests WHERE id=?", (ad_id,))
    if not item or item["status"] != "pending":
        await call.answer("Заявка уже обработана.", show_alert=True)
        return
    status = "accepted" if action == "approve" else "rejected"
    await execute("UPDATE ad_requests SET status=?,reviewed_at=?,reviewed_by=? WHERE id=?", (status, dt_to_db(), call.from_user.id, ad_id))
    notice = "✅ Заявка на рекламу принята. Администратор свяжется с тобой для согласования деталей." if status == "accepted" else "❌ Заявка на рекламу отклонена. Если есть вопросы, напиши в поддержку."
    try:
        await call.bot.send_message(item["user_id"], notice)
    except Exception:
        logger.exception("Could not notify ad requester")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Статус заявки обновлён.")


@router.callback_query(F.data.startswith("ad:reply:"))
async def ad_reply_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    ad_id = int(call.data.rsplit(":", 1)[1])
    await state.set_state(AdminStates.ad_reply)
    await state.update_data(ad_id=ad_id)
    await call.message.answer(f"Отправь сообщение рекламодателю по заявке #{ad_id}.")
    await call.answer()


@router.message(AdminStates.ad_reply, F.text)
async def ad_reply_send(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    ad_id = int(data["ad_id"])
    item = await fetchone("SELECT user_id FROM ad_requests WHERE id=?", (ad_id,))
    await state.clear()
    if not item:
        await message.answer("Заявка не найдена.")
        return
    try:
        await message.bot.send_message(item["user_id"], f"💎 <b>Сообщение по рекламной заявке #{ad_id}</b>\n\n{safe_html(message.text or '')}")
        await message.answer("✅ Сообщение отправлено.")
    except Exception:
        logger.exception("Could not send ad reply")
        await message.answer("Не удалось отправить сообщение.")


@router.callback_query(F.data == "admin:orders")
async def admin_orders(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    orders = await fetchall("SELECT id,user_id,tariff_title,amount,status,created_at FROM orders WHERE status IN ('pending_review','awaiting_receipt','pending_crypto') ORDER BY id DESC LIMIT 20")
    lines = ["<b>💳 Заказы, ожидающие оплаты или проверки</b>\n"]
    rows = []
    if not orders:
        lines.append("Ожидающих заказов нет.")
    for order in orders:
        status_label = {"pending_review": "ожидает проверки чека", "awaiting_receipt": "ожидает чек", "pending_crypto": "ожидает оплату CryptoBot"}.get(order["status"], "в обработке")
        lines.append(f"#{order['id']} · <code>{order['user_id']}</code> · {safe_html(order['tariff_title'])} · {order['amount']:g} · {status_label}")
        if order["status"] == "pending_review":
            rows.append([cb(f"✅ Подтвердить #{order['id']}", f"order:approve:{order['id']}"), cb("❌ Отклонить", f"order:reject:{order['id']}")])
    rows.append([cb("⬅️ Назад", "admin:home")])
    await call.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data == "admin:buttons")
async def admin_buttons(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    items = await fetchall("SELECT button_id,title FROM buttons ORDER BY button_id")
    rows = []
    lines = ["<b>🔘 Названия кнопок</b>\n\nВыбери кнопку, название которой нужно изменить:"]
    for item in items:
        lines.append(f"• <code>{safe_html(item['button_id'])}</code> — {safe_html(item['title'])}")
        rows.append([cb(f"✏️ {item['title'][:35]}", f"admin:button:{item['button_id']}")])
    rows.append([cb("⬅️ Назад", "admin:home")])
    await call.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("admin:button:"))
async def admin_button_edit(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    key = call.data.split(":", 2)[2]
    row = await fetchone("SELECT title FROM buttons WHERE button_id=?", (key,))
    if not row:
        await call.answer("Кнопка не найдена.", show_alert=True)
        return
    await state.set_state(AdminStates.button_edit)
    await state.update_data(button_key=key)
    await call.message.answer(f"Отправь новое название для <code>{safe_html(key)}</code>. Текущее: {safe_html(row['title'])}")
    await call.answer()


@router.message(AdminStates.button_edit, F.text)
async def admin_button_save(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    title = (message.text or "").strip()
    if not 1 <= len(title) <= 60:
        await message.answer("Название кнопки должно содержать от 1 до 60 символов.")
        return
    await execute("UPDATE buttons SET title=? WHERE button_id=?", (title, data["button_key"]))
    await audit(message.from_user.id, "button_title_changed", data["button_key"])
    await state.clear()
    await message.answer("✅ Название кнопки обновлено.", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:game_contents")
async def admin_game_contents(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    defaults = [("roblox", "Roblox"), ("standoff", "Standoff 2"), ("brawl", "Brawl Stars"), ("pubg", "PUBG Mobile"), ("clash", "Clash of Clans")]
    rows = [[cb(f"✏️ {title}", f"admin:game:{key}")] for key, title in defaults]
    rows.append([cb("⬅️ Назад", "admin:home")])
    await call.message.answer("🎮 <b>Редактирование игровых разделов</b>\n\nВыбери раздел. Отправь новый текст в формате <code>Заголовок | Полное описание</code>.", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("admin:game:"))
async def admin_game_edit(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    key = call.data.split(":", 2)[2]
    if key not in {"roblox", "standoff", "brawl", "pubg", "clash"}:
        await call.answer("Раздел не найден.", show_alert=True)
        return
    row = await fetchone("SELECT title,content FROM game_contents WHERE game_key=?", (key,))
    current = f"\n\nТекущий заголовок: {row['title']}\nТекущий текст: {row['content'][:700]}" if row else "\n\nСейчас текст не заполнен."
    await state.set_state(AdminStates.game_content_edit)
    await state.update_data(game_key=key)
    await call.message.answer(f"Раздел <code>{key}</code>. Отправь <code>Заголовок | Полное описание</code>.{current}")
    await call.answer()


@router.message(AdminStates.game_content_edit, F.text)
async def admin_game_save(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    raw = (message.text or "").strip()
    if "|" not in raw:
        await message.answer("Формат: <code>Заголовок | Полное описание</code>")
        return
    title, content = [part.strip() for part in raw.split("|", 1)]
    if not title or not content or len(title) > 100 or len(content) > 3000:
        await message.answer("Проверь данные: заголовок до 100 символов, описание до 3000.")
        return
    await execute("INSERT INTO game_contents(game_key,title,content) VALUES(?,?,?) ON CONFLICT(game_key) DO UPDATE SET title=excluded.title,content=excluded.content", (data["game_key"], title, content))
    await audit(message.from_user.id, "game_content_changed", data["game_key"])
    await state.clear()
    await message.answer("✅ Игровой раздел обновлён.", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:raffle_stats")
async def admin_raffle_stats(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    total = await fetchone("SELECT COUNT(*) AS n FROM giveaways")
    active = await fetchone("SELECT COUNT(*) AS n FROM giveaways WHERE status='active'")
    completed = await fetchone("SELECT COUNT(*) AS n FROM giveaways WHERE status='completed'")
    participants = await fetchone("SELECT COUNT(*) AS n FROM giveaway_participants")
    winners = await fetchone("SELECT COUNT(*) AS n FROM giveaway_winners")
    await call.message.answer(
        "<b>📈 Статистика розыгрышей</b>\n\n"
        f"Всего розыгрышей: <b>{total['n']}</b>\nАктивных: <b>{active['n']}</b>\nЗавершённых: <b>{completed['n']}</b>\n"
        f"Всего участий: <b>{participants['n']}</b>\nВыдано призовых мест: <b>{winners['n']}</b>",
        reply_markup=back_keyboard("admin:home"),
    )
    await call.answer()


@router.callback_query(F.data == "admin:settings")
@router.callback_query(F.data == "admin:payment")
async def settings_menu(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    keys = [
        ("main_channel_id", "ID / @username основного канала"),
        ("main_channel_url", "Ссылка на основной канал"),
        ("project_name", "Название проекта"),
        ("private_channel_id", "ID закрытого канала"),
        ("support_chat_id", "ID чата поддержки"),
        ("support_text", "Дополнительный текст поддержки"),
        ("ads_url", "Ссылка по рекламе"),
        ("payment_details", "Реквизиты оплаты"),
        ("ad_price_1_24", "Цена рекламы 1/24"),
        ("ad_price_2_48", "Цена рекламы 2/48"),
        ("ad_price_pin", "Цена рекламы с закрепом"),
        ("ad_price_mailing", "Цена рассылки"),
        ("currency", "Валюта"),
    ]
    lines = ["<b>⚙️ Настройки бота</b>\n"]
    rows = []
    for key, label in keys:
        value = await get_setting(key, "")
        lines.append(f"{label}: <code>{safe_html(value or 'не задано')[:120]}</code>")
        rows.append([cb(f"✏️ {label}", f"admin:setting:{key}")])
    required = await get_setting("require_subscription", "1" if settings.subscription_required else "0")
    rows.append([cb(f"Обязательная подписка: {'ВКЛ' if required == '1' else 'ВЫКЛ'}", "admin:settings:toggle_sub")])
    rows.append([cb("🛠 Техобслуживание", "admin:settings:maintenance")])
    rows.append([cb("⬅️ Назад", "admin:home")])
    await call.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data == "admin:settings:toggle_sub")
async def toggle_subscription_requirement(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    current = await get_setting("require_subscription", "1" if settings.subscription_required else "0")
    new = "0" if current == "1" else "1"
    await set_setting("require_subscription", new)
    await audit(call.from_user.id, "subscription_requirement_toggled", new)
    await settings_menu(call)


@router.callback_query(F.data == "admin:settings:maintenance")
@router.callback_query(F.data == "admin:maintenance")
async def toggle_maintenance(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    current = await get_setting("maintenance", "0")
    new = "0" if current == "1" else "1"
    await set_setting("maintenance", new)
    await audit(call.from_user.id, "maintenance_toggled", new)
    await settings_menu(call)


@router.callback_query(F.data.startswith("admin:setting:"))
async def setting_edit_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    key = call.data.split(":", 2)[2]
    key = {"channel_url": "main_channel_url", "channel_chat_id": "main_channel_id"}.get(key, key)
    allowed = {"main_channel_id", "main_channel_url", "private_channel_id", "support_chat_id", "support_text", "ads_url", "payment_details", "ad_price_1_24", "ad_price_2_48", "ad_price_pin", "ad_price_mailing", "currency", "project_name"}
    if key not in allowed:
        await call.answer("Настройка не найдена.", show_alert=True)
        return
    await state.set_state(AdminStates.edit_setting)
    await state.update_data(setting_key=key)
    current = await get_setting(key, "")
    await call.message.answer(f"Отправь новое значение для <code>{key}</code>.\nТекущее значение: <code>{safe_html(current or 'не задано')}</code>\nДля очистки отправь дефис <code>-</code>.")
    await call.answer()


@router.message(AdminStates.edit_setting, F.text)
async def setting_edit_save(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    value = (message.text or "").strip()
    if value == "-":
        value = ""
    if len(value) > 1000:
        await message.answer("Значение слишком длинное. Максимум 1000 символов.")
        return
    await set_setting(data["setting_key"], value)
    await audit(message.from_user.id, "setting_changed", data["setting_key"])
    await state.clear()
    await message.answer("✅ Настройка сохранена.", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:texts")
async def texts_menu(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    keys = [
        ("welcome", "Приветствие"), ("about", "Описание привата"), ("faq", "FAQ"),
        ("paid_locked", "Закрытый доступ"), ("paid_open", "Активный доступ"),
        ("free", "Бесплатное меню"), ("daily_empty", "Пустой список публикаций"),
        ("games", "Описание игр"), ("safety", "Безопасность"),
        ("payment_intro", "Вводный текст оплаты"), ("promo_prompt", "Запрос промокода"),
        ("support_prompt", "Текст поддержки"), ("support_received", "Подтверждение обращения"),
        ("referral", "Реферальный текст"), ("payment_instructions", "Инструкция оплаты"),
        ("private_page_0", "Приват: о разделе"), ("private_page_1", "Приват: материалы"),
        ("private_page_2", "Приват: софт и боты"), ("private_page_3", "Приват: чат"),
        ("private_page_4", "Приват: отзывы"),
    ]
    rows = [[cb(label, f"admin:text:{key}")] for key, label in keys]
    rows.append([cb("⬅️ Назад", "admin:home")])
    await call.message.answer("📝 <b>Редактирование текстов</b>\n\nВыбери текст, который нужно изменить:", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("admin:text:"))
async def text_edit_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    key = call.data.split(":", 2)[2]
    allowed = {"welcome", "about", "faq", "paid_locked", "paid_open", "free", "daily_empty", "games", "safety", "payment_intro", "promo_prompt", "support_prompt", "support_received", "referral", "payment_instructions", "private_page_0", "private_page_1", "private_page_2", "private_page_3", "private_page_4"}
    if key not in allowed:
        await call.answer("Текст не найден.", show_alert=True)
        return
    current = await get_text(key, "")
    await state.set_state(AdminStates.edit_text)
    await state.update_data(text_key=key)
    await call.message.answer(f"Отправь новый текст для <code>{key}</code>. HTML-разметка Telegram поддерживается.\n\nТекущий текст:\n{current[:1200]}")
    await call.answer()


@router.message(AdminStates.edit_text, F.text)
async def text_edit_save(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    body = (message.text or "").strip()
    if not body or len(body) > 3500:
        await message.answer("Текст должен содержать от 1 до 3500 символов.")
        return
    if data.get("text_key") == "__new_post__":
        if "|" not in body:
            await message.answer("Формат: <code>Заголовок | Полный текст</code>")
            return
        title, content = [part.strip() for part in body.split("|", 1)]
        if not title or not content:
            await message.answer("Заголовок и текст не должны быть пустыми.")
            return
        await execute("INSERT INTO daily_posts(title,content,active,created_at) VALUES(?,?,1,?)", (title[:150], content[:3500], dt_to_db()))
        await audit(message.from_user.id, "daily_post_created", title)
        await state.clear()
        await message.answer("✅ Публикация добавлена.", reply_markup=admin_keyboard())
        return
    await set_text(data["text_key"], body)
    await audit(message.from_user.id, "text_changed", data["text_key"])
    await state.clear()
    await message.answer("✅ Текст сохранён.", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:channel_post")
async def channel_post_prompt(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    channel_id = (await get_setting("main_channel_id", settings.main_channel_id)).strip()
    if not channel_id:
        await call.answer("Сначала укажи ID канала в настройках.", show_alert=True)
        return
    await state.set_state(AdminStates.channel_post)
    await call.message.answer("📣 Отправь сообщение для публикации в основном канале. Поддерживаются текст, фото, видео, GIF и документы. Для отмены — /cancel.")
    await call.answer()


@router.message(AdminStates.channel_post)
async def channel_post_publish(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    target = (await get_setting("main_channel_id", settings.main_channel_id)).strip()
    if not target:
        await message.answer("ID канала не настроен. Открой настройки и укажи его.")
        return
    try:
        await message.bot.copy_message(chat_id=target, from_chat_id=message.chat.id, message_id=message.message_id)
    except Exception:
        logger.exception("Channel post failed")
        await message.answer("Не удалось опубликовать сообщение. Проверь ID канала и права бота.")
        return
    await state.clear()
    await audit(message.from_user.id, "channel_post_published", f"channel={target}")
    await message.answer("✅ Сообщение опубликовано в канале.", reply_markup=admin_keyboard())


@router.callback_query(F.data == "admin:broadcast")
async def broadcast_start(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    await call.message.answer("📢 <b>Настройка рассылки</b>\n\nВыбери аудиторию:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [cb("👥 Все пользователи", "broadcast:aud:all")],
        [cb("💎 Только VIP", "broadcast:aud:vip")],
        [cb("👤 Без VIP", "broadcast:aud:nonvip")],
        [cb("🟢 Активные за 7 дней", "broadcast:aud:active7")],
        [cb("⬅️ Назад", "admin:home")],
    ]))
    await call.answer()


@router.callback_query(F.data.startswith("broadcast:aud:"))
async def broadcast_audience(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    audience = call.data.rsplit(":", 1)[1]
    if audience not in {"all", "vip", "nonvip", "active7"}:
        await call.answer("Аудитория не найдена.", show_alert=True)
        return
    await state.set_state(AdminStates.broadcast_content)
    await state.update_data(broadcast_audience=audience)
    await call.message.answer("Отправь сообщение для рассылки. Поддерживаются текст, фото, видео, GIF и документы. Можно добавить подпись.\n\nДля отмены используй /cancel.")
    await call.answer()


@router.message(AdminStates.broadcast_content)
async def broadcast_content_received(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    if not any([message.text, message.photo, message.video, message.animation, message.document]):
        await message.answer("Отправь текст, фото, видео, GIF или документ.")
        return
    await state.update_data(source_chat_id=message.chat.id, source_message_id=message.message_id)
    await state.set_state(AdminStates.broadcast_button)
    await message.answer("Добавить к рассылке URL-кнопку? Отправь в формате <code>Текст кнопки | https://ссылка</code> или дефис <code>-</code> без кнопки.")


@router.message(AdminStates.broadcast_button, F.text)
async def broadcast_button_received(message: Message, state: FSMContext) -> None:
    if not _admin_only(message.from_user.id):
        await state.clear()
        return
    raw = (message.text or "").strip()
    label = link = ""
    if raw != "-":
        if "|" not in raw:
            await message.answer("Формат: <code>Текст кнопки | https://ссылка</code> или дефис.")
            return
        label, link = [part.strip() for part in raw.split("|", 1)]
        if not label or len(label) > 50 or not valid_url(link):
            await message.answer("Проверь текст кнопки и ссылку. Допустима ссылка http:// или https://.")
            return
    await state.update_data(broadcast_button_label=label, broadcast_button_url=link)
    await state.set_state(AdminStates.broadcast_confirm)
    data = await state.get_data()
    rows = []
    if link:
        rows.append([InlineKeyboardButton(text=label, url=link)])
    rows.append([cb("🚀 Запустить рассылку", "broadcast:confirm"), cb("❌ Отмена", "broadcast:cancel")])
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    await message.answer("<b>Предпросмотр рассылки</b>\n\nАудитория: <b>" + {
        "all": "Все пользователи", "vip": "Только VIP", "nonvip": "Без VIP", "active7": "Активные за 7 дней"
    }.get(data["broadcast_audience"], "—") + "</b>\n\nНиже копия сообщения. Проверь содержимое и нажми «Запустить рассылку».")
    try:
        await message.bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=data["source_chat_id"],
            message_id=data["source_message_id"],
            reply_markup=markup,
        )
    except Exception:
        logger.exception("Broadcast preview failed")
        await message.answer("Не удалось показать предпросмотр. Отмени рассылку и отправь сообщение ещё раз.")


@router.callback_query(F.data == "broadcast:cancel")
async def broadcast_cancel(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    await state.clear()
    await call.message.answer("Рассылка отменена.", reply_markup=admin_keyboard())
    await call.answer()


@router.callback_query(F.data == "broadcast:confirm")
async def broadcast_confirm(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    data = await state.get_data()
    if not data.get("source_message_id") or not data.get("broadcast_audience"):
        await state.clear()
        await call.answer("Данные рассылки не найдены. Настрой её заново.", show_alert=True)
        return
    audience = data["broadcast_audience"]
    await call.answer("Рассылка запускается…")
    now = dt_to_db()
    if audience == "vip":
        users = await fetchall("SELECT user_id FROM users WHERE banned=0 AND (vip_forever=1 OR paid_until>?)", (now,))
    elif audience == "nonvip":
        users = await fetchall("SELECT user_id FROM users WHERE banned=0 AND vip_forever=0 AND (paid_until IS NULL OR paid_until<=?)", (now,))
    elif audience == "active7":
        users = await fetchall("SELECT user_id FROM users WHERE banned=0 AND last_active>=?", (dt_to_db(utcnow() - timedelta(days=7)),))
    else:
        users = await fetchall("SELECT user_id FROM users WHERE banned=0")
    await state.clear()
    source_chat_id = int(data["source_chat_id"])
    source_message_id = int(data["source_message_id"])
    label, link = data.get("broadcast_button_label", ""), data.get("broadcast_button_url", "")
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label, url=link)]]) if label and link else None
    total = len(users)
    status_message = await call.message.answer(f"📢 Рассылка запущена. Получателей: <b>{total}</b>\n{0}/{total}")
    sent = failed = 0
    for index, user in enumerate(users, start=1):
        target_id = int(user["user_id"])
        try:
            await call.bot.copy_message(
                chat_id=target_id,
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                reply_markup=markup,
            )
            sent += 1
        except TelegramRetryAfter as exc:
            import asyncio
            await asyncio.sleep(float(exc.retry_after) + 1)
            try:
                await call.bot.copy_message(chat_id=target_id, from_chat_id=source_chat_id, message_id=source_message_id, reply_markup=markup)
                sent += 1
            except Exception:
                failed += 1
        except TelegramForbiddenError:
            failed += 1
        except Exception:
            failed += 1
            logger.exception("Broadcast failed for user %s", target_id)
        if index % 20 == 0 or index == total:
            try:
                await status_message.edit_text(f"📢 <b>Рассылка выполняется</b>\n{progress_bar(index, total)} {index}/{total}\n{sent} доставлено · {failed} ошибок")
            except Exception:
                logger.debug("Could not update broadcast progress message", exc_info=True)
        import asyncio
        await asyncio.sleep(settings.broadcast_delay)
    await audit(call.from_user.id, "broadcast_finished", f"audience={audience}; sent={sent}; failed={failed}; total={total}")
    await status_message.edit_text(
        f"✅ <b>Рассылка завершена</b>\n\n"
        f"Аудитория: {audience}\nВсего: <b>{total}</b>\n"
        f"Доставлено: <b>{sent}</b>\nОшибок / заблокировали бота: <b>{failed}</b>"
    )


@router.callback_query(F.data == "admin:posts")
async def admin_posts(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    posts = await fetchall("SELECT id,title,active FROM daily_posts ORDER BY id DESC LIMIT 15")
    rows = [[cb("➕ Добавить публикацию", "admin:post:add")]]
    lines = ["<b>📚 Бесплатные публикации</b>\n"]
    for post in posts:
        lines.append(f"{'🟢' if post['active'] else '⚫'} #{post['id']} {safe_html(post['title'])}")
        rows.append([cb(f"Вкл/выкл #{post['id']}", f"admin:post:toggle:{post['id']}")])
    rows.append([cb("⬅️ Назад", "admin:home")])
    await call.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data == "admin:post:add")
@router.callback_query(F.data == "admin:post_add")
async def admin_post_add(call: CallbackQuery, state: FSMContext) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    await state.set_state(AdminStates.edit_text)
    await state.update_data(text_key="__new_post__")
    await call.message.answer("Отправь публикацию в формате <code>Заголовок | Полный текст</code>.")
    await call.answer()


@router.callback_query(F.data.startswith("admin:post:toggle:"))
@router.callback_query(F.data.regexp(r"^admin:post_toggle:\d+$"))
async def admin_post_toggle(call: CallbackQuery) -> None:
    if not _admin_only(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    post_id = int(call.data.rsplit(":", 1)[1])
    row = await fetchone("SELECT active FROM daily_posts WHERE id=?", (post_id,))
    if not row:
        await call.answer("Публикация не найдена.", show_alert=True)
        return
    await execute("UPDATE daily_posts SET active=? WHERE id=?", (0 if row["active"] else 1, post_id))
    await admin_posts(call)

