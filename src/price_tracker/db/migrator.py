"""Versioned SQL migrator for SQLite."""

from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

SCHEMA_VERSION_TABLE = "schema_version"
_FILENAME_RE = re.compile(r"^(\d{3})_.+\.sql$")


def list_migrations(migrations_dir: Path) -> list[tuple[int, Path]]:
    """Return [(version, path), ...] sorted by version ascending."""
    out: list[tuple[int, Path]] = []
    for f in sorted(migrations_dir.glob("*.sql")):
        m = _FILENAME_RE.match(f.name)
        if not m:
            continue
        out.append((int(m.group(1)), f))
    out.sort(key=lambda x: x[0])
    return out


async def _ensure_schema_version_table(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        f"CREATE TABLE IF NOT EXISTS {SCHEMA_VERSION_TABLE} ("
        f"  version INTEGER PRIMARY KEY,"
        f"  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
        f")"
    )
    await conn.commit()


async def get_current_version(conn: aiosqlite.Connection) -> int:
    """Return the highest applied schema version, or 0 if no migrations applied yet."""
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (SCHEMA_VERSION_TABLE,),
    )
    if await cursor.fetchone() is None:
        return 0
    cursor = await conn.execute(f"SELECT MAX(version) FROM {SCHEMA_VERSION_TABLE}")
    row = await cursor.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


async def _column_exists(conn: aiosqlite.Connection, table: str, column: str) -> bool:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    rows = await cursor.fetchall()
    return any(r[1] == column for r in rows)


async def _execute_migration_sql(conn: aiosqlite.Connection, sql: str) -> None:
    """Execute a migration SQL. Each ALTER TABLE ADD COLUMN is wrapped to be idempotent."""
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    for stmt in statements:
        upper = stmt.upper()
        if upper.startswith("ALTER TABLE") and "ADD COLUMN" in upper:
            m = re.match(
                r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
                stmt,
                re.IGNORECASE,
            )
            if m:
                table, col = m.group(1), m.group(2)
                if await _column_exists(conn, table, col):
                    logger.debug("Skip ADD COLUMN %s.%s (already exists)", table, col)
                    continue
        await conn.execute(stmt)


async def apply_migrations(
    conn: aiosqlite.Connection,
    migrations_dir: Path,
    *,
    max_version: int | None = None,
) -> int:
    """Apply all unapplied migrations in order. Returns the new current version.

    If ``max_version`` is given, only migrations with version ``<= max_version``
    are applied. Useful in tests to bootstrap an older schema baseline.
    """
    await _ensure_schema_version_table(conn)
    current = await get_current_version(conn)
    pending = [(v, p) for v, p in list_migrations(migrations_dir) if v > current]
    if max_version is not None:
        pending = [(v, p) for v, p in pending if v <= max_version]

    for version, path in pending:
        logger.info("Applying migration %03d (%s)", version, path.name)
        sql = path.read_text(encoding="utf-8")
        await _execute_migration_sql(conn, sql)
        await conn.execute(
            f"INSERT INTO {SCHEMA_VERSION_TABLE}(version) VALUES (?)",
            (version,),
        )
        await conn.commit()

    return await get_current_version(conn)


_DEFAULT_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class Migrator:
    """Object-oriented wrapper around the functional migrator helpers.

    Usage::

        migrator = Migrator(db_path=Path("my.db"))
        await migrator.migrate()
        async with migrator._connect() as conn:
            ...
    """

    def __init__(
        self,
        db_path: Path,
        *,
        migrations_dir: Path = _DEFAULT_MIGRATIONS_DIR,
        max_version: int | None = None,
    ) -> None:
        self._db_path = db_path
        self._migrations_dir = migrations_dir
        self._max_version = max_version

    async def migrate(self) -> int:
        """Apply all pending migrations. Returns the resulting schema version."""
        async with self._connect() as conn:
            return await apply_migrations(conn, self._migrations_dir, max_version=self._max_version)

    @contextlib.asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        """Open a connection to the database file."""
        async with aiosqlite.connect(self._db_path) as conn:
            yield conn
