"""Deliver release highlights once per version to the current allowed users."""

from __future__ import annotations

from asyncio import sleep
from datetime import timedelta
from typing import TYPE_CHECKING

import structlog
from telegram.error import RetryAfter, TelegramError

import price_tracker
from price_tracker.bot.messages import reset_locale, set_locale, user_locale
from price_tracker.release_notes import render_release_notes, version_key

if TYPE_CHECKING:
    from telegram import Bot

    from price_tracker.db.repository import Repository

log = structlog.get_logger(__name__)
ANNOUNCED_VERSION_KEY = "last_announced_version"
SEND_DELAY_SECONDS = 0.1


async def announce_release(bot: Bot, repo: Repository, fallback_language: str) -> None:
    """Attempt each active recipient once, then persist even partial delivery."""
    current = price_tracker.__version__
    previous = await repo.get_config(ANNOUNCED_VERSION_KEY) or "1.0.0"
    if version_key(current) <= version_key(previous):
        return

    for index, user in enumerate(await repo.list_active_users()):
        token = set_locale(await user_locale(repo, user.user_id, fallback_language))
        try:
            text = render_release_notes(previous, current)
            if not text:
                continue
            if index:
                await sleep(SEND_DELAY_SECONDS)
            await bot.send_message(
                chat_id=user.user_id,
                text=text,
                parse_mode="HTML",
            )
        except TelegramError as exc:
            # Exception text can contain request details; log only the recipient id.
            log.warning("release_update.send_failed", user_id=user.user_id)
            if isinstance(exc, RetryAfter):
                delay = exc.retry_after
                await sleep(delay.total_seconds() if isinstance(delay, timedelta) else delay)
        finally:
            reset_locale(token)
    await repo.set_config(ANNOUNCED_VERSION_KEY, current)
