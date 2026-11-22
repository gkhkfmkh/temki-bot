"""Environment-based application configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "да"}


def _ids(name: str) -> frozenset[int]:
    result: set[int] = set()
    for item in os.getenv(name, "").split(","):
        item = item.strip()
        if item.isdigit():
            result.add(int(item))
    return frozenset(result)


def _int(name: str, default: int, minimum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, value) if minimum is not None else value


def _float(name: str, default: float, minimum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip().replace(",", "."))
    except (TypeError, ValueError):
        value = default
    return max(minimum, value) if minimum is not None else value


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    admin_ids: frozenset[int]
    database_path: str
    main_channel_id: str
    main_channel_url: str
    private_channel_id: str
    support_chat_id: str
    support_username: str
    crypto_bot_token: str
    crypto_rate_rub_usdt: float
    yoomoney_token: str
    currency: str
    subscription_required: bool
    daily_bonus_base: int
    referral_bonus_points: int
    referral_cashback_percent: float
    broadcast_delay: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            bot_token=os.getenv("BOT_TOKEN", "").strip(),
            admin_ids=_ids("ADMIN_IDS"),
            database_path=os.getenv("DATABASE_PATH", "playfunpay.db").strip() or "playfunpay.db",
            main_channel_id=os.getenv("MAIN_CHANNEL_ID", "").strip(),
            main_channel_url=os.getenv("MAIN_CHANNEL_URL", "").strip(),
            private_channel_id=os.getenv("PRIVATE_CHANNEL_ID", "").strip(),
            support_chat_id=os.getenv("SUPPORT_CHAT_ID", "").strip(),
            support_username=os.getenv("SUPPORT_USERNAME", "").strip(),
            crypto_bot_token=os.getenv("CRYPTO_BOT_TOKEN", "").strip(),
            crypto_rate_rub_usdt=_float("CRYPTO_RATE_RUB_USDT", 0.0, 0.0),
            yoomoney_token=os.getenv("YOOMONEY_TOKEN", "").strip(),
            currency=os.getenv("DEFAULT_CURRENCY", "₽").strip() or "₽",
            subscription_required=_bool("SUBSCRIPTION_REQUIRED", False),
            daily_bonus_base=_int("DAILY_BONUS_BASE", 10, 1),
            referral_bonus_points=_int("REFERRAL_BONUS_POINTS", 25, 0),
            referral_cashback_percent=min(100.0, _float("REFERRAL_CASHBACK_PERCENT", 5.0, 0.0)),
            broadcast_delay=max(0.05, _float("BROADCAST_DELAY", 0.05, 0.05)),
        )

    def validate(self) -> None:
        if not self.bot_token or "REPLACE_WITH" in self.bot_token:
            raise RuntimeError("BOT_TOKEN is missing or still contains the example placeholder. Configure the real token in the hosting environment.")
        if not self.admin_ids:
            raise RuntimeError("ADMIN_IDS is missing. Add at least one numeric Telegram user ID.")


settings = Settings.from_env()


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids

