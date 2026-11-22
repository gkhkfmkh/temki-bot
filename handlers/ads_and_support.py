"""Advertising requests, support tickets and suggestions."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from config import settings
from database.db import execute, fetchone, get_setting, get_text
from keyboards.inline import back_keyboard, cb
from services.access import get_notification_targets, notify_admins
from states.user_states import AdsStates, SupportStates
from utils.helpers import dt_to_db, safe_html

router = Router(name="ads_and_support")
logger = logging.getLogger(__name__)

AD_FORMATS = {
    "1_24": "Пост 1/24",
    "2_48": "Пост 2/48",
    "pin": "Пост с закрепом",
    "mailing": "Рассылка",
}


@router.callback_query(F.data == "menu:ads")
async def ads_menu(call: CallbackQuery) -> None:
    lines = ["<b>💎 Реклама и сотрудничество</b>", "", "Выбери формат размещения:"]
    rows = []
    ads_url = (await get_setting("ads_url", "")).strip()
    if ads_url:
        from aiogram.types import InlineKeyboardButton
        rows.append([InlineKeyboardButton(text="📄 Прайс и условия", url=ads_url)])
    for key, label in AD_FORMATS.items():
        price = await get_setting(f"ad_price_{key}", "Стоимость уточняется")
        rows.append([cb(f"{label} · {price}", f"ads:format:{key}")])
    rows.append([cb("⬅️ Назад", "nav:home")])
    await call.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await call.answer()


@router.callback_query(F.data.startswith("ads:format:"))
async def select_ad_format(call: CallbackQuery, state: FSMContext) -> None:
    key = call.data.rsplit(":", 1)[1]
    if key not in AD_FORMATS:
        await call.answer("Формат не найден.", show_alert=True)
        return
    await state.clear()
    await state.update_data(ad_format=AD_FORMATS[key])
    await state.set_state(AdsStates.waiting_date)
    await call.message.answer(
        f"💎 Формат: <b>{AD_FORMATS[key]}</b>\n\nУкажи желаемую дату и время размещения (например, <code>28.11.2026 после 18:00</code>).",
        reply_markup=back_keyboard("menu:ads"),
    )
    await call.answer()


@router.message(AdsStates.waiting_date, F.text)
async def ads_date(message: Message, state: FSMContext) -> None:
    value = (message.text or "").strip()
    if not 3 <= len(value) <= 100:
        await message.answer("Укажи дату и время в понятном формате (до 100 символов).")
        return
    await state.update_data(ad_date=value)
    await state.set_state(AdsStates.waiting_brief)
    await message.answer("Теперь отправь ссылку на рекламируемый ресурс и краткое описание публикации одним сообщением.")


@router.message(AdsStates.waiting_brief, F.text)
async def ads_brief(message: Message, state: FSMContext) -> None:
    brief = (message.text or "").strip()
    if not 5 <= len(brief) <= 2500:
        await message.answer("Описание должно содержать от 5 до 2500 символов.")
        return
    data = await state.get_data()
    request_id = await execute(
        "INSERT INTO ad_requests(user_id,format,desired_date,brief,status,created_at) VALUES(?,?,?,?, 'pending',?)",
        (message.from_user.id, data["ad_format"], data["ad_date"], brief, dt_to_db()),
    )
    await state.clear()
    await message.answer(f"✅ Заявка на рекламу <b>#{request_id}</b> отправлена. Администратор свяжется с тобой после рассмотрения.", reply_markup=back_keyboard())
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [cb("✅ Принять", f"ad:approve:{request_id}"), cb("❌ Отклонить", f"ad:reject:{request_id}")],
        [cb("💬 Ответить", f"ad:reply:{request_id}")],
    ])
    user = message.from_user
    body = (
        f"💎 <b>Заявка на рекламу #{request_id}</b>\n"
        f"Пользователь: {safe_html(user.full_name)} (<code>{user.id}</code>)\n"
        f"Формат: <b>{safe_html(data['ad_format'])}</b>\n"
        f"Дата: <b>{safe_html(data['ad_date'])}</b>\n\n{safe_html(brief)}"
    )
    for admin_id in await get_notification_targets():
        try:
            await message.bot.send_message(admin_id, body, reply_markup=markup)
        except Exception:
            logger.exception("Could not notify admin about ad request")


@router.callback_query(F.data == "menu:support")
async def support_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(SupportStates.waiting_message)
    prompt = await get_text("support_prompt", "<b>🤖💬 Поддержка</b>\n\nОпиши вопрос одним сообщением. Можно приложить фото.")
    extra = await get_setting("support_text", "")
    if extra:
        prompt += "\n\n" + safe_html(extra)
    if settings.support_username:
        prompt += f"\n\nКонтакт: {safe_html(settings.support_username)}"
    await call.message.answer(prompt, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [cb("💡 Предложить функцию", "menu:suggest")],
        [cb("⬅️ Назад", "nav:home")],
    ]))
    await call.answer()


@router.callback_query(F.data == "menu:suggest")
async def suggestion_menu(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(ticket_category="suggestion")
    await state.set_state(SupportStates.waiting_message)
    await call.message.answer("💡 <b>Предложение по работе бота</b>\n\nНапиши, какую функцию или улучшение ты предлагаешь. Можно приложить изображение.", reply_markup=back_keyboard())
    await call.answer()


@router.message(SupportStates.waiting_message)
async def create_ticket(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    category = data.get("ticket_category", "support")
    if not (message.text or message.caption or message.photo or message.document):
        await message.answer("Отправь текст, фото или документ.")
        return
    body = (message.text or message.caption or "[Вложение]").strip()
    if len(body) > 3000:
        body = body[:3000]
    file_id = None
    file_type = None
    if message.photo:
        file_id, file_type = message.photo[-1].file_id, "photo"
    elif message.document:
        file_id, file_type = message.document.file_id, "document"
    ticket_id = await execute(
        "INSERT INTO tickets_suggestions(user_id,category,message,file_id,file_type,status,created_at) VALUES(?,?,?,?,?,'open',?)",
        (message.from_user.id, category, body, file_id, file_type, dt_to_db()),
    )
    await state.clear()
    await message.answer(f"✅ Обращение <b>#{ticket_id}</b> зарегистрировано. Ответ придёт сюда же.", reply_markup=back_keyboard())
    markup = InlineKeyboardMarkup(inline_keyboard=[[cb("✉️ Ответить на обращение", f"ticket:reply:{ticket_id}")]])
    caption = (
        f"{'💡 Предложение' if category == 'suggestion' else '🆘 Обращение в поддержку'} <b>#{ticket_id}</b>\n"
        f"От: {safe_html(message.from_user.full_name)} (<code>{message.from_user.id}</code>)\n\n{safe_html(body)}"
    )
    for admin_id in await get_notification_targets():
        try:
            if file_type == "photo":
                await message.bot.send_photo(admin_id, file_id, caption=caption, reply_markup=markup)
            elif file_type == "document":
                await message.bot.send_document(admin_id, file_id, caption=caption, reply_markup=markup)
            else:
                await message.bot.send_message(admin_id, caption, reply_markup=markup)
        except Exception:
            logger.exception("Could not forward ticket %s", ticket_id)
