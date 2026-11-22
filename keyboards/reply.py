"""Optional reply keyboard shortcuts."""

from aiogram.types import KeyboardButton, ReplyKeyboardMarkup


def quick_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="🏠 Главное меню"), KeyboardButton(text="👤 Профиль")]],
        resize_keyboard=True,
        is_persistent=True,
    )
