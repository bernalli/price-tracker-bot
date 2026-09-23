"""`channel_post` / `edited_channel_post` must not reach message-shaped handlers.

In python-telegram-bot 22.8, ``Update.effective_user`` started falling through to
``channel_post.from_user`` / ``edited_channel_post.from_user`` (previously ``None``
for those two update types). Because ``MessageHandler.check_update`` filters on
``Update.effective_message`` — which has always included ``channel_post`` and
``edited_channel_post`` — a channel post whose sender happens to be an authorized
user now clears ``@restricted`` too. Three handlers registered as ``MessageHandler``
dereference ``update.message`` directly and would raise ``AttributeError`` on such
an update, since ``update.message`` stays ``None`` for a channel post:

* ``handle_url`` and ``handle_text_input`` (``text_input.register``, PLC0415-local
  ``filters.TEXT`` handlers around line 236-240)
* ``cmd_import`` (``product_io.register``, ``filters.Document.FileExtension("csv")``
  handler around line 207)

This test reproduces the property with the exact mechanism PTB uses to route an
update: ``BaseHandler.check_update()``. It registers the handlers via the real
``register()`` functions (so a future change to their filters is caught here too),
then feeds each one three update shapes built from real ``telegram`` objects — an
ordinary ``message`` (must be accepted) and a ``channel_post`` / ``edited_channel_post``
from an authorized user (must be rejected).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, cast

import pytest
from telegram import Chat, Document, Message, Update, User
from telegram.ext import Application, MessageHandler

from price_tracker.bot.handlers import product_io, text_input
from price_tracker.bot.handlers.product_io import cmd_import
from price_tracker.bot.handlers.text_input import handle_text_input, handle_url

if TYPE_CHECKING:
    from types import ModuleType

    from telegram.ext import BaseHandler

_UPDATE_ID = 1000
_CHAT_ID = 42
_AUTHORIZED_USER = User(id=42, first_name="Mario", is_bot=False, username="mario")

_CSV_DOCUMENT = Document(file_id="file-1", file_unique_id="uniq-1", file_name="prodotti.csv")


class _HandlerRecorder:
    """Stand-in for `Application`: `register()` only calls `add_handler`.

    Building a real `Application` needs a bot token; this records exactly what
    `text_input.register` / `product_io.register` pass to `add_handler`, which is
    all they do (verified by reading both functions before writing this test).
    """

    def __init__(self) -> None:
        self.handlers: list[BaseHandler[Any, Any, Any]] = []

    def add_handler(self, handler: BaseHandler[Any, Any, Any], group: int = 0) -> None:
        del group  # unused — kept to match Application.add_handler's signature
        self.handlers.append(handler)


def _registered_message_handler(module: ModuleType, callback: Any) -> MessageHandler[Any, Any]:
    """Call `module.register()` on a fresh recorder and return the one
    `MessageHandler` whose `.callback` is `callback` (identity: decorators wrap the
    name but keep `functools.wraps`, and it's the same object the module exports).

    Filters on `isinstance(..., MessageHandler)` on purpose: `cmd_import` is also
    registered as `CommandHandler("importa", cmd_import)`, which shares the same
    callback but is not a `MessageHandler` and does not carry the file-extension
    filter this test is about.
    """
    recorder = _HandlerRecorder()
    module.register(cast("Application[Any, Any, Any, Any, Any, Any]", recorder))
    matches = [
        h for h in recorder.handlers if isinstance(h, MessageHandler) and h.callback is callback
    ]
    assert len(matches) == 1, (
        f"expected exactly one MessageHandler registered for {callback!r}, found {len(matches)}"
    )
    return matches[0]


_HANDLERS: dict[str, MessageHandler[Any, Any]] = {
    "handle_url": _registered_message_handler(text_input, handle_url),
    "handle_text_input": _registered_message_handler(text_input, handle_text_input),
    "cmd_import": _registered_message_handler(product_io, cmd_import),
}

# update_kind -> (chat type the update would realistically carry, must be accepted)
_UPDATE_KINDS: dict[str, tuple[str, bool]] = {
    "message": (Chat.PRIVATE, True),
    "channel_post": (Chat.CHANNEL, False),
    "edited_channel_post": (Chat.CHANNEL, False),
}


def _message_for(handler_name: str, chat_type: str) -> Message:
    """Build the Message each handler's filter is meant to accept."""
    message_id = 1
    date = datetime.now(tz=UTC)
    chat = Chat(id=_CHAT_ID, type=chat_type)
    if handler_name == "handle_url":
        return Message(
            message_id=message_id,
            date=date,
            chat=chat,
            from_user=_AUTHORIZED_USER,
            text="guarda questo https://example.com/prodotto/1",
        )
    if handler_name == "handle_text_input":
        return Message(
            message_id=message_id,
            date=date,
            chat=chat,
            from_user=_AUTHORIZED_USER,
            text="20",
        )
    if handler_name == "cmd_import":
        return Message(
            message_id=message_id,
            date=date,
            chat=chat,
            from_user=_AUTHORIZED_USER,
            document=_CSV_DOCUMENT,
        )
    raise ValueError(f"unknown handler case: {handler_name!r}")  # pragma: no cover


def _build_update(update_kind: str, message: Message) -> Update:
    """Wrap `message` as `message`, `channel_post` or `edited_channel_post` — the
    three Update fields `effective_message` (and, since PTB 22.8, `effective_user`)
    fall through to."""
    if update_kind == "message":
        return Update(update_id=_UPDATE_ID, message=message)
    if update_kind == "channel_post":
        return Update(update_id=_UPDATE_ID, channel_post=message)
    if update_kind == "edited_channel_post":
        return Update(update_id=_UPDATE_ID, edited_channel_post=message)
    raise ValueError(f"unknown update kind: {update_kind!r}")  # pragma: no cover


@pytest.mark.parametrize("update_kind", sorted(_UPDATE_KINDS))
@pytest.mark.parametrize("handler_name", sorted(_HANDLERS))
def test_message_handler_ignores_channel_posts(handler_name: str, update_kind: str) -> None:
    handler = _HANDLERS[handler_name]
    chat_type, should_accept = _UPDATE_KINDS[update_kind]
    message = _message_for(handler_name, chat_type)
    update = _build_update(update_kind, message)

    accepted = bool(handler.check_update(update))

    if should_accept:
        assert accepted, (
            f"{handler_name}: check_update() rejected an ordinary 'message' update "
            f"from an authorized user — it should have been accepted"
        )
    else:
        assert not accepted, (
            f"{handler_name}: check_update() accepted a {update_kind!r} update whose "
            f"from_user is an authorized user. In PTB 22.8 Update.effective_user "
            f"surfaces channel_post/edited_channel_post senders too, so this handler "
            f"would run with update.message is None and crash on the first attribute "
            f"access."
        )
