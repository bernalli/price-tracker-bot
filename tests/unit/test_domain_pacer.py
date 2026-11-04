"""Per-store spacing of outgoing requests (``core.pacer``).

Every unit test drives a virtual clock: ``sleep`` advances it instead of waiting,
so no test sleeps for real.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.core import pacer as pacer_module
from price_tracker.core.fetch import fetch_page
from price_tracker.core.http_client import public_request
from price_tracker.core.pacer import (
    DEFAULT_MIN_GAP_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
    DomainPacer,
    configure_domain_pacer,
    domain_key,
    get_domain_pacer,
    parse_retry_after,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_WALL = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


class _VirtualClock:
    """Monotonic time that only moves when a waiter sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        target = self.now + seconds
        await asyncio.sleep(0)  # let concurrent waiters run, as a real sleep would
        self.now = max(self.now, target)


def _pacer(clock: _VirtualClock, *, min_gap: float = 30.0) -> DomainPacer:
    return DomainPacer(min_gap=min_gap, clock=clock, wall_clock=lambda: _WALL, sleep=clock.sleep)


@pytest.fixture
def clock() -> _VirtualClock:
    return _VirtualClock()


@pytest.fixture
def installed(clock: _VirtualClock) -> Iterator[DomainPacer]:
    """A pacer on the virtual clock, installed as the process-wide one."""
    previous = get_domain_pacer()
    pacer = _pacer(clock)
    configure_domain_pacer(pacer)
    try:
        yield pacer
    finally:
        configure_domain_pacer(previous)


# ── spacing ──


async def test_second_request_to_the_same_store_waits_the_gap(clock: _VirtualClock) -> None:
    pacer = _pacer(clock)
    await pacer.wait_turn("shop.example")
    started = clock.now
    await pacer.wait_turn("shop.example")

    assert clock.now - started == pytest.approx(30.0)


async def test_other_stores_do_not_wait(clock: _VirtualClock) -> None:
    pacer = _pacer(clock)
    await pacer.wait_turn("shop.example")
    await pacer.wait_turn("other.example")

    assert clock.sleeps == []


async def test_a_request_after_the_gap_does_not_wait(clock: _VirtualClock) -> None:
    pacer = _pacer(clock)
    await pacer.wait_turn("shop.example")
    clock.now += 31
    await pacer.wait_turn("shop.example")

    assert clock.sleeps == []


async def test_zero_gap_disables_spacing(clock: _VirtualClock) -> None:
    pacer = _pacer(clock, min_gap=0)
    for _ in range(5):
        await pacer.wait_turn("shop.example")

    assert clock.sleeps == []


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_unusable_gap_is_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="min_gap"):
        DomainPacer(min_gap=bad)


async def test_concurrent_requests_to_one_store_are_spaced(clock: _VirtualClock) -> None:
    pacer = _pacer(clock)
    starts: list[float] = []

    async def request() -> None:
        await pacer.wait_turn("shop.example")
        starts.append(clock.now)

    await asyncio.gather(*(request() for _ in range(4)))

    starts.sort()
    gaps = [later - earlier for earlier, later in zip(starts, starts[1:], strict=False)]
    assert gaps == [pytest.approx(30.0)] * 3


async def test_no_lock_is_held_while_a_request_is_in_flight(clock: _VirtualClock) -> None:
    """A slow response must not delay the next turn beyond the gap, on any store."""
    pacer = _pacer(clock)
    in_flight = asyncio.Event()
    release = asyncio.Event()

    async def slow_request() -> None:
        async with pacer.turn("https://store.com/products/a"):
            in_flight.set()
            await release.wait()

    slow = asyncio.create_task(slow_request())
    await in_flight.wait()

    await asyncio.wait_for(pacer.wait_turn("other.example"), timeout=1)
    await asyncio.wait_for(pacer.wait_turn("store.com"), timeout=1)

    assert not slow.done()
    assert clock.sleeps == [pytest.approx(30.0)]
    release.set()
    await slow


async def test_a_held_turn_is_not_waited_again(clock: _VirtualClock) -> None:
    """The hops of one redirect chain share the turn of the request that started it."""
    pacer = _pacer(clock)
    async with pacer.turn("https://store.com/a"), pacer.turn("https://www.store.com/b"):
        pass

    assert clock.sleeps == []


# ── Retry-After ──


@pytest.mark.parametrize("status", [429, 503])
async def test_retry_after_seconds_holds_the_store(clock: _VirtualClock, status: int) -> None:
    pacer = _pacer(clock, min_gap=0)
    await pacer.wait_turn("shop.example")
    await pacer.note_response("shop.example", status, "60")
    await pacer.wait_turn("shop.example")

    assert clock.sleeps == [pytest.approx(60.0)]


async def test_retry_after_http_date_holds_the_store(clock: _VirtualClock) -> None:
    pacer = _pacer(clock, min_gap=0)
    when = format_datetime(_WALL + timedelta(seconds=90), usegmt=True)
    await pacer.note_response("shop.example", 429, when)
    await pacer.wait_turn("shop.example")

    assert clock.sleeps == [pytest.approx(90.0)]


async def test_retry_after_is_capped(clock: _VirtualClock) -> None:
    pacer = _pacer(clock, min_gap=0)
    await pacer.note_response("shop.example", 429, "86400")
    await pacer.wait_turn("shop.example")

    assert clock.sleeps == [pytest.approx(MAX_RETRY_AFTER_SECONDS)]


async def test_retry_after_on_a_success_is_ignored(clock: _VirtualClock) -> None:
    pacer = _pacer(clock, min_gap=0)
    await pacer.note_response("shop.example", 200, "60")
    await pacer.wait_turn("shop.example")

    assert clock.sleeps == []


async def test_retry_after_only_holds_its_own_store(clock: _VirtualClock) -> None:
    pacer = _pacer(clock, min_gap=0)
    await pacer.note_response("shop.example", 429, "60")
    await pacer.wait_turn("other.example")

    assert clock.sleeps == []


async def test_a_shorter_retry_after_does_not_shorten_a_hold(clock: _VirtualClock) -> None:
    pacer = _pacer(clock, min_gap=0)
    await pacer.note_response("shop.example", 429, "120")
    await pacer.note_response("shop.example", 429, "5")
    await pacer.wait_turn("shop.example")

    assert clock.sleeps == [pytest.approx(120.0)]


async def test_a_waiter_sees_a_hold_that_arrived_while_it_waited(clock: _VirtualClock) -> None:
    pacer = _pacer(clock)
    await pacer.wait_turn("shop.example")  # booked; the next turn is 30 s away
    waiter = asyncio.create_task(pacer.wait_turn("shop.example"))
    await asyncio.sleep(0)  # the waiter is now sleeping towards its turn
    await pacer.note_response("shop.example", 429, "100")
    await waiter

    assert clock.now - 1000.0 >= 100.0


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("60", 60.0),
        (" 0 ", 0.0),
        ("Wed, 04 Nov 2026 12:01:00 GMT", 60.0),
        ("Wed, 04 Nov 2026 11:00:00 GMT", 0.0),
        ("", None),
        (None, None),
        ("-5", None),
        ("1.5", None),
        ("soon", None),
        ("Wed, 04 Nov 2026 12:01:00", None),
        ("٣٠", None),
    ],
)
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value, now=_WALL) == expected


# ── store identity ──


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.shop.co.uk/products/a", "shop.co.uk"),
        ("https://SHOP.co.uk/x", "shop.co.uk"),
        ("https://eu.fillingpieces.com/products/a", "fillingpieces.com"),
        ("http://93.184.216.34/products/a", "93.184.216.34"),
        ("not a url", None),
    ],
)
def test_domain_key_is_the_registrable_domain(url: str, expected: str | None) -> None:
    assert domain_key(url) == expected


def test_default_gap_is_at_least_twenty_seconds() -> None:
    assert DEFAULT_MIN_GAP_SECONDS >= 20
    assert pacer_module.MAX_RETRY_AFTER_SECONDS == 300


# ── every outgoing request goes through it ──


async def test_public_request_spaces_requests_to_one_store(
    installed: DomainPacer, clock: _VirtualClock
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(url__regex=r"https://(www\.)?store\.com/.*").respond(200, text="ok")
        router.get("https://other.com/a").respond(200, text="ok")
        async with httpx.AsyncClient() as client:
            await public_request(client, "GET", "https://store.com/a")
            await public_request(client, "GET", "https://other.com/a")
            await public_request(client, "GET", "https://www.store.com/b")

    assert clock.sleeps == [pytest.approx(30.0)]


async def test_public_request_honours_retry_after(
    installed: DomainPacer, clock: _VirtualClock
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://store.com/a.json").respond(
            429, text="Too Many Requests", headers={"Retry-After": "60"}
        )
        router.get("https://store.com/a").respond(200, text="ok")
        async with httpx.AsyncClient() as client:
            await public_request(client, "GET", "https://store.com/a.json")
            await public_request(client, "GET", "https://store.com/a")

    assert clock.sleeps == [pytest.approx(60.0)]


async def test_redirect_hops_do_not_wait_behind_their_own_request(
    installed: DomainPacer, clock: _VirtualClock
) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://store.com/old").respond(
            301, headers={"Location": "https://store.com/new"}
        )
        router.get("https://store.com/new").respond(200, text="<html>ok</html>")
        async with httpx.AsyncClient() as client:
            await public_request(client, "GET", "https://store.com/old")
            clock.now += 60
            page = await fetch_page("https://store.com/old", client)

    assert page.url_final == "https://store.com/new"
    assert clock.sleeps == []


async def test_fetch_page_waits_its_turn(installed: DomainPacer, clock: _VirtualClock) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://store.com/a").respond(200, text="<html>ok</html>")
        async with httpx.AsyncClient() as client:
            await fetch_page("https://store.com/a", client)
            await fetch_page("https://store.com/a", client)

    assert clock.sleeps == [pytest.approx(30.0)]
