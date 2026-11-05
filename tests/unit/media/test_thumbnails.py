"""Background image jobs share store spacing, bounded deduplication and backoff."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from price_tracker.core.domain_spacing import DomainSpacing
from price_tracker.media import thumbnails
from price_tracker.media.cache import ThumbnailCache
from price_tracker.media.fetch import ImageFetchError
from price_tracker.media.thumbnails import MAX_TRACKED_URLS, ThumbnailService
from tests.support.images import jpeg_bytes

A1 = "https://cdn.shop.com/a.jpg"
A2 = "https://www.shop.com/b.jpg"
A3 = "https://shop.com/c.jpg"
B1 = "https://cdn.other.net/a.jpg"
B2 = "https://other.net/b.jpg"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.waits: dict[asyncio.Future[None], float] = {}

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        assert seconds > 0
        future = asyncio.get_running_loop().create_future()
        self.waits[future] = self.now + seconds
        try:
            await future
        finally:
            self.waits.pop(future, None)

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for future, until in list(self.waits.items()):
            if until <= self.now and not future.done():
                future.set_result(None)


async def settle() -> None:
    for _ in range(30):
        await asyncio.sleep(0)


@pytest.fixture
async def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = Clock()
    cache = ThumbnailCache(tmp_path / "cache")
    spacing = DomainSpacing(
        clock=clock, sleep=clock.sleep, wall_clock=lambda: datetime(2026, 11, 4, tzinfo=UTC)
    )
    refs: list[str] = []

    async def referenced() -> list[str]:
        return refs

    async def inline(function: Any, *args: Any) -> Any:
        return function(*args)

    monkeypatch.setattr("price_tracker.media.thumbnails.asyncio.to_thread", inline)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("unexpected network"))
    ) as client:

        def make(**kwargs: Any) -> ThumbnailService:
            return ThumbnailService(
                cache,
                client=client,
                job_pacing=spacing,
                referenced_urls=referenced,
                clock=clock,
                sleep=clock.sleep,
                **kwargs,
            )

        yield SimpleNamespace(
            clock=clock, cache=cache, spacing=spacing, refs=refs, make=make, client=client
        )


@pytest.mark.parametrize("field", ["retry_after_failure", "prune_every"])
@pytest.mark.parametrize(
    "value", [None, "1", True, False, 0, -1, float("nan"), float("inf"), -float("inf")]
)
async def test_invalid_intervals_are_refused_before_worker_state(
    rig: Any, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    def forbidden(*args: Any) -> Any:
        pytest.fail("invalid parameters must precede worker event creation")

    monkeypatch.setattr("price_tracker.media.thumbnails.asyncio.Event", forbidden)
    with pytest.raises(ValueError, match="positive"):
        rig.make(**{field: value})


@pytest.mark.parametrize(
    "value", [None, "1", True, False, 0, -1, 1.0, float("nan"), float("inf"), -float("inf"), 2049]
)
async def test_invalid_queue_sizes_are_refused(rig: Any, value: Any) -> None:
    with pytest.raises(ValueError, match="queue_size"):
        rig.make(queue_size=value)


@pytest.mark.parametrize("size", [1, 2048])
async def test_constructor_accepts_queue_boundaries_and_has_no_health_dependency(
    rig: Any, size: int
) -> None:
    service = rig.make(queue_size=size, prune_every=0.1, retry_after_failure=0.1)
    assert service.request(A1)
    assert "health" not in str(inspect.signature(ThumbnailService)).lower()


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "/relative",
        "http://shop.com/a",
        "https://[bad",
        "https://shop.com:bad/a",
        "https://shop.com:99999/a",
        "\ud800",
        True,
        {},
    ],
)
async def test_bad_urls_never_access_the_cache_or_queue(
    rig: Any, monkeypatch: pytest.MonkeyPatch, url: Any
) -> None:
    service = rig.make()

    def forbidden(*args: Any) -> Any:
        pytest.fail("invalid URL accessed the cache")

    monkeypatch.setattr(rig.cache, "has", forbidden)
    monkeypatch.setattr(rig.cache, "read", forbidden)
    assert not service.request(url)
    assert service.cached_bytes(url) is None


async def test_request_deduplicates_bounds_the_queue_and_ignores_cached_images(rig: Any) -> None:
    service = rig.make(queue_size=2)
    assert service.request(A1)
    assert not service.request(A1)
    assert service.request(B1)
    assert not service.request(A2)
    rig.cache.write(A3, b"\xff\xd8\xffcached")
    assert not service.request(A3)


async def test_cached_bytes_only_reads_and_never_enqueues(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = rig.make()
    rig.cache.write(A1, b"\xff\xd8\xffcached")

    def forbidden(*args: Any) -> Any:
        pytest.fail("cache read must not check a path or enqueue")

    monkeypatch.setattr(rig.cache, "has", forbidden)
    monkeypatch.setattr(service, "request", forbidden)
    # The real cache uses path_for internally; only the service is forbidden to do so.
    assert service.cached_bytes(A1) == b"\xff\xd8\xffcached"
    assert service.cached_bytes(A2) is None
    rig.cache.path_for(A1).write_bytes(b"corrupt")
    assert service.cached_bytes(A1) is None


async def test_cache_access_errors_are_harmless_and_logs_do_not_include_urls(
    rig: Any, monkeypatch: pytest.MonkeyPatch, caplog: Any
) -> None:
    service = rig.make()

    def denied(*args: Any) -> Any:
        raise OSError("access denied")

    monkeypatch.setattr(rig.cache, "has", denied)
    monkeypatch.setattr(rig.cache, "read", denied)
    with caplog.at_level("DEBUG"):
        assert not service.request(A1)
        assert service.cached_bytes(A1) is None
    assert caplog.records
    assert A1 not in caplog.text


async def test_success_stores_only_a_clean_thumbnail_and_frees_the_slot(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = jpeg_bytes()
    starts = []

    async def download(url: str, client: Any) -> bytes:
        starts.append((url, rig.clock()))
        assert client is rig.client
        assert not service.request(url)
        return source

    monkeypatch.setattr(thumbnails, "download_image", download)
    service = rig.make(queue_size=1)
    assert service.request(A1)
    await service.process_one(A1)
    data = rig.cache.read(A1)
    assert data is not None
    assert data != source
    import io

    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (320, 320)
        assert image.format == "JPEG"
    assert starts == [(A1, 0)]
    assert service.request(B1)
    assert not service.request(A1)


@pytest.mark.parametrize("failure", ["download", "decode", "write"])
async def test_failures_are_backed_off_for_a_full_day(
    rig: Any, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    async def download(url: str, client: Any) -> bytes:
        if failure == "download":
            raise ImageFetchError("failed")
        return b"not image" if failure == "decode" else jpeg_bytes()

    monkeypatch.setattr(thumbnails, "download_image", download)
    if failure == "write":

        def denied(*args: Any) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(rig.cache, "write", denied)
    service = rig.make()
    await service.process_one(A1)
    assert not rig.cache.has(A1)
    assert not service.request(A1)
    rig.clock.advance(86399)
    assert not service.request(A1)
    rig.clock.advance(1)
    assert service.request(A1)


async def test_an_already_satisfied_job_never_consumes_a_spacing_start(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = rig.make()
    assert service.request(A1)
    rig.cache.write(A1, b"\xff\xd8\xffcached")
    await service.process_one(A1)
    assert rig.spacing.wait_for("shop.com") == 0
    assert rig.spacing.tracked_domains() == 0


async def test_denied_admission_stays_queued_without_a_failure(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts = []

    async def download(url: str, client: Any) -> bytes:
        starts.append(rig.clock())
        return jpeg_bytes()

    monkeypatch.setattr(thumbnails, "download_image", download)
    service = rig.make()
    rig.spacing.record_start("shop.com")
    await service.process_one(A1)
    assert starts == []
    assert not service.request(A1)
    rig.clock.advance(30)
    await service.process_one(A1)
    assert starts == [30]


async def stop(task: asyncio.Task[None], clock: Clock) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not clock.waits


async def test_download_starts_are_spaced_per_registrable_domain(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts = []

    async def download(url: str, client: Any) -> bytes:
        starts.append((url, rig.clock()))
        return jpeg_bytes()

    monkeypatch.setattr(thumbnails, "download_image", download)
    service = rig.make()
    rig.refs.extend([A1, A2, A3, B1, B2])
    for url in rig.refs:
        assert service.request(url)
    task = asyncio.create_task(service.run())
    try:
        await settle()
        assert starts == [(A1, 0), (B1, 0)]
        rig.clock.advance(30)
        await settle()
        assert starts == [(A1, 0), (B1, 0), (A2, 30), (B2, 30)]
        rig.clock.advance(30)
        await settle()
        assert starts[-1] == (A3, 60)
    finally:
        await stop(task, rig.clock)


async def test_a_waiting_domain_does_not_block_a_new_ready_domain(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts = []

    async def download(url: str, client: Any) -> bytes:
        starts.append((url, rig.clock()))
        return jpeg_bytes()

    monkeypatch.setattr(thumbnails, "download_image", download)
    service = rig.make()
    rig.refs.extend([A1, A2, B1])
    service.request(A1)
    service.request(A2)
    task = asyncio.create_task(service.run())
    try:
        await settle()
        assert starts == [(A1, 0)]
        assert rig.clock.waits
        assert service.request(B1)
        await settle()
        assert starts == [(A1, 0), (B1, 0)]
        assert rig.spacing.wait_for("shop.com") == 30
    finally:
        await stop(task, rig.clock)


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize(
    ("header", "delay"),
    [
        ("90", 90),
        ("9" * 400, 3600),
        ("garbage", 0),
        ("-5", 0),
        ("Wed, 04 Nov 2026 00:01:30 GMT", 90),
    ],
)
async def test_retry_after_defers_only_the_response_domain(
    rig: Any, monkeypatch: pytest.MonkeyPatch, status: int, header: str, delay: int
) -> None:
    async def download(url: str, client: Any) -> bytes:
        raise ImageFetchError(
            f"status {status}",
            status_code=status,
            response_domain="other.net",
            retry_after_header=header,
        )

    monkeypatch.setattr(thumbnails, "download_image", download)
    service = rig.make()
    await service.process_one(A1)
    assert rig.spacing.deferred_for("other.net") == delay
    assert not rig.spacing.is_deferred("shop.com")
    rig.clock.advance(3600)
    assert not rig.spacing.is_deferred("other.net")
    assert not service.request(A1)


async def test_manual_starts_move_the_background_deadline_without_blocking_the_request(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts = []

    async def download(url: str, client: Any) -> bytes:
        starts.append(rig.clock())
        return jpeg_bytes()

    monkeypatch.setattr(thumbnails, "download_image", download)
    service = rig.make()
    rig.spacing.record_start("shop.com")
    assert service.request(A1)
    task = asyncio.create_task(service.run())
    try:
        await settle()
        rig.clock.advance(15)
        rig.spacing.record_start("shop.com")
        rig.clock.advance(15)
        await settle()
        assert starts == []
        rig.clock.advance(15)
        await settle()
        assert starts == [45]
    finally:
        await stop(task, rig.clock)


async def test_zero_gap_keeps_retry_after_deferrals(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig.spacing = DomainSpacing(min_gap=0, clock=rig.clock)

    # Construct with the real zero-gap instance, preserving the same clock.
    async def refs() -> list[str]:
        return []

    service = ThumbnailService(
        rig.cache,
        client=rig.client,
        job_pacing=rig.spacing,
        referenced_urls=refs,
        clock=rig.clock,
        sleep=rig.clock.sleep,
    )
    starts = []

    async def download(url: str, client: Any) -> bytes:
        starts.append(rig.clock())
        return jpeg_bytes()

    monkeypatch.setattr(thumbnails, "download_image", download)
    await service.process_one(A1)
    await service.process_one(A2)
    assert starts == [0, 0]
    rig.spacing.defer("shop.com", "60")
    await service.process_one(A3)
    assert starts == [0, 0]
    rig.clock.advance(60)
    await service.process_one(A3)
    assert starts == [0, 0, 60]


async def test_unexpected_job_errors_preserve_backoff_and_the_worker_continues(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = rig.make()
    processed = []
    original = service.process_one

    async def download(url: str, client: Any) -> bytes:
        return jpeg_bytes()

    async def process(url: str) -> None:
        if url == A1:
            raise RuntimeError("unexpected failure")
        await original(url)
        processed.append(url)

    monkeypatch.setattr(thumbnails, "download_image", download)
    monkeypatch.setattr(service, "process_one", process)
    service.request(A1)
    service.request(B1)
    task = asyncio.create_task(service.run())
    try:
        await settle()
        assert processed == [B1]
        assert rig.cache.has(B1)
        assert not service.request(A1)
        rig.clock.advance(86400)
        assert service.request(A1)
    finally:
        await stop(task, rig.clock)


async def test_prune_deadlines_are_not_postponed_by_arrivals(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    times = []
    original = rig.cache.prune

    def prune(urls: Any) -> int:
        times.append(rig.clock())
        return int(original(urls))

    async def download(url: str, client: Any) -> bytes:
        return jpeg_bytes()

    monkeypatch.setattr(rig.cache, "prune", prune)
    monkeypatch.setattr(thumbnails, "download_image", download)
    service = rig.make(prune_every=10)
    task = asyncio.create_task(service.run())
    try:
        await settle()
        assert times == [0]
        for n in range(1, 11):
            rig.clock.advance(1)
            service.request(f"https://s{n}.net/image.jpg")
            await settle()
        assert times == [0, 10]
        rig.clock.advance(10)
        await settle()
        assert times == [0, 10, 20]
    finally:
        await stop(task, rig.clock)


async def test_failure_reservations_are_never_evicted_early_to_admit_new_urls(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def download(url: str, client: Any) -> bytes:
        raise ImageFetchError("failed")

    monkeypatch.setattr(thumbnails, "download_image", download)
    service = rig.make()
    for index in range(MAX_TRACKED_URLS):
        url = f"https://s{index}.net/image.jpg"
        await service.process_one(url)
        assert not service.request(url)
    assert not service.request("https://fresh.net/image.jpg")
    rig.clock.advance(86400)
    assert service.request("https://fresh.net/image.jpg")


async def test_prune_failures_do_not_stop_jobs_or_assign_url_failures(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = rig.cache.prune
    calls = []

    def prune(urls: Any) -> int:
        calls.append(rig.clock())
        if len(calls) == 1:
            raise RuntimeError("prune failed")
        return int(original(urls))

    async def download(url: str, client: Any) -> bytes:
        return jpeg_bytes()

    monkeypatch.setattr(rig.cache, "prune", prune)
    monkeypatch.setattr(thumbnails, "download_image", download)
    service = rig.make(prune_every=10)
    assert service.request(A1)
    task = asyncio.create_task(service.run())
    try:
        await settle()
        assert rig.cache.has(A1)
        rig.clock.advance(10)
        assert service.request(B1)
        await settle()
        assert calls == [0, 10]
        assert rig.cache.has(B1)
    finally:
        await stop(task, rig.clock)


async def test_cancellation_during_download_releases_its_dedup_slot(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()

    async def download(url: str, client: Any) -> bytes:
        entered.set()
        await asyncio.Event().wait()
        return jpeg_bytes()

    monkeypatch.setattr(thumbnails, "download_image", download)
    service = rig.make()
    assert service.request(A1)
    task = asyncio.create_task(service.run())
    await entered.wait()
    await stop(task, rig.clock)
    assert service.request(A1)


async def test_simultaneous_arrival_gap_and_prune_preserve_every_job(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts = []
    prunes = []
    original = rig.cache.prune

    def prune(urls: Any) -> int:
        prunes.append(rig.clock())
        return int(original(urls))

    async def download(url: str, client: Any) -> bytes:
        starts.append((url, rig.clock()))
        return jpeg_bytes()

    monkeypatch.setattr(thumbnails, "download_image", download)
    monkeypatch.setattr(rig.cache, "prune", prune)
    service = rig.make(prune_every=30)
    rig.refs.extend([A1, A2, B1])
    service.request(A1)
    service.request(A2)
    task = asyncio.create_task(service.run())
    try:
        await settle()
        rig.clock.advance(30)
        assert service.request(B1)
        await settle()
        assert {url for url, _ in starts} == {A1, A2, B1}
        assert len(starts) == 3
        assert (A2, 30) in starts
        assert (B1, 30) in starts
        assert prunes == [0, 30]
        assert all(rig.cache.has(url) for url in rig.refs)
    finally:
        await stop(task, rig.clock)


async def test_deferrals_are_queued_without_holding_resources(
    rig: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    starts = []

    async def download(url: str, client: Any) -> bytes:
        starts.append((url, rig.clock()))
        return jpeg_bytes()

    monkeypatch.setattr(thumbnails, "download_image", download)
    service = rig.make()
    rig.spacing.defer("shop.com", "60")
    assert service.request(A1)
    task = asyncio.create_task(service.run())
    try:
        await settle()
        assert starts == []
        assert len(rig.clock.waits) == 1
        assert service.request(B1)
        await settle()
        assert starts == [(B1, 0)]
        assert not service.request(A1)
        rig.clock.advance(60)
        await settle()
        assert starts == [(B1, 0), (A1, 60)]
    finally:
        await stop(task, rig.clock)


@given(
    st.lists(
        st.tuples(st.integers(0, 9), st.integers(0, 35), st.integers(0, 90)),
        min_size=1,
        max_size=80,
    )
)
@settings(max_examples=50, deadline=None)
def test_random_job_sequences_respect_domain_spacing_and_retention(
    events: list[tuple[int, int, int]],
) -> None:
    import tempfile

    from price_tracker.core.url_utils import extract_etld_plus_one

    async def scenario(root: Path) -> None:
        clock = Clock()
        cache = ThumbnailCache(root)
        spacing = DomainSpacing(min_gap=30, clock=clock)

        async def referenced() -> list[str]:
            return []

        async with httpx.AsyncClient() as client:
            service = ThumbnailService(
                cache,
                client=client,
                job_pacing=spacing,
                referenced_urls=referenced,
                clock=clock,
                sleep=clock.sleep,
            )
            observed: dict[str, float] = {}
            pending: set[str] = set()

            async def download(url: str, passed_client: Any) -> bytes:
                domain = extract_etld_plus_one(url)
                assert domain not in observed or clock() - observed[domain] >= 30
                assert not spacing.is_deferred(domain)
                observed[domain] = clock()
                return jpeg_bytes()

            patch = pytest.MonkeyPatch()
            patch.setattr(thumbnails, "download_image", download)
            try:
                for index, (domain_number, advance, deferral) in enumerate(events):
                    clock.advance(advance)
                    domain = f"s{domain_number}.com"
                    if deferral:
                        spacing.defer(domain, str(deferral))
                    url = f"https://cdn.{domain}/p{index}.jpg"
                    assert service.request(url)
                    pending.add(url)
                    for waiting in list(pending):
                        await service.process_one(waiting)
                        if cache.has(waiting):
                            pending.remove(waiting)
                        else:
                            assert not service.request(waiting)
                    assert len(pending) <= MAX_TRACKED_URLS
                clock.advance(3600)
                for waiting in pending:
                    clock.advance(30)
                    await service.process_one(waiting)
                    assert cache.has(waiting)
            finally:
                patch.undo()

    with tempfile.TemporaryDirectory() as directory:
        asyncio.run(scenario(Path(directory)))


@pytest.mark.parametrize("sweep", ["due", "all"])
async def test_worker_and_both_scheduler_sweeps_share_job_pacing(
    rig: Any, monkeypatch: pytest.MonkeyPatch, sweep: str
) -> None:
    import aiosqlite

    from price_tracker.db.migrator import apply_migrations
    from price_tracker.db.repository import Repository
    from tests.integration.test_scheduler_domain_spacing import (
        _add,
        _Clock,
        _scheduler,
        _TimedScraper,
    )

    async with aiosqlite.connect(":memory:") as connection:
        connection.row_factory = aiosqlite.Row
        await apply_migrations(connection, Path("src/price_tracker/db/migrations"))
        repo = Repository(connection)
        await repo.ensure_user(user_id=1)
        await _add(repo, "https://shop.com/product")
        clock = _Clock()
        scraper = _TimedScraper(clock)
        scheduler = _scheduler(repo, scraper, clock)
        scheduler.deps.client = rig.client

        async def referenced() -> list[str]:
            return []

        service = ThumbnailService(
            rig.cache,
            client=scheduler.deps.client,
            job_pacing=scheduler.deps.domain_spacing,
            referenced_urls=referenced,
            clock=clock,
            sleep=clock.sleep,
        )
        starts = []

        async def download(url: str, client: Any) -> bytes:
            starts.append(clock())
            return jpeg_bytes()

        monkeypatch.setattr(thumbnails, "download_image", download)
        if sweep == "due":
            await scheduler.run_check_due(global_interval_minutes=360)
        else:
            await scheduler.run_check_all()
        assert scraper.starts == [("shop.com/product", 0)]
        assert service.request(A1)
        await service.process_one(A1)
        assert starts == []
        clock.now = 30
        await service.process_one(A1)
        assert starts == [30]
        await scheduler.run_check_all()
        assert scraper.starts[-1] == ("shop.com/product", 60)
        assert clock.sleeps == [30]
        clock.now = 65
        scheduler.record_manual_start("https://shop.com/manual")
        assert clock.now == 65
        assert service.request(A2)
        await service.process_one(A2)
        assert starts == [30]
        clock.now = 95
        await service.process_one(A2)
        assert starts == [30, 95]


@pytest.mark.parametrize("cross_domain", [False, True])
async def test_redirect_chain_is_one_download_job(rig: Any, cross_domain: bool) -> None:
    import respx

    from price_tracker.core.http_client import build_client

    middle = "https://other.net/redirect.jpg" if cross_domain else "https://shop.com/redirect.jpg"
    final = "https://shop.com/final.jpg"

    async def referenced() -> list[str]:
        return [A1]

    with respx.mock as router:
        router.get(A1).respond(302, headers={"Location": middle})
        router.get(middle).respond(302, headers={"Location": final})
        router.get(final).respond(200, content=jpeg_bytes(), headers={"Content-Type": "image/jpeg"})
        async with build_client() as client:
            service = ThumbnailService(
                rig.cache,
                client=client,
                job_pacing=rig.spacing,
                referenced_urls=referenced,
                clock=rig.clock,
                sleep=rig.clock.sleep,
            )
            await service.process_one(A1)
        assert len(router.calls) == 3
    assert rig.cache.has(A1)
    assert rig.spacing.wait_for("shop.com") == 30
    assert rig.spacing.wait_for("other.net") == 0
    assert rig.spacing.tracked_domains() == 1
    assert not rig.clock.waits
