"""Job-level spacing of scheduled checks on the same store.

Some stores rate-limit per address burst: three products of one store checked a
few seconds apart answer 429 and push the domain into quarantine. The scheduler
therefore starts two checks on the same registrable domain (eTLD+1) at least
``min_gap`` seconds apart, and interleaves stores: while one store waits for its
gap, the products of the other stores go ahead.

A check is one job, whatever HTTP requests it makes (a redirect chain, a JSON
endpoint and its product page): the spacing is decided before the job starts,
so nothing (no lock, no connection slot, no idle connection) is held while a
store waits for its turn.

A 429 or 503 carrying ``Retry-After`` defers the store in the scheduler for as
long as it asked (capped): its products are left for a later sweep instead of
being checked again too early. This sits next to the quarantine circuit breaker
in ``core.health``, which still counts the block.

Manual checks never wait for the gap, but they record their start so the next
scheduled check on that store keeps its distance.
"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Final, TypeVar

import httpx

from price_tracker.core.exceptions import HTTPBlockStatus

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

DEFAULT_MIN_GAP_SECONDS: Final = 30.0
MAX_DEFERRAL_SECONDS: Final = 3600.0
RETRY_AFTER_STATUSES: Final = frozenset({429, 503})

JobT = TypeVar("JobT")


def parse_retry_after(value: str | None, *, now: datetime) -> float | None:
    """Seconds a ``Retry-After`` header asks for, or ``None`` when it is unusable.

    Accepts delay-seconds (``"60"``, any number of digits: a huge value becomes
    ``inf`` and is capped by the caller) and an HTTP date; a date in the past
    means no wait (``0.0``). Anything else (a negative or fractional number,
    garbage, a date without a zone) is ignored rather than guessed.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.isascii() and text.isdigit():
        return float(text)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when.tzinfo is None:
        return None
    return max(0.0, (when - now).total_seconds())


def retry_after_of(exc: BaseException) -> str | None:
    """The ``Retry-After`` of a failed check's 429/503 response, if it carried one.

    A 429 reaches the scheduler as :class:`HTTPBlockStatus`, which keeps the header
    of the response it was raised from; a 503 is not a block and arrives, when a
    scraper lets it through, as :class:`httpx.HTTPStatusError`.
    """
    if isinstance(exc, HTTPBlockStatus):
        return exc.retry_after if exc.status in RETRY_AFTER_STATUSES else None
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        if response.status_code in RETRY_AFTER_STATUSES:
            header = response.headers.get("Retry-After")
            return header if isinstance(header, str) else None
    return None


class DomainSpacing:
    """When each store may next be checked by the scheduler.

    Holds, per domain, the start of its latest check and the end of an active
    ``Retry-After`` deferral. Entries that no longer constrain anything (a start
    older than the gap, an expired deferral) are pruned, so the state stays
    bounded by the stores checked within the last gap or deferral.
    """

    def __init__(
        self,
        *,
        min_gap: float = DEFAULT_MIN_GAP_SECONDS,
        max_deferral: float = MAX_DEFERRAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not math.isfinite(min_gap) or min_gap < 0:
            raise ValueError("min_gap must be a finite number >= 0")
        if not math.isfinite(max_deferral) or max_deferral < 0:
            raise ValueError("max_deferral must be a finite number >= 0")
        self._min_gap = min_gap
        self._max_deferral = max_deferral
        self._clock = clock
        self._wall_clock = wall_clock
        self._sleep = sleep
        self._last_start: dict[str, float] = {}
        self._deferred_until: dict[str, float] = {}

    @property
    def min_gap(self) -> float:
        """Seconds between the starts of two scheduled checks on the same store."""
        return self._min_gap

    def now(self) -> float:
        """The spacing clock (monotonic seconds)."""
        return self._clock()

    async def sleep(self, seconds: float) -> None:
        """Wait ``seconds`` on the spacing clock."""
        await self._sleep(seconds)

    def tracked_domains(self) -> int:
        """How many domains currently hold state (for the boundedness guarantee)."""
        return len(self._last_start.keys() | self._deferred_until.keys())

    def _prune(self, now: float) -> None:
        stale = [d for d, start in self._last_start.items() if now - start >= self._min_gap]
        for domain in stale:
            del self._last_start[domain]
        expired = [d for d, until in self._deferred_until.items() if until <= now]
        for domain in expired:
            del self._deferred_until[domain]

    def record_start(self, domain: str) -> None:
        """Note that a check on ``domain`` starts now (scheduled or manual)."""
        now = self._clock()
        self._prune(now)
        if self._min_gap > 0:
            self._last_start[domain] = now

    def wait_for(self, domain: str) -> float:
        """Seconds until the gap allows the next scheduled check on ``domain``."""
        start = self._last_start.get(domain)
        if start is None:
            return 0.0
        return max(0.0, start + self._min_gap - self._clock())

    def is_deferred(self, domain: str) -> bool:
        """Whether a ``Retry-After`` still defers ``domain``."""
        until = self._deferred_until.get(domain)
        return until is not None and until > self._clock()

    def deferred_for(self, domain: str) -> float:
        """Seconds until the active deferral ends, without changing stored state."""
        until = self._deferred_until.get(domain)
        if until is None:
            return 0.0
        return max(0.0, until - self._clock())

    def defer(self, domain: str, retry_after: str | None) -> None:
        """Defer ``domain`` for as long as a 429/503 ``Retry-After`` asks, capped.

        An unusable header leaves the store alone; a shorter deferral never cuts
        an active longer one.
        """
        seconds = parse_retry_after(retry_after, now=self._wall_clock())
        if seconds is None:
            return
        now = self._clock()
        self._prune(now)
        until = now + min(seconds, self._max_deferral)
        if until > now and until > self._deferred_until.get(domain, -math.inf):
            self._deferred_until[domain] = until


async def run_spaced(
    jobs: Sequence[JobT],
    *,
    spacing: DomainSpacing,
    domain_of: Callable[[JobT], str | None],
    admit: Callable[[JobT, str], bool],
    run: Callable[[JobT], Awaitable[None]],
    drop: Callable[[JobT], None] | None = None,
) -> None:
    """Run ``jobs`` so that two starts on the same domain are ``min_gap`` apart.

    Jobs keep their order except when a store must wait: then the first job in
    order whose store may start goes first. The loop sleeps only when every
    pending job waits for its store's gap, and wakes when the first of them may
    start. Before a job starts, ``admit(job, domain)`` decides whether it still
    runs (quarantine, one probe per half-open store); a job refused there, or
    whose store is deferred by ``Retry-After``, is dropped from this run and
    handed to ``drop``. A job without a domain is never spaced.
    """
    pending = list(jobs)
    while pending:
        chosen: list[JobT] = []
        chosen_domain: str | None = None
        wait = math.inf
        kept: list[JobT] = []
        for job in pending:
            if chosen:
                kept.append(job)
                continue
            domain = domain_of(job)
            if domain is not None:
                if spacing.is_deferred(domain) or not admit(job, domain):
                    if drop is not None:
                        drop(job)
                    continue
                remaining = spacing.wait_for(domain)
                if remaining > 0:
                    wait = min(wait, remaining)
                    kept.append(job)
                    continue
            chosen.append(job)
            chosen_domain = domain
        pending = kept
        if not chosen:
            if pending:
                await spacing.sleep(wait)
            continue
        if chosen_domain is not None:
            spacing.record_start(chosen_domain)
        await run(chosen[0])
