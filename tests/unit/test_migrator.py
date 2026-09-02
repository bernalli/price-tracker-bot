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
from price_tracker.db.repository import Repository

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")

# Derived, not hardcoded: adding a migration should not mean editing four
# assertions that only ever encoded "the newest one".
LATEST_VERSION = max(v for v, _ in list_migrations(MIGRATIONS_DIR))


@pytest.mark.asyncio
async def test_list_migrations_is_a_contiguous_run_from_one():
    files = list_migrations(MIGRATIONS_DIR)
    versions = [v for v, _ in files]
    assert versions == list(range(1, LATEST_VERSION + 1))


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
        assert version == LATEST_VERSION
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
        assert "last_error" in cols
        assert "last_error_at" in cols


@pytest.mark.asyncio
async def test_apply_migrations_is_idempotent():
    async with aiosqlite.connect(":memory:") as conn:
        await apply_migrations(conn, MIGRATIONS_DIR)
        await apply_migrations(conn, MIGRATIONS_DIR)
        version = await get_current_version(conn)
        assert version == LATEST_VERSION


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
        assert version == LATEST_VERSION


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


class TestMigration011:
    @pytest.mark.asyncio
    async def test_dedupes_preexisting_global_pref_duplicates(self, tmp_db_path):
        """011 must keep only the most recent global row per user (#58)."""
        await Migrator(db_path=tmp_db_path, max_version=10).migrate()
        async with aiosqlite.connect(tmp_db_path) as conn:
            await conn.execute("INSERT INTO users (user_id) VALUES (1)")
            await conn.execute(
                "INSERT INTO notification_prefs "
                "(user_id, product_id, digest_interval_minutes, updated_at) "
                "VALUES (1, NULL, 60, '2026-01-01 00:00:00')"
            )
            await conn.execute(
                "INSERT INTO notification_prefs "
                "(user_id, product_id, digest_interval_minutes, updated_at) "
                "VALUES (1, NULL, 15, '2026-02-01 00:00:00')"
            )
            await conn.commit()

        await Migrator(db_path=tmp_db_path).migrate()

        async with aiosqlite.connect(tmp_db_path) as conn:
            cursor = await conn.execute(
                "SELECT digest_interval_minutes FROM notification_prefs "
                "WHERE user_id = 1 AND product_id IS NULL"
            )
            rows = list(await cursor.fetchall())
            assert len(rows) == 1
            assert rows[0][0] == 15

    @pytest.mark.asyncio
    async def test_unique_index_rejects_duplicate_global_rows(self, tmp_db_path):
        await Migrator(db_path=tmp_db_path).migrate()
        async with aiosqlite.connect(tmp_db_path) as conn:
            await conn.execute(
                "INSERT INTO notification_prefs (user_id, product_id) VALUES (1, NULL)"
            )
            with pytest.raises(aiosqlite.IntegrityError):
                await conn.execute(
                    "INSERT INTO notification_prefs (user_id, product_id) VALUES (1, NULL)"
                )


@pytest.mark.asyncio
async def test_migration_014_adds_provenance_columns():
    async with aiosqlite.connect(":memory:") as conn:
        await apply_migrations(conn, MIGRATIONS_DIR)

        cursor = await conn.execute("PRAGMA table_info(products)")
        columns = {row[1]: row for row in await cursor.fetchall()}
        assert columns["gone_streak"][2] == "INTEGER"
        assert columns["gone_streak"][3] == 1
        assert columns["gone_streak"][4] == "0"
        assert columns["suspension_kind"][2] == "TEXT"
        assert columns["suspension_kind"][3] == 0
        assert columns["suspension_reason"][2] == "TEXT"
        assert columns["suspension_reason"][3] == 0

        await apply_migrations(conn, MIGRATIONS_DIR)
        await conn.execute("INSERT INTO users (user_id) VALUES (1)")
        await conn.execute(
            "INSERT INTO products (user_id, url) VALUES (?, ?)",
            (1, "https://example.com/product"),
        )
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute("UPDATE products SET suspension_kind = 'weird'")


@pytest.mark.asyncio
async def test_migration_015_rebuilds_digest_queue_with_set_null():
    async with aiosqlite.connect(":memory:") as conn:
        await apply_migrations(conn, MIGRATIONS_DIR, max_version=14)
        await conn.execute("INSERT INTO users (user_id) VALUES (1)")
        await conn.execute(
            "INSERT INTO products (id, user_id, url) VALUES (?, ?, ?)",
            (10, 1, "https://example.com/first"),
        )
        await conn.execute(
            "INSERT INTO products (id, user_id, url) VALUES (?, ?, ?)",
            (20, 1, "https://example.com/second"),
        )
        queued_id = 41
        queued_row = (
            queued_id,
            1,
            10,
            '{"kind":"operational"}',
            "2026-09-02 10:00:00",
            None,
        )
        await conn.execute(
            "INSERT INTO digest_queue "
            "(id, user_id, product_id, alert_payload_json, enqueued_at, flushed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            queued_row,
        )
        await conn.commit()

        await apply_migrations(conn, MIGRATIONS_DIR, max_version=15)

        cursor = await conn.execute("PRAGMA foreign_key_list(digest_queue)")
        foreign_keys = await cursor.fetchall()
        product_fk = next(row for row in foreign_keys if row[3] == "product_id")
        assert product_fk[2] == "products"
        assert product_fk[6] == "SET NULL"

        cursor = await conn.execute(
            "SELECT id, user_id, product_id, alert_payload_json, enqueued_at, flushed_at "
            "FROM digest_queue WHERE id = ?",
            (queued_id,),
        )
        assert await cursor.fetchone() == queued_row

        repo = Repository(conn)
        assert await repo.delete_product(10, user_id=1) is True
        cursor = await conn.execute(
            "SELECT product_id FROM digest_queue WHERE id = ?", (queued_id,)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] is None

        cursor = await conn.execute(
            "INSERT INTO digest_queue (user_id, product_id, alert_payload_json) "
            "VALUES (?, NULL, ?)",
            (1, "{}"),
        )
        assert cursor.lastrowid == queued_id + 1

        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_digest_pending'"
        )
        assert await cursor.fetchone() is not None
