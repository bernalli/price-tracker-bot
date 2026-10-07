"""``/debug`` without a URL answers with a usage line Telegram can parse as HTML."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.debug import cmd_debug
from price_tracker.core.textlimits import _is_valid_telegram_markup


@pytest.mark.asyncio
async def test_usage_without_url_is_valid_telegram_html() -> None:
    """Defect: ``<url>`` was sent raw with parse_mode HTML, so Telegram rejected the reply."""
    db = MagicMock()
    db.is_user_admin = AsyncMock(return_value=True)
    db.is_user_allowed = AsyncMock(return_value=True)
    context = MagicMock()
    context.args = []
    context.bot_data = {"db": db}
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_user.language_code = "en"
    update.message.reply_text = AsyncMock()

    await cmd_debug(update, context)

    call: Any = update.message.reply_text.await_args
    text = str(call.args[0])
    assert call.kwargs.get("parse_mode") is not None
    assert _is_valid_telegram_markup(text), text
    assert "/debug" in text
