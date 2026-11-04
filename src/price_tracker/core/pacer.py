"""Per-store spacing of outgoing requests.

Some stores rate-limit per address burst: three products of the same store
checked within a few seconds answer 429 and push the domain into quarantine.
Every physical request therefore waits its turn per registrable domain
(eTLD+1), each hop of a redirect chain included, for the domain that hop
contacts: at least ``min_gap`` seconds after the previous request to that store
started, and at least as long as the store last asked for with ``Retry-After``
on a 429 or 503. The wait happens right before the request and is not counted
against the request deadline; no lock is held while the request is in flight,
so a slow response never delays requests to other stores.

The pacer is process-wide because every client in the process shares the same
outbound address: the store sees all of them as one visitor.
"""

from __future__ import annotations

import asyncio
import math
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Final

import httpx

from price_tracker.core.url_utils import extract_etld_plus_one

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

DEFAULT_MIN_GAP_SECONDS: Final = 30.0
MAX_RETRY_AFTER_SECONDS: Final = 300.0
_RETRY_AFTER_STATUSES: Final = frozenset({429, 503})

# Deadlines the current task is inside of. A turn's wait is politeness towards
# the store, not network time: every wait pushes each of them back.
_DEADLINES: ContextVar[tuple[asyncio.Timeout, ...]] = ContextVar("pacing_deadlines", default=())


@asynccontextmanager
async def excluded_from_deadline(deadline: asyncio.Timeout) -> AsyncIterator[None]:
    """Push ``deadline`` back by every pacing wait taken inside this block."""
    token = _DEADLINES.set((*_DEADLINES.get(), deadline))
    try:
        yield
    finally:
        _DEADLINES.reset(token)


def _extend_deadlines(seconds: float) -> None:
    for deadline in _DEADLINES.get():
        when = deadline.when()
        if when is not None:
            deadline.reschedule(when + seconds)


def domain_key(url: httpx.URL | str) -> str | None:
    """Registrable domain of ``url`` (eTLD+1), else its lower-cased host, else ``None``."""
    try:
        parsed = url if isinstance(url, httpx.URL) else httpx.URL(url)
    except (httpx.InvalidURL, TypeError):
        return None
    host = parsed.host.lower()
    if not host:
        return None
    registrable = extract_etld_plus_one(str(parsed))
    return registrable.lower() if registrable else host


def parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    """Seconds a ``Retry-After`` header asks for, or ``None`` when it is unusable.

    Accepts delay-seconds (``"60"``) and an HTTP date. A date in the past means
    no wait (``0.0``). Anything else — a negative or fractional number, garbage,
    a date without a zone — is ignored rather than guessed.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if text.isascii() and text.isdigit():
        return float(int(text))
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - now).total_seconds())


class DomainPacer:
    """Minimum gap between requests to the same store, plus ``Retry-After`` holds."""

    def __init__(
        self,
        *,
        min_gap: float = DEFAULT_MIN_GAP_SECONDS,
        max_retry_after: float = MAX_RETRY_AFTER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not math.isfinite(min_gap) or min_gap < 0:
            raise ValueError("min_gap must be a finite number >= 0")
        if not math.isfinite(max_retry_after) or max_retry_after < 0:
            raise ValueError("max_retry_after must be a finite number >= 0")
        self._min_gap = min_gap
        self._max_retry_after = max_retry_after
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_start: dict[str, float] = {}
        self._hold_until: dict[str, float] = {}

    @property
    def min_gap(self) -> float:
        """Seconds between the starts of two requests to the same store."""
        return self._min_gap

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def wait_turn(self, key: str) -> None:
        """Return once a request to ``key`` may start, and book that start.

        The lock only guards the bookkeeping: it is released before sleeping, so
        a waiter recomputes its turn after every sleep and sees a ``Retry-After``
        that arrived meanwhile.
        """
        while True:
            async with self._lock(key):
                now = self._clock()
                start = max(
                    now,
                    self._next_start.get(key, now),
                    self._hold_until.get(key, now),
                )
                if start <= now:
                    self._next_start[key] = now + self._min_gap
                    return
            # Extend before sleeping: a deadline due during the sleep would
            # otherwise fire before it could be moved.
            _extend_deadlines(start - now)
            await self._sleep(start - now)

    async def note_response(self, key: str, status_code: int, retry_after: str | None) -> None:
        """Hold ``key`` for as long as a 429/503 ``Retry-After`` asks, capped."""
        if status_code not in _RETRY_AFTER_STATUSES:
            return
        seconds = parse_retry_after(retry_after, now=self._wall_clock())
        if seconds is None:
            return
        async with self._lock(key):
            until = self._clock() + min(seconds, self._max_retry_after)
            if until > self._hold_until.get(key, -math.inf):
                self._hold_until[key] = until

    async def before_request(self, url: httpx.URL | str) -> None:
        """Book the turn of the store ``url`` contacts; call right before sending.

        There is no exemption for a request that follows another one: every hop
        of a redirect chain is a request the store sees, and books its own turn.
        """
        key = domain_key(url)
        if key is not None:
            await self.wait_turn(key)

    async def after_response(
        self, url: httpx.URL | str, status_code: int, retry_after: str | None
    ) -> None:
        """Record what the store ``url`` contacts answered (``Retry-After`` holds)."""
        key = domain_key(url)
        if key is not None:
            await self.note_response(key, status_code, retry_after)


_pacer = DomainPacer()


def get_domain_pacer() -> DomainPacer:
    """The process-wide pacer every outgoing request goes through."""
    return _pacer


def configure_domain_pacer(pacer: DomainPacer) -> None:
    """Replace the process-wide pacer (startup configuration and tests)."""
    global _pacer
    _pacer = pacer
