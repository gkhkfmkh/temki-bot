"""Administrator keyboard builders."""

from aiogram.types import InlineKeyboardMarkup

from keyboards.inline import cb


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [cb("📊 Статистика", "admin:stats"), cb("👥 Пользователи", "admin:users")],
        [cb("🔘 Названия кнопок", "admin:buttons"), cb("🎮 Тексты игр", "admin:game_contents")],
        [cb("📢 Рассылка", "admin:broadcast"), cb("📥 Экспорт", "admin:export")],
        [cb("🛒 Тарифы", "admin:tariffs"), cb("🎟 Промокоды", "admin:promos")],
        [cb("🎁 Розыгрыши", "admin:giveaways"), cb("🎯 Задания", "admin:tasks")],
        [cb("🆘 Обращения", "admin:tickets"), cb("💎 Заявки на рекламу", "admin:ads")],
        [cb("💳 Платежи", "admin:orders"), cb("📝 Тексты", "admin:texts")],
        [cb("📚 Бесплатные публикации", "admin:posts")],
        [cb("⚙️ Настройки", "admin:settings")],
        [cb("🏠 Пользовательское меню", "nav:home")],
    ])


def confirm_keyboard(yes: str, no: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[cb("✅ Подтвердить", yes), cb("❌ Отмена", no)]])
