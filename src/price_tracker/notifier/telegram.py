"""Telegram notifier — sends alerts and chart embeds."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from telegram.constants import ParseMode

if TYPE_CHECKING:
    from telegram import Bot

    from price_tracker.observability.metrics import MetricsRegistry

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, bot: Bot, *, metrics: MetricsRegistry | None = None) -> None:
        self._bot = bot
        self._metrics = metrics

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
            return
        if self._metrics is not None:
            self._metrics.notification_sent_total.labels(type="immediate", channel="telegram").inc()
