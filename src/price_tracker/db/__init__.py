"""Database layer: migrations, repository and runtime connection helpers."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite


async def apply_runtime_pragmas(conn: aiosqlite.Connection) -> None:
    """Apply the per-connection PRAGMAs every runtime connection needs.

    ``PRAGMA foreign_keys`` is connection-scoped and OFF by default in
    SQLite: the ``PRAGMA foreign_keys=ON`` in 001_initial.sql only affected
    the ephemeral connection that ran the migration, so ON DELETE CASCADE
    never fired on the long-lived runtime connection (#57).
    """
    await conn.execute("PRAGMA foreign_keys=ON")
