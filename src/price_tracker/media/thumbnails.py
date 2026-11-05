"""A bounded background queue sharing the scheduler's store-spacing state."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import TYPE_CHECKING, Final

from price_tracker.core.image_url import normalize_image_url
from price_tracker.core.url_utils import extract_etld_plus_one
from price_tracker.media.decode import ImageRejected, make_thumbnail
from price_tracker.media.fetch import ImageFetchError, download_image

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    import httpx

    from price_tracker.core.domain_spacing import DomainSpacing
    from price_tracker.media.cache import ThumbnailCache

logger = logging.getLogger(__name__)
MAX_TRACKED_URLS: Final = 2048


class ThumbnailService:
    """Fetches, shrinks and caches product images in the background, one at a time."""

    def __init__(
        self,
        cache: ThumbnailCache,
        *,
        client: httpx.AsyncClient,
        job_pacing: DomainSpacing,
        referenced_urls: Callable[[], Awaitable[Iterable[str]]],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        retry_after_failure: float = 86_400.0,
        prune_every: float = 86_400.0,
        queue_size: int = 256,
    ) -> None:
        if (
            not isinstance(queue_size, int)
            or isinstance(queue_size, bool)
            or not 1 <= queue_size <= MAX_TRACKED_URLS
        ):
            raise ValueError("queue_size must be an integer in 1..2048")
        for value in (retry_after_failure, prune_every):
            if (
                not isinstance(value, int | float)
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("worker intervals must be finite and positive")
        self._cache = cache
        self._client = client
        self._spacing = job_pacing
        self._referenced_urls = referenced_urls
        self._clock = clock
        self._sleep = sleep
        self._retry_after_failure = retry_after_failure
        self._prune_every = prune_every
        self._queue_size = queue_size
        self._pending: dict[str, None] = {}
        self._in_progress: set[str] = set()
        self._failed: dict[str, float] = {}
        self._wake = asyncio.Event()
        self._last_domain: str | None = None

    def _expire_failures(self) -> None:
        now = self._clock()
        for url, failed_at in list(self._failed.items()):
            if now - failed_at >= self._retry_after_failure:
                del self._failed[url]

    def request(self, url: str | None) -> bool:
        if not isinstance(url, str) or normalize_image_url(url, base_url=url) != url:
            return False
        self._expire_failures()
        try:
            if self._cache.has(url):
                return False
        except OSError:
            logger.info("thumbnail cache lookup failed for %s", extract_etld_plus_one(url))
            return False
        if url in self._pending or url in self._in_progress or url in self._failed:
            return False
        if len(self._pending) >= self._queue_size:
            logger.debug("thumbnail queue full")
            return False
        if len(self._pending.keys() | self._in_progress | self._failed.keys()) >= MAX_TRACKED_URLS:
            logger.debug("thumbnail tracking limit reached")
            return False
        self._pending[url] = None
        self._wake.set()
        return True

    def cached_bytes(self, url: str | None) -> bytes | None:
        if not isinstance(url, str) or normalize_image_url(url, base_url="") != url:
            return None
        try:
            return self._cache.read(url)
        except OSError:
            logger.info("thumbnail cache read failed for %s", extract_etld_plus_one(url))
            return None

    def _delay(self, domain: str) -> float:
        return max(self._spacing.wait_for(domain), self._spacing.deferred_for(domain))

    async def process_one(self, url: str) -> None:
        if url in self._in_progress:
            return
        if url not in self._pending and not self.request(url):
            return
        try:
            if self._cache.has(url):
                self._pending.pop(url, None)
                self._failed.pop(url, None)
                return
        except OSError:
            self._pending.pop(url, None)
            self._failed[url] = self._clock()
            logger.info("thumbnail cache lookup failed for %s", extract_etld_plus_one(url))
            return
        domain = extract_etld_plus_one(url)
        if self._delay(domain) > 0:
            return
        self._pending.pop(url, None)
        self._in_progress.add(url)
        self._last_domain = domain
        try:
            self._spacing.record_start(domain)
            data = await download_image(url, self._client)
            thumb = await asyncio.to_thread(make_thumbnail, data)
            await asyncio.to_thread(self._cache.write, url, thumb)
            self._failed.pop(url, None)
        except (ImageFetchError, ImageRejected, OSError, ValueError) as exc:
            self._failed[url] = self._clock()
            if (
                isinstance(exc, ImageFetchError)
                and exc.status_code in (429, 503)
                and exc.response_domain
            ):
                self._spacing.defer(exc.response_domain, exc.retry_after_header)
            reason = (
                exc.reason
                if isinstance(exc, ImageFetchError | ImageRejected)
                else type(exc).__name__
            )
            logger.info("thumbnail failed for %s: %s", domain, reason)
        finally:
            self._in_progress.discard(url)

    def _ready(self) -> str | None:
        ready: dict[str, str] = {}
        seen: set[str] = set()
        for url in self._pending:
            domain = extract_etld_plus_one(url)
            if domain not in seen:
                seen.add(domain)
                if self._delay(domain) == 0:
                    ready[domain] = url
        for domain, url in ready.items():
            if domain != self._last_domain:
                return url
        return next(iter(ready.values()), None)

    async def _wait(self, next_prune: float) -> None:
        deadlines = [max(0.0, next_prune - self._clock())]
        deadlines.extend(self._delay(extract_etld_plus_one(url)) for url in self._pending)
        tasks = [
            asyncio.ensure_future(self._sleep(min(deadlines))),
            asyncio.create_task(self._wake.wait()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def run(self) -> None:
        next_prune = self._clock()
        while True:
            self._wake.clear()
            url = None
            pruning = self._clock() >= next_prune
            try:
                if pruning:
                    next_prune = self._clock() + self._prune_every
                    await asyncio.to_thread(self._cache.prune, await self._referenced_urls())
                else:
                    url = self._ready()
                    if url is None:
                        await self._wait(next_prune)
                    else:
                        await self.process_one(url)
            except Exception:  # noqa: BLE001 — one bad image must not stop the worker
                if url is not None:
                    self._failed[url] = self._clock()
                    self._pending.pop(url, None)
                    self._in_progress.discard(url)
                logger.warning("thumbnail worker operation failed", exc_info=True)
