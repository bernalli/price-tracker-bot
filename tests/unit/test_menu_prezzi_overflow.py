"""The Prices menu lists the first 8 active products and points to the list for the rest."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

import price_tracker
from price_tracker.bot.callbacks import Action, decode
from price_tracker.bot.handlers.callbacks import handle_callback
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path(price_tracker.__file__).resolve().parent / "db" / "migrations"
USER = 10


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repository = Repository(conn)
    await repository.ensure_user(USER)
    try:
        yield repository
    finally:
        await conn.close()


async def add_products(repo: Repository, count: int) -> None:
    for index in range(1, count + 1):
        await repo.add_product(
            user_id=USER,
            url=f"https://shop.example.com/item-{index}",
            name=f"Item {index}",
            domain="shop.example.com",
            initial_price=Decimal("10"),
            currency="EUR",
        )


async def prices_menu(repo: Repository, language: str = "en") -> list[tuple[str, str]]:
    """The (label, callback) of every button of the Prices menu, in order."""
    query = MagicMock()
    query.data = "menu_prezzi"
    query.from_user.id = USER
    query.from_user.language_code = language
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = USER
    update.effective_user.language_code = language
    context: Any = MagicMock()
    context.bot_data = {
        "db": repo,
        "repository": repo,
        "config": SimpleNamespace(check_interval_minutes=360),
    }
    await handle_callback(update, context)
    markup = query.edit_message_text.await_args.kwargs["reply_markup"]
    return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def overflow(buttons: list[tuple[str, str]]) -> list[tuple[str, str]]:
    return [(label, data) for label, data in buttons if "more" in label]


@pytest.mark.parametrize("count", [0, 1, 8])
async def test_up_to_eight_products_have_no_overflow_row(repo: Repository, count: int) -> None:
    await add_products(repo, count)
    buttons = await prices_menu(repo)
    assert overflow(buttons) == []
    assert buttons[0][1] == "menu_checkall"
    assert sum(data.startswith("check_") for _, data in buttons) == count


@pytest.mark.parametrize(("count", "more"), [(9, 1), (12, 4)])
async def test_more_than_eight_products_point_to_the_list(
    repo: Repository, count: int, more: int
) -> None:
    await add_products(repo, count)
    buttons = await prices_menu(repo)
    assert sum(data.startswith("check_") for _, data in buttons) == 8
    assert overflow(buttons) == [(f"… {more} more → Products", "l:a:1")]
    assert decode("l:a:1") == Action("list.page", ("a", 1))
    labels = [label for label, _ in buttons]
    # The overflow row follows the last product and precedes the history and back rows.
    assert labels.index(f"… {more} more → Products") == 9


async def test_the_overflow_row_is_translated(repo: Repository) -> None:
    await add_products(repo, 10)
    buttons = await prices_menu(repo, "it")
    assert ("… altri 2 → Prodotti", "l:a:1") in buttons
