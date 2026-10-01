"""Admin user-management must not reach past its own boundaries.

Uses the real in-memory SQLite `Repository` (migrations applied), not a mock, so
that every assertion reads the state the bot would actually leave behind.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest_asyncio

from price_tracker.bot.handlers.auth import cmd_add_user, cmd_remove_user
from price_tracker.bot.handlers.callbacks._admin import handle_admin_menu
from price_tracker.bot.handlers.callbacks._product import handle_delete_flow
from price_tracker.bot.handlers.text_input import handle_text_input
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


# ── admin pending_action, second step ─────────────────────────────────


def _text_update(user_id: int, text: str) -> MagicMock:
    update = _command_update(user_id, text)
    update.effective_user.first_name = "User"
    update.effective_user.full_name = "User"
    update.effective_user.username = None
    return update


async def test_admin_pending_action_is_refused_after_the_admin_is_demoted(
    repo: Repository,
) -> None:
    new_user_id = PLAIN_USER_ID + 2
    context = _context()
    context.bot_data = {"db": repo}
    context.user_data["pending_action"] = ("admin_adduser", 0)
    await repo.set_admin(ADMIN_ID, is_admin=False)

    await handle_text_input(_text_update(ADMIN_ID, str(new_user_id)), context)

    assert await repo.get_user(new_user_id) is None
    assert "pending_action" not in context.user_data


async def test_admin_pending_action_is_refused_after_the_admin_is_deactivated(
    repo: Repository,
) -> None:
    new_user_id = PLAIN_USER_ID + 3
    context = _context()
    context.bot_data = {"db": repo}
    context.user_data["pending_action"] = ("admin_adduser", 0)
    await repo.remove_user(ADMIN_ID)

    await handle_text_input(_text_update(ADMIN_ID, str(new_user_id)), context)

    assert await repo.get_user(new_user_id) is None


# ── confirm_delete_ on another user's product ─────────────────────────


async def _add_plain_user_product(repo: Repository) -> int:
    return await repo.add_product(
        user_id=PLAIN_USER_ID,
        url="https://example.com/products/widget",
        name="Widget",
        domain="example.com",
        initial_price=Decimal("10.00"),
        currency="EUR",
    )


async def test_confirm_delete_by_an_admin_deletes_another_users_product(
    repo: Repository,
) -> None:
    product_id = await _add_plain_user_product(repo)
    query = _query()
    context = _context()
    context.bot_data = {"db": repo}

    handled = await handle_delete_flow(
        query, context, repo, ADMIN_ID, f"confirm_delete_{product_id}"
    )

    assert handled is True
    assert await repo.get_product(product_id) is None
    assert "Eliminato" in _text(query)


async def test_confirm_delete_by_a_plain_user_leaves_another_users_product(
    repo: Repository,
) -> None:
    product_id = await _add_plain_user_product(repo)
    await repo.ensure_user(PLAIN_USER_ID + 4)
    query = _query()
    context = _context()
    context.bot_data = {"db": repo}

    await handle_delete_flow(
        query, context, repo, PLAIN_USER_ID + 4, f"confirm_delete_{product_id}"
    )

    assert await repo.get_product(product_id) is not None
    assert "Eliminato" not in _text(query)


# ── /adduser on a deactivated user ────────────────────────────────────


async def test_adduser_reactivates_a_deactivated_user(repo: Repository) -> None:
    await repo.remove_user(PLAIN_USER_ID)
    update = _command_update(ADMIN_ID, f"/adduser {PLAIN_USER_ID}")
    context = _context()
    context.args = [str(PLAIN_USER_ID)]
    context.bot_data = {"db": repo}
    context.bot.send_message = AsyncMock()

    await cmd_add_user(update, context)

    assert await repo.is_user_allowed(PLAIN_USER_ID)
    assert "added" in str(update.message.reply_text.await_args.args[0])


# ── remove_user outcome ───────────────────────────────────────────────


async def test_remove_user_reports_whether_an_active_user_was_deactivated(
    repo: Repository,
) -> None:
    assert await repo.remove_user(PLAIN_USER_ID) is True
    assert await repo.remove_user(PLAIN_USER_ID) is False
    assert await repo.remove_user(PLAIN_USER_ID + 5) is False


async def test_removeuser_confirms_the_removal(repo: Repository) -> None:
    update = _command_update(ADMIN_ID, f"/removeuser {PLAIN_USER_ID}")
    context = _context()
    context.args = [str(PLAIN_USER_ID)]
    context.bot_data = {"db": repo}

    await cmd_remove_user(update, context)

    assert not await repo.is_user_allowed(PLAIN_USER_ID)
    assert "removed" in str(update.message.reply_text.await_args.args[0])
