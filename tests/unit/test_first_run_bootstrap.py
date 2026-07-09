"""Regression: first run on a fresh database must not crash (#no-such-table).

`amain` used to read `bot_config` (persisted check interval) BEFORE migrations
ran in `post_init`, so a brand-new deployment crashed with
`sqlite3.OperationalError: no such table: bot_config` on its very first start.
`bootstrap_database` must hand back a connection with the schema already applied.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from price_tracker.main import bootstrap_database

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_fresh_database_is_migrated_before_first_config_read(tmp_path: Path) -> None:
    db_conn = await bootstrap_database(str(tmp_path / "fresh" / "pricetracker.db"))
    try:
        cursor = await db_conn.execute(
            "SELECT value FROM bot_config WHERE key = ?", ("check_interval_minutes",)
        )
        row = await cursor.fetchone()
        assert row is None  # fresh install: table exists, no persisted value yet
    finally:
        await db_conn.close()


@pytest.mark.asyncio
async def test_bootstrap_database_is_idempotent(tmp_path: Path) -> None:
    path = str(tmp_path / "pricetracker.db")
    first = await bootstrap_database(path)
    await first.close()
    second = await bootstrap_database(path)
    try:
        cursor = await second.execute("SELECT COUNT(*) FROM bot_config")
        assert (await cursor.fetchone()) is not None
    finally:
        await second.close()
