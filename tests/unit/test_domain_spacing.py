"""Job-level spacing of checks on the same store (virtual clock, no real sleeps)."""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.domain_spacing import (
    MAX_DEFERRAL_SECONDS,
    DomainSpacing,
    parse_retry_after,
    retry_after_of,
    run_spaced,
)
from price_tracker.core.exceptions import HTTPBlockStatus

_NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class _Clock:
    """A virtual monotonic clock whose sleeps only move time forward."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[tuple[float, float]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append((self.now, seconds))
        self.now += seconds
        await asyncio.sleep(0)


def _spacing(clock: _Clock, *, gap: float = 30.0) -> DomainSpacing:
    return DomainSpacing(min_gap=gap, clock=clock, wall_clock=lambda: _NOW, sleep=clock.sleep)


# ── Retry-After parsing ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("60", 60.0),
        (" 120 ", 120.0),
        ("0", 0.0),
        ("0" * 5000 + "7", 7.0),
        (format_datetime(_NOW + timedelta(seconds=90), usegmt=True), 90.0),
        (format_datetime(_NOW - timedelta(seconds=90), usegmt=True), 0.0),
    ],
)
def test_usable_retry_after_values(value: str, expected: float) -> None:
    assert parse_retry_after(value, now=_NOW) == expected


@pytest.mark.parametrize("digits", [309, 4301, 10_000])
def test_huge_retry_after_parses_without_raising(digits: int) -> None:
    assert parse_retry_after("9" * digits, now=_NOW) == math.inf


@pytest.mark.parametrize(
    "value",
    [None, "", "  ", "-5", "-0", "1.5", "soon", "60s", "١٢", "Wed, 04 Nov 2026 12:01:30", 60],
)
def test_unusable_retry_after_values_are_ignored(value: object) -> None:
    assert parse_retry_after(value, now=_NOW) is None  # type: ignore[arg-type]


def test_retry_after_is_read_from_a_429_block_and_a_503_status_error() -> None:
    assert retry_after_of(HTTPBlockStatus(status=429, url="u", retry_after="60")) == "60"
    assert retry_after_of(HTTPBlockStatus(status=403, url="u", retry_after="60")) is None
    assert retry_after_of(HTTPBlockStatus(status=429, url="u")) is None
    request = httpx.Request("GET", "https://store.com/p")
    for status, header, expected in [(503, "30", "30"), (500, "30", None), (503, None, None)]:
        headers = {} if header is None else {"Retry-After": header}
        response = httpx.Response(status, headers=headers, request=request)
        error = httpx.HTTPStatusError("x", request=request, response=response)
        assert retry_after_of(error) == expected
    assert retry_after_of(ValueError("x")) is None


def test_a_block_keeps_only_a_textual_retry_after() -> None:
    assert HTTPBlockStatus(status=429, url="u", retry_after=object()).retry_after is None  # type: ignore[arg-type]


# ── Deferral ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("header", "deferred_for"),
    [
        ("90", 90.0),
        (format_datetime(_NOW + timedelta(seconds=45), usegmt=True), 45.0),
        ("9" * 400, MAX_DEFERRAL_SECONDS),
        ("86400", MAX_DEFERRAL_SECONDS),
    ],
)
def test_retry_after_defers_the_store_until_it_expires(header: str, deferred_for: float) -> None:
    clock = _Clock()
    spacing = _spacing(clock)
    spacing.defer("store.com", header)
    clock.now = deferred_for - 0.001
    assert spacing.is_deferred("store.com")
    assert not spacing.is_deferred("other.com")
    clock.now = deferred_for
    assert not spacing.is_deferred("store.com")


@pytest.mark.parametrize("header", [None, "", "garbage", "-30", "2.5", "0"])
def test_unusable_or_zero_retry_after_does_not_defer(header: str | None) -> None:
    spacing = _spacing(_Clock())
    spacing.defer("store.com", header)
    assert not spacing.is_deferred("store.com")
    assert spacing.tracked_domains() == 0


def test_a_shorter_deferral_never_cuts_a_longer_one() -> None:
    clock = _Clock()
    spacing = _spacing(clock)
    spacing.defer("store.com", "600")
    spacing.defer("store.com", "10")
    clock.now = 599
    assert spacing.is_deferred("store.com")


# ── Gap and state ────────────────────────────────────────────────


def test_remaining_deferral_counts_down_without_mutating_state() -> None:
    clock = _Clock()
    spacing = _spacing(clock)
    assert spacing.deferred_for("store.com") == 0.0
    spacing.defer("store.com", "120")
    assert spacing.deferred_for("store.com") == 120.0
    clock.now = 20.0
    spacing.defer("store.com", "10")
    assert spacing.deferred_for("store.com") == 100.0
    assert spacing.deferred_for("other.com") == 0.0
    clock.now = 120.0
    assert spacing.deferred_for("store.com") == 0.0
    clock.now = 150.0
    assert spacing.deferred_for("store.com") == 0.0
    assert spacing.tracked_domains() == 1


@given(
    start=st.integers(min_value=-1_000_000, max_value=1_000_000),
    seconds=st.integers(min_value=0, max_value=10_000),
    elapsed=st.integers(min_value=0, max_value=20_000),
)
def test_remaining_deferral_matches_the_boolean_property(
    start: int, seconds: int, elapsed: int
) -> None:
    clock = _Clock()
    clock.now = float(start)
    spacing = _spacing(clock)
    spacing.defer("store.com", str(seconds))
    before = spacing.tracked_domains()
    clock.now += elapsed
    remaining = spacing.deferred_for("store.com")
    assert remaining == max(0.0, min(seconds, MAX_DEFERRAL_SECONDS) - elapsed)
    assert spacing.is_deferred("store.com") == (remaining > 0.0)
    assert spacing.tracked_domains() == before


def test_the_gap_counts_from_the_latest_start() -> None:
    clock = _Clock()
    spacing = _spacing(clock)
    assert spacing.wait_for("store.com") == 0
    spacing.record_start("store.com")
    clock.now = 10
    assert spacing.wait_for("store.com") == 20
    assert spacing.wait_for("other.com") == 0
    clock.now = 30
    assert spacing.wait_for("store.com") == 0


def test_gap_zero_keeps_no_state() -> None:
    spacing = _spacing(_Clock(), gap=0)
    for n in range(50):
        spacing.record_start(f"store{n}.com")
    assert spacing.wait_for("store1.com") == 0
    assert spacing.tracked_domains() == 0


def test_state_is_pruned_once_it_no_longer_constrains() -> None:
    clock = _Clock()
    spacing = _spacing(clock)
    for n in range(100):
        spacing.record_start(f"store{n}.com")
    spacing.defer("held.com", "120")
    assert spacing.tracked_domains() == 101
    clock.now = 30
    spacing.record_start("fresh.com")
    assert spacing.tracked_domains() == 2  # fresh.com and the active deferral
    clock.now = 200
    spacing.record_start("fresh.com")
    assert spacing.tracked_domains() == 1


@pytest.mark.parametrize("bad", [-1.0, math.inf, math.nan])
def test_unusable_gap_is_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="min_gap"):
        DomainSpacing(min_gap=bad)
    with pytest.raises(ValueError, match="max_deferral"):
        DomainSpacing(max_deferral=bad)


# ── The run loop ─────────────────────────────────────────────────


async def _run(
    clock: _Clock,
    spacing: DomainSpacing,
    jobs: list[tuple[str | None, str]],
) -> tuple[list[tuple[str, float]], list[str]]:
    starts: list[tuple[str, float]] = []
    dropped: list[str] = []

    async def run(job: tuple[str | None, str]) -> None:
        starts.append((job[1], clock.now))

    await run_spaced(
        jobs,
        spacing=spacing,
        domain_of=lambda job: job[0],
        admit=lambda _job, _domain: True,
        run=run,
        drop=lambda job: dropped.append(job[1]),
    )
    return starts, dropped


async def test_three_products_on_one_store_and_two_on_another() -> None:
    clock = _Clock()
    jobs: list[tuple[str | None, str]] = [
        ("a.com", "a1"),
        ("a.com", "a2"),
        ("a.com", "a3"),
        ("b.com", "b1"),
        ("b.com", "b2"),
    ]
    starts, dropped = await _run(clock, _spacing(clock), jobs)
    assert starts == [("a1", 0), ("b1", 0), ("a2", 30), ("b2", 30), ("a3", 60)]
    assert dropped == []
    assert [wait for _at, wait in clock.sleeps] == [30, 30]


async def test_a_waiting_store_never_holds_back_a_ready_one() -> None:
    clock = _Clock()
    jobs: list[tuple[str | None, str]] = [("a.com", f"a{n}") for n in range(4)]
    jobs += [("b.com", "b1"), (None, "x1"), ("c.com", "c1")]
    starts, _ = await _run(clock, _spacing(clock), jobs)
    assert dict(starts) == {"a0": 0, "b1": 0, "x1": 0, "c1": 0, "a1": 30, "a2": 60, "a3": 90}


async def test_jobs_without_a_domain_are_never_spaced() -> None:
    clock = _Clock()
    starts, _ = await _run(clock, _spacing(clock), [(None, "x1"), (None, "x2"), (None, "x3")])
    assert [at for _name, at in starts] == [0, 0, 0]
    assert clock.sleeps == []


async def test_gap_zero_runs_everything_at_once() -> None:
    clock = _Clock()
    jobs: list[tuple[str | None, str]] = [("a.com", f"a{n}") for n in range(5)]
    starts, _ = await _run(clock, _spacing(clock, gap=0), jobs)
    assert [at for _name, at in starts] == [0] * 5
    assert clock.sleeps == []


async def test_a_deferred_store_is_left_for_a_later_sweep() -> None:
    clock = _Clock()
    spacing = _spacing(clock)
    spacing.defer("a.com", "120")
    jobs: list[tuple[str | None, str]] = [("a.com", "a1"), ("b.com", "b1"), ("a.com", "a2")]
    starts, dropped = await _run(clock, spacing, jobs)
    assert starts == [("b1", 0)]
    assert dropped == ["a1", "a2"]
    assert clock.sleeps == []


async def test_a_deferral_raised_mid_sweep_drops_the_rest_of_that_store() -> None:
    clock = _Clock()
    spacing = _spacing(clock)
    starts: list[str] = []
    dropped: list[str] = []

    async def run(job: tuple[str, str]) -> None:
        starts.append(job[1])
        if job[1] == "a1":
            spacing.defer("a.com", "600")

    jobs = [("a.com", "a1"), ("a.com", "a2"), ("b.com", "b1"), ("b.com", "b2")]
    await run_spaced(
        jobs,
        spacing=spacing,
        domain_of=lambda job: job[0],
        admit=lambda _job, _domain: True,
        run=run,
        drop=lambda job: dropped.append(job[1]),
    )
    assert starts == ["a1", "b1", "b2"]
    assert dropped == ["a2"]
    assert clock.now == 30  # only b.com's own gap was waited


async def test_admission_is_decided_right_before_the_start() -> None:
    clock = _Clock()
    probed: set[str] = set()

    def admit(job: tuple[str | None, str], domain: str) -> bool:
        return domain not in probed

    async def run(job: tuple[str | None, str]) -> None:
        if job[0] is not None:
            probed.add(job[0])

    jobs: list[tuple[str | None, str]] = [("a.com", "a1"), ("a.com", "a2"), ("b.com", "b1")]
    dropped: list[str] = []
    await run_spaced(
        jobs,
        spacing=_spacing(clock),
        domain_of=lambda job: job[0],
        admit=admit,
        run=run,
        drop=lambda job: dropped.append(job[1]),
    )
    assert dropped == ["a2"]
    assert clock.sleeps == []  # a refused job is not waited for


async def test_a_manual_start_delays_the_next_scheduled_check() -> None:
    clock = _Clock()
    spacing = _spacing(clock)
    clock.now = 100
    spacing.record_start("a.com")  # a manual check, which does not wait itself
    clock.now = 110
    starts, _ = await _run(clock, spacing, [("a.com", "a1"), ("b.com", "b1")])
    assert starts == [("b1", 110), ("a1", 130)]


# ── Property: spacing holds and a ready store is never kept waiting ──


_DOMAINS = st.sampled_from(["a.com", "b.com", "c.com", "d.com"])


@settings(max_examples=150, deadline=None)
@given(
    gap=st.sampled_from([0.0, 5.0, 30.0]),
    jobs=st.lists(st.tuples(_DOMAINS, st.floats(0, 40)), min_size=1, max_size=12),
    earlier=st.dictionaries(_DOMAINS, st.floats(0, 60), max_size=4),
)
def test_spacing_property(
    gap: float, jobs: list[tuple[str, float]], earlier: dict[str, float]
) -> None:
    """Random stores, scrape durations and earlier (manual) starts.

    * two starts on one store are never closer than the gap, earlier starts included;
    * the loop sleeps only while every pending store waits for its gap, and wakes
      exactly when the first of them becomes ready (no ready store waits on a sleep);
    * among the stores ready at a decision, the first pending job in order starts.
    """
    clock = _Clock()
    clock.now = 60.0
    spacing = _spacing(clock, gap=gap)
    last: dict[str, float] = {}
    for domain, ago in sorted(earlier.items(), key=lambda item: -item[1]):
        clock.now = 60.0 - ago
        spacing.record_start(domain)
        last[domain] = clock.now
    clock.now = 60.0
    numbered = [(domain, duration, n) for n, (domain, duration) in enumerate(jobs)]
    pending = list(numbered)
    starts: list[tuple[str, float]] = []

    def ready_at(domain: str) -> float:
        return last[domain] + gap if domain in last else -math.inf

    original_sleep = clock.sleep

    async def checked_sleep(seconds: float) -> None:
        now = clock.now
        assert all(ready_at(d) > now for d, _dur, _n in pending)
        assert seconds == pytest.approx(min(ready_at(d) for d, _dur, _n in pending) - now)
        await original_sleep(seconds)

    spacing._sleep = checked_sleep

    async def run(job: tuple[str, float, int]) -> None:
        domain, duration, _n = job
        now = clock.now
        ready = [j for j in pending if ready_at(j[0]) <= now]
        assert ready
        assert ready[0] == job
        pending.remove(job)
        starts.append((domain, now))
        last[domain] = now
        clock.now += duration

    asyncio.run(
        run_spaced(
            numbered,
            spacing=spacing,
            domain_of=lambda job: job[0],
            admit=lambda _job, _domain: True,
            run=run,
        )
    )
    assert pending == []
    for domain in {d for d, _dur, _n in numbered}:
        times = sorted(
            [at for d, at in starts if d == domain]
            + ([] if domain not in earlier else [60.0 - earlier[domain]])
        )
        assert all(b - a >= gap - 1e-9 for a, b in zip(times, times[1:], strict=False))
