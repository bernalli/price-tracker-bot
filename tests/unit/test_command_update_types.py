"""``/menu``, ``/help``, ``/lista`` and ``/list`` serve private messages only.

An update whose sender is an authorized user must not reach these handlers when it
is a channel post or an edited channel post: they dereference ``update.message``.
The handlers come from the real registration functions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from telegram import Chat, Message, MessageEntity, Update, User
from telegram.ext import CommandHandler

from price_tracker.bot.handlers import _register_legacy
from tests.support.fake_telegram import FakeRequest, make_application

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from telegram.ext import Application

COMMANDS = ("menu", "help", "lista", "list")
_USER = User(id=42, first_name="Mario", is_bot=False)


@pytest.fixture
async def app() -> AsyncIterator[Application[Any, Any, Any, Any, Any, Any]]:
    application = make_application(FakeRequest())
    _register_legacy(application)
    await application.initialize()
    try:
        yield application
    finally:
        await application.shutdown()


def _update(application: Any, command: str, kind: str) -> Update:
    chat = Chat(id=42, type=Chat.PRIVATE if kind == "message" else Chat.CHANNEL)
    text = f"/{command}"
    message = Message(
        message_id=1,
        date=datetime.now(tz=UTC),
        chat=chat,
        from_user=_USER,
        text=text,
        entities=(MessageEntity(MessageEntity.BOT_COMMAND, 0, len(text)),),
    )
    message.set_bot(application.bot)
    if kind == "message":
        return Update(update_id=1, message=message)
    if kind == "channel_post":
        return Update(update_id=1, channel_post=message)
    return Update(update_id=1, edited_channel_post=message)


def _handler(application: Any, command: str) -> CommandHandler[Any, Any]:
    found = [
        h
        for handlers in application.handlers.values()
        for h in handlers
        if isinstance(h, CommandHandler) and command in h.commands
    ]
    assert len(found) == 1, command
    return found[0]


@pytest.mark.parametrize("command", COMMANDS)
def test_a_private_message_is_accepted(app: Any, command: str) -> None:
    assert _handler(app, command).check_update(_update(app, command, "message"))


@pytest.mark.parametrize("kind", ["channel_post", "edited_channel_post"])
@pytest.mark.parametrize("command", COMMANDS)
def test_a_channel_post_is_rejected(app: Any, command: str, kind: str) -> None:
    assert not _handler(app, command).check_update(_update(app, command, kind))
