"""Versioned SQL migrator for SQLite."""

from __future__ import annotations

import contextlib
import logging
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

SCHEMA_VERSION_TABLE = "schema_version"
_FILENAME_RE = re.compile(r"^(\d{3})_.+\.sql$")
_PRAGMA_RE = re.compile(r"PRAGMA\b", re.IGNORECASE)


class MigrationError(RuntimeError):
    """A migration could not be applied; its changes were rolled back."""

    def __init__(self, version: int, filename: str, reason: str) -> None:
        super().__init__(f"migration {version:03d} ({filename}) failed: {reason}")
        self.version = version
        self.filename = filename


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


def _strip_leading_comments(sql: str) -> str:
    """Return ``sql`` without leading whitespace, ``--`` and ``/* */`` comments."""
    text = sql
    while True:
        text = text.lstrip()
        if text.startswith("--"):
            newline = text.find("\n")
            text = "" if newline == -1 else text[newline + 1 :]
        elif text.startswith("/*"):
            end = text.find("*/", 2)
            text = "" if end == -1 else text[end + 2 :]
        else:
            return text


def _split_statements(sql: str) -> list[str]:
    """Split a migration script into complete SQL statements.

    A ``;`` only ends a statement when SQLite agrees the text so far is a
    complete statement, so semicolons inside comments, string literals and
    ``CREATE TRIGGER ... BEGIN ... END`` bodies are kept. Empty statements are
    dropped. Raises ``ValueError`` if code (not just comments or whitespace)
    follows the last complete statement.
    """
    statements: list[str] = []
    buffer = ""
    pieces = sql.split(";")
    for piece in pieces[:-1]:
        buffer += piece + ";"
        if sqlite3.complete_statement(buffer):
            if _strip_leading_comments(buffer) != ";":
                statements.append(buffer.strip())
            buffer = ""
    tail = buffer + pieces[-1]
    if _strip_leading_comments(tail):
        raise ValueError(f"unterminated statement at end of file: {tail.strip()[:80]!r}")
    return statements


def _is_pragma(statement: str) -> bool:
    return _PRAGMA_RE.match(_strip_leading_comments(statement)) is not None


def _parse_migration(sql: str) -> tuple[list[str], list[str]]:
    """Return ``(prologue, body)``: the leading PRAGMA statements and the rest.

    Some PRAGMAs cannot run inside a transaction (``journal_mode`` raises,
    ``foreign_keys`` is silently ignored), so they are only accepted as a
    prologue that runs before the migration's transaction. A PRAGMA after the
    first non-PRAGMA statement raises ``ValueError``.
    """
    prologue: list[str] = []
    body: list[str] = []
    for stmt in _split_statements(sql):
        if not _is_pragma(stmt):
            body.append(stmt)
        elif body:
            raise ValueError(
                "PRAGMA is only allowed before the first non-PRAGMA statement: "
                f"{_strip_leading_comments(stmt)[:80]!r}"
            )
        else:
            prologue.append(stmt)
    return prologue, body


async def _execute_statements(conn: aiosqlite.Connection, statements: list[str]) -> None:
    """Execute migration statements. Each ALTER TABLE ADD COLUMN is made idempotent."""
    for stmt in statements:
        code = _strip_leading_comments(stmt)
        upper = code.upper()
        if upper.startswith("ALTER TABLE") and "ADD COLUMN" in upper:
            m = re.match(
                r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)",
                code,
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

    Each migration runs in its own transaction together with its
    ``schema_version`` row: if any statement fails, the migration is rolled
    back entirely and ``MigrationError`` names its version and file. Leading
    PRAGMA statements run before that transaction and are not rolled back.
    """
    await _ensure_schema_version_table(conn)
    current = await get_current_version(conn)
    pending = [(v, p) for v, p in list_migrations(migrations_dir) if v > current]
    if max_version is not None:
        pending = [(v, p) for v, p in pending if v <= max_version]

    for version, path in pending:
        logger.info("Applying migration %03d (%s)", version, path.name)
        try:
            prologue, body = _parse_migration(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise MigrationError(version, path.name, str(exc)) from exc
        try:
            for stmt in prologue:
                await conn.execute(stmt)
            # The sqlite3 module does not open implicit transactions for DDL,
            # so without an explicit BEGIN every CREATE/DROP/ALTER would be
            # committed on its own and a failure would leave a half-applied
            # schema with no version recorded.
            await conn.execute("BEGIN")
            await _execute_statements(conn, body)
            await conn.execute(
                f"INSERT INTO {SCHEMA_VERSION_TABLE}(version) VALUES (?)",
                (version,),
            )
            await conn.commit()
        except BaseException as exc:
            await conn.rollback()
            if isinstance(exc, sqlite3.Error):
                raise MigrationError(version, path.name, str(exc)) from exc
            raise

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
