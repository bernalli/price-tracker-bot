"""A failure in the optional grid photo never costs the user the product list."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from price_tracker.bot import list_media
from price_tracker.bot.handlers import product_list
from price_tracker.bot.handlers.callbacks import _nav
from price_tracker.bot.messages import set_locale
from tests.support.list_variants import record


def _context() -> Any:
    db = MagicMock()
    db.get_all_products = AsyncMock(return_value=[record(1, name="Kettle"), record(2)])
    db.get_config = AsyncMock(return_value=None)
    return SimpleNamespace(
        bot_data={"db": db, "config": SimpleNamespace(check_interval_minutes=60)}
    )


async def _explode(*args: object, **kwargs: object) -> None:
    raise RuntimeError("grid broke")


@pytest.fixture
def grid_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    mode = list_media.TwoMessageMode(MagicMock(), MagicMock())
    monkeypatch.setattr(list_media, "two_message_mode", lambda context: mode)
    monkeypatch.setattr(list_media, "send_list", _explode)
    monkeypatch.setattr(list_media, "edit_list", _explode)


async def test_list_command_falls_back_to_the_text_list(grid_mode: None) -> None:
    set_locale("en")
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    await inspect.unwrap(product_list.cmd_list)(update, _context())
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.await_args.args[0]
    assert "<b>1.</b> Kettle" in text


async def test_list_pages_fall_back_to_editing_the_text(grid_mode: None) -> None:
    set_locale("en")
    context = _context()
    query = MagicMock(edit_message_text=AsyncMock())
    await _nav.show_list(query, context, context.bot_data["db"], 1, list_filter="a", page=1)
    query.edit_message_text.assert_awaited_once()
    assert "<b>1.</b> Kettle" in query.edit_message_text.await_args.args[0]


async def test_without_the_grid_the_list_is_one_text_message() -> None:
    set_locale("en")
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.reply_photo = AsyncMock()
    await inspect.unwrap(product_list.cmd_list)(update, _context())
    update.message.reply_text.assert_awaited_once()
    update.message.reply_photo.assert_not_awaited()
