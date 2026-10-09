"""The three admin prompts that wait for typed text have a Cancel button.

Cancel returns to the admin menu and forgets the prompt, so the next message is
not taken as a user id, a nickname or a number of minutes.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot.handlers.callbacks._admin import handle_admin_menu
from price_tracker.bot.handlers.text_input import handle_text_input

ADMIN = 1
TARGET = 7
OPENINGS = {
    "menu_admin_adduser": ("admin_adduser", 0),
    f"admin_nick_{TARGET}": ("admin_nick", TARGET),
    "menu_admin_interval": ("admin_interval", 0),
}


def _db(*, is_admin: bool = True) -> MagicMock:
    db = MagicMock()
    db.is_user_admin = AsyncMock(return_value=is_admin)
    db.is_user_allowed = AsyncMock(return_value=True)
    db.get_user = AsyncMock(return_value={"display_name": "Bob"})
    db.list_active_users = AsyncMock(return_value=[{"user_id": ADMIN}])
    db.get_config = AsyncMock(return_value=None)
    db.add_user = AsyncMock()
    db.update_user_info = AsyncMock()
    db.set_config = AsyncMock()
    return db


def _context(db: MagicMock, user_data: dict[str, Any]) -> MagicMock:
    context = MagicMock()
    context.bot_data = {"db": db, "config": SimpleNamespace(check_interval_minutes=360)}
    context.user_data = user_data
    return context


def _query() -> MagicMock:
    query = MagicMock()
    query.from_user.id = ADMIN
    query.edit_message_text = AsyncMock()
    return query


@pytest.mark.parametrize("data", sorted(OPENINGS))
async def test_each_admin_prompt_has_only_a_cancel_button_to_the_admin_menu(data: str) -> None:
    query = _query()
    user_data: dict[str, Any] = {}
    await handle_admin_menu(query, _context(_db(), user_data), _db(), ADMIN, data)
    assert user_data["pending_action"] == OPENINGS[data]
    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    buttons = [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]
    assert buttons == [("❌ Cancel", "a")]


@pytest.mark.parametrize("data", sorted(OPENINGS))
async def test_cancel_forgets_the_prompt_and_the_next_message_changes_nothing(data: str) -> None:
    db = _db()
    user_data: dict[str, Any] = {}
    context = _context(db, user_data)
    await handle_admin_menu(_query(), context, db, ADMIN, data)
    cancel = _query()
    assert await handle_admin_menu(cancel, context, db, ADMIN, "menu_admin")
    assert "pending_action" not in user_data
    cancel.edit_message_text.assert_awaited_once()

    update = MagicMock()
    update.effective_user.id = ADMIN
    update.effective_user.language_code = "en"
    update.message.text = "12345"
    update.message.reply_text = AsyncMock()
    db.get_user = AsyncMock(return_value=None)  # the reply language falls back to the app's
    await handle_text_input(update, context)
    db.add_user.assert_not_called()
    # The sender's own name refresh may run; no nickname is written from the text.
    nick_writes = [c for c in db.update_user_info.call_args_list if c.args[0] == TARGET]
    assert nick_writes == []
    db.set_config.assert_not_called()
    update.message.reply_text.assert_not_called()


async def test_the_admin_menu_pressed_by_a_non_admin_forgets_nothing() -> None:
    db = _db(is_admin=False)
    user_data: dict[str, Any] = {"pending_action": ("admin_interval", 0)}
    query = _query()
    assert await handle_admin_menu(query, _context(db, user_data), db, ADMIN, "menu_admin")
    assert user_data["pending_action"] == ("admin_interval", 0)
    query.edit_message_text.assert_not_called()
