"""Tests for versioned DB migrator."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from price_tracker.db.migrator import (
    SCHEMA_VERSION_TABLE,
    Migrator,
    apply_migrations,
    get_current_version,
    list_migrations,
)

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")


@pytest.mark.asyncio
async def test_list_migrations_finds_001_to_010():
    files = list_migrations(MIGRATIONS_DIR)
    versions = [v for v, _ in files]
    assert versions == [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]


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
        assert version == 10
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
        assert version == 10


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
            await conn.execute(
                f"INSERT INTO {SCHEMA_VERSION_TABLE}(version) VALUES (?)",
                (version,),
            )
        await conn.commit()

        await apply_migrations(conn, MIGRATIONS_DIR)
        version = await get_current_version(conn)
        assert version == 10


class TestMigration008:
    @pytest.mark.asyncio
    async def test_creates_scraper_health_table(self, tmp_db_path):
        migrator = Migrator(db_path=tmp_db_path)
        await migrator.migrate()  # applies 001..008

        async with migrator._connect() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='scraper_health'"
            )
            row = await cursor.fetchone()
            assert row is not None

    @pytest.mark.asyncio
    async def test_idempotent_replay_through_008(self, tmp_db_path):
        migrator = Migrator(db_path=tmp_db_path)
        await migrator.migrate()
        await migrator.migrate()  # second call should be no-op

        async with migrator._connect() as conn:
            cursor = await conn.execute("SELECT version FROM schema_version ORDER BY version")
            versions = [r[0] async for r in cursor]
            assert versions == sorted(versions)
            assert versions[-1] >= 8


class TestScraperHealthModel:
    def test_dataclass_fields(self):
        from datetime import UTC, datetime

        from price_tracker.db.models import ScraperHealth

        h = ScraperHealth(
            domain="amazon.com",
            state="CLOSED",
            consecutive_blocks=0,
            locked_until=None,
            last_block_at=None,
            last_block_reason=None,
            last_success_at=datetime.now(UTC),
        )
        assert h.domain == "amazon.com"
        assert h.state == "CLOSED"


class TestMigration009:
    @pytest.mark.asyncio
    async def test_creates_notification_prefs_table(self, tmp_db_path):
        migrator = Migrator(db_path=tmp_db_path)
        await migrator.migrate()
        async with migrator._connect() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='notification_prefs'"
            )
            assert await cursor.fetchone() is not None

    @pytest.mark.asyncio
    async def test_default_timezone_is_europe_rome(self, tmp_db_path):
        migrator = Migrator(db_path=tmp_db_path)
        await migrator.migrate()
        async with migrator._connect() as conn:
            await conn.execute("INSERT INTO users (user_id) VALUES (1)")
            await conn.execute(
                "INSERT INTO notification_prefs (user_id, product_id) VALUES (1, NULL)"
            )
            await conn.commit()
            cursor = await conn.execute(
                "SELECT timezone FROM notification_prefs WHERE user_id=1 AND product_id IS NULL"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] == "Europe/Rome"


def test_notification_prefs_dataclass():
    from price_tracker.db.models import NotificationPrefs

    p = NotificationPrefs(
        user_id=1,
        product_id=None,
        mute=False,
        digest_mode=False,
        digest_interval_minutes=60,
        quiet_hours_start=None,
        quiet_hours_end=None,
        throttle_per_hour=None,
        timezone="Europe/Rome",
    )
    assert p.user_id == 1
    assert p.product_id is None
    assert p.timezone == "Europe/Rome"


class TestMigration010:
    @pytest.mark.asyncio
    async def test_creates_digest_queue_table(self, tmp_db_path):
        migrator = Migrator(db_path=tmp_db_path)
        await migrator.migrate()
        async with migrator._connect() as conn:
            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='digest_queue'"
            )
            assert await cursor.fetchone() is not None

    @pytest.mark.asyncio
    async def test_idempotent_replay_010(self, tmp_db_path):
        migrator = Migrator(db_path=tmp_db_path)
        await migrator.migrate()
        await migrator.migrate()
        async with migrator._connect() as conn:
            cursor = await conn.execute("SELECT MAX(version) FROM schema_version")
            row = await cursor.fetchone()
            assert row is not None
            assert row[0] >= 10
