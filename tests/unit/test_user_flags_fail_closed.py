"""Only the integer 1 means true in ``users.is_admin`` / ``users.is_active``.

The columns have no CHECK, so a hand-edited or damaged row can hold any value.
Rows are written with plain SQL here, with no patching of the repository.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS = Path(__file__).resolve().parents[2] / "src/price_tracker/db/migrations"


@pytest_asyncio.fixture
async def conn() -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(":memory:") as connection:
        connection.row_factory = aiosqlite.Row
        await apply_migrations(connection, MIGRATIONS)
        yield connection


async def _insert(conn: aiosqlite.Connection, admin: object, active: object) -> int:
    await conn.execute(
        "INSERT INTO users(user_id, is_admin, is_active) VALUES (5, ?, ?)", (admin, active)
    )
    await conn.commit()
    return 5


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [2, -1, "x"])
async def test_a_non_one_value_reads_false_everywhere(
    conn: aiosqlite.Connection, value: object
) -> None:
    uid = await _insert(conn, value, value)
    repo = Repository(conn)
    assert await repo.is_user_admin(uid) is False
    assert await repo.is_user_allowed(uid) is False
    user = await repo.get_user(uid)
    assert user is not None
    assert (user.is_admin, user.is_active) == (False, False)
    (listed,) = await repo.list_users()
    assert (listed.is_admin, listed.is_active) == (False, False)
    assert await repo.list_active_users() == []


@pytest.mark.asyncio
async def test_one_reads_true_everywhere(conn: aiosqlite.Connection) -> None:
    uid = await _insert(conn, 1, 1)
    repo = Repository(conn)
    assert await repo.is_user_admin(uid) is True
    assert await repo.is_user_allowed(uid) is True
    user = await repo.get_user(uid)
    assert user is not None
    assert (user.is_admin, user.is_active) == (True, True)
    (listed,) = await repo.list_users()
    assert (listed.is_admin, listed.is_active) == (True, True)
    assert [u.user_id for u in await repo.list_active_users()] == [uid]


@pytest.mark.asyncio
async def test_the_text_one_is_converted_by_the_column_affinity(conn: aiosqlite.Connection) -> None:
    uid = await _insert(conn, "1", "1")
    cursor = await conn.execute(
        "SELECT typeof(is_admin), is_admin FROM users WHERE user_id = ?", (uid,)
    )
    row = await cursor.fetchone()
    assert row is not None
    assert tuple(row) == ("integer", 1)
    assert await Repository(conn).is_user_admin(uid) is True


@pytest.mark.asyncio
async def test_null_is_refused_by_the_schema(conn: aiosqlite.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await _insert(conn, None, 1)
    with pytest.raises(sqlite3.IntegrityError):
        await _insert(conn, 1, None)
