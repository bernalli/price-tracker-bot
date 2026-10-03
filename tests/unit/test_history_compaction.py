"""Price history compaction: bounded growth without losing any price movement."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest_asyncio
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from telegram.ext import Application

from price_tracker.core.outlier import HISTORY_WINDOW
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from price_tracker.main import (
    HISTORY_COMPACTION_AFTER_DAYS,
    history_compaction_job,
    schedule_jobs,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")
NOW = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
CUTOFF = (NOW - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    try:
        yield Repository(conn)
    finally:
        await conn.close()


async def _product(repo: Repository, n: int = 1) -> int:
    return await repo.add_product(
        user_id=1,
        url=f"https://shop.example/p/{n}",
        name=f"Widget {n}",
        domain="shop.example",
        initial_price=Decimal("100"),
        currency="EUR",
    )


def _ts(at: datetime, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return at.strftime(fmt)


async def _insert(repo: Repository, pid: int, rows: Sequence[tuple[datetime, str]]) -> None:
    await repo._conn.executemany(
        "INSERT INTO price_history(product_id, price, checked_at) VALUES(?, ?, ?)",
        [(pid, price, _ts(at)) for at, price in rows],
    )
    await repo._conn.commit()


async def _rows(repo: Repository, pid: int) -> list[tuple[str, str]]:
    cursor = await repo._conn.execute(
        "SELECT replace(replace(checked_at, 'T', ' '), 'Z', ''), price FROM price_history "
        "WHERE product_id = ? ORDER BY 1, id",
        (pid,),
    )
    return [(r[0], r[1]) for r in await cursor.fetchall()]


def _runs(rows: Sequence[tuple[str, str]]) -> list[tuple[str, str, str]]:
    """(price, first timestamp, last timestamp) of each run of equal prices."""
    runs: list[tuple[str, str, str]] = []
    for ts, price in rows:
        if runs and runs[-1][0] == price:
            runs[-1] = (price, runs[-1][1], ts)
        else:
            runs.append((price, ts, ts))
    return runs


def _hourly(start: datetime, hours: int, price: str) -> list[tuple[datetime, str]]:
    return [(start + timedelta(hours=h), price) for h in range(hours)]


async def test_old_unchanged_run_is_reduced_to_one_reading_per_day(repo: Repository) -> None:
    pid = await _product(repo)
    start = NOW - timedelta(days=60)
    await _insert(repo, pid, _hourly(start, 60 * 24, "100.00"))

    deleted = await repo.compact_price_history(older_than=CUTOFF, keep_recent=HISTORY_WINDOW)

    rows = await _rows(repo, pid)
    old = [ts for ts, _ in rows if ts < CUTOFF]
    recent = [ts for ts, _ in rows if ts >= CUTOFF]
    assert deleted == 60 * 24 - len(rows)
    old_days = {_ts(at)[:10] for at, _ in _hourly(start, 60 * 24, "") if _ts(at) < CUTOFF}
    assert sorted(old) == sorted({min(t for t in old if t[:10] == d) for d in old_days})
    assert len(recent) == 30 * 24  # nothing younger than the cutoff is touched
    assert rows[0][0] == _ts(start)  # the run keeps its first reading


async def test_every_price_change_survives_with_both_ends(repo: Repository) -> None:
    pid = await _product(repo)
    start = NOW - timedelta(days=50)
    series = (
        _hourly(start, 30, "100.00")
        + _hourly(start + timedelta(hours=30), 30, "90.00")
        + _hourly(start + timedelta(hours=60), 30, "100.00")
    )
    await _insert(repo, pid, series)
    before = _runs(await _rows(repo, pid))

    await repo.compact_price_history(older_than=CUTOFF, keep_recent=0)

    assert _runs(await _rows(repo, pid)) == before


async def test_newest_readings_are_kept_even_when_old(repo: Repository) -> None:
    """A product checked rarely still shows the outlier gate its full window."""
    pid = await _product(repo)
    start = NOW - timedelta(days=90)
    await _insert(repo, pid, _hourly(start, 24 * 3, "100.00"))

    await repo.compact_price_history(older_than=CUTOFF, keep_recent=HISTORY_WINDOW)

    assert len(await _rows(repo, pid)) >= HISTORY_WINDOW
    recent = await repo.get_price_history(pid, limit=HISTORY_WINDOW)
    assert recent[0].checked_at == _ts(start + timedelta(hours=24 * 3 - 1))
    assert recent[-1].checked_at == _ts(start + timedelta(hours=24 * 3 - HISTORY_WINDOW))


async def test_mixed_timestamp_formats_are_ordered_as_one_series(repo: Repository) -> None:
    pid = await _product(repo)
    start = NOW - timedelta(days=40)
    rows = []
    for h in range(48):
        at = start + timedelta(hours=h)
        rows.append(
            (pid, "100.00", _ts(at, "%Y-%m-%dT%H:%M:%SZ" if h % 2 else "%Y-%m-%d %H:%M:%S"))
        )
    await repo._conn.executemany(
        "INSERT INTO price_history(product_id, price, checked_at) VALUES(?, ?, ?)", rows
    )
    # A price change written in the other format sits between two readings.
    await repo._conn.execute(
        "INSERT INTO price_history(product_id, price, checked_at) VALUES(?, ?, ?)",
        (pid, "80.00", _ts(start + timedelta(hours=20, minutes=30), "%Y-%m-%dT%H:%M:%SZ")),
    )
    await repo._conn.commit()
    before = await _rows(repo, pid)

    deleted = await repo.compact_price_history(older_than=CUTOFF, keep_recent=0)

    after = await _rows(repo, pid)
    assert deleted > 0
    assert _runs(after) == _runs(before)
    assert {ts[:10] for ts, _ in after} == {ts[:10] for ts, _ in before}


async def test_products_are_compacted_independently(repo: Repository) -> None:
    a = await _product(repo, 1)
    b = await _product(repo, 2)
    start = NOW - timedelta(days=40)
    # Interleaved: A's readings alternate in time with B's, at different prices.
    await _insert(repo, a, _hourly(start, 48, "100.00"))
    await _insert(
        repo, b, [(at + timedelta(minutes=30), "50.00") for at, _ in _hourly(start, 48, "")]
    )

    await repo.compact_price_history(older_than=CUTOFF, keep_recent=0)

    for pid, price in ((a, "100.00"), (b, "50.00")):
        rows = await _rows(repo, pid)
        assert {p for _, p in rows} == {price}
        # The first reading of each of the three days touched, and the run's last.
        assert len(rows) == 4


async def test_compaction_is_idempotent(repo: Repository) -> None:
    pid = await _product(repo)
    await _insert(repo, pid, _hourly(NOW - timedelta(days=45), 24 * 10, "100.00"))

    assert await repo.compact_price_history(older_than=CUTOFF, keep_recent=0) > 0
    assert await repo.compact_price_history(older_than=CUTOFF, keep_recent=0) == 0


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    steps=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=20), st.sampled_from(["9.99", "10.00", "12.50"])
        ),
        min_size=1,
        max_size=120,
    ),
    keep_recent=st.integers(min_value=0, max_value=10),
)
async def test_compaction_preserves_runs_days_and_recent_readings(
    steps: list[tuple[int, str]], keep_recent: int
) -> None:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    try:
        pid = await _product(repo)
        at = NOW - timedelta(days=45)
        series = []
        for gap_hours, price in steps:
            at += timedelta(hours=gap_hours)
            series.append((at, price))
        await _insert(repo, pid, series)
        before = await _rows(repo, pid)

        await repo.compact_price_history(older_than=CUTOFF, keep_recent=keep_recent)

        after = await _rows(repo, pid)
        assert set(after) <= set(before)
        assert _runs(after) == _runs(before)
        assert {ts[:10] for ts, _ in after} == {ts[:10] for ts, _ in before}
        assert [r for r in before if r[0] >= CUTOFF] == [r for r in after if r[0] >= CUTOFF]
        if keep_recent:
            assert before[-keep_recent:] == after[-keep_recent:]
    finally:
        await conn.close()


async def test_job_compacts_with_the_outlier_window_and_cutoff() -> None:
    repo = AsyncMock()
    repo.compact_price_history = AsyncMock(return_value=3)
    context = MagicMock()
    context.bot_data = {"repo": repo}

    before = datetime.now(UTC) - timedelta(days=HISTORY_COMPACTION_AFTER_DAYS)
    await history_compaction_job(context)
    after = datetime.now(UTC) - timedelta(days=HISTORY_COMPACTION_AFTER_DAYS)

    kwargs = repo.compact_price_history.await_args.kwargs
    assert kwargs["keep_recent"] == HISTORY_WINDOW
    cutoff = datetime.strptime(kwargs["older_than"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    assert before.replace(microsecond=0) <= cutoff <= after


async def test_job_survives_a_failing_compaction() -> None:
    repo = AsyncMock()
    repo.compact_price_history = AsyncMock(side_effect=aiosqlite.OperationalError("locked"))
    context = MagicMock()
    context.bot_data = {"repo": repo}
    await history_compaction_job(context)  # must not raise


async def test_job_is_a_noop_before_the_repository_exists() -> None:
    context = MagicMock()
    context.bot_data = {}
    await history_compaction_job(context)  # must not raise


def test_compaction_is_scheduled_with_the_other_jobs() -> None:
    application = Application.builder().token("123:fake").build()  # noqa: S106 — test fixture
    assert application.job_queue is not None

    schedule_jobs(application.job_queue)

    jobs = {job.name: job for job in application.job_queue.jobs()}
    assert set(jobs) == {"periodic_check", "digest_flush", "history_compaction"}
    assert jobs["history_compaction"].callback is history_compaction_job
