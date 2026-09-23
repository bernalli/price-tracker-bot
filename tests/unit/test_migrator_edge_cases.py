"""Migrator rejection and atomicity cases.

Covers truncated comments, strings and statements, transaction control inside a
migration, the connection state the migrator accepts, failed rollbacks, invalid
rows, failures while recording the version or committing, and EXPLAIN or BOM
around PRAGMA statements in the prologue.
"""

import asyncio
import sqlite3
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import aiosqlite
import pytest

from price_tracker.db import migrator as m

# sqlite3.connect(autocommit=...) and PEP 249 transaction control exist only from
# Python 3.12; on 3.11 a connection cannot be in either mode, so there is nothing to test.
requires_sqlite_autocommit = pytest.mark.skipif(
    sys.version_info < (3, 12), reason="sqlite3 autocommit attribute requires Python 3.12"
)


async def rows(conn: aiosqlite.Connection, sql: str) -> Iterable[Any]:
    return await (await conn.execute(sql)).fetchall()


def migration(tmp_path: Path, sql: str) -> Path:
    directory = tmp_path / "migrations"
    directory.mkdir(exist_ok=True)
    (directory / "001_probe.sql").write_text(sql)
    return directory


@pytest.mark.parametrize(
    "sql",
    [
        "/* cut\nCREATE TABLE t(x);",
        "CREATE TABLE t(x); /* cut\nCREATE TABLE u(x);",
        "CREATE TABLE t(x); /* cut",
        "CREATE TABLE t(x); INSERT INTO t VALUES('cut;",
        'CREATE TABLE t("cut;',
        "CREATE TABLE t([cut;",
        "CREATE TABLE t(`cut;",
        "CREATE TABLE t(x); CREATE TRIGGER tr AFTER INSERT ON t BEGIN SELECT 1;",
        "CREATE TABLE t(x); CREATE TABLE u(x)",
    ],
)
async def test_reject_truncation(tmp_path, sql):
    directory = migration(tmp_path, sql)
    async with aiosqlite.connect(tmp_path / "db") as conn:
        with pytest.raises(m.MigrationError, match="unterminated"):
            await m.apply_migrations(conn, directory)
        assert await m.get_current_version(conn) == 0
        assert await rows(conn, "SELECT name FROM sqlite_master WHERE name IN ('t','u')") == []


@pytest.mark.parametrize(
    "tx",
    [
        "BEGIN",
        "BEGIN IMMEDIATE",
        "COMMIT",
        "END TRANSACTION",
        "ROLLBACK",
        "SAVEPOINT s",
        "RELEASE s",
        "ROLLBACK TO s",
    ],
)
@pytest.mark.parametrize("prefix", ["", "-- note\n/* prefix */ ", "\ufeff"])
async def test_reject_transaction_control(tmp_path, tx, prefix):
    directory = migration(
        tmp_path, "PRAGMA user_version=73; CREATE TABLE t(x); " + prefix + tx + ";"
    )
    async with aiosqlite.connect(tmp_path / "db") as conn:
        with pytest.raises(m.MigrationError, match="transaction control"):
            await m.apply_migrations(conn, directory)
        assert await rows(conn, "PRAGMA user_version") == [(0,)]
        assert await m.get_current_version(conn) == 0
        assert await rows(conn, "SELECT name FROM sqlite_master WHERE name='t'") == []


async def test_bom_prologue(tmp_path):
    directory = migration(tmp_path, "\ufeffPRAGMA foreign_keys=ON; CREATE TABLE t(x);")
    async with aiosqlite.connect(tmp_path / "db") as conn:
        assert await m.apply_migrations(conn, directory) == 1
        assert await rows(conn, "PRAGMA foreign_keys") == [(1,)]


async def test_bom_late_pragma(tmp_path):
    directory = migration(
        tmp_path, "CREATE TABLE t(x); /* prefix */ \ufeff PRAGMA foreign_keys=ON;"
    )
    async with aiosqlite.connect(tmp_path / "db") as conn:
        with pytest.raises(m.MigrationError, match="PRAGMA"):
            await m.apply_migrations(conn, directory)
        assert await m.get_current_version(conn) == 0


async def test_caller_transaction_is_untouched(tmp_path):
    directory = migration(tmp_path, "CREATE TABLE t(x);")
    async with aiosqlite.connect(tmp_path / "db") as conn:
        await conn.execute("CREATE TABLE caller(x)")
        await conn.execute("INSERT INTO caller VALUES(42)")
        with pytest.raises(ValueError, match="transaction"):
            await m.apply_migrations(conn, directory)
        assert conn.in_transaction
        await conn.rollback()
        assert await rows(conn, "SELECT * FROM caller") == []
        assert await rows(conn, "SELECT name FROM sqlite_master WHERE name='schema_version'") == []


@requires_sqlite_autocommit
async def test_pep249_rejected_before_touching_db(tmp_path):
    directory = migration(tmp_path, "CREATE TABLE t(x);")
    async with aiosqlite.connect(tmp_path / "db", autocommit=False) as conn:
        with pytest.raises(ValueError, match="transaction"):
            await m.apply_migrations(conn, directory)
        assert await rows(conn, "SELECT name FROM sqlite_master") == []


@requires_sqlite_autocommit
@pytest.mark.parametrize("fail", [False, True])
async def test_true_autocommit_is_durable_and_atomic(tmp_path, fail):
    directory = migration(
        tmp_path, "CREATE TABLE t(x); INSERT INTO t VALUES(7);" + ("THIS FAILS;" if fail else "")
    )
    async with aiosqlite.connect(tmp_path / "db", autocommit=True) as conn:
        if fail:
            with pytest.raises(m.MigrationError):
                await m.apply_migrations(conn, directory)
        else:
            assert await m.apply_migrations(conn, directory) == 1
        assert not conn.in_transaction
    async with aiosqlite.connect(tmp_path / "db") as conn:
        assert await m.get_current_version(conn) == (0 if fail else 1)
        assert await rows(conn, "SELECT name FROM sqlite_master WHERE name='t'") == (
            [] if fail else [("t",)]
        )
        if not fail:
            assert await rows(conn, "SELECT * FROM t") == [(7,)]


@pytest.mark.parametrize("cancel", [False, True])
async def test_rollback_failure_preserves_original(tmp_path, monkeypatch, cancel):
    directory = migration(tmp_path, "CREATE TABLE t(x); THIS FAILS;")
    async with aiosqlite.connect(tmp_path / "db") as conn:
        execute = conn.execute
        original = (
            asyncio.CancelledError("shutdown")
            if cancel
            else sqlite3.OperationalError("primary failure")
        )

        async def execute_fault(sql, *args, **kwargs):
            if sql == "ROLLBACK":
                raise sqlite3.OperationalError("rollback failed")
            if "THIS FAILS" in sql:
                raise original
            return await execute(sql, *args, **kwargs)

        async def bad_rollback():
            raise sqlite3.OperationalError("rollback failed")

        monkeypatch.setattr(conn, "execute", execute_fault)
        monkeypatch.setattr(conn, "rollback", bad_rollback)
        with pytest.raises(asyncio.CancelledError if cancel else m.MigrationError) as caught:
            await m.apply_migrations(conn, directory)
        assert (caught.value if cancel else caught.value.__cause__) is original
        assert any("rollback failed" in note for note in original.__notes__)
        await execute("ROLLBACK")


@pytest.mark.parametrize(
    "bad_sql",
    [
        "INSERT INTO t VALUES(1, 5);",
        "INSERT INTO t VALUES(2, 'wrong');",
        "INSERT INTO t VALUES(2);",
        "INSERT INTO t VALUES(2, 5, 6);",
        "INSERT INTO t VALUES(2, 101);",
        "INSERT INTO t VALUES(2, NULL);",
    ],
)
async def test_bad_rows_roll_back_all_changes(tmp_path, bad_sql):
    directory = migration(
        tmp_path,
        "CREATE TABLE t(id INTEGER PRIMARY KEY, n INTEGER NOT NULL "
        "CHECK(n BETWEEN 0 AND 100)) STRICT; INSERT INTO t VALUES(1, 50);",
    )
    async with aiosqlite.connect(tmp_path / "db") as conn:
        await m.apply_migrations(conn, directory)
        (directory / "002_bad.sql").write_text(
            "CREATE TABLE extra(x); INSERT INTO t VALUES(3, 60);" + bad_sql
        )
        with pytest.raises(m.MigrationError):
            await m.apply_migrations(conn, directory)
        assert not conn.in_transaction
    async with aiosqlite.connect(tmp_path / "db") as conn:
        assert await rows(conn, "SELECT * FROM t") == [(1, 50)]
        assert await rows(conn, "SELECT name FROM sqlite_master WHERE name='extra'") == []
        assert await m.get_current_version(conn) == 1


async def test_version_write_is_atomic(tmp_path):
    directory = migration(tmp_path, "CREATE TABLE base(x); INSERT INTO base VALUES(7);")
    async with aiosqlite.connect(tmp_path / "db") as conn:
        await m.apply_migrations(conn, directory)
        await conn.execute(
            "CREATE TRIGGER reject_version BEFORE INSERT ON schema_version "
            "WHEN NEW.version=2 BEGIN SELECT RAISE(ABORT, 'version blocked'); END;"
        )
        await conn.commit()
        (directory / "002_body.sql").write_text(
            "CREATE TABLE extra(x); INSERT INTO base VALUES(8);"
        )
        with pytest.raises(m.MigrationError, match="version blocked"):
            await m.apply_migrations(conn, directory)
        assert not conn.in_transaction
    async with aiosqlite.connect(tmp_path / "db") as conn:
        assert await rows(conn, "SELECT * FROM base") == [(7,)]
        assert await rows(conn, "SELECT name FROM sqlite_master WHERE name='extra'") == []
        assert await m.get_current_version(conn) == 1
        await conn.execute("DROP TRIGGER reject_version")
        await conn.commit()
        assert await m.apply_migrations(conn, directory) == 2
        assert await rows(conn, "SELECT * FROM base") == [(7,), (8,)]


async def test_commit_failure_is_atomic(tmp_path):
    directory = migration(
        tmp_path,
        "PRAGMA foreign_keys=ON; CREATE TABLE p(id PRIMARY KEY); "
        "CREATE TABLE c(p REFERENCES p(id) DEFERRABLE INITIALLY DEFERRED);",
    )
    async with aiosqlite.connect(tmp_path / "db") as conn:
        await m.apply_migrations(conn, directory)
        (directory / "002_bad.sql").write_text("CREATE TABLE extra(x); INSERT INTO c VALUES(99);")
        with pytest.raises(m.MigrationError, match="FOREIGN KEY"):
            await m.apply_migrations(conn, directory)
        assert not conn.in_transaction
    async with aiosqlite.connect(tmp_path / "db") as conn:
        assert await rows(conn, "SELECT * FROM c") == []
        assert await rows(conn, "SELECT name FROM sqlite_master WHERE name='extra'") == []
        assert await m.get_current_version(conn) == 1


@pytest.mark.parametrize("prefix", ["", "CREATE TABLE t(x); "])
@pytest.mark.parametrize(
    "explain", ["EXPLAIN PRAGMA", "EXPLAIN /* prefix */ PRAGMA", "EXPLAIN QUERY PLAN PRAGMA"]
)
async def test_explain_pragma_is_rejected(tmp_path, prefix, explain):
    directory = migration(tmp_path, prefix + explain + " foreign_keys=ON;")
    async with aiosqlite.connect(tmp_path / "db") as conn:
        with pytest.raises(m.MigrationError, match="EXPLAIN"):
            await m.apply_migrations(conn, directory)
        assert await m.get_current_version(conn) == 0
        assert await rows(conn, "PRAGMA foreign_keys") == [(0,)]
        assert await rows(conn, "SELECT name FROM sqlite_master WHERE name='t'") == []
