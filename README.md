# PlayFunpay Team Telegram Bot

A modular Telegram bot built with Python 3.12+, aiogram 3.x and asynchronous SQLite.
All user-facing and administrator-facing bot messages are in Russian.

## Included modules

- `bot.py` — application entry point, dispatcher, middleware, startup/shutdown and scheduler lifecycle.
- `main.py` — compatibility entry point for hosts that still run `python main.py`.
- `config.py` — environment-based configuration.
- `database/` — SQLite schema, safe idempotent initialization and legacy database migration.
- `handlers/` — onboarding, profile, loyalty, shop, payments, giveaways, promo codes, advertising, support and admin panel.
- `services/` — VIP access, referrals, cashback and scheduled maintenance.
- `middlewares/` — ban checks, mandatory subscription checks and rate limiting.
- `states/` and `keyboards/` — FSM definitions and keyboard builders.

## Features

- Russian-language main menu, profile, wallet, VIP status and purchase history.
- Referral links, one-time referral points, referral cashback and top-10 leaderboard.
- Daily rewards with streaks and configurable tasks with automatic or administrator review.
- Private showcase with five editable pages and configurable access tariffs.
- Internal-balance purchases, manual card/SBP receipt review and optional CryptoBot invoices.
- One-time private-channel invite links and scheduled VIP expiry reminders/removal.
- Promo codes for ruble balance, FP-coins, VIP days and personal purchase discounts.
- Giveaways with sponsor-channel checks, eligibility conditions, scheduled drawing and rerolls.
- Advertising applications, support tickets and administrator replies.
- Administrator dashboard, user management, tariffs, promos, tasks, giveaways, settings, broadcasts and CSV/XLSX exports.
- Automatic migration from the supplied legacy single-file PlayFunpay database. Existing users, tariffs, payment requests, promo codes, settings, texts, support messages and raffles are retained.

## Installation

1. Use Python 3.12 or newer.
2. Install dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and set the real `BOT_TOKEN` and your numeric Telegram `ADMIN_IDS`.
4. Configure `MAIN_CHANNEL_ID`, `MAIN_CHANNEL_URL` and `PRIVATE_CHANNEL_ID` when those channels are ready.
5. Make the bot an administrator in the private channel with permission to invite and remove members. For membership checks, the bot must be able to call `getChatMember` for the relevant channels.
6. Start the bot:

   ```bash
   python bot.py
   ```

   `python main.py` is also supported for legacy hosting configurations.

## Upgrade from the original PlayFunpay bot

1. Stop the old bot process before replacing files.
2. Keep the existing SQLite database file. Set `DATABASE_PATH` to the exact same path used by the old bot. If the old bot used the default `playfunpay.db`, keep that value and deploy from the same application working directory.
3. Upload the **contents** of this project archive into the application root. `bot.py`, `main.py`, `config.py`, `database/`, `handlers/`, `services/` and `requirements.txt` must be directly inside the root directory (commonly `/app`). Do not keep the files nested inside an extra folder.
4. Replace the old `main.py`; the new `main.py` is a short compatibility launcher that imports `bot.py`. If the hosting logs still point to old handler line numbers such as `main.py:663`, the old file is still deployed.
5. Install the updated dependencies and start with `python bot.py` (or `python main.py`).
6. The first migration creates a one-time `*.pre-v2.bak` copy beside the database, adds missing columns and creates the new tables. It does not intentionally delete old records. Keep both the live database and backup.

## Payments

- Bank card / SBP transfers are manually reviewed by an administrator. A receipt screenshot is not treated as proof of payment until an administrator approves the order.
- CryptoBot invoice creation and polling are enabled only when a valid `CRYPTO_BOT_TOKEN` and a positive `CRYPTO_RATE_RUB_USDT` are configured.
- `YOOMONEY_TOKEN` is reserved for a future integration; it does not activate a YooMoney payment flow by itself.
- Internal-balance payments, order approval and VIP activation are committed transactionally in SQLite.

## Operational notes

- FSM input states use aiogram `MemoryStorage`; unfinished multi-step forms are reset if the process restarts. Permanent business data is stored in SQLite.
- The SQLite database is intended for one bot process. Do not run multiple polling workers against the same SQLite file. For horizontal scaling, move persistence and coordination to PostgreSQL/Redis.
- Back up the database regularly. The first schema upgrade creates a one-time backup, but this is not a replacement for regular backups.
- Test channel permissions, payment details and the private invite flow before announcing the bot to users.
- Never commit `.env`, bot tokens, payment credentials or database backups to a public GitHub repository.

## Hosting

The included `Procfile` uses:

```text
worker: python bot.py
```

Configure the same command in the hosting dashboard if it does not read `Procfile` automatically. Set `DATABASE_PATH` to a persistent disk location if the host provides one; otherwise the database may be lost when the container is rebuilt.
