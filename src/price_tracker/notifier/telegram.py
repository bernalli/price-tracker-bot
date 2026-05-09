"""Telegram notifier — sends alerts and chart embeds."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram.constants import ParseMode

if TYPE_CHECKING:
    from telegram import Bot

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def __call__(self, user_id: int, text: str) -> None:
        try:
            await self._bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=False,
            )
        except Exception as e:  # noqa: BLE001 — Telegram errors are non-deterministic
            logger.warning("Telegram send failed for user %d: %s", user_id, e)
