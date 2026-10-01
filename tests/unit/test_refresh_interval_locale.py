"""`/refresh <id> <minutes>` confirms the interval in the user's language.

The confirmation built the hours with `ngettext("{n} hour", "{n} hours", n)`,
but the Italian catalogue has no plural entry for that msgid, so an Italian
user read "ogni 1 hour". The interval now goes through the same formatter as
the product card.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.monitoring import cmd_refresh
from price_tracker.i18n.format import duration

PRODUCT_ID = 7
USER_ID = 900000010


def _update_and_context(language_code: str, minutes: str) -> tuple[MagicMock, MagicMock]:
    db = AsyncMock()
    db.is_user_allowed = AsyncMock(return_value=True)
    db.is_user_admin = AsyncMock(return_value=False)
    db.get_product_for_user = AsyncMock(return_value={"id": PRODUCT_ID, "name": "Widget"})

    update = MagicMock()
    update.effective_user.id = USER_ID
    update.effective_user.language_code = language_code
    update.effective_user.first_name = "User"
    update.effective_user.full_name = "User"
    update.effective_user.username = None
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.args = [str(PRODUCT_ID), minutes]
    context.bot_data = {"db": db}
    return update, context


@pytest.mark.parametrize(
    ("language_code", "locale", "minutes"),
    [
        ("it", "it_IT", 60),
        ("it", "it_IT", 120),
        ("it", "it_IT", 1440),
        ("it", "it_IT", 90),
        ("en", "en", 60),
    ],
)
async def test_refresh_confirms_the_interval_in_the_users_language(
    language_code: str, locale: str, minutes: int
) -> None:
    update, context = _update_and_context(language_code, str(minutes))

    await cmd_refresh(update, context)

    reply = str(update.message.reply_text.await_args.args[0])
    assert duration(minutes, locale=locale) in reply
    if locale == "it_IT":
        assert "hour" not in reply
