"""admin_* pending-action branches must not depend on a product lookup.

`handle_text_input` fetches `_get_user_product(context, product_id, user_id)`
BEFORE dispatching on `action_type` (handlers/text_input.py, lines ~82-86).
Admin flows that set `pending_action` with a placeholder `product_id=0`
(admin_adduser, admin_interval, admin_debug — see handlers/callbacks/_admin.py)
are not about any product: on a real database `get_product(0)` returns
`None` because SQLite `AUTOINCREMENT` ids start at 1, so the shared
lookup always fails and the admin's reply is swallowed with
"Prodotto non trovato" instead of being processed.

Uses the real in-memory SQLite `Repository` (migrations applied), not a
mock, because a mock DB that returns a product for every id hides this
defect entirely (see test_text_input_admin_interval.py, which stubs
`db.get_product` to always return `{"name": "x"}`).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.bot.handlers.text_input import handle_text_input
from price_tracker.config import Config
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")

ADMIN_ID = 987654321
NEW_USER_ID = 555000111


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


def _make_update_and_context(repo: Repository, text: str) -> tuple[MagicMock, MagicMock]:
    job_queue = MagicMock()
    job_queue.get_jobs_by_name.return_value = []

    update = MagicMock()
    update.effective_user.id = ADMIN_ID
    update.effective_user.language_code = "it"
    update.effective_user.first_name = "Admin"
    update.effective_user.full_name = "Admin"
    update.effective_user.username = None
    update.message.text = text
    update.message.reply_text = AsyncMock()

    context = MagicMock()
    context.user_data = {"pending_action": ("admin_adduser", 0)}
    context.bot_data = {"db": repo, "config": _make_config()}
    context.job_queue = job_queue

    return update, context


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Confirmed defect: handle_text_input's shared pending-action product "
        "lookup (handlers/text_input.py ~82-86) runs before the action_type "
        "dispatch, so admin_adduser's placeholder product_id=0 always misses "
        "on a real DB and the admin's reply is dropped. Fix pending in a "
        "coordinated PR touching the same file; xfail marks the defect until "
        "then so its disappearance becomes a visible event."
    ),
)
async def test_admin_adduser_is_swallowed_by_the_product_lookup(repo: Repository) -> None:
    """Reproduces the suspected defect for the admin_adduser branch.

    `pending_action = ("admin_adduser", 0)` is set by handlers/callbacks/_admin.py
    when an admin taps "add user" and is asked to paste the new user's numeric
    Telegram id. `product_id=0` is a placeholder, never a real product: on the
    real SQLite repository `get_product(0)` is always `None`, so the shared
    lookup at the top of `handle_text_input` rejects the admin's reply before
    it ever reaches the `admin_adduser` branch, and the new user is never
    added.
    """
    await repo.ensure_user(ADMIN_ID, is_admin=True)

    update, context = _make_update_and_context(repo, str(NEW_USER_ID))

    await handle_text_input(update, context)

    # What SHOULD have happened (per the admin_adduser branch, lines 125-148):
    added = await repo.get_user(NEW_USER_ID)
    assert added is not None, (
        "admin_adduser must add the new user; instead the pending-action "
        "product lookup rejected the reply as 'Prodotto non trovato' before "
        "the admin_adduser branch ever ran"
    )
    reply_text = update.message.reply_text.await_args.args[0]
    assert "aggiunto" in reply_text.lower()
