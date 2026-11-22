"""PlayFunpay Team Telegram bot entry point."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from database.db import database_file_path, fetchone, init_database
from handlers import ads_and_support, admin_panel, giveaways, private_shop, profile_loyalty, promocodes, start_menu
from middlewares.ban_check import BanCheckMiddleware
from middlewares.sub_check import SubscriptionMiddleware
from middlewares.throttling import ThrottlingMiddleware
from services.scheduler import scheduler_loop

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("playfunpay")

bot: Bot | None = None
dp = Dispatcher(storage=MemoryStorage())

# Apply middleware to both user messages and inline callbacks.
for observer in (dp.message, dp.callback_query):
    observer.outer_middleware(BanCheckMiddleware())
    observer.outer_middleware(ThrottlingMiddleware())
    observer.outer_middleware(SubscriptionMiddleware())

# Register feature routers. Each module owns its callbacks and FSM states.
dp.include_routers(
    start_menu.router,
    profile_loyalty.router,
    private_shop.router,
    giveaways.router,
    promocodes.router,
    ads_and_support.router,
    admin_panel.router,
)

_scheduler_task: asyncio.Task | None = None


@dp.errors()
async def global_error_handler(event) -> bool:
    error = event.exception
    logger.error("Unhandled update error: %r", error, exc_info=(type(error), error, error.__traceback__))
    try:
        update = event.update
        if update.message:
            await update.message.answer("⚠️ Произошла внутренняя ошибка. Попробуй ещё раз позже.")
        elif update.callback_query:
            await update.callback_query.answer("Произошла ошибка. Попробуй позже.", show_alert=True)
    except Exception:
        logger.exception("Could not send error notification")
    return True


async def on_startup() -> None:
    global _scheduler_task, bot
    settings.validate()
    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await init_database()
    # Fail before polling if the required schema is not available.
    await fetchone("SELECT COUNT(*) AS total FROM users")
    me = await bot.get_me()
    logger.info("Bot started: @%s (%s); SQLite database: %s", me.username, me.id, database_file_path())
    _scheduler_task = asyncio.create_task(scheduler_loop(bot), name="playfunpay-scheduler")
    for admin_id in settings.admin_ids:
        try:
            await bot.send_message(
                admin_id,
                f"🟢 <b>PlayFunpay Team запущен</b>\nБот: @{me.username}\nБаза данных: <code>{database_file_path()}</code>",
            )
        except Exception:
            logger.exception("Could not send startup notice to admin %s", admin_id)


async def on_shutdown() -> None:
    global _scheduler_task, bot
    if _scheduler_task:
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            logger.debug("Background scheduler cancelled")
        _scheduler_task = None
    if bot is not None:
        await bot.session.close()
        bot = None


async def main() -> None:
    try:
        await on_startup()
        if bot is None:
            raise RuntimeError("Bot was not initialized")
        await dp.start_polling(
            bot,
            allowed_updates=dp.resolve_used_update_types(),
            close_bot_session=False,
        )
    finally:
        await on_shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped")
