"""Migration 018 preserves existing data and stores accepted readings atomically."""

from __future__ import annotations

import asyncio
import hashlib
import re
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import aiosqlite
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.db.migrator import MigrationError, apply_migrations, get_current_version
from price_tracker.db.repository import Repository
from price_tracker.db.repository_common import _PRODUCT_COLS
from tests.unit.test_migration_016 import _load_compat_repository

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "src/price_tracker/db/migrations"
FILE = MIGRATIONS / "018_add_product_image_url.sql"
SHA = "c228a3e29417399b072a45288140084bba8c24bfcf97c38f0d758999bda0f337"


async def rows(conn: aiosqlite.Connection, sql: str) -> list[tuple[Any, ...]]:
    cursor = await conn.execute(sql)
    return [tuple(row) for row in await cursor.fetchall()]


async def columns(conn: aiosqlite.Connection) -> list[str]:
    return [row[1] for row in await rows(conn, "PRAGMA table_info(products)")]


async def seed(conn: aiosqlite.Connection) -> None:
    await apply_migrations(conn, MIGRATIONS, max_version=17)
    await conn.execute("INSERT INTO users(user_id, is_admin) VALUES (1, 1), (2, 0)")
    await conn.execute(
        "INSERT INTO products(id, user_id, url, name, current_price, is_active, "
        "suspension_kind, suspension_reason, gone_streak, pending_read_price, "
        "pending_read_count, pending_read_streak) VALUES "
        "(1, 1, 'https://shop.example/a', 'Kettle', '10', 1, NULL, NULL, 0, NULL, 0, 0), "
        "(2, 1, 'https://shop.example/b', 'Fan', '20', 0, 'manual', NULL, 0, NULL, 0, 0), "
        "(3, 2, 'https://shop.example/c', 'Lamp', '30', 0, 'automatic', 'blocked', "
        "2, '27', 1, 2)"
    )
    await conn.execute("INSERT INTO price_history(product_id, price) VALUES (1, '10'), (3, '30')")
    await conn.execute("INSERT INTO scraper_health(domain) VALUES ('shop.example')")
    await conn.execute("INSERT INTO notification_prefs(user_id) VALUES (1)")
    await conn.execute(
        "INSERT INTO digest_queue(user_id, product_id, alert_payload_json) VALUES (2, 3, '{}')"
    )
    await conn.commit()


def test_the_file_matches_its_pinned_hash():
    assert hashlib.sha256(FILE.read_bytes()).hexdigest() == SHA


def test_no_comment_contains_a_semicolon():
    comments = [line for line in FILE.read_text().splitlines() if line.startswith("--")]
    assert comments
    assert all(";" not in line for line in comments)


def test_the_migration_names_only_image_url():
    assert re.findall(r"ADD COLUMN (\w+)", FILE.read_text()) == ["image_url"]


async def test_upgrading_a_populated_schema_17_only_adds_image_url():
    async with aiosqlite.connect(":memory:") as conn:
        await seed(conn)
        tables = [
            r[0]
            for r in await rows(conn, "SELECT name FROM sqlite_master WHERE type='table'")
            if r[0] != "schema_version"
        ]
        before = {table: await rows(conn, f"SELECT * FROM {table}") for table in tables}
        previous_columns = await columns(conn)
        assert await apply_migrations(conn, MIGRATIONS, max_version=18) == 18
        assert await columns(conn) == [*previous_columns, "image_url"]
        after = await rows(conn, "SELECT * FROM products")
        assert [r[:-1] for r in after] == before["products"]
        assert all(r[-1] is None for r in after)
        for table in tables:
            if table != "products":
                assert await rows(conn, f"SELECT * FROM {table}") == before[table]
        assert await rows(conn, "PRAGMA integrity_check") == [("ok",)]
        assert await rows(conn, "PRAGMA foreign_key_check") == []


async def test_applying_again_is_a_no_op():
    async with aiosqlite.connect(":memory:") as conn:
        await seed(conn)
        assert await apply_migrations(conn, MIGRATIONS, max_version=18) == 18
        before = await rows(conn, "SELECT type, name, sql FROM sqlite_master ORDER BY name")
        assert await apply_migrations(conn, MIGRATIONS, max_version=18) == 18
        assert await rows(conn, "SELECT type, name, sql FROM sqlite_master ORDER BY name") == before


async def test_a_failing_last_statement_rolls_back_the_whole_migration(tmp_path):
    broken = tmp_path / "migrations"
    shutil.copytree(MIGRATIONS, broken)
    (broken / FILE.name).write_text(
        FILE.read_text().rstrip()
        + "\nALTER TABLE products ADD COLUMN invalid INTEGER CHECK (missing_check_column > 0);\n"
    )
    async with aiosqlite.connect(":memory:") as conn:
        await seed(conn)
        with pytest.raises(MigrationError):
            await apply_migrations(conn, broken)
        assert await get_current_version(conn) == 17
        assert "image_url" not in await columns(conn)


@pytest.mark.parametrize(
    "value",
    [
        "http://a.example/x.jpg",
        "javascript:alert(1)",
        "https://",
        "",
        "h" * 2049,
        "https://" + "a" * 2041,
        "\0https://x.example/a",
        "https://x\0" + "a" * 3000,
        b"https://cdn.example/x.jpg",
    ],
)
async def test_the_check_refuses_what_the_code_never_writes(value):
    async with aiosqlite.connect(":memory:") as conn:
        await seed(conn)
        await apply_migrations(conn, MIGRATIONS)
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute("UPDATE products SET image_url=? WHERE id=1", (value,))
        await conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            await conn.execute(
                "INSERT INTO products(user_id, url, image_url) "
                "VALUES (1, 'https://shop.example/d', ?)",
                (value,),
            )
        await conn.rollback()
        for good in ("https://cdn.example/x.jpg", "https://" + "a" * 2040, None):
            await conn.execute("UPDATE products SET image_url=? WHERE id=1", (good,))
            await conn.execute(
                "INSERT INTO products(user_id, url, image_url) VALUES (1, ?, ?)",
                (f"https://shop.example/{good}", good),
            )
        await conn.commit()


async def test_the_repository_round_trips_the_image_url():
    async with aiosqlite.connect(":memory:") as conn:
        await seed(conn)
        await apply_migrations(conn, MIGRATIONS)
        repo = Repository(conn)
        url = "https://cdn.example/a.jpg"
        product_id = await repo.add_product(
            user_id=2,
            url="https://shop.example/new",
            name="New",
            domain="shop.example",
            initial_price=Decimal("7"),
            currency="EUR",
            image_url=url,
        )
        record = await repo.get_product(product_id)
        assert record is not None
        assert record.image_url == url
        await repo.update_price_and_image(1, Decimal("8"), url)
        await repo.update_price_and_image(2, Decimal("9"), "https://cdn.example/b.jpg")
        all_products = [*await repo.get_all_products(1), *await repo.get_all_products(2)]
        assert {p.id: p.image_url for p in all_products} == {
            1: url,
            2: "https://cdn.example/b.jpg",
            3: None,
            product_id: url,
        }
        assert await repo.list_image_urls() == {url, "https://cdn.example/b.jpg"}
        await repo.update_price_and_image(product_id, Decimal("6"), None)
        record = await repo.get_product(product_id)
        assert record is not None
        assert record.image_url is None
        statements: list[str] = []
        await conn.set_trace_callback(statements.append)
        await repo.update_price_and_image(1, Decimal("11"), url)
        assert sum(s.startswith("UPDATE ") for s in statements) == 1
        assert sum(s == "COMMIT" for s in statements) == 1


@settings(max_examples=100, deadline=None)
@given(st.lists(st.tuples(st.integers(-10000, 10000), st.booleans()), min_size=1, max_size=12))
def test_update_price_and_image_matches_update_price(readings):
    async def check() -> None:
        async with aiosqlite.connect(":memory:") as conn:
            await seed(conn)
            await apply_migrations(conn, MIGRATIONS)
            repo = Repository(conn)
            await conn.execute("UPDATE products SET current_price=NULL WHERE id IN (1, 2)")
            await conn.commit()
            for amount, has_image in readings:
                price = Decimal(amount) / 100
                image = "https://cdn.example/a.jpg" if has_image else None
                await repo.update_price(1, price)
                await repo.update_price_and_image(2, price, image)
                a, b = await repo.get_product(1), await repo.get_product(2)
                assert a is not None
                assert b is not None
                assert (a.current_price, a.lowest_price, a.highest_price) == (
                    b.current_price,
                    b.lowest_price,
                    b.highest_price,
                )
                assert b.image_url == image

    asyncio.run(check())


async def test_the_previous_release_repository_still_works_on_schema_18():
    old = _load_compat_repository()
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await seed(conn)
        await apply_migrations(conn, MIGRATIONS)
        assert await get_current_version(conn) == 19
        repo = old.Repository(conn)
        assert (await repo.get_product(1)).name == "Kettle"
        assert [u.user_id for u in await repo.list_users()] == [1, 2]
        product_id = await repo.add_product(
            user_id=1,
            url="https://shop.example/old",
            name="Old",
            domain="shop.example",
            initial_price=Decimal("4"),
            currency="EUR",
        )
        await repo.update_price(product_id, Decimal("3"))
        assert (await repo.get_product(product_id)).current_price == Decimal("3")


def test_no_product_query_selects_star():
    assert _PRODUCT_COLS.split(", ")[31] == "image_url"
    for path in (ROOT / "src/price_tracker/db").glob("*.py"):
        assert not re.search(r"SELECT\s+\*\s+FROM\s+products", path.read_text(), re.IGNORECASE)
