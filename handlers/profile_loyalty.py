"""User profile, wallet, referrals, daily rewards and quests."""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from config import is_admin, settings
from database.db import connection, execute, fetchall, fetchone, get_currency, log_transaction
from keyboards.inline import back_keyboard, cb, profile_keyboard
from middlewares.sub_check import is_subscribed
from services.access import get_notification_targets, has_vip
from services.referrals import reward_referrer
from states.user_states import QuestStates
from utils.helpers import dt_to_db, format_datetime, parse_datetime, safe_html, truncate, utcnow

router = Router(name="profile_loyalty")
logger = logging.getLogger(__name__)


@router.callback_query(F.data == "menu:profile")
async def show_profile(call: CallbackQuery) -> None:
    row = await fetchone("SELECT * FROM users WHERE user_id=?", (call.from_user.id,))
    currency = await get_currency()
    if not row:
        await call.answer("Профиль ещё не создан. Нажми /start.", show_alert=True)
        return
    if row["vip_forever"]:
        vip_text = "Навсегда"
    elif await has_vip(call.from_user.id):
        vip_text = f"Активен до {format_datetime(row['paid_until'], True)}"
    else:
        vip_text = "Не активен"
    ref_count = await fetchone("SELECT COUNT(*) AS n FROM referrals WHERE referrer_id=?", (call.from_user.id,))
    text = (
        "<b>👤 Личный кабинет</b>\n\n"
        f"🆔 Telegram ID: <code>{call.from_user.id}</code>\n"
        f"👤 Имя: {safe_html(row['first_name'])}\n"
        f"🔗 Username: {('@' + safe_html(row['username'])) if row['username'] else 'не указан'}\n"
        f"📅 Регистрация: {format_datetime(row['joined_at'])}\n"
        f"🔐 Статус ПРИВАТА: <b>{vip_text}</b>\n"
        f"💰 Баланс: <b>{float(row['balance']):.2f} {safe_html(currency)}</b>\n"
        f"🪙 FP-коины: <b>{int(row['points'])}</b>\n"
        f"🛒 Покупок: <b>{int(row['total_purchases'])}</b>\n"
        f"👥 Приглашено: <b>{int(ref_count['n']) if ref_count else 0}</b>"
    )
    await call.message.answer(text, reply_markup=await profile_keyboard())
    await call.answer()


@router.callback_query(F.data == "profile:balance")
async def show_balance(call: CallbackQuery) -> None:
    row = await fetchone("SELECT balance,points FROM users WHERE user_id=?", (call.from_user.id,))
    currency = await get_currency()
    balance = float(row["balance"]) if row else 0.0
    points = int(row["points"]) if row else 0
    transactions = await fetchall(
        "SELECT transaction_type,amount,currency,description,created_at FROM transactions_log WHERE user_id=? ORDER BY id DESC LIMIT 5",
        (call.from_user.id,),
    )
    lines = [
        "<b>💰 Мой баланс</b>",
        f"Рубли: <b>{balance:.2f} {safe_html(currency)}</b>",
        f"FP-коины: <b>{points}</b>",
        "",
        "<b>Последние операции:</b>",
    ]
    negative_types = {"purchase_balance", "balance_debit", "admin_balance_sub", "admin_points_sub"}
    for item in transactions:
        kind = item["transaction_type"]
        sign = "−" if kind in negative_types else ("•" if kind == "purchase" else "+")
        unit = "FP" if item["currency"] == "FP" else (currency if item["currency"] == "RUB" else item["currency"])
        lines.append(f"• {sign}{item['amount']:g} {safe_html(unit)} — {safe_html(item['description'])} ({format_datetime(item['created_at'])})")
    if not transactions:
        lines.append("Операций пока нет.")
    await call.message.answer("\n".join(lines), reply_markup=back_keyboard("menu:profile"))
    await call.answer()


@router.callback_query(F.data == "profile:referrals")
@router.callback_query(F.data == "menu:referral")
async def show_referrals(call: CallbackQuery) -> None:
    me = await call.bot.get_me()
    row = await fetchone("SELECT COUNT(*) AS total,SUM(rewarded) AS rewarded FROM referrals WHERE referrer_id=?", (call.from_user.id,))
    count = int(row["total"] or 0) if row else 0
    rewarded = int(row["rewarded"] or 0) if row else 0
    link = f"https://t.me/{me.username}?start=ref_{call.from_user.id}"
    text = (
        "<b>👥 Реферальная программа</b>\n\n"
        f"Твоя персональная ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: <b>{count}</b>\n"
        f"Подтверждено наградой: <b>{rewarded}</b>\n\n"
        f"За каждого приглашённого после проверки подписки начисляется <b>{settings.referral_bonus_points} FP-коинов</b>.\n"
        f"Дополнительно действует кэшбэк <b>{settings.referral_cashback_percent:g}%</b> с его покупок."
    )
    share_url = "https://t.me/share/url?" + urlencode({"url": link, "text": "Присоединяйся к PlayFunpay Team!"})
    from aiogram.types import InlineKeyboardButton
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=share_url)],
        [cb("🏆 Топ рефоводов", "profile:leaderboard")],
        [cb("⬅️ Назад", "menu:profile")],
    ])
    await call.message.answer(text, reply_markup=keyboard)
    await call.answer()


@router.callback_query(F.data == "profile:leaderboard")
async def referral_leaderboard(call: CallbackQuery) -> None:
    rows = await fetchall(
        "SELECT u.user_id,u.username,u.first_name,COUNT(r.referred_id) AS total "
        "FROM users u JOIN referrals r ON r.referrer_id=u.user_id "
        "GROUP BY u.user_id ORDER BY total DESC,u.user_id ASC LIMIT 10"
    )
    medals = ["🥇", "🥈", "🥉"]
    lines = ["<b>🏆 Топ-10 рефоводов</b>\n"]
    for index, row in enumerate(rows):
        name = "@" + safe_html(row["username"]) if row["username"] else safe_html(row["first_name"] or f"ID {row['user_id']}")
        medal = medals[index] if index < 3 else f"{index + 1}."
        lines.append(f"{medal} {name} — <b>{row['total']}</b>")
    if not rows:
        lines.append("Пока никто не попал в рейтинг. Стань первым!")
    await call.message.answer("\n".join(lines), reply_markup=back_keyboard("profile:referrals"))
    await call.answer()


@router.callback_query(F.data == "menu:daily")
async def daily_hub(call: CallbackQuery) -> None:
    await call.message.answer(
        "<b>⚡ Бонусы и задания</b>\n\nЗабирай ежедневную награду, выполняй задания и копи FP-коины.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [cb("🎁 Забрать ежедневный бонус", "profile:daily")],
            [cb("🎯 Посмотреть задания", "profile:tasks")],
            [cb("⬅️ Назад", "nav:home")],
        ]),
    )
    await call.answer()


@router.callback_query(F.data == "profile:daily")
async def claim_daily_bonus(call: CallbackQuery) -> None:
    user_id = call.from_user.id
    now = utcnow()
    async with connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT last_daily_at,daily_streak FROM users WHERE user_id=?", (user_id,)
        )).fetchone()
        if not row:
            await db.rollback()
            await call.answer("Профиль не найден. Нажми /start.", show_alert=True)
            return
        last = parse_datetime(row["last_daily_at"])
        if last and (now - last).total_seconds() < 86400:
            remaining = int(86400 - (now - last).total_seconds())
            hours, rem = divmod(remaining, 3600)
            minutes = rem // 60
            await db.rollback()
            await call.answer(f"Следующий бонус через {hours} ч. {minutes} мин.", show_alert=True)
            return
        streak = int(row["daily_streak"] or 0)
        if last and (now - last).total_seconds() >= 48 * 3600:
            streak = 0
        streak = min(streak + 1, 7)
        reward = settings.daily_bonus_base * streak
        if streak == 7:
            reward += settings.daily_bonus_base * 5
        await db.execute(
            "UPDATE users SET points=points+?,daily_streak=?,last_daily_at=? WHERE user_id=?",
            (reward, streak, dt_to_db(now), user_id),
        )
        await db.execute(
            "INSERT INTO transactions_log(user_id,transaction_type,amount,currency,description,created_at) VALUES(?,?,?,?,?,?)",
            (user_id, "daily_bonus", reward, "FP", f"Ежедневный бонус, серия {streak} дней", dt_to_db(now)),
        )
        await db.commit()
    await call.message.answer(
        f"🎉 <b>Бонус получен!</b>\n\nНачислено: <b>+{reward} FP-коинов</b>\nТекущая серия: <b>{streak}/7 дней</b>\n\n{'🎁 Недельный сундук открыт! Отличная серия!' if streak == 7 else 'Возвращайся завтра за следующей наградой.'}",
        reply_markup=back_keyboard("menu:daily"),
    )
    await call.answer("Бонус начислен!")


@router.callback_query(F.data == "profile:tasks")
async def show_tasks(call: CallbackQuery) -> None:
    tasks = await fetchall("SELECT * FROM quests_tasks WHERE active=1 ORDER BY id DESC LIMIT 20")
    rows = []
    lines = ["<b>🎯 Доступные задания</b>\n"]
    if not tasks:
        lines.append("Новых заданий пока нет. Загляни позже!")
    for task in tasks:
        completion = await fetchone(
            "SELECT status FROM quest_completions WHERE task_id=? AND user_id=?",
            (task["id"], call.from_user.id),
        )
        status = "✅ Выполнено" if completion and completion["status"] == "approved" else ("⏳ На проверке" if completion else "Доступно")
        lines.append(f"<b>#{task['id']} {safe_html(task['title'])}</b>\n{safe_html(task['description'])}\nНаграда: <b>{task['reward_points']} FP</b> · {status}")
        if not completion:
            rows.append([cb(f"🎯 Выполнить #{task['id']}", f"task:do:{task['id']}")])
    rows.append([cb("⬅️ Назад", "menu:daily")])
    await call.message.answer("\n\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("task:do:"))
async def complete_task(call: CallbackQuery, state: FSMContext) -> None:
    try:
        task_id = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        await call.answer("Задание не найдено.", show_alert=True)
        return
    task = await fetchone("SELECT * FROM quests_tasks WHERE id=? AND active=1", (task_id,))
    if not task:
        await call.answer("Задание уже недоступно.", show_alert=True)
        return
    existing = await fetchone("SELECT status FROM quest_completions WHERE task_id=? AND user_id=?", (task_id, call.from_user.id))
    if existing:
        await call.answer("Ты уже отправил это задание.", show_alert=True)
        return
    task_type = task["task_type"]
    if task_type == "channel_sub":
        channel_id = (task["channel_id"] or "").strip()
        if not channel_id or not await is_subscribed(call.bot, call.from_user.id, channel_id):
            await call.answer("Сначала подпишись на указанный канал.", show_alert=True)
            return
        status = "approved"
    elif task_type == "referrals":
        count = await fetchone("SELECT COUNT(*) AS n FROM referrals WHERE referrer_id=?", (call.from_user.id,))
        if int(count["n"] or 0) < int(task["required_count"]):
            await call.answer(f"Нужно пригласить ещё {int(task['required_count']) - int(count['n'] or 0)} пользователей.", show_alert=True)
            return
        status = "approved"
    else:
        await state.set_state(QuestStates.waiting_proof)
        await state.update_data(quest_task_id=task_id)
        await call.message.answer("📎 Отправь подтверждение выполнения задания текстом, фото или документом. После отправки заявка попадёт администрации на проверку. Для отмены — /cancel.")
        await call.answer("Жду подтверждение выполнения.")
        return
    try:
        await execute(
            "INSERT INTO quest_completions(task_id,user_id,status,completed_at) VALUES(?,?,?,?)",
            (task_id, call.from_user.id, "approved", dt_to_db()),
        )
    except Exception:
        await call.answer("Заявка уже существует.", show_alert=True)
        return
    if status == "approved":
        await execute("UPDATE users SET points=points+? WHERE user_id=?", (task["reward_points"], call.from_user.id))
        await log_transaction(call.from_user.id, "quest_reward", task["reward_points"], f"Награда за задание #{task_id}", "FP")
        await call.message.answer(f"✅ Задание выполнено! Начислено <b>{task['reward_points']} FP-коинов</b>.", reply_markup=back_keyboard("profile:tasks"))
    else:
        await notify_task_admins(call.bot, task_id, call.from_user.id)
        await call.message.answer("⏳ Заявка отправлена администраторам на проверку.", reply_markup=back_keyboard("profile:tasks"))
    await call.answer()


@router.message(QuestStates.waiting_proof)
async def submit_task_proof(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    task_id = int(data.get("quest_task_id", 0))
    task = await fetchone("SELECT id FROM quests_tasks WHERE id=? AND active=1", (task_id,))
    if not task:
        await state.clear()
        await message.answer("Задание больше недоступно. Открой список заданий заново.")
        return
    body = (message.text or message.caption or "").strip()
    if not body and not (message.photo or message.document):
        await message.answer("Отправь текст, фото или документ с подтверждением выполнения.")
        return
    if len(body) > 2000:
        await message.answer("Текст подтверждения слишком длинный. Максимум 2000 символов.")
        return
    file_id = None
    file_type = None
    if message.photo:
        file_id, file_type = message.photo[-1].file_id, "photo"
    elif message.document:
        file_id, file_type = message.document.file_id, "document"
    try:
        await execute(
            "INSERT INTO quest_completions(task_id,user_id,status,completed_at,proof_text,proof_file_id,proof_file_type) VALUES(?,?,'pending',?,?,?,?)",
            (task_id, message.from_user.id, dt_to_db(), body, file_id, file_type),
        )
    except Exception:
        await state.clear()
        await message.answer("Заявка на это задание уже отправлена.")
        return
    await state.clear()
    await message.answer("⏳ Подтверждение отправлено администрации. Ответ придёт сюда же.")
    await notify_task_admins(message.bot, task_id, message.from_user.id)


async def notify_task_admins(bot, task_id: int, user_id: int) -> None:
    from aiogram.types import InlineKeyboardMarkup
    from keyboards.inline import cb
    task = await fetchone("SELECT title FROM quests_tasks WHERE id=?", (task_id,))
    proof = await fetchone("SELECT proof_text,proof_file_id,proof_file_type FROM quest_completions WHERE task_id=? AND user_id=?", (task_id, user_id))
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [cb("✅ Одобрить", f"task:approve:{task_id}:{user_id}"), cb("❌ Отклонить", f"task:reject:{task_id}:{user_id}")]
    ])
    caption = truncate((
        f"🎯 <b>Задание #{task_id} ожидает проверки</b>\n"
        f"Задание: {safe_html(task['title'] if task else 'Без названия')}\n"
        f"Пользователь: <code>{user_id}</code>\n\n"
        f"{safe_html(proof['proof_text'] if proof else '')}"
    ), 900)
    for admin_id in await get_notification_targets():
        try:
            if proof and proof["proof_file_type"] == "photo":
                await bot.send_photo(admin_id, proof["proof_file_id"], caption=caption, reply_markup=markup)
            elif proof and proof["proof_file_type"] == "document":
                await bot.send_document(admin_id, proof["proof_file_id"], caption=caption, reply_markup=markup)
            else:
                await bot.send_message(admin_id, caption, reply_markup=markup)
        except Exception:
            logger.exception("Could not notify task review admin")


@router.callback_query(F.data.startswith("task:approve:") | F.data.startswith("task:reject:"))
async def review_task(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    try:
        _, action, task_id_raw, user_id_raw = call.data.split(":")
        task_id, user_id = int(task_id_raw), int(user_id_raw)
    except ValueError:
        await call.answer("Некорректная заявка.", show_alert=True)
        return
    status = "approved" if action == "approve" else "rejected"
    async with connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        row = await (await db.execute(
            "SELECT qc.status,qt.reward_points FROM quest_completions qc JOIN quests_tasks qt ON qt.id=qc.task_id WHERE qc.task_id=? AND qc.user_id=?",
            (task_id, user_id),
        )).fetchone()
        if not row or row["status"] != "pending":
            await db.rollback()
            await call.answer("Заявка уже обработана.", show_alert=True)
            return
        await db.execute(
            "UPDATE quest_completions SET status=?,reviewed_at=?,reviewed_by=? WHERE task_id=? AND user_id=?",
            (status, dt_to_db(), call.from_user.id, task_id, user_id),
        )
        if status == "approved":
            reward = int(row["reward_points"])
            await db.execute("UPDATE users SET points=points+? WHERE user_id=?", (reward, user_id))
            await db.execute(
                "INSERT INTO transactions_log(user_id,transaction_type,amount,currency,description,created_at) VALUES(?,?,?,?,?,?)",
                (user_id, "quest_reward", reward, "FP", f"Награда за задание #{task_id}", dt_to_db()),
            )
        await db.commit()
    try:
        await call.bot.send_message(user_id, "✅ Задание подтверждено, награда начислена!" if status == "approved" else "❌ Заявка на задание отклонена.")
    except Exception:
        logger.exception("Could not notify task owner")
    await call.message.edit_reply_markup(reply_markup=None)
    await call.answer("Решение сохранено.")
