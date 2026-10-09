"""Every command and prompt that names a user or a product parses the id with `_parse_id`.

`/adduser`, `/removeuser`, `/nick`, the admin "add user" prompt and `/prefs` used
`int()`, which accepts signs, underscores, non-ASCII digits and numbers too large
for an SQLite INTEGER. Each malformed id must be refused before any database
access, and must leave the users table as it was.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.bot.handlers._helpers import ID_MAX
from price_tracker.bot.handlers.auth import cmd_add_user, cmd_nick, cmd_remove_user
from price_tracker.bot.handlers.settings import prefs_command
from price_tracker.bot.handlers.text_input import handle_text_input
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
ADMIN_ID = 900000001

NOT_WELL_FORMED = [
    "-12",
    "+12",
    "1_2",
    "0",
    "#",
    "1e3",
    "١٢",  # ARABIC-INDIC DIGIT ONE, TWO
    "１２",  # FULLWIDTH DIGIT ONE, TWO
    "²",  # SUPERSCRIPT TWO
    str(ID_MAX + 1),
    "9" * 40,
]


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    try:
        await Repository(conn).ensure_user(ADMIN_ID, is_admin=True)
        yield Repository(conn)
    finally:
        await conn.close()


async def _user_ids(repo: Repository) -> list[int]:
    return sorted(int(u["user_id"]) for u in await repo.list_users())


def _update(text: str) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = ADMIN_ID
    update.effective_user.language_code = "en"
    update.effective_user.first_name = "Admin"
    update.effective_user.full_name = "Admin"
    update.effective_user.username = None
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def _context(repo: Repository, args: list[str]) -> MagicMock:
    context = MagicMock()
    context.user_data = {}
    context.args = args
    context.bot_data = {"db": repo, "repository": repo}
    context.bot.send_message = AsyncMock()
    return context


def _replies(update: MagicMock) -> list[str]:
    return [str(c.args[0]) for c in update.message.reply_text.await_args_list]


@pytest.mark.parametrize("raw", NOT_WELL_FORMED, ids=ascii)
async def test_adduser_refuses_a_malformed_id(repo: Repository, raw: str) -> None:
    before = await _user_ids(repo)
    update = _update(f"/adduser {raw}")

    await cmd_add_user(update, _context(repo, [raw]))

    assert await _user_ids(repo) == before
    assert _replies(update) == ["❌ Invalid ID. Must be a number."]


@pytest.mark.parametrize("raw", NOT_WELL_FORMED, ids=ascii)
async def test_removeuser_refuses_a_malformed_id(repo: Repository, raw: str) -> None:
    update = _update(f"/removeuser {raw}")

    await cmd_remove_user(update, _context(repo, [raw]))

    assert _replies(update) == ["❌ Invalid ID."]


@pytest.mark.parametrize("raw", NOT_WELL_FORMED, ids=ascii)
async def test_nick_refuses_a_malformed_id(repo: Repository, raw: str) -> None:
    update = _update(f"/nick {raw} Bob")

    await cmd_nick(update, _context(repo, [raw, "Bob"]))

    assert _replies(update) == ["❌ Invalid ID."]


@pytest.mark.parametrize("raw", NOT_WELL_FORMED, ids=ascii)
async def test_adduser_prompt_refuses_a_malformed_id(repo: Repository, raw: str) -> None:
    before = await _user_ids(repo)
    update = _update(raw)
    context = _context(repo, [])
    context.user_data["pending_action"] = ("admin_adduser", 0)

    await handle_text_input(update, context)

    assert await _user_ids(repo) == before
    assert context.user_data["pending_action"] == ("admin_adduser", 0)
    assert _replies(update) == ["❌ Invalid ID. Must be a number."]


@pytest.mark.parametrize("raw", NOT_WELL_FORMED, ids=ascii)
async def test_prefs_refuses_a_malformed_product_id(repo: Repository, raw: str) -> None:
    update = _update(f"/prefs {raw}")

    await prefs_command(update, _context(repo, [raw]))

    assert len(_replies(update)) == 1
    assert _replies(update)[0].startswith("product_id must be a")
