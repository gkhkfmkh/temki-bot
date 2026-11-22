"""Onboarding, main navigation and public information pages."""

from __future__ import annotations

import logging
import re

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import is_admin, settings
from database.db import execute, fetchall, fetchone, get_button_title, get_currency, get_setting, get_text
from keyboards.inline import back_keyboard, main_keyboard, subscription_keyboard
from services.referrals import register_user, reward_referrer
from services.access import has_vip
from states.user_states import ShopStates
from middlewares.sub_check import is_subscribed
from utils.helpers import safe_html, truncate

router = Router(name="start_menu")
logger = logging.getLogger(__name__)


async def _cancel_pending_receipt(state: FSMContext, user_id: int) -> None:
    """Release a reserved discount when a user explicitly cancels receipt upload."""
    if await state.get_state() != ShopStates.waiting_receipt.state:
        return
    data = await state.get_data()
    order_id = data.get("order_id")
    if not order_id:
        return
    order = await fetchone(
        "SELECT id FROM orders WHERE id=? AND user_id=? AND status='awaiting_receipt'",
        (int(order_id), user_id),
    )
    if order:
        await execute("UPDATE orders SET status='cancelled' WHERE id=? AND status='awaiting_receipt'", (int(order_id),))
        await execute(
            "UPDATE users SET discount_reserved_order_id=NULL WHERE user_id=? AND discount_reserved_order_id=?",
            (user_id, int(order_id)),
        )


async def build_welcome(user_id: int, first_name: str) -> str:
    row = await fetchone(
        "SELECT balance,points,paid_until,vip_forever FROM users WHERE user_id=?", (user_id,)
    )
    balance = float(row["balance"] or 0) if row else 0.0
    points = int(row["points"] or 0) if row else 0
    currency = await get_currency()
    if is_admin(user_id):
        status = "Администратор"
    elif await has_vip(user_id):
        status = "VIP"
    else:
        status = "Базовый"
    template = await get_text(
        "welcome",
        "<b>PlayFunpay Team | Официальный бот ⚡</b>\n\nПривет, {first_name}!\nСтатус: <b>{status}</b>\nБаланс: <b>{balance}</b>\nFP-коины: <b>{points}</b>",
    )
    display_name = safe_html(first_name or "друг")
    try:
        rendered = template.format(
            first_name=display_name,
            status=status,
            balance=f"{balance:.2f} {safe_html(currency)}",
            points=points,
        )
    except (KeyError, ValueError, IndexError):
        rendered = template
    if "{first_name}" not in template:
        if "Привет!" in rendered:
            rendered = rendered.replace("Привет!", f"Привет, {display_name}!", 1)
        else:
            rendered = f"Привет, {display_name}!\n\n" + rendered
    if not any(token in template for token in ("{status}", "{balance}", "{points}")):
        rendered += (
            f"\n\nСтатус: <b>{status}</b>"
            f"\nБаланс: <b>{balance:.2f} {safe_html(currency)}</b>"
            f"\nFP-коины: <b>{points}</b>"
        )
    return truncate(rendered)


@router.message(CommandStart())
async def command_start(message: Message, state: FSMContext) -> None:
    if not message.from_user:
        return
    await state.clear()
    payload = (message.text or "").split(maxsplit=1)
    referrer_id = None
    promo_code = None
    if len(payload) > 1:
        match = re.fullmatch(r"ref_(\d+)", payload[1].strip())
        if match:
            referrer_id = int(match.group(1))
        promo_match = re.fullmatch(r"promo_([A-Za-z0-9_-]{3,32})", payload[1].strip(), re.I)
        if promo_match:
            promo_code = promo_match.group(1).upper()
    is_new = await register_user(message.from_user, referrer_id)
    required_setting = (await get_setting("require_subscription", "1" if settings.subscription_required else "0")) == "1"
    configured_channel = (await get_setting("main_channel_id", settings.main_channel_id)).strip()
    if is_new and (not required_setting or not configured_channel):
        await reward_referrer(message.from_user.id)
    banned = await fetchone("SELECT banned FROM users WHERE user_id=?", (message.from_user.id,))
    if banned and banned["banned"]:
        await message.answer("⛔ Доступ к боту ограничен. Если это ошибка, свяжись с администрацией.")
        return
    maintenance = await get_setting("maintenance", "0")
    if maintenance == "1" and not is_admin(message.from_user.id):
        await message.answer("🛠 Бот временно находится на техническом обслуживании. Попробуй немного позже.")
        return
    required = (await get_setting("require_subscription", "1" if settings.subscription_required else "0")) == "1"
    channel_id = (await get_setting("main_channel_id", settings.main_channel_id)).strip()
    if required and channel_id and not is_admin(message.from_user.id):
        if not await is_subscribed(message.bot, message.from_user.id, channel_id):
            channel_url = (await get_setting("main_channel_url", settings.main_channel_url)).strip()
            if not channel_url and channel_id.startswith("@"):
                channel_url = f"https://t.me/{channel_id[1:]}"
            await message.answer(
                "🔒 <b>Добро пожаловать в PlayFunpay Team!</b>\n\nЧтобы пользоваться ботом, подпишись на наш канал и нажми «Проверить подписку».",
                reply_markup=subscription_keyboard(channel_url),
            )
            return
        await reward_referrer(message.from_user.id)
    if promo_code:
        from handlers.promocodes import redeem_promo_code
        result = await redeem_promo_code(message.bot, message.from_user.id, promo_code)
        await message.answer(result)
    await message.answer(
        await build_welcome(message.from_user.id, message.from_user.first_name),
        reply_markup=await main_keyboard(message.from_user.id),
    )


@router.message(Command("id"))
async def command_id(message: Message) -> None:
    await message.answer(f"🆔 Твой Telegram ID: <code>{message.from_user.id}</code>")


@router.message(Command("admin"))
async def command_admin(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("⛔ Эта команда доступна только администраторам.")
        return
    from keyboards.admin_kb import admin_keyboard
    await state.clear()
    await message.answer("<b>⚙️ Центр управления PlayFunpay</b>\nВыбери нужный раздел:", reply_markup=admin_keyboard())


@router.message(Command("cancel"))
async def command_cancel(message: Message, state: FSMContext) -> None:
    await _cancel_pending_receipt(state, message.from_user.id)
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=await main_keyboard(message.from_user.id))


@router.callback_query(F.data == "nav:home")
async def nav_home(call: CallbackQuery, state: FSMContext) -> None:
    await _cancel_pending_receipt(state, call.from_user.id)
    await state.clear()
    if call.message:
        await call.message.answer(
            await build_welcome(call.from_user.id, call.from_user.first_name),
            reply_markup=await main_keyboard(call.from_user.id),
        )
    await call.answer()


@router.callback_query(F.data == "sub:check")
async def check_subscription(call: CallbackQuery) -> None:
    channel_id = (await get_setting("main_channel_id", settings.main_channel_id)).strip()
    if not channel_id or await is_subscribed(call.bot, call.from_user.id, channel_id):
        await reward_referrer(call.from_user.id)
        await call.answer("Подписка подтверждена!", show_alert=True)
        if call.message:
            await call.message.answer(
                await build_welcome(call.from_user.id, call.from_user.first_name),
                reply_markup=await main_keyboard(call.from_user.id),
            )
        return
    await call.answer("Пока не вижу подписку. Подпишись и попробуй ещё раз.", show_alert=True)


@router.callback_query(F.data == "menu:channel")
async def menu_channel(call: CallbackQuery) -> None:
    channel_id = (await get_setting("main_channel_id", settings.main_channel_id)).strip()
    channel_url = (await get_setting("main_channel_url", settings.main_channel_url)).strip()
    if not channel_url and channel_id.startswith("@"):
        channel_url = f"https://t.me/{channel_id[1:]}"
    subscribers = None
    if channel_id:
        try:
            subscribers = await call.bot.get_chat_member_count(channel_id)
        except Exception:
            logger.exception("Could not get channel member count")
    text = "📢 <b>Наш канал PlayFunpay</b>\n\n"
    text += f"Подписчиков: <b>{subscribers}</b>\n\n" if subscribers is not None else "Здесь публикуются новости и материалы проекта.\n\n"
    text += "Подписывайся, чтобы не пропускать обновления!"
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    if channel_url:
        rows.append([InlineKeyboardButton(text="📢 Открыть канал", url=channel_url)])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="nav:home")])
    await call.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data == "menu:faq")
async def menu_faq(call: CallbackQuery) -> None:
    text = await get_text("faq", "<b>❓ Частые вопросы</b>\n\nЕсли нужна помощь, напиши в поддержку.")
    await call.message.answer(text, reply_markup=back_keyboard())
    await call.answer()


@router.callback_query(F.data == "menu:free")
async def menu_free(call: CallbackQuery) -> None:
    posts = await fetchall("SELECT title,content FROM daily_posts WHERE active=1 ORDER BY id DESC LIMIT 8")
    text = await get_text("free", "<b>📚 Бесплатные материалы</b>\n\nЗдесь находятся открытые материалы проекта.")
    if posts:
        text += "\n\n" + "\n\n".join(f"📌 <b>{safe_html(row['title'])}</b>\n{safe_html(row['content'])}" for row in posts)
    else:
        text += "\n\n" + await get_text("daily_empty", "Новые открытые материалы появятся здесь позже.")
    from aiogram.types import InlineKeyboardMarkup
    from keyboards.inline import cb
    await call.message.answer(truncate(text), reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [cb("🎮 Игры и гайды", "menu:games")], [cb("⬅️ Назад", "nav:home")]
    ]))
    await call.answer()


@router.callback_query(F.data == "menu:games")
async def menu_games(call: CallbackQuery) -> None:
    from aiogram.types import InlineKeyboardMarkup
    from keyboards.inline import cb
    keys = [("roblox", "🤖 Roblox"), ("standoff", "⚡ Standoff 2"), ("brawl", "🌵 Brawl Stars"),
            ("pubg", "🔫 PUBG Mobile"), ("clash", "🏰 Clash of Clans"), ("safety", "📜 Безопасность")]
    rows = [[cb(await get_button_title(key, label), f"game:{key}")] for key, label in keys]
    rows.append([cb("⬅️ Назад", "menu:free")])
    intro = await get_text("games", "<b>🎮 Игры и гайды</b>\n\nВыбери интересующий раздел:")
    await call.message.answer(intro, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("game:"))
async def menu_game(call: CallbackQuery) -> None:
    key = call.data.split(":", 1)[1]
    if key == "safety":
        text = await get_text("safety", "<b>📜 Безопасность</b>\n\nПроверяй ссылки и адресата перед оплатой.")
        title = "Безопасность"
    else:
        row = await fetchone("SELECT title,content FROM game_contents WHERE game_key=?", (key,))
        if row:
            title, content = row["title"], row["content"]
        else:
            title, content = key.title(), "Материалы для этого раздела пока не добавлены."
        text = f"<b>{safe_html(title)}</b>\n\n{safe_html(content)}"
    await call.message.answer(text, reply_markup=back_keyboard("menu:games"))
    await call.answer()
