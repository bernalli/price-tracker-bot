"""Every product record read from the database carries the product's creation time.

The scheduler's floor between two scheduled checks counts from the latest
attempt or, when the product was never checked, from its creation. The
``created_at`` column already exists; the record now reads it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.core.scheduler_common import _parse_db_timestamp
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.models import ProductRecord
from price_tracker.db.repository import Repository
from price_tracker.db.repository_common import _PRODUCT_COLS

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
USER_ID = 1
URL = "https://example.com/p/1"


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repository = Repository(conn)
    await repository.ensure_user(user_id=USER_ID)
    try:
        yield repository
    finally:
        await conn.close()


async def _add(repo: Repository) -> int:
    return await repo.add_product(
        user_id=USER_ID,
        url=URL,
        name="Widget",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )


async def _sql(repo: Repository, statement: str, params: tuple[object, ...]) -> None:
    await repo._conn.execute(statement, params)
    await repo._conn.commit()


async def _via_get_product(repo: Repository, pid: int) -> ProductRecord | None:
    return await repo.get_product(pid)


async def _via_list_products_for_user(repo: Repository, pid: int) -> ProductRecord | None:
    records = await repo.list_products_for_user(user_id=USER_ID)
    return next((record for record in records if record.id == pid), None)


async def _via_list_auto_suspended_products(repo: Repository, pid: int) -> ProductRecord | None:
    await _sql(
        repo,
        "UPDATE products SET is_active = 0, suspension_kind = 'automatic' WHERE id = ?",
        (pid,),
    )
    records = await repo.list_auto_suspended_products(user_id=USER_ID)
    return next((record for record in records if record.id == pid), None)


async def _via_list_pending_status_notices(repo: Repository, pid: int) -> ProductRecord | None:
    await _sql(repo, "UPDATE products SET pending_notice_kind = 'unavailable' WHERE id = ?", (pid,))
    records = await repo.list_pending_status_notices()
    return next((record for record in records if record.id == pid), None)


async def _via_get_product_by_url_for_user(repo: Repository, pid: int) -> ProductRecord | None:
    del pid
    return await repo.get_product_by_url_for_user(URL, USER_ID)


async def _via_get_product_for_user(repo: Repository, pid: int) -> ProductRecord | None:
    return await repo.get_product_for_user(pid, USER_ID)


_READERS: dict[str, Callable[[Repository, int], Awaitable[ProductRecord | None]]] = {
    "get_product": _via_get_product,
    "list_products_for_user": _via_list_products_for_user,
    "list_auto_suspended_products": _via_list_auto_suspended_products,
    "list_pending_status_notices": _via_list_pending_status_notices,
    "get_product_by_url_for_user": _via_get_product_by_url_for_user,
    "get_product_for_user": _via_get_product_for_user,
}


@pytest.mark.parametrize("reader", sorted(_READERS))
async def test_every_product_reader_carries_created_at(repo: Repository, reader: str) -> None:
    pid = await _add(repo)

    record = await _READERS[reader](repo, pid)

    assert record is not None
    assert isinstance(record.created_at, str)
    created = _parse_db_timestamp(record.created_at)
    assert abs(datetime.now(UTC) - created) <= timedelta(seconds=120)


async def test_stored_created_at_is_returned_verbatim(repo: Repository) -> None:
    pid = await _add(repo)
    await _sql(
        repo, "UPDATE products SET created_at = ? WHERE id = ?", ("2026-01-02 03:04:05", pid)
    )

    record = await repo.get_product(pid)

    assert record is not None
    assert record.created_at == "2026-01-02 03:04:05"


def test_record_built_by_hand_has_no_created_at() -> None:
    record = ProductRecord(
        id=1,
        user_id=USER_ID,
        url=URL,
        name="Widget",
        domain="example.com",
        initial_price=None,
        current_price=None,
        lowest_price=None,
        highest_price=None,
        target_price=None,
        threshold_type="percentage",
        threshold_value=Decimal("10"),
        is_active=True,
        is_available=True,
        consecutive_errors=0,
        currency="EUR",
        check_interval_minutes=None,
        last_checked_at=None,
        last_notified_at=None,
    )

    assert record.created_at is None


def test_created_at_is_appended_after_every_existing_column() -> None:
    columns = _PRODUCT_COLS.split(", ")

    assert columns[31] == "image_url"
    assert columns[-1] == "created_at"
