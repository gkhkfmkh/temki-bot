"""User-facing inline keyboard builders."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import is_admin, settings
from database.db import get_button_title, get_setting


def cb(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def url(text: str, link: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, url=link)


def back_keyboard(target: str = "nav:home") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[cb("⬅️ Назад", target)]])


async def main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    labels = {
        "channel": await get_button_title("channel", "📢 Наш канал"),
        "ads": await get_button_title("ads", "💎 Реклама и сотрудничество"),
        "about": await get_button_title("about", "🛒 Что в ПРИВАТЕ?"),
        "buy": await get_button_title("buy", "🔐 Купить доступ"),
        "profile": await get_button_title("profile", "👤 Личный кабинет"),
        "raffle": await get_button_title("raffle", "🎁 Розыгрыши"),
        "promo": await get_button_title("promo", "🎟 Ввести промокод"),
        "daily": await get_button_title("daily", "⚡ Бонусы и задания"),
        "faq": await get_button_title("faq", "❓ FAQ и отзывы"),
        "support": await get_button_title("support", "🤖💬 Поддержка"),
        "free": await get_button_title("free", "📚 Бесплатные материалы"),
    }
    rows: list[list[InlineKeyboardButton]] = [
        [cb(labels["channel"], "menu:channel")],
        [cb(labels["ads"], "menu:ads")],
        [cb(labels["about"], "menu:about"), cb(labels["buy"], "menu:buy")],
        [cb(labels["profile"], "menu:profile"), cb(labels["raffle"], "menu:giveaways")],
        [cb(labels["promo"], "menu:promo"), cb(labels["daily"], "menu:daily")],
        [cb(labels["faq"], "menu:faq"), cb(labels["support"], "menu:support")],
        [cb(labels["free"], "menu:free")],
    ]
    if is_admin(user_id):
        rows.append([cb("⚙️ Админ-панель", "admin:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def profile_keyboard() -> InlineKeyboardMarkup:
    balance = await get_button_title("balance", "💰 Баланс")
    referral = await get_button_title("referral", "👥 Рефералы")
    leaderboard = await get_button_title("leaderboard", "🏆 Топ рефоводов")
    daily = await get_button_title("daily_bonus", "⚡ Забрать ежедневный бонус")
    tasks = await get_button_title("tasks", "🎯 Задания")
    back = await get_button_title("back", "⬅️ В главное меню")
    return InlineKeyboardMarkup(inline_keyboard=[
        [cb(balance, "profile:balance"), cb(referral, "profile:referrals")],
        [cb(leaderboard, "profile:leaderboard")],
        [cb(daily, "profile:daily")],
        [cb(tasks, "profile:tasks")],
        [cb(back, "nav:home")],
    ])


def private_pages_keyboard(page: int, total: int = 5) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(cb("◀️ Назад", f"private:page:{page - 1}"))
    nav.append(cb(f"{page + 1}/{total}", "private:noop"))
    if page < total - 1:
        nav.append(cb("Вперёд ▶️", f"private:page:{page + 1}"))
    rows.append(nav)
    rows.append([cb("🔐 Выбрать тариф", "menu:buy")])
    rows.append([cb("⬅️ В главное меню", "nav:home")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def subscription_keyboard(channel_url: str = "") -> InlineKeyboardMarkup:
    rows = []
    if channel_url:
        rows.append([url("📢 Подписаться", channel_url)])
    rows.append([cb("✅ Проверить подписку", "sub:check")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
