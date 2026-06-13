"""Auth contract: every per-user settings command must be @restricted.

The bot is allowlist-gated (``@restricted`` → ``db.is_user_allowed``). The
notification/preference commands in ``settings.py`` (/mute, /unmute,
/digest_mode, /quiet_hours, /timezone, /throttle, /prefs, /digest_now) were
registered with only ``@with_locale`` — a non-allowlisted Telegram user could
invoke them and write to the DB (bug #1). This test asserts each one runs the
allowlist gate before any handler body.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.settings import (
    digest_mode_command,
    digest_now_command,
    mute_command,
    prefs_command,
    quiet_hours_command,
    throttle_command,
    timezone_command,
    unmute_command,
)

_USER_COMMANDS = [
    mute_command,
    unmute_command,
    digest_mode_command,
    quiet_hours_command,
    timezone_command,
    throttle_command,
    prefs_command,
    digest_now_command,
]


@pytest.mark.parametrize("handler", _USER_COMMANDS, ids=lambda h: h.__name__)
async def test_settings_command_runs_allowlist_gate(handler) -> None:
    db = AsyncMock()
    db.is_user_allowed = AsyncMock(return_value=False)
    update = MagicMock()
    update.effective_user.id = 999
    update.effective_user.language_code = "en"
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot_data = {"db": db, "config": MagicMock()}
    context.args = []

    await handler(update, context)

    # A non-allowlisted user must trip the gate before any DB write.
    db.is_user_allowed.assert_awaited_once_with(999)
