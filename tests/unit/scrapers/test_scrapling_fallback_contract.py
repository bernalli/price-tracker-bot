"""Contract: the scrapling fallback must not block the event loop (bugs #21/#59).

scrapling 0.4.7's ``Fetcher.get`` is synchronous (``FetcherClient.get``, with
``time.sleep`` between retries — up to 3x30s). Calling it directly inside
``async def _fetch_via_scrapling`` froze the whole event loop (scheduler tick,
bot handlers, health heartbeats) for the duration of the fetch. The call must
be offloaded to a worker thread via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import sys
import time
import types

import pytest

from price_tracker.scrapers.amazon import _fetch_via_scrapling

_FAKE_HTML = "<html>scrapling-ok</html>"
_FETCH_SECONDS = 0.5
_HEARTBEAT_INTERVAL = 0.05
_MIN_TICKS = 3


class _FakePage:
    """Minimal scrapling response stub (status + body)."""

    status = 200
    text = _FAKE_HTML
    html_content = _FAKE_HTML


class _SlowSyncFetcher:
    """Fake scrapling Fetcher whose .get is synchronous and slow."""

    @staticmethod
    def get(url: str, **kwargs: object) -> _FakePage:  # noqa: ARG004
        time.sleep(_FETCH_SECONDS)  # simulates sync I/O + inter-retry sleeps
        return _FakePage()


def _make_fake_scrapling() -> types.ModuleType:
    module = types.ModuleType("scrapling")
    module.Fetcher = _SlowSyncFetcher  # type: ignore[attr-defined]
    return module


@pytest.mark.slow
async def test_scrapling_fallback_does_not_block_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent heartbeat must keep ticking while scrapling fetches."""
    monkeypatch.setitem(sys.modules, "scrapling", _make_fake_scrapling())

    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)
            ticks += 1

    heartbeat_task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)  # let the heartbeat task start
    try:
        html = await _fetch_via_scrapling("https://www.amazon.it/dp/HEARTBEAT")
    finally:
        heartbeat_task.cancel()

    assert html == _FAKE_HTML
    assert ticks >= _MIN_TICKS, (
        f"event loop starved during scrapling fetch: {ticks} heartbeat ticks "
        f"in {_FETCH_SECONDS}s (expected >= {_MIN_TICKS})"
    )
