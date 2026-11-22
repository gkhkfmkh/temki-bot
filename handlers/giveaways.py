"""Giveaway participation, sponsor checks and winner selection."""

from __future__ import annotations

import json
import logging
import secrets
from datetime import datetime

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from config import is_admin, settings
from database.db import connection, execute, fetchall, fetchone
from keyboards.inline import back_keyboard, cb
from middlewares.sub_check import is_subscribed
from services.access import notify_admins
from utils.helpers import dt_to_db, format_datetime, parse_datetime, safe_html, utcnow

router = Router(name="giveaways")
logger = logging.getLogger(__name__)


async def giveaway_card(call: CallbackQuery, giveaway_id: int) -> None:
    item = await fetchone("SELECT * FROM giveaways WHERE id=?", (giveaway_id,))
    if not item:
        await call.answer("Розыгрыш не найден.", show_alert=True)
        return
    count = await fetchone("SELECT COUNT(*) AS n FROM giveaway_participants WHERE giveaway_id=?", (giveaway_id,))
    joined = await fetchone("SELECT 1 FROM giveaway_participants WHERE giveaway_id=? AND user_id=?", (giveaway_id, call.from_user.id))
    try:
        sponsors = json.loads(item["sponsor_channels"] or "[]")
        if not isinstance(sponsors, list):
            sponsors = []
    except (TypeError, ValueError):
        logger.exception("Invalid sponsor_channels JSON for giveaway %s", giveaway_id)
        sponsors = []
    end = format_datetime(item["ends_at"], True)
    text = (
        f"🎁 <b>{safe_html(item['title'])}</b>\n\n"
        f"🏆 Приз: <b>{safe_html(item['prize'])}</b>\n"
        f"{safe_html(item['description'])}\n\n"
        f"⏰ Итоги: <b>{end}</b>\n"
        f"👥 Участников: <b>{int(count['n'] or 0)}</b>\n"
        f"🏅 Победителей: <b>{item['winners_count']}</b>"
    )
    if item["min_referrals"]:
        text += f"\n👥 Минимум приглашённых: <b>{item['min_referrals']}</b>"
    if item["min_balance"]:
        text += f"\n💰 Минимальный баланс: <b>{item['min_balance']:g} ₽</b>"
    rows = []
    for channel in sponsors:
        value = str(channel).strip()
        if value.startswith("@"):
            rows.append([InlineKeyboardButton(text=f"📢 Подписаться: {value}", url=f"https://t.me/{value[1:]}")])
    if item["status"] == "active" and not joined:
        rows.append([cb("🎉 Участвовать", f"giveaway:join:{giveaway_id}")])
    elif joined:
        text += "\n\n✅ Ты участвуешь в розыгрыше."
    rows.append([cb("⬅️ К розыгрышам", "menu:giveaways")])
    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@router.callback_query(F.data == "menu:giveaways")
@router.callback_query(F.data == "menu:raffle")
async def list_giveaways(call: CallbackQuery) -> None:
    active = await fetchall("SELECT id,title,prize,ends_at FROM giveaways WHERE status='active' ORDER BY ends_at LIMIT 15")
    completed = await fetchall("SELECT id,title,prize,ends_at FROM giveaways WHERE status='completed' ORDER BY finished_at DESC LIMIT 5")
    lines = ["<b>🎁 Розыгрыши PlayFunpay</b>\n"]
    rows = []
    if active:
        lines.append("<b>Активные:</b>")
        for item in active:
            lines.append(f"• <b>{safe_html(item['title'])}</b> — {safe_html(item['prize'])}, итоги {format_datetime(item['ends_at'])}")
            rows.append([cb(f"🎁 {item['title'][:40]}", f"giveaway:view:{item['id']}")])
    else:
        lines.append("Сейчас нет активных розыгрышей.")
    if completed:
        lines.append("\n<b>Завершённые:</b>")
        for item in completed:
            lines.append(f"• {safe_html(item['title'])} — {safe_html(item['prize'])}")
            rows.append([cb(f"🏁 {item['title'][:40]}", f"giveaway:view:{item['id']}")])
    rows.append([cb("⬅️ Назад", "nav:home")])
    await call.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("giveaway:view:"))
@router.callback_query(F.data.startswith("raffle:view:"))
async def view_giveaway(call: CallbackQuery) -> None:
    try:
        giveaway_id = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        await call.answer("Розыгрыш не найден.", show_alert=True)
        return
    await giveaway_card(call, giveaway_id)
    await call.answer()


@router.callback_query(F.data.startswith("giveaway:join:"))
async def join_giveaway(call: CallbackQuery) -> None:
    try:
        giveaway_id = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        await call.answer("Розыгрыш не найден.", show_alert=True)
        return
    item = await fetchone("SELECT * FROM giveaways WHERE id=? AND status='active'", (giveaway_id,))
    if not item:
        await call.answer("Этот розыгрыш уже завершён.", show_alert=True)
        return
    end = parse_datetime(item["ends_at"])
    if end and end <= utcnow():
        await call.answer("Время участия закончилось.", show_alert=True)
        return
    try:
        sponsors = json.loads(item["sponsor_channels"] or "[]")
        if not isinstance(sponsors, list):
            sponsors = []
    except (TypeError, ValueError):
        await call.answer("Условия розыгрыша временно недоступны. Сообщи администрации.", show_alert=True)
        logger.exception("Invalid sponsor_channels JSON for giveaway %s", giveaway_id)
        return
    if int(item["min_referrals"] or 0) > 0:
        ref_count = await fetchone("SELECT COUNT(*) AS n FROM referrals WHERE referrer_id=?", (call.from_user.id,))
        if int(ref_count["n"] or 0) < int(item["min_referrals"]):
            await call.answer(f"Для участия нужно пригласить минимум {item['min_referrals']} пользователей.", show_alert=True)
            return
    if float(item["min_balance"] or 0) > 0:
        balance_row = await fetchone("SELECT balance FROM users WHERE user_id=?", (call.from_user.id,))
        if not balance_row or float(balance_row["balance"] or 0) < float(item["min_balance"]):
            await call.answer(f"Для участия нужен баланс не менее {item['min_balance']:g} ₽.", show_alert=True)
            return
    for channel in sponsors:
        if not await is_subscribed(call.bot, call.from_user.id, str(channel)):
            await call.answer(f"Сначала подпишись на канал {channel}.", show_alert=True)
            return
    async with connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        current = await (await db.execute(
            "SELECT status,ends_at FROM giveaways WHERE id=?", (giveaway_id,)
        )).fetchone()
        if not current or current["status"] != "active" or (parse_datetime(current["ends_at"]) and parse_datetime(current["ends_at"]) <= utcnow()):
            await db.rollback()
            await call.answer("Время участия закончилось.", show_alert=True)
            return
        cursor = await db.execute(
            "INSERT OR IGNORE INTO giveaway_participants(giveaway_id,user_id,joined_at) VALUES(?,?,?)",
            (giveaway_id, call.from_user.id, dt_to_db()),
        )
        await db.commit()
        inserted = cursor.rowcount == 1
    if not inserted:
        await call.answer("Ты уже участвуешь! Удачи 🍀", show_alert=True)
        return
    await call.answer("Участие подтверждено! Удачи 🍀", show_alert=True)
    await giveaway_card(call, giveaway_id)


@router.callback_query(F.data.startswith("raffle:join:"))
async def legacy_raffle_join(call: CallbackQuery) -> None:
    """Handle old raffle buttons left in messages sent by the legacy bot."""
    try:
        giveaway_id = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        await call.answer("Розыгрыш не найден.", show_alert=True)
        return
    item = await fetchone("SELECT * FROM giveaways WHERE id=? AND status='active'", (giveaway_id,))
    if not item:
        await call.answer("Этот розыгрыш уже завершён.", show_alert=True)
        return
    end = parse_datetime(item["ends_at"])
    if end and end <= utcnow():
        await call.answer("Время участия закончилось.", show_alert=True)
        return
    try:
        sponsors = json.loads(item["sponsor_channels"] or "[]")
        if not isinstance(sponsors, list):
            sponsors = []
    except (TypeError, ValueError):
        await call.answer("Условия розыгрыша временно недоступны.", show_alert=True)
        logger.exception("Invalid sponsor_channels JSON for giveaway %s", giveaway_id)
        return
    if int(item["min_referrals"] or 0) > 0:
        ref_count = await fetchone("SELECT COUNT(*) AS n FROM referrals WHERE referrer_id=?", (call.from_user.id,))
        if int(ref_count["n"] or 0) < int(item["min_referrals"]):
            await call.answer(f"Для участия нужно пригласить минимум {item['min_referrals']} пользователей.", show_alert=True)
            return
    if float(item["min_balance"] or 0) > 0:
        balance_row = await fetchone("SELECT balance FROM users WHERE user_id=?", (call.from_user.id,))
        if not balance_row or float(balance_row["balance"] or 0) < float(item["min_balance"]):
            await call.answer(f"Для участия нужен баланс не менее {item['min_balance']:g} ₽.", show_alert=True)
            return
    for channel in sponsors:
        if not await is_subscribed(call.bot, call.from_user.id, str(channel)):
            await call.answer(f"Сначала подпишись на канал {channel}.", show_alert=True)
            return
    async with connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        current = await (await db.execute("SELECT status,ends_at FROM giveaways WHERE id=?", (giveaway_id,))).fetchone()
        if not current or current["status"] != "active" or (parse_datetime(current["ends_at"]) and parse_datetime(current["ends_at"]) <= utcnow()):
            await db.rollback()
            await call.answer("Время участия закончилось.", show_alert=True)
            return
        cursor = await db.execute(
            "INSERT OR IGNORE INTO giveaway_participants(giveaway_id,user_id,joined_at) VALUES(?,?,?)",
            (giveaway_id, call.from_user.id, dt_to_db()),
        )
        await db.commit()
        inserted = cursor.rowcount == 1
    if not inserted:
        await call.answer("Ты уже участвуешь! Удачи 🍀", show_alert=True)
        return
    await call.answer("Участие подтверждено! Удачи 🍀", show_alert=True)
    await giveaway_card(call, giveaway_id)


@router.callback_query(F.data == "giveaway:history")
@router.callback_query(F.data == "raffle:history")
async def giveaway_history(call: CallbackQuery) -> None:
    await list_giveaways(call)


async def draw_giveaway(bot, giveaway_id: int, forced: bool = False) -> bool:
    async with connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        item = await (await db.execute("SELECT * FROM giveaways WHERE id=?", (giveaway_id,))).fetchone()
        if not item or item["status"] != "active":
            await db.rollback()
            return False
        end = parse_datetime(item["ends_at"])
        if not forced and end and end > utcnow():
            await db.rollback()
            return False
        participants = await (await db.execute(
            "SELECT user_id FROM giveaway_participants WHERE giveaway_id=? ORDER BY joined_at,user_id",
            (giveaway_id,),
        )).fetchall()
        ids = [int(row["user_id"]) for row in participants]
        winner_count = min(int(item["winners_count"]), len(ids))
        winners = secrets.SystemRandom().sample(ids, winner_count) if winner_count else []
        for place, user_id in enumerate(winners, start=1):
            await db.execute(
                "INSERT INTO giveaway_winners(giveaway_id,user_id,place,selected_at) VALUES(?,?,?,?)",
                (giveaway_id, user_id, place, dt_to_db()),
            )
            await db.execute(
                "INSERT OR IGNORE INTO giveaway_winner_history(giveaway_id,user_id,place,selected_at) VALUES(?,?,?,?)",
                (giveaway_id, user_id, place, dt_to_db()),
            )
        await db.execute("UPDATE giveaways SET status='completed',finished_at=? WHERE id=?", (dt_to_db(), giveaway_id))
        await db.commit()
    if winners:
        for place, user_id in enumerate(winners, start=1):
            try:
                await bot.send_message(
                    user_id,
                    f"🎉 <b>Поздравляем! Ты занял {place}-е место в розыгрыше!</b>\n\nРозыгрыш: <b>{safe_html(item['title'])}</b>\nПриз: <b>{safe_html(item['prize'])}</b>\n\nСвяжись с администрацией для получения приза.",
                )
            except Exception:
                logger.exception("Could not notify giveaway winner %s", user_id)
    winner_lines = []
    for place, user_id in enumerate(winners, start=1):
        user = await fetchone("SELECT username,first_name FROM users WHERE user_id=?", (user_id,))
        name = "@" + user["username"] if user and user["username"] else (user["first_name"] if user else "Пользователь")
        winner_lines.append(f"{place}. {safe_html(name)} — <code>{user_id}</code>")
    summary = (
        f"🏁 <b>Итоги розыгрыша #{giveaway_id}</b>\n\n"
        f"{safe_html(item['title'])}\nПриз: <b>{safe_html(item['prize'])}</b>\n"
        f"Участников: <b>{len(ids)}</b>\n\n"
        + ("\n".join(winner_lines) if winner_lines else "Победителей нет: никто не участвовал.")
    )
    await notify_admins(bot, summary)
    return True


async def process_due_giveaways(bot) -> None:
    rows = await fetchall("SELECT id FROM giveaways WHERE status='active' AND ends_at<=? LIMIT 50", (dt_to_db(),))
    for row in rows:
        try:
            await draw_giveaway(bot, int(row["id"]))
        except Exception:
            logger.exception("Could not draw giveaway %s", row["id"])


@router.callback_query(F.data.startswith("admin:giveaway:draw:"))
@router.callback_query(F.data.startswith("admin:raffle:draw:"))
async def admin_draw_giveaway(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    try:
        giveaway_id = int(call.data.rsplit(":", 1)[1])
    except ValueError:
        await call.answer("Розыгрыш не найден.", show_alert=True)
        return
    success = await draw_giveaway(call.bot, giveaway_id, forced=True)
    await call.answer("Итоги подведены." if success else "Не удалось подвести итоги.", show_alert=True)
    if success:
        await call.message.answer(f"🏁 Итоги розыгрыша #{giveaway_id} отправлены.")


async def reroll_giveaway(bot, giveaway_id: int) -> bool:
    async with connection() as db:
        await db.execute("BEGIN IMMEDIATE")
        item = await (await db.execute("SELECT * FROM giveaways WHERE id=? AND status='completed'", (giveaway_id,))).fetchone()
        if not item:
            await db.rollback()
            return False
        previous = await (await db.execute("SELECT user_id FROM giveaway_winner_history WHERE giveaway_id=?", (giveaway_id,))).fetchall()
        excluded = {int(row["user_id"]) for row in previous}
        participants = await (await db.execute("SELECT user_id FROM giveaway_participants WHERE giveaway_id=?", (giveaway_id,))).fetchall()
        candidates = [int(row["user_id"]) for row in participants if int(row["user_id"]) not in excluded]
        count = min(int(item["winners_count"]), len(candidates))
        if count <= 0:
            await db.rollback()
            return False
        winners = secrets.SystemRandom().sample(candidates, count)
        await db.execute("DELETE FROM giveaway_winners WHERE giveaway_id=?", (giveaway_id,))
        for place, user_id in enumerate(winners, start=1):
            selected_at = dt_to_db()
            await db.execute("INSERT INTO giveaway_winners(giveaway_id,user_id,place,selected_at) VALUES(?,?,?,?)", (giveaway_id, user_id, place, selected_at))
            await db.execute("INSERT OR IGNORE INTO giveaway_winner_history(giveaway_id,user_id,place,selected_at) VALUES(?,?,?,?)", (giveaway_id, user_id, place, selected_at))
        await db.commit()
    for place, user_id in enumerate(winners, start=1):
        try:
            await bot.send_message(user_id, f"🔄 <b>Результаты розыгрыша обновлены!</b>\n\nТы выбран победителем ({place}-е место) в розыгрыше <b>{safe_html(item['title'])}</b>. Приз: <b>{safe_html(item['prize'])}</b>. Свяжись с администрацией.")
        except Exception:
            logger.exception("Could not notify rerolled winner %s", user_id)
    await notify_admins(bot, f"🔄 <b>Победители розыгрыша #{giveaway_id} перевыбраны.</b>\n" + "\n".join(f"{place}. <code>{uid}</code>" for place, uid in enumerate(winners, start=1)))
    return True


@router.callback_query(F.data.startswith("admin:giveaway:reroll:"))
async def admin_reroll_giveaway(call: CallbackQuery) -> None:
    if not is_admin(call.from_user.id):
        await call.answer("Недостаточно прав.", show_alert=True)
        return
    giveaway_id = int(call.data.rsplit(":", 1)[1])
    success = await reroll_giveaway(call.bot, giveaway_id)
    await call.answer("Победители перевыбраны." if success else "Нет новых участников для перевыбора.", show_alert=True)
