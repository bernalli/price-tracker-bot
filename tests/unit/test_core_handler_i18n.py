"""English-locale coverage for user-facing text in core handlers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Update

from price_tracker.bot.handlers import error_handler
from price_tracker.bot.handlers.debug import cmd_debug
from price_tracker.bot.messages import set_locale


@pytest.mark.asyncio
async def test_core_handler_fallbacks_render_in_english() -> None:
    set_locale("en")

    debug_update = MagicMock()
    debug_update.message.reply_text = AsyncMock()
    debug_context = SimpleNamespace(args=[])
    raw_debug = cast("Any", cmd_debug).__wrapped__.__wrapped__
    await raw_debug(debug_update, debug_context)

    error_update = MagicMock(spec=Update)
    error_update.effective_user.id = 1
    error_update.effective_user.language_code = "en"
    error_update.message.reply_text = AsyncMock()
    error_context = SimpleNamespace(error=RuntimeError("boom"))
    await error_handler(error_update, cast("Any", error_context))

    debug_text = debug_update.message.reply_text.await_args.args[0]
    error_text = error_update.message.reply_text.await_args.args[0]
    assert debug_text == "❌ Usage: /debug &lt;url&gt;"
    assert error_text == "❌ An error occurred. Please try again in a moment."
    rendered = f"{debug_text}\n{error_text}"
    assert all(fragment not in rendered for fragment in ("Uso:", "errore", "Riprova"))
