"""Integration test — migrator handles a pre-existing v2 schema as no-op."""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from price_tracker.db.migrator import apply_migrations

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")


# This SQL recreates the schema as it exists in v2 after all 22 inline ALTERs ran
V2_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE users (
    user_id INTEGER PRIMARY KEY,
    is_admin INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    display_name TEXT,
    username TEXT
);

CREATE TABLE products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    name TEXT,
    initial_price TEXT,
    current_price TEXT,
    threshold_type TEXT NOT NULL DEFAULT 'percentage',
    threshold_value TEXT NOT NULL DEFAULT '10',
    last_notified_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    domain TEXT,
    target_price TEXT,
    lowest_price TEXT,
    highest_price TEXT,
    updated_at TEXT DEFAULT '',
    user_id INTEGER NOT NULL DEFAULT 0,
    check_interval_minutes INTEGER,
    last_checked_at TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    is_available INTEGER NOT NULL DEFAULT 1,
    currency TEXT NOT NULL DEFAULT 'EUR',
    preferred_condition TEXT DEFAULT NULL,
    preferred_seller TEXT DEFAULT NULL,
    pending_alert_price TEXT DEFAULT NULL,
    pending_alert_at TEXT DEFAULT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL,
    price TEXT NOT NULL,
    checked_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (product_id) REFERENCES products(id) ON DELETE CASCADE
);

CREATE INDEX idx_price_history_product ON price_history(product_id, checked_at DESC);
CREATE INDEX idx_products_active ON products(is_active);
CREATE INDEX idx_products_user ON products(user_id, is_active);

CREATE TABLE bot_config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@pytest.mark.asyncio
async def test_migrator_treats_pre_existing_v2_schema_as_idempotent():
    async with aiosqlite.connect(":memory:") as conn:
        await conn.executescript(V2_SCHEMA_SQL)
        await conn.commit()

        new_version = await apply_migrations(conn, MIGRATIONS_DIR)
        assert new_version == 8

        await conn.execute(
            "INSERT INTO products(url, name, initial_price, currency) VALUES(?, ?, ?, ?)",
            ("https://x", "X", "10", "EUR"),
        )
        await conn.commit()


@pytest.mark.asyncio
async def test_migrator_data_preserved_across_run():
    async with aiosqlite.connect(":memory:") as conn:
        await conn.executescript(V2_SCHEMA_SQL)
        await conn.execute(
            "INSERT INTO products(url, name, initial_price, currency) VALUES(?, ?, ?, ?)",
            ("https://existing", "Existing", "100", "EUR"),
        )
        await conn.commit()

        await apply_migrations(conn, MIGRATIONS_DIR)

        cursor = await conn.execute(
            "SELECT name FROM products WHERE url = ?", ("https://existing",)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row[0] == "Existing"
