"""No image download while its host or a store asking for it is paused.

The thumbnail service reads the stores' quarantine through a predicate and never
writes it. A queued job remembers which stores asked for its URL; a paused image
host or a paused store drops the job before any download, with the normal
failure backoff.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.domain_spacing import DomainSpacing
from price_tracker.core.url_utils import extract_etld_plus_one
from price_tracker.media import thumbnails
from price_tracker.media.cache import ThumbnailCache
from price_tracker.media.fetch import ImageFetchError
from price_tracker.media.thumbnails import ThumbnailService
from tests.support.images import jpeg_bytes

A1 = "https://cdn.shop.com/a.jpg"
B1 = "https://cdn.other.net/a.jpg"
RETRY_AFTER_FAILURE = 86_400.0


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = Clock()
    cache = ThumbnailCache(tmp_path / "cache")
    spacing = DomainSpacing(
        clock=clock, sleep=clock.sleep, wall_clock=lambda: datetime(2026, 11, 4, tzinfo=UTC)
    )
    paused: set[str] = set()
    asked: list[object] = []
    downloads: list[str] = []

    def store_paused(domain: str) -> bool:
        asked.append(domain)
        return domain in paused

    async def referenced() -> list[str]:
        return []

    async def inline(function: Any, *args: Any) -> Any:
        return function(*args)

    async def download(url: str, client: Any) -> bytes:
        downloads.append(url)
        return jpeg_bytes()

    monkeypatch.setattr("price_tracker.media.thumbnails.asyncio.to_thread", inline)
    monkeypatch.setattr(thumbnails, "download_image", download)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected network"))
    ) as client:
        service = ThumbnailService(
            cache,
            client=client,
            job_pacing=spacing,
            referenced_urls=referenced,
            store_paused=store_paused,
            clock=clock,
            sleep=clock.sleep,
            retry_after_failure=RETRY_AFTER_FAILURE,
        )
        yield SimpleNamespace(
            clock=clock,
            cache=cache,
            paused=paused,
            asked=asked,
            downloads=downloads,
            service=service,
        )


async def test_paused_image_host_drops_the_job_without_download(rig: Any) -> None:
    rig.paused.add("shop.com")

    assert rig.service.request(A1)
    await rig.service.process_one(A1)

    assert rig.downloads == []
    assert A1 not in rig.service._pending
    assert not rig.cache.has(A1)
    assert rig.service.request(A1) is False


async def test_paused_store_drops_the_job_of_an_open_image_host(rig: Any) -> None:
    rig.paused.add("paused.com")

    assert rig.service.request(B1, store="paused.com")
    await rig.service.process_one(B1)

    assert rig.downloads == []
    assert rig.service.request(B1, store="paused.com") is False


@pytest.mark.parametrize("paused", ["example.com", "shop.com"])
async def test_pause_between_request_and_download_is_honoured(rig: Any, paused: str) -> None:
    assert rig.service.request(A1, store="example.com")
    rig.paused.add(paused)

    await rig.service.process_one(A1)

    assert rig.downloads == []


async def test_nothing_paused_downloads_as_before(rig: Any) -> None:
    assert rig.service.request(A1, store="example.com")

    await rig.service.process_one(A1)

    assert rig.downloads == [A1]
    assert rig.cache.has(A1)


async def test_store_open_again_after_the_backoff_downloads(rig: Any) -> None:
    rig.paused.add("paused.com")
    assert rig.service.request(A1, store="paused.com")
    await rig.service.process_one(A1)
    assert rig.downloads == []

    rig.clock.advance(RETRY_AFTER_FAILURE)
    rig.paused.discard("paused.com")
    assert rig.service.request(A1, store="paused.com")
    await rig.service.process_one(A1)

    assert rig.downloads == [A1]


async def test_duplicate_request_while_queued_adds_its_store(rig: Any) -> None:
    assert rig.service.request(A1, store="open.com")
    assert rig.service.request(A1, store="paused.com") is False
    rig.paused.add("paused.com")

    await rig.service.process_one(A1)

    assert rig.downloads == []


@pytest.mark.parametrize("store", [None, "", 42, b"paused.com", ["paused.com"]])
async def test_unusable_store_is_ignored(rig: Any, store: object) -> None:
    rig.paused.add("paused.com")

    assert rig.service.request(A1, store=store)
    await rig.service.process_one(A1)

    assert rig.downloads == [A1]
    assert all(isinstance(name, str) and name for name in rig.asked)
    assert set(rig.asked) == {"shop.com"}


# --- property: an independent oracle of queued jobs and their stores ---------

_IMAGE_URLS = (
    "https://cdn.alpha.com/a.jpg",
    "https://alpha.com/b.jpg",
    "https://beta.com/c.jpg",
    "https://cdn.gamma.net/d.jpg",
    "https://delta.org/e.jpg",
)
_STORES: tuple[object, ...] = ("alpha.com", "beta.com", "shop1.com", "shop2.com", None, "", 7)
_DOMAINS = ("alpha.com", "beta.com", "gamma.net", "delta.org", "shop1.com", "shop2.com")

_EVENTS = st.one_of(
    st.tuples(
        st.just("request"),
        st.integers(0, len(_IMAGE_URLS) - 1),
        st.integers(0, len(_STORES) - 1),
    ),
    st.tuples(st.just("pause"), st.integers(0, len(_DOMAINS) - 1)),
    st.tuples(st.just("resume"), st.integers(0, len(_DOMAINS) - 1)),
    st.tuples(st.just("advance"), st.integers(0, 90_000)),
    st.tuples(st.just("process"), st.integers(0, len(_IMAGE_URLS) - 1)),
    st.tuples(st.just("fail_next")),
)


class _Oracle:
    """What the service must do, modelled without reading the service's state."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.queued: dict[str, set[str]] = {}
        self.cached: set[str] = set()
        self.failed: dict[str, float] = {}

    def _backing_off(self, url: str) -> bool:
        failed_at = self.failed.get(url)
        return failed_at is not None and self.clock() - failed_at < RETRY_AFTER_FAILURE

    def request(self, url: str, store: object) -> bool:
        if url in self.cached:
            return False
        stores = {store} if isinstance(store, str) and store else set()
        if url in self.queued:
            self.queued[url] |= stores
            return False
        if self._backing_off(url):
            return False
        self.failed.pop(url, None)
        self.queued[url] = stores
        return True

    def end(self, url: str) -> None:
        self.queued.pop(url, None)


@given(st.lists(_EVENTS, min_size=1, max_size=60))
@settings(max_examples=100, deadline=None)
def test_no_download_reaches_a_paused_host_or_store(events: list[tuple[Any, ...]]) -> None:
    async def scenario(root: Path) -> None:
        clock = Clock()
        cache = ThumbnailCache(root)
        spacing = DomainSpacing(min_gap=0, clock=clock)
        paused: set[str] = set()
        oracle = _Oracle(clock)
        fail_next = False
        expected: dict[str, Any] = {"url": None, "stores": set()}
        downloaded: list[str] = []

        async def referenced() -> list[str]:
            return []

        async def download(url: str, passed_client: Any) -> bytes:
            nonlocal fail_next
            assert url == expected["url"], "download that the oracle did not expect"
            assert extract_etld_plus_one(url) not in paused
            assert not (expected["stores"] & paused)
            downloaded.append(url)
            if fail_next:
                fail_next = False
                raise ImageFetchError("HTTP 500", status_code=500)
            return jpeg_bytes()

        async with httpx.AsyncClient() as client:
            service = ThumbnailService(
                cache,
                client=client,
                job_pacing=spacing,
                referenced_urls=referenced,
                store_paused=lambda domain: domain in paused,
                clock=clock,
                sleep=clock.sleep,
                retry_after_failure=RETRY_AFTER_FAILURE,
            )
            patch = pytest.MonkeyPatch()
            patch.setattr(thumbnails, "download_image", download)
            try:
                for event in events:
                    kind = event[0]
                    if kind == "request":
                        url, store = _IMAGE_URLS[event[1]], _STORES[event[2]]
                        assert service.request(url, store=store) is oracle.request(url, store)
                    elif kind == "pause":
                        paused.add(_DOMAINS[event[1]])
                    elif kind == "resume":
                        paused.discard(_DOMAINS[event[1]])
                    elif kind == "advance":
                        clock.advance(event[1])
                    elif kind == "fail_next":
                        fail_next = True
                    else:
                        url = _IMAGE_URLS[event[1]]
                        if url not in oracle.queued and not oracle.request(url, None):
                            expected["url"] = None
                        else:
                            stores = oracle.queued[url]
                            names = {extract_etld_plus_one(url), *stores} - {""}
                            if names & paused:
                                oracle.end(url)
                                oracle.failed[url] = clock()
                                expected["url"] = None
                            else:
                                expected["url"], expected["stores"] = url, set(stores)
                        before = len(downloaded)
                        failing = fail_next
                        await service.process_one(url)
                        if expected["url"] is not None:
                            assert len(downloaded) == before + 1
                            oracle.end(url)
                            if failing:
                                oracle.failed[url] = clock()
                            else:
                                oracle.cached.add(url)
                        else:
                            assert len(downloaded) == before
                        expected["url"], expected["stores"] = None, set()
            finally:
                patch.undo()

    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(scenario(Path(directory)))


@pytest.mark.parametrize("status", [403, 429, 503])
async def test_image_refusals_never_write_the_quarantine(
    rig: Any, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    from unittest.mock import AsyncMock

    from price_tracker.core.health import HealthManager

    repo = AsyncMock()
    repo.list_all_scraper_health = AsyncMock(return_value=[])
    health = HealthManager(repo)
    await health.load()
    record_block = AsyncMock(wraps=health.record_block)
    record_success = AsyncMock(wraps=health.record_success)
    monkeypatch.setattr(health, "record_block", record_block)
    monkeypatch.setattr(health, "record_success", record_success)
    rig.service._store_paused = health.is_quarantined

    async def refused(url: str, client: Any) -> bytes:
        raise ImageFetchError(
            f"HTTP {status}",
            status_code=status,
            response_domain="shop.com",
            retry_after_header="60",
        )

    monkeypatch.setattr(thumbnails, "download_image", refused)
    assert rig.service.request(A1, store="example.com")
    await rig.service.process_one(A1)
    rig.clock.advance(RETRY_AFTER_FAILURE)
    assert rig.service.request(A1, store="example.com")
    await rig.service.process_one(A1)

    record_block.assert_not_called()
    record_success.assert_not_called()
    assert health.is_quarantined("shop.com") is False
    assert health.is_quarantined("example.com") is False
