"""Migrator robustness: statement splitting, PRAGMA prologue and per-migration atomicity.

Every case writes its own migration files into a temporary directory and runs
against a database FILE, so that "what survives a failed migration" can be
observed from a fresh connection, the way a restarted process would see it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest

from price_tracker.db import apply_runtime_pragmas
from price_tracker.db import migrator as migrator_module
from price_tracker.db.migrator import apply_migrations, get_current_version, list_migrations

if TYPE_CHECKING:
    from collections.abc import Sequence

REAL_MIGRATIONS_DIR = Path(migrator_module.__file__).parent / "migrations"
REAL_LATEST_VERSION = max(v for v, _ in list_migrations(REAL_MIGRATIONS_DIR))

# ``""`` is the sqlite3 default (legacy implicit transactions, as used by the
# runtime connection in main.py); ``None`` is autocommit, where every statement
# not wrapped in an explicit transaction is durable on its own.
ISOLATION_LEVELS = [pytest.param("", id="default-isolation"), pytest.param(None, id="autocommit")]


def _write_migrations(directory: Path, files: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, sql in files.items():
        (directory / name).write_text(sql, encoding="utf-8")
    return directory


async def _schema(conn: aiosqlite.Connection) -> list[tuple[Any, ...]]:
    cursor = await conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    )
    return [tuple(row) for row in await cursor.fetchall()]


async def _columns(conn: aiosqlite.Connection, table: str) -> list[str]:
    cursor = await conn.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in await cursor.fetchall()]


async def _rows(conn: aiosqlite.Connection, sql: str) -> Sequence[tuple[Any, ...]]:
    cursor = await conn.execute(sql)
    return [tuple(row) for row in await cursor.fetchall()]


async def _table_exists(conn: aiosqlite.Connection, name: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,),
    )
    return await cursor.fetchone() is not None


# ---------------------------------------------------------------------------
# (a) semicolons that do not terminate a statement
# ---------------------------------------------------------------------------

_COMMENT_SQL = """\
-- Items table; the semicolon in this comment must not end a statement.
CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT NOT NULL);
INSERT INTO items (label) VALUES ('plain');
"""

_COMMENT_SQL_CONTROL = _COMMENT_SQL.replace("table; the", "table, the")

_LITERAL_SQL = """\
CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT NOT NULL);
INSERT INTO items (label) VALUES ('a;b');
INSERT INTO items (label) VALUES ('x -- y; z');
"""

_LITERAL_SQL_CONTROL = _LITERAL_SQL.replace("'a;b'", "'a,b'").replace("'x -- y; z'", "'x y z'")

_TRIGGER_SQL = """\
CREATE TABLE items (id INTEGER PRIMARY KEY, label TEXT NOT NULL);
CREATE TABLE audit (item_id INTEGER NOT NULL, kind TEXT NOT NULL);
CREATE TRIGGER items_audit AFTER INSERT ON items
BEGIN
    INSERT INTO audit (item_id, kind) VALUES (NEW.id, 'first');
    INSERT INTO audit (item_id, kind) VALUES (NEW.id, 'second');
END;
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", [_COMMENT_SQL, _COMMENT_SQL_CONTROL], ids=["defect", "control"])
async def test_semicolon_inside_line_comment_does_not_split(tmp_path: Path, sql: str) -> None:
    mig = _write_migrations(tmp_path / "m", {"001_items.sql": sql})
    async with aiosqlite.connect(tmp_path / "db.sqlite") as conn:
        assert await apply_migrations(conn, mig) == 1
        assert await _rows(conn, "SELECT label FROM items") == [("plain",)]
        assert await _columns(conn, "items") == ["id", "label"]


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", [_LITERAL_SQL, _LITERAL_SQL_CONTROL], ids=["defect", "control"])
async def test_semicolon_inside_string_literal_is_preserved(tmp_path: Path, sql: str) -> None:
    mig = _write_migrations(tmp_path / "m", {"001_items.sql": sql})
    expected = [("a;b",), ("x -- y; z",)] if sql is _LITERAL_SQL else [("a,b",), ("x y z",)]
    async with aiosqlite.connect(tmp_path / "db.sqlite") as conn:
        assert await apply_migrations(conn, mig) == 1
        rows = await _rows(conn, "SELECT label FROM items ORDER BY id")
        assert rows == expected
        # Byte for byte, not just "looks equal".
        assert [r[0].encode("utf-8") for r in rows] == [e[0].encode("utf-8") for e in expected]


@pytest.mark.asyncio
@pytest.mark.parametrize("via_migrator", [True, False], ids=["defect", "control"])
async def test_trigger_body_with_several_statements_is_one_statement(
    tmp_path: Path, via_migrator: bool
) -> None:
    # A trigger body always contains ``;`` before END, so no variant of it
    # survives a naive split. The control runs the very same SQL through
    # SQLite's own script executor, proving the fixture is valid SQL and that
    # a red "defect" case is the migrator's doing.
    mig = _write_migrations(tmp_path / "m", {"001_items.sql": _TRIGGER_SQL})
    async with aiosqlite.connect(tmp_path / "db.sqlite") as conn:
        if via_migrator:
            assert await apply_migrations(conn, mig) == 1
        else:
            await conn.executescript(_TRIGGER_SQL)
        await conn.execute("INSERT INTO items (label) VALUES ('fires')")
        await conn.commit()
        kinds = [r[0] for r in await _rows(conn, "SELECT kind FROM audit ORDER BY rowid")]
        assert kinds == ["first", "second"]


# ---------------------------------------------------------------------------
# (b) ADD COLUMN idempotency guard with a leading comment
# ---------------------------------------------------------------------------

_BASE_T = {"001_t.sql": "CREATE TABLE t (id INTEGER PRIMARY KEY, c TEXT);\n"}


@pytest.mark.asyncio
async def test_commented_add_column_on_existing_column_is_skipped(tmp_path: Path) -> None:
    mig = _write_migrations(
        tmp_path / "m",
        {
            **_BASE_T,
            "002_readd.sql": "-- re-add c, it exists\nALTER TABLE t ADD COLUMN c TEXT;\n",
        },
    )
    async with aiosqlite.connect(tmp_path / "db.sqlite") as conn:
        assert await apply_migrations(conn, mig) == 2
        assert await _columns(conn, "t") == ["id", "c"]


@pytest.mark.asyncio
async def test_commented_add_column_on_new_column_is_added(tmp_path: Path) -> None:
    mig = _write_migrations(
        tmp_path / "m",
        {**_BASE_T, "002_add.sql": "/* new column */\n-- d\nALTER TABLE t ADD COLUMN d INTEGER;\n"},
    )
    async with aiosqlite.connect(tmp_path / "db.sqlite") as conn:
        assert await apply_migrations(conn, mig) == 2
        assert await _columns(conn, "t") == ["id", "c", "d"]


# ---------------------------------------------------------------------------
# (c) a failing migration leaves no trace; (e) the error names it
# ---------------------------------------------------------------------------

_ATOMIC_BASE = """\
CREATE TABLE base (id INTEGER PRIMARY KEY, v TEXT NOT NULL);
INSERT INTO base (v) VALUES ('keep1');
INSERT INTO base (v) VALUES ('keep2');
"""

_ATOMIC_GOOD = """\
CREATE TABLE extra (id INTEGER PRIMARY KEY);
ALTER TABLE base ADD COLUMN flag INTEGER;
INSERT INTO base (v) VALUES ('new');
"""

_ATOMIC_BAD = _ATOMIC_GOOD + "THIS IS NOT SQL;\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("isolation", ISOLATION_LEVELS)
async def test_failed_migration_is_rolled_back_entirely(
    tmp_path: Path, isolation: str | None
) -> None:
    mig = _write_migrations(tmp_path / "m", {"001_base.sql": _ATOMIC_BASE})
    db = tmp_path / "db.sqlite"
    async with aiosqlite.connect(db, isolation_level=isolation) as conn:
        assert await apply_migrations(conn, mig) == 1
        schema_before = await _schema(conn)

        _write_migrations(mig, {"002_extra.sql": _ATOMIC_BAD})
        with pytest.raises(Exception, match="syntax error"):
            await apply_migrations(conn, mig)

        # Nothing half-done may stay pending on the caller's connection: a
        # later commit by the application would otherwise persist it.
        assert not conn.in_transaction
        await conn.commit()
        assert await _schema(conn) == schema_before

    # What a restarted process sees.
    async with aiosqlite.connect(db) as conn:
        assert await _schema(conn) == schema_before
        assert not await _table_exists(conn, "extra")
        assert await _columns(conn, "base") == ["id", "v"]
        assert await get_current_version(conn) == 1
        assert await _rows(conn, "SELECT id, v FROM base ORDER BY id") == [
            (1, "keep1"),
            (2, "keep2"),
        ]

        # Fixed file: the migration now applies cleanly and is recorded.
        _write_migrations(mig, {"002_extra.sql": _ATOMIC_GOOD})
        assert await apply_migrations(conn, mig) == 2
        assert await _table_exists(conn, "extra")
        assert await _columns(conn, "base") == ["id", "v", "flag"]
        assert await _rows(conn, "SELECT v FROM base ORDER BY id") == [
            ("keep1",),
            ("keep2",),
            ("new",),
        ]


@pytest.mark.asyncio
async def test_failed_migration_error_names_version_and_file(tmp_path: Path) -> None:
    mig = _write_migrations(
        tmp_path / "m", {"001_base.sql": _ATOMIC_BASE, "002_extra.sql": _ATOMIC_BAD}
    )
    async with aiosqlite.connect(tmp_path / "db.sqlite") as conn:
        with pytest.raises(Exception) as excinfo:  # noqa: PT011 - message asserted below
            await apply_migrations(conn, mig)
        message = str(excinfo.value)
        assert "002" in message
        assert "002_extra.sql" in message
        # The underlying SQLite error is kept, not swallowed.
        assert "syntax error" in message


# ---------------------------------------------------------------------------
# (d) table rebuild in the style of 015, failing between DROP and RENAME
# ---------------------------------------------------------------------------

_REBUILD_BASE = """\
CREATE TABLE q (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL);
CREATE INDEX idx_q_payload ON q(payload);
INSERT INTO q (payload) VALUES ('one');
INSERT INTO q (payload) VALUES ('two');
INSERT INTO q (payload) VALUES ('three');
"""

_REBUILD_BROKEN = """\
CREATE TABLE q_v2 (id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT);
INSERT INTO q_v2 (id, payload) SELECT id, payload FROM q;
DROP INDEX IF EXISTS idx_q_payload;
DROP TABLE q;
THIS IS NOT SQL;
ALTER TABLE q_v2 RENAME TO q;
CREATE INDEX IF NOT EXISTS idx_q_payload ON q(payload);
"""


@pytest.mark.asyncio
@pytest.mark.parametrize("isolation", ISOLATION_LEVELS)
async def test_rebuild_failing_after_drop_keeps_original_table(
    tmp_path: Path, isolation: str | None
) -> None:
    mig = _write_migrations(tmp_path / "m", {"001_q.sql": _REBUILD_BASE})
    db = tmp_path / "db.sqlite"
    async with aiosqlite.connect(db, isolation_level=isolation) as conn:
        assert await apply_migrations(conn, mig) == 1
        schema_before = await _schema(conn)

        _write_migrations(mig, {"002_rebuild.sql": _REBUILD_BROKEN})
        with pytest.raises(Exception, match="syntax error"):
            await apply_migrations(conn, mig)
        await conn.commit()  # a later, unrelated commit by the application

    async with aiosqlite.connect(db) as conn:
        assert await _table_exists(conn, "q")
        assert not await _table_exists(conn, "q_v2")
        assert await _rows(conn, "SELECT id, payload FROM q ORDER BY id") == [
            (1, "one"),
            (2, "two"),
            (3, "three"),
        ]
        assert await _schema(conn) == schema_before
        assert await get_current_version(conn) == 1


# ---------------------------------------------------------------------------
# (f) the real migrations on a fresh FILE database: WAL survives the change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_migrations_on_fresh_file_db_enable_wal(tmp_path: Path) -> None:
    async with aiosqlite.connect(tmp_path / "fresh.db") as conn:
        await apply_runtime_pragmas(conn)  # same order as main.py
        assert await apply_migrations(conn, REAL_MIGRATIONS_DIR) == REAL_LATEST_VERSION
        assert REAL_LATEST_VERSION >= 15
        assert await _rows(conn, "PRAGMA journal_mode") == [("wal",)]
        assert await get_current_version(conn) == REAL_LATEST_VERSION


# ---------------------------------------------------------------------------
# (g) PRAGMA only as a leading prologue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pragma_after_ddl_is_rejected_before_touching_schema(tmp_path: Path) -> None:
    mig = _write_migrations(tmp_path / "m", _BASE_T)
    async with aiosqlite.connect(tmp_path / "db.sqlite") as conn:
        assert await apply_migrations(conn, mig) == 1
        schema_before = await _schema(conn)

        _write_migrations(
            mig,
            {"002_late_pragma.sql": "CREATE TABLE later (id INTEGER);\nPRAGMA foreign_keys=ON;\n"},
        )
        with pytest.raises(Exception, match="PRAGMA") as excinfo:
            await apply_migrations(conn, mig)
        assert "002" in str(excinfo.value)
        assert "002_late_pragma.sql" in str(excinfo.value)
        assert not conn.in_transaction
        assert await _schema(conn) == schema_before
        assert not await _table_exists(conn, "later")
        assert await get_current_version(conn) == 1


@pytest.mark.asyncio
async def test_leading_pragma_prologue_runs_outside_the_transaction(tmp_path: Path) -> None:
    # Control for (g): a commented prologue is accepted, and it really takes
    # effect -- inside a transaction ``PRAGMA foreign_keys`` is a silent no-op.
    mig = _write_migrations(
        tmp_path / "m",
        {
            **_BASE_T,
            "002_prologue.sql": (
                "-- prologue\nPRAGMA foreign_keys=ON;\nCREATE TABLE later (id INTEGER);\n"
            ),
        },
    )
    async with aiosqlite.connect(tmp_path / "db.sqlite") as conn:
        assert await _rows(conn, "PRAGMA foreign_keys") == [(0,)]
        assert await apply_migrations(conn, mig) == 2
        assert await _rows(conn, "PRAGMA foreign_keys") == [(1,)]
        assert await _table_exists(conn, "later")


# ---------------------------------------------------------------------------
# (h) the fragment after the last semicolon
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trailing_comment_only_fragment_is_accepted(tmp_path: Path) -> None:
    mig = _write_migrations(
        tmp_path / "m",
        {"001_t.sql": "CREATE TABLE t (id INTEGER);\n-- trailing note\n/* and a block */\n"},
    )
    async with aiosqlite.connect(tmp_path / "db.sqlite") as conn:
        assert await apply_migrations(conn, mig) == 1
        assert await _table_exists(conn, "t")


@pytest.mark.asyncio
async def test_trailing_unterminated_code_is_rejected(tmp_path: Path) -> None:
    mig = _write_migrations(tmp_path / "m", _BASE_T)
    async with aiosqlite.connect(tmp_path / "db.sqlite") as conn:
        assert await apply_migrations(conn, mig) == 1
        schema_before = await _schema(conn)

        _write_migrations(
            mig,
            {
                "002_unterminated.sql": (
                    "CREATE TABLE u1 (id INTEGER);\nCREATE TABLE u2 (id INTEGER)\n"
                )
            },
        )
        with pytest.raises(Exception, match="unterminated") as excinfo:
            await apply_migrations(conn, mig)
        assert "002" in str(excinfo.value)
        assert "002_unterminated.sql" in str(excinfo.value)
        assert await _schema(conn) == schema_before
        assert await get_current_version(conn) == 1


@pytest.mark.asyncio
async def test_interruption_mid_migration_is_rolled_back_and_not_wrapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A non-SQLite interruption (task cancelled at shutdown) after the body ran
    # but before the version row was written: nothing may stay pending.
    mig = _write_migrations(tmp_path / "m", {"001_base.sql": _ATOMIC_BASE})
    real_execute = migrator_module._execute_statements

    async def execute_then_cancel(conn: aiosqlite.Connection, statements: list[str]) -> None:
        await real_execute(conn, statements)
        raise asyncio.CancelledError

    db = tmp_path / "db.sqlite"
    async with aiosqlite.connect(db) as conn:
        assert await apply_migrations(conn, mig) == 1
        schema_before = await _schema(conn)
        _write_migrations(mig, {"002_extra.sql": _ATOMIC_GOOD})
        monkeypatch.setattr(migrator_module, "_execute_statements", execute_then_cancel)
        with pytest.raises(asyncio.CancelledError):
            await apply_migrations(conn, mig)
        assert not conn.in_transaction
        await conn.commit()

    async with aiosqlite.connect(db) as conn:
        assert await _schema(conn) == schema_before
        assert await get_current_version(conn) == 1
        assert await _rows(conn, "SELECT v FROM base ORDER BY id") == [("keep1",), ("keep2",)]


@pytest.mark.asyncio
async def test_version_insert_failure_rolls_back_body(tmp_path: Path) -> None:
    mig = _write_migrations(tmp_path / "m", {"001_base.sql": _ATOMIC_BASE})
    db = tmp_path / "db.sqlite"
    async with aiosqlite.connect(db) as conn:
        assert await apply_migrations(conn, mig) == 1
        await conn.execute(
            "CREATE TRIGGER reject_version BEFORE INSERT ON schema_version "
            "WHEN NEW.version = 2 BEGIN "
            "SELECT RAISE(ABORT, 'version blocked'); END;"
        )
        await conn.commit()
        before = await _schema(conn)
        _write_migrations(mig, {"002_extra.sql": _ATOMIC_GOOD})
        with pytest.raises(migrator_module.MigrationError, match="version blocked"):
            await apply_migrations(conn, mig)
        assert not conn.in_transaction
        await conn.commit()
    async with aiosqlite.connect(db) as conn:
        assert await _schema(conn) == before
        assert await get_current_version(conn) == 1
        assert await _rows(conn, "SELECT v FROM base ORDER BY id") == [("keep1",), ("keep2",)]
        await conn.execute("DROP TRIGGER reject_version")
        await conn.commit()
        assert await apply_migrations(conn, mig) == 2
        assert await _table_exists(conn, "extra")
        assert await _rows(conn, "SELECT v FROM base ORDER BY id") == [
            ("keep1",),
            ("keep2",),
            ("new",),
        ]
