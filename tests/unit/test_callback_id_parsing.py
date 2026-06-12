"""Tampered callback_data must not crash callback handlers (#62).

Every per-product/per-user callback handler parsed its numeric id with a bare
``int(data.replace(prefix, ""))``: a tampered payload like ``edit_abc`` raised
ValueError, crashing the handler with no feedback to the user (the global
error handler cannot reply to callback queries). Handlers must instead guard
the parse via ``_parse_id`` and answer with an error message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.callbacks import _actions, _admin, _product

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# One case per int()-parsing prefix across the three callback modules.
CASES: list[tuple[Any, str]] = [
    # callbacks/_actions.py
    (_actions.handle_edit_button, "edit_abc"),
    (_actions.handle_pause_button, "pause_NaN"),
    (_actions.handle_remove_button, "remove_\U0001f4a5"),
    (_actions.handle_reset_button, "reset_abc"),
    (_actions.handle_reactivate_button, "reactivate_abc"),
    (_actions.handle_picker, "settarget_abc"),
    (_actions.handle_picker, "setsoglia_NaN"),
    (_actions.handle_picker, "setrefresh_abc"),
    # callbacks/_product.py
    (_product.handle_delete_flow, "confirm_delete_abc"),
    (_product.handle_check_button, "check_NaN"),
    (_product.handle_chart_button, "chart_abc"),
    (_product.handle_amazon_pref, "pref_new_abc"),
    (_product.handle_track_choice, "track_any_abc"),
    (_product.handle_track_choice, "track_threshold_NaN"),
    (_product.handle_track_choice, "track_target_abc"),
    (_product.handle_track_choice, "track_default_abc"),
    # callbacks/_admin.py
    (_admin.handle_admin_menu, "admin_rm_\U0001f4a5"),
    (_admin.handle_admin_menu, "admin_nick_abc"),
]


def _mock_query() -> MagicMock:
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.message.reply_text = AsyncMock()
    query.message.reply_photo = AsyncMock()
    return query


@pytest.mark.parametrize(("handler", "data"), CASES, ids=[data for _, data in CASES])
async def test_tampered_callback_id_replies_error_instead_of_crashing(
    handler: Callable[..., Awaitable[bool]], data: str
) -> None:
    query = _mock_query()
    context = MagicMock()
    context.user_data = {}
    db = AsyncMock()
    db.is_user_admin = AsyncMock(return_value=True)

    handled = await handler(query, context, db, 1, data)  # must NOT raise ValueError

    assert handled is True
    query.edit_message_text.assert_awaited_once()
    msg = query.edit_message_text.await_args.args[0]
    assert "ID non valido" in msg
