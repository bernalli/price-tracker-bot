"""Migration 016: two additive columns on ``users``, nothing else."""

from __future__ import annotations

import hashlib
import importlib.util
import re
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from price_tracker.db.migrator import MigrationError, apply_migrations, get_current_version
from price_tracker.db.repository import Repository

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "src/price_tracker/db/migrations"
FILE_016 = MIGRATIONS_DIR / "016_add_user_language.sql"
SHA_016 = "0816ce02762b21f0c26e9c9033069ddfbf951230b1c0e6784169b33115a1126d"
COMPAT_REPOSITORY = REPO_ROOT / "tests/fixtures/compat/repository_82feefd.py"
SHA_COMPAT = "49df1c633edccb8b707bba69c5cbc905b5949793cc6d210cb969a7e938f70690"
NEW_COLUMNS = ["language", "telegram_language_tag"]


async def _columns(conn: aiosqlite.Connection, table: str) -> list[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in await cursor.fetchall()]


async def _rows(conn: aiosqlite.Connection, sql: str) -> list[tuple[Any, ...]]:
    cursor = await conn.execute(sql)
    return [tuple(row) for row in await cursor.fetchall()]


async def _seed_schema_15(conn: aiosqlite.Connection) -> None:
    await apply_migrations(conn, MIGRATIONS_DIR, max_version=15)
    await conn.execute("INSERT INTO users(user_id, is_admin) VALUES (1, 1), (2, 0)")
    await conn.execute(
        "INSERT INTO products(id, user_id, url, name, current_price) VALUES "
        "(1, 1, 'https://shop.example/a', 'Kettle', '10.00'), "
        "(2, 2, 'https://shop.example/b', 'Fan', '20.00')"
    )
    await conn.execute(
        "INSERT INTO price_history(product_id, price) "
        "VALUES (1, '10.00'), (1, '9.00'), (2, '20.00')"
    )
    await conn.commit()


def test_the_file_matches_its_pinned_hash() -> None:
    assert hashlib.sha256(FILE_016.read_bytes()).hexdigest() == SHA_016


def test_no_comment_contains_a_semicolon() -> None:
    comments = [line for line in FILE_016.read_text().splitlines() if line.startswith("--")]
    assert comments
    assert all(";" not in line for line in comments)


@pytest.mark.asyncio
async def test_upgrading_a_populated_schema_15_only_adds_the_two_columns() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await _seed_schema_15(conn)
        users_before = await _rows(conn, "SELECT * FROM users ORDER BY user_id")
        products_before = await _rows(conn, "SELECT * FROM products ORDER BY id")
        history_before = await _rows(conn, "SELECT * FROM price_history ORDER BY id")
        columns_before = await _columns(conn, "users")

        assert await apply_migrations(conn, MIGRATIONS_DIR) == 16

        assert await _columns(conn, "users") == [*columns_before, *NEW_COLUMNS]
        users_after = await _rows(conn, "SELECT * FROM users ORDER BY user_id")
        assert [row[:-2] for row in users_after] == users_before
        assert [row[-2:] for row in users_after] == [(None, None), (None, None)]
        assert await _rows(conn, "SELECT * FROM products ORDER BY id") == products_before
        assert await _rows(conn, "SELECT * FROM price_history ORDER BY id") == history_before
        assert await _rows(conn, "PRAGMA integrity_check") == [("ok",)]
        assert await _rows(conn, "PRAGMA foreign_key_check") == []


@pytest.mark.asyncio
async def test_applying_again_is_a_no_op() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        await _seed_schema_15(conn)
        await apply_migrations(conn, MIGRATIONS_DIR)
        schema = await _rows(conn, "SELECT type, name, sql FROM sqlite_master ORDER BY name")
        assert await apply_migrations(conn, MIGRATIONS_DIR) == 16
        assert (
            await _rows(conn, "SELECT type, name, sql FROM sqlite_master ORDER BY name") == schema
        )


@pytest.mark.asyncio
async def test_a_failing_last_statement_rolls_back_the_whole_migration(tmp_path: Path) -> None:
    broken_dir = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS_DIR, broken_dir, ignore=shutil.ignore_patterns("__pycache__"))
    broken = broken_dir / FILE_016.name
    text = FILE_016.read_text()
    assert text.rstrip().endswith("telegram_language_tag TEXT;")
    broken.write_text(text.rstrip().removesuffix("TEXT;") + "NOT A TYPE;\n")
    assert broken.read_bytes() != FILE_016.read_bytes()

    async with aiosqlite.connect(":memory:") as conn:
        await _seed_schema_15(conn)
        with pytest.raises(MigrationError):
            await apply_migrations(conn, broken_dir)
        assert await get_current_version(conn) == 15
        assert not set(NEW_COLUMNS) & set(await _columns(conn, "users"))


@pytest.mark.asyncio
async def test_the_repository_reads_and_writes_on_schema_16() -> None:
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await _seed_schema_15(conn)
        await apply_migrations(conn, MIGRATIONS_DIR)
        repo = Repository(conn)
        user = await repo.get_user(1)
        assert user is not None
        assert (user.language, user.telegram_language_tag) == (None, None)
        product = await repo.get_product(1)
        assert product is not None
        new_id = await repo.add_product(
            user_id=2,
            url="https://shop.example/c",
            name="Lamp",
            domain="shop.example",
            initial_price=Decimal("5"),
            currency="EUR",
        )
        assert (await repo.get_product(new_id)) is not None


def _load_compat_repository() -> Any:
    spec = importlib.util.spec_from_file_location("repository_82feefd", COMPAT_REPOSITORY)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_the_previous_release_repository_still_works_on_schema_16() -> None:
    assert hashlib.sha256(COMPAT_REPOSITORY.read_bytes()).hexdigest() == SHA_COMPAT
    old = _load_compat_repository()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await _seed_schema_15(conn)
        await apply_migrations(conn, MIGRATIONS_DIR)
        repo = old.Repository(conn)
        user = await repo.get_user(1)
        assert user is not None
        assert user.is_admin is True
        assert [u.user_id for u in await repo.list_users()] == [1, 2]
        assert (await repo.get_product(1)).name == "Kettle"
        new_id = await repo.add_product(
            user_id=1,
            url="https://shop.example/d",
            name="Lamp",
            domain="shop.example",
            initial_price=Decimal("5"),
            currency="EUR",
        )
        assert (await repo.get_product(new_id)).url == "https://shop.example/d"


def test_the_migration_names_only_the_two_columns() -> None:
    added = re.findall(r"ADD COLUMN (\w+)", FILE_016.read_text())
    assert added == NEW_COLUMNS
