"""Tests for versioned DB migrator."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from price_tracker.db.migrator import (
    SCHEMA_VERSION_TABLE,
    apply_migrations,
    get_current_version,
    list_migrations,
)


MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")


@pytest.mark.asyncio
async def test_list_migrations_finds_001_to_007():
    files = list_migrations(MIGRATIONS_DIR)
    versions = [v for v, _ in files]
    assert versions == [1, 2, 3, 4, 5, 6, 7]


@pytest.mark.asyncio
async def test_get_current_version_zero_on_fresh_db():
    async with aiosqlite.connect(":memory:") as conn:
        version = await get_current_version(conn)
        assert version == 0


@pytest.mark.asyncio
async def test_apply_migrations_brings_fresh_db_to_latest():
    async with aiosqlite.connect(":memory:") as conn:
        await apply_migrations(conn, MIGRATIONS_DIR)
        version = await get_current_version(conn)
        assert version == 7
        cursor = await conn.execute("PRAGMA table_info(products)")
        cols = [row[1] async for row in cursor]
        assert "id" in cols
        assert "user_id" in cols
        assert "threshold_type" in cols
        assert "threshold_value" in cols
        assert "currency" in cols
        assert "pending_alert_price" in cols
        assert "preferred_condition" in cols
        assert "check_interval_minutes" in cols


@pytest.mark.asyncio
async def test_apply_migrations_is_idempotent():
    async with aiosqlite.connect(":memory:") as conn:
        await apply_migrations(conn, MIGRATIONS_DIR)
        await apply_migrations(conn, MIGRATIONS_DIR)
        version = await get_current_version(conn)
        assert version == 7


@pytest.mark.asyncio
async def test_apply_migrations_creates_schema_version_table():
    async with aiosqlite.connect(":memory:") as conn:
        await apply_migrations(conn, MIGRATIONS_DIR)
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (SCHEMA_VERSION_TABLE,),
        )
        row = await cursor.fetchone()
        assert row is not None


@pytest.mark.asyncio
async def test_apply_migrations_partial_then_complete():
    async with aiosqlite.connect(":memory:") as conn:
        all_migs = list_migrations(MIGRATIONS_DIR)
        partial = [(v, p) for v, p in all_migs if v <= 3]
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} "
            f"(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        for version, path in partial:
            sql = path.read_text()
            await conn.executescript(sql)
            await conn.execute(f"INSERT INTO {SCHEMA_VERSION_TABLE}(version) VALUES (?)", (version,))
        await conn.commit()

        await apply_migrations(conn, MIGRATIONS_DIR)
        version = await get_current_version(conn)
        assert version == 7
