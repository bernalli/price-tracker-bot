"""Admin user-management must not reach past its own boundaries.

Uses the real in-memory SQLite `Repository` (migrations applied), not a mock, so
that every assertion reads the state the bot would actually leave behind.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest_asyncio

from price_tracker.bot.handlers.auth import cmd_add_user
from price_tracker.bot.handlers.callbacks._admin import handle_admin_menu
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")

ADMIN_ID = 900000001
OTHER_ADMIN_ID = 900000002
PLAIN_USER_ID = 900000003


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    try:
        await Repository(conn).ensure_user(ADMIN_ID, is_admin=True)
        await Repository(conn).ensure_user(OTHER_ADMIN_ID, is_admin=True)
        await Repository(conn).ensure_user(PLAIN_USER_ID)
        yield Repository(conn)
    finally:
        await conn.close()


def _query() -> MagicMock:
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    return query


def _context() -> MagicMock:
    context = MagicMock()
    context.user_data = {}
    return context


def _text(query: MagicMock) -> str:
    return str(query.edit_message_text.await_args.args[0])


# ── admin_rm_ ─────────────────────────────────────────────────────────


async def test_admin_rm_refuses_to_deactivate_another_admin(repo: Repository) -> None:
    query = _query()

    handled = await handle_admin_menu(
        query, _context(), repo, ADMIN_ID, f"admin_rm_{OTHER_ADMIN_ID}"
    )

    assert handled is True
    assert await repo.is_user_allowed(OTHER_ADMIN_ID)
    assert "administrator" in _text(query) or "amministratore" in _text(query)


async def test_admin_rm_refuses_to_deactivate_the_caller(repo: Repository) -> None:
    query = _query()

    handled = await handle_admin_menu(query, _context(), repo, ADMIN_ID, f"admin_rm_{ADMIN_ID}")

    assert handled is True
    assert await repo.is_user_allowed(ADMIN_ID)
    assert "yourself" in _text(query) or "te stesso" in _text(query)


# ── admin_only ────────────────────────────────────────────────────────


def _command_update(user_id: int, text: str) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.language_code = "en"
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


async def test_admin_only_refuses_a_deactivated_admin(repo: Repository) -> None:
    await repo.remove_user(OTHER_ADMIN_ID)
    update = _command_update(OTHER_ADMIN_ID, f"/adduser {PLAIN_USER_ID + 1}")
    context = _context()
    context.args = [str(PLAIN_USER_ID + 1)]
    context.bot_data = {"db": repo}

    await cmd_add_user(update, context)

    assert await repo.get_user(PLAIN_USER_ID + 1) is None
    replies = [str(c.args[0]) for c in update.message.reply_text.await_args_list]
    assert replies == ["⛔ Admin-only command."]
