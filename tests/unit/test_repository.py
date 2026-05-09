"""Tests for the SQLite repository layer."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import pytest_asyncio

from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    try:
        yield Repository(conn)
    finally:
        await conn.close()


async def test_add_product_returns_id(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://example.com/p/1",
        name="Widget",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    assert pid > 0


async def test_get_product_round_trip(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://example.com/p/1",
        name="Widget",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    p = await repo.get_product(pid)
    assert p is not None
    assert p.id == pid
    assert p.name == "Widget"
    assert p.initial_price == Decimal("100")
    assert p.currency == "EUR"


async def test_list_products_for_user(repo: Repository):
    await repo.add_product(
        user_id=1,
        url="https://x/1",
        name="A",
        domain="x",
        initial_price=Decimal("10"),
        currency="EUR",
    )
    await repo.add_product(
        user_id=1,
        url="https://x/2",
        name="B",
        domain="x",
        initial_price=Decimal("20"),
        currency="EUR",
    )
    await repo.add_product(
        user_id=2,
        url="https://x/3",
        name="C",
        domain="x",
        initial_price=Decimal("30"),
        currency="EUR",
    )
    items = await repo.list_products_for_user(user_id=1)
    names = [i.name for i in items]
    assert "A" in names
    assert "B" in names
    assert "C" not in names


async def test_set_config_and_get_config(repo: Repository):
    assert await repo.get_config("foo") is None
    await repo.set_config("foo", "bar")
    assert await repo.get_config("foo") == "bar"
    await repo.set_config("foo", "baz")
    assert await repo.get_config("foo") == "baz"


async def test_user_admin_flow(repo: Repository):
    await repo.ensure_user(user_id=42, is_admin=True)
    assert await repo.is_user_allowed(42) is True
    user = await repo.get_user(42)
    assert user is not None
    assert user.is_admin is True


async def test_increment_errors_and_reset(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://x/1",
        name="A",
        domain="x",
        initial_price=Decimal("10"),
        currency="EUR",
    )
    await repo.increment_errors(pid)
    await repo.increment_errors(pid)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.consecutive_errors == 2
    await repo.reset_errors(pid)
    p = await repo.get_product(pid)
    assert p is not None
    assert p.consecutive_errors == 0


async def test_add_price_history_and_query(repo: Repository):
    pid = await repo.add_product(
        user_id=1,
        url="https://x/1",
        name="A",
        domain="x",
        initial_price=Decimal("10"),
        currency="EUR",
    )
    await repo.add_price_history(pid, Decimal("9"))
    await repo.add_price_history(pid, Decimal("8"))
    history = await repo.get_price_history(pid, limit=10)
    assert len(history) == 2
    # Ordered DESC by checked_at
    assert history[0].price == Decimal("8")
    assert history[1].price == Decimal("9")
