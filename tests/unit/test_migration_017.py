"""Migration 017: one additive column on ``users``, nothing else."""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from price_tracker.db.migrator import MigrationError, apply_migrations, get_current_version
from price_tracker.db.repository import Repository

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "src/price_tracker/db/migrations"
FILE_017 = MIGRATIONS_DIR / "017_add_user_min_drop.sql"
SHA_017 = "d57b16f39bfa9cd72df2041c814ee0b1b28ed3770de57897b2a90538a4af9abc"
NEW_COLUMNS = ["min_drop"]


async def _columns(conn: aiosqlite.Connection, table: str) -> list[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in await cursor.fetchall()]


async def _rows(conn: aiosqlite.Connection, sql: str) -> list[tuple[Any, ...]]:
    cursor = await conn.execute(sql)
    return [tuple(row) for row in await cursor.fetchall()]


async def _seed_schema_16(conn: aiosqlite.Connection) -> None:
    await apply_migrations(conn, MIGRATIONS_DIR, max_version=16)
    await conn.execute(
        "INSERT INTO users(user_id, is_admin, language) VALUES (1, 1, 'it'), (2, 0, NULL)"
    )
    await conn.execute(
        "INSERT INTO products(id, user_id, url, name, current_price) VALUES "
        "(1, 1, 'https://shop.example/a', 'Kettle', '10.00')"
    )
    await conn.commit()


def test_the_file_matches_its_pinned_hash() -> None:
    assert hashlib.sha256(FILE_017.read_bytes()).hexdigest() == SHA_017


def test_no_comment_contains_a_semicolon() -> None:
    comments = [line for line in FILE_017.read_text().splitlines() if line.startswith("--")]
    assert comments
    assert all(";" not in line for line in comments)


def test_the_migration_names_only_the_new_column() -> None:
    assert re.findall(r"ADD COLUMN (\w+)", FILE_017.read_text()) == NEW_COLUMNS


async def test_upgrading_a_populated_schema_16_only_adds_the_column() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await _seed_schema_16(conn)
        users_before = await _rows(conn, "SELECT * FROM users ORDER BY user_id")
        products_before = await _rows(conn, "SELECT * FROM products ORDER BY id")
        columns_before = await _columns(conn, "users")

        assert await apply_migrations(conn, MIGRATIONS_DIR, max_version=17) == 17

        assert await _columns(conn, "users") == [*columns_before, *NEW_COLUMNS]
        users_after = await _rows(conn, "SELECT * FROM users ORDER BY user_id")
        assert [row[:-1] for row in users_after] == users_before
        assert [row[-1] for row in users_after] == [None, None]
        assert await _rows(conn, "SELECT * FROM products ORDER BY id") == products_before
        assert await _rows(conn, "PRAGMA integrity_check") == [("ok",)]
        assert await _rows(conn, "PRAGMA foreign_key_check") == []


async def test_applying_again_is_a_no_op() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await _seed_schema_16(conn)
        await apply_migrations(conn, MIGRATIONS_DIR, max_version=17)
        schema = await _rows(conn, "SELECT type, name, sql FROM sqlite_master ORDER BY name")
        assert await apply_migrations(conn, MIGRATIONS_DIR, max_version=17) == 17
        assert (
            await _rows(conn, "SELECT type, name, sql FROM sqlite_master ORDER BY name") == schema
        )


async def test_a_failing_statement_rolls_back_the_whole_migration(tmp_path: Path) -> None:
    broken_dir = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, broken_dir, ignore=shutil.ignore_patterns("__pycache__"))
    text = FILE_017.read_text()
    assert text.rstrip().endswith("min_drop TEXT;")
    (broken_dir / FILE_017.name).write_text(text.rstrip().removesuffix("TEXT;") + "NOT A TYPE;\n")

    async with aiosqlite.connect(":memory:") as conn:
        await _seed_schema_16(conn)
        with pytest.raises(MigrationError):
            await apply_migrations(conn, broken_dir, max_version=17)
        assert await get_current_version(conn) == 16
        assert "min_drop" not in await _columns(conn, "users")


async def test_the_repository_reads_and_writes_after_the_upgrade() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await _seed_schema_16(conn)
        await apply_migrations(conn, MIGRATIONS_DIR)
        repo = Repository(conn)
        user = await repo.get_user(1)
        assert user is not None
        assert user.language == "it"
        assert user.min_drop is None
        assert await repo.get_user_min_drop(1) is None
        assert await repo.set_user_min_drop(1, "5")
        assert await repo.get_user_min_drop(1) == "5"
        assert [u.min_drop for u in await repo.list_users()] == ["5", None]
