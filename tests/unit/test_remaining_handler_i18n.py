"""English-locale coverage for the remaining legacy handler text."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

from price_tracker.bot.handlers.callbacks._product import handle_delete_flow
from price_tracker.bot.handlers.settings import cmd_set_interval
from price_tracker.bot.messages import reset_locale, set_locale


async def test_callbacks_and_settings_render_in_english() -> None:
    token = set_locale("en")
    try:
        query = MagicMock()
        query.edit_message_text = AsyncMock()
        await handle_delete_flow(query, MagicMock(), MagicMock(), 1, "cancel_delete")

        update = MagicMock()
        update.message.reply_text = AsyncMock()
        context = SimpleNamespace(
            args=[],
            bot_data={"config": SimpleNamespace(check_interval_minutes=360)},
        )
        raw_set_interval = cast("Any", cmd_set_interval).__wrapped__.__wrapped__
        await raw_set_interval(update, context)

        rendered = (
            query.edit_message_text.await_args.args[0],
            update.message.reply_text.await_args.args[0],
        )
        assert rendered == (
            "👍 Operation cancelled.",
            "⏱ Current interval: <b>every 360 minutes</b>\n\n"
            "Usage: /setinterval &lt;minutes&gt;\n"
            "Example: <code>/setinterval 120</code> for every 2 hours",
        )
    finally:
        reset_locale(token)
