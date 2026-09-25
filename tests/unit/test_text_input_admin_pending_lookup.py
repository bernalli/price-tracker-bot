"""Pending-action product lookup must not swallow admin replies.

`handle_text_input` used to fetch `_get_user_product(context, product_id,
user_id)` unconditionally, BEFORE dispatching on `action_type`
(handlers/text_input.py, previously lines ~82-86). Admin flows set
`pending_action` with a placeholder `product_id=0` (admin_adduser,
admin_interval, admin_debug) or with an unrelated target user id
(admin_nick) — see handlers/callbacks/_admin.py. On a real database
`get_product(0)` returns `None` (SQLite `AUTOINCREMENT` ids start at 1),
so the shared lookup always failed and every admin reply was swallowed
with "Prodotto non trovato" instead of being processed.

Uses the real in-memory SQLite `Repository` (migrations applied), not a
mock, because a mock DB that returns a product for every id hides this
defect entirely.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest_asyncio

from price_tracker.bot.handlers.text_input import handle_text_input
from price_tracker.config import Config
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")

ADMIN_ID = 987654321
NEW_USER_ID = 555000111
TARGET_NICK_USER_ID = 444555666


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    try:
        yield Repository(conn)
    finally:
        await conn.close()


def _make_config() -> Config:
    return Config(
        telegram_bot_token="x",
        admin_users=(),
        check_interval_minutes=360,
        database_path=":memory:",
        default_threshold_type="percentage",
        default_threshold_value="10",
        max_consecutive_errors=10,
        check_delay_seconds=0.0,
        notification_cooldown_hours=24,
        request_timeout=30,
        log_level="INFO",
        lang="it",
    )


def _make_update_and_context(
    repo: Repository, *, user_id: int, text: str, pending_action: tuple[str, int]
) -> tuple[MagicMock, MagicMock]:
    job_queue = MagicMock()
    job_queue.get_jobs_by_name.return_value = []

    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.language_code = "it"
    update.effective_user.first_name = "User"
    update.effective_user.full_name = "User"
    update.effective_user.username = None
    update.message.text = text
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {"pending_action": pending_action}
    context.bot_data = {"db": repo, "config": _make_config()}
    context.job_queue = job_queue

    return update, context


def _last_reply(update: MagicMock) -> str:
    return str(update.message.reply_text.await_args.args[0])


# ── admin_adduser ─────────────────────────────────────────────────────


async def test_admin_adduser_routes_without_a_product_lookup(repo: Repository) -> None:
    """pending_action=("admin_adduser", 0): 0 is a placeholder, not a product id."""
    await repo.ensure_user(ADMIN_ID, is_admin=True)
    update, context = _make_update_and_context(
        repo, user_id=ADMIN_ID, text=str(NEW_USER_ID), pending_action=("admin_adduser", 0)
    )

    await handle_text_input(update, context)

    added = await repo.get_user(NEW_USER_ID)
    assert added is not None, "admin_adduser must add the new user"
    assert added.is_active
    assert "aggiunto" in _last_reply(update).lower()


# ── admin_interval ───────────────────────────────────────────────────


async def test_admin_interval_routes_without_a_product_lookup(repo: Repository) -> None:
    """pending_action=("admin_interval", 0): 0 is a placeholder, not a product id."""
    await repo.ensure_user(ADMIN_ID, is_admin=True)
    update, context = _make_update_and_context(
        repo, user_id=ADMIN_ID, text="45", pending_action=("admin_interval", 0)
    )

    await handle_text_input(update, context)

    stored = await repo.get_config("check_interval_minutes")
    assert stored == "45", "admin_interval must persist the new global interval"
    assert "aggiornato" in _last_reply(update).lower()


# ── admin_debug ──────────────────────────────────────────────────────


async def test_admin_debug_routes_without_a_product_lookup(
    repo: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    """pending_action=("admin_debug", 0): 0 is a placeholder, not a product id.

    `cmd_debug` itself (network scraping) is out of scope here: the property
    under test is only that the admin_debug branch is reached at all instead
    of being rejected by the shared product lookup, so `cmd_debug` is
    replaced with a spy.

    The input text starts with "http" (satisfying `admin_debug`'s own
    `url_input.startswith("http")` check) but has no "://", so it does not
    match `handle_text_input`'s unrelated, pre-existing `URL_PATTERN` guard
    at the top of the function — a real "https://..." input would return
    there before ever reaching pending-action dispatch, which is a separate,
    out-of-scope routing question from the one this fix addresses.
    """
    import price_tracker.bot.handlers.debug as debug_module

    spy = AsyncMock()
    monkeypatch.setattr(debug_module, "cmd_debug", spy)

    await repo.ensure_user(ADMIN_ID, is_admin=True)
    update, context = _make_update_and_context(
        repo,
        user_id=ADMIN_ID,
        text="httpdebugtarget",
        pending_action=("admin_debug", 0),
    )

    await handle_text_input(update, context)

    # If the product lookup had swallowed the reply, cmd_debug would never
    # have been invoked at all — that is the property under test.
    spy.assert_awaited_once()
    assert context.args == ["httpdebugtarget"]


# ── admin_nick ───────────────────────────────────────────────────────


async def test_admin_nick_routes_without_a_product_lookup(repo: Repository) -> None:
    """pending_action=("admin_nick", target_id): target_id is a USER id, not a product id.

    `TARGET_NICK_USER_ID` deliberately does not match any product row, to
    prove the nickname update no longer depends on one existing.
    """
    await repo.ensure_user(ADMIN_ID, is_admin=True)
    await repo.ensure_user(TARGET_NICK_USER_ID, is_admin=False)

    update, context = _make_update_and_context(
        repo,
        user_id=ADMIN_ID,
        text="Mario",
        pending_action=("admin_nick", TARGET_NICK_USER_ID),
    )

    await handle_text_input(update, context)

    updated = await repo.get_user(TARGET_NICK_USER_ID)
    assert updated is not None
    assert updated.display_name == "Mario", "admin_nick must update the target user's nickname"
    assert "aggiornato" in _last_reply(update).lower()
