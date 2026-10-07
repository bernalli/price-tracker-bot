"""/utenti lists only the users that are currently authorized.

``/removeuser`` deactivates a user (``is_active = 0``) instead of deleting the
row, so the user listing must leave deactivated rows out. Uses the real
in-memory SQLite ``Repository`` so the ``is_active`` column is the one the
removal actually writes.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.bot.handlers.auth import cmd_users
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")

ADMIN_ID = 987654321
ACTIVE_ID = 555000111
REMOVED_ID = 444555666


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    try:
        yield Repository(conn)
    finally:
        await conn.close()


def _update_and_context(repo: Repository) -> tuple[MagicMock, MagicMock]:
    update = MagicMock()
    update.effective_user.id = ADMIN_ID
    update.effective_user.language_code = "en"
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot_data = {"db": repo}
    return update, context


async def test_users_listing_leaves_out_removed_users(repo: Repository) -> None:
    await repo.ensure_user(ADMIN_ID, is_admin=True)
    await repo.ensure_user(ACTIVE_ID, is_admin=False)
    await repo.ensure_user(REMOVED_ID, is_admin=False)
    assert await repo.remove_user(REMOVED_ID)
    update, context = _update_and_context(repo)

    await cmd_users(update, context)

    text = str(update.message.reply_text.await_args.args[0])
    assert f"<code>{ADMIN_ID}</code>" in text
    assert f"<code>{ACTIVE_ID}</code>" in text
    assert f"<code>{REMOVED_ID}</code>" not in text


@pytest.mark.parametrize(
    "relative_path",
    [
        "src/price_tracker/bot/handlers/callbacks/_admin.py",
        "src/price_tracker/bot/handlers/callbacks/_menu.py",
        "src/price_tracker/bot/handlers/debug.py",
    ],
)
def test_admin_user_surfaces_never_query_historical_user_rows(relative_path: str) -> None:
    """Defect: removed users reappeared in admin counts, lists and pickers."""
    tree = ast.parse(Path(relative_path).read_text(encoding="utf-8"))
    historical_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_all_users"
    ]
    assert historical_calls == []
