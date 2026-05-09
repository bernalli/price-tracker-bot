"""Configuration loader — reads from .env and provides typed defaults."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    admin_users: tuple[int, ...]
    check_interval_minutes: int
    database_path: str
    default_threshold_type: str
    default_threshold_value: str
    max_consecutive_errors: int
    check_delay_seconds: float
    notification_cooldown_hours: int
    request_timeout: int
    log_level: str
    lang: str

    @classmethod
    def from_env(cls) -> Config:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required in .env")

        raw_users = os.getenv("ALLOWED_USERS", "")
        admin_users = tuple(int(x.strip()) for x in raw_users.split(",") if x.strip())

        return cls(
            telegram_bot_token=token,
            admin_users=admin_users,
            check_interval_minutes=int(os.getenv("CHECK_INTERVAL_MINUTES", "360")),
            database_path=os.getenv("DATABASE_PATH", "/data/pricetracker.db"),
            default_threshold_type=os.getenv("DEFAULT_THRESHOLD_TYPE", "percentage"),
            default_threshold_value=os.getenv("DEFAULT_THRESHOLD_VALUE", "10"),
            max_consecutive_errors=int(os.getenv("MAX_CONSECUTIVE_ERRORS", "10")),
            check_delay_seconds=float(os.getenv("CHECK_DELAY_SECONDS", "5.0")),
            notification_cooldown_hours=int(os.getenv("NOTIFICATION_COOLDOWN_HOURS", "24")),
            request_timeout=int(os.getenv("REQUEST_TIMEOUT", "30")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            lang=os.getenv("LANG", "en"),
        )
