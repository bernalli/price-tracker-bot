"""Concurrent Telegram updates, with deterministic service barriers."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pytest

from tests.integration.test_guided_flow import (
    PRIVATE,
    URL_EUR,
    URL_NOCUR,
    USER,
    _services,
    _token_of,
)
from tests.support.flow_harness import Harness, ServiceBarrier


@asynccontextmanager
async def concurrent_harness():
    h = Harness(_services(), concurrent_updates=2)
    await h.start()
    assert h.app.concurrent_updates == 2
    try:
        yield h
        assert h.request.violations == []
    finally:
        await h.stop()


async def start_add(h: Harness, phase: str) -> asyncio.Task[None]:
    if phase == "prepare_add":
        return asyncio.create_task(h.text(PRIVATE, USER, f"/add {URL_NOCUR}"))
    await h.text(PRIVATE, USER, f"/add {URL_NOCUR}")
    token = _token_of(h.last_prompt(PRIVATE).callback_data())
    return asyncio.create_task(h.press(PRIVATE, USER, f"p:{token}:cur:USD"))


@pytest.mark.parametrize("phase", ["prepare_add", "add_product"])
async def test_older_add_must_not_replace_newer_prompt(phase: str) -> None:
    async with concurrent_harness() as h:
        barrier = ServiceBarrier(h.services, phase)
        task = await start_add(h, phase)
        try:
            await barrier.wait()
            await h.press(PRIVATE, USER, "p:1:tg")
            newer = h.flow.registry.get((PRIVATE, USER))
            barrier.release.set()
            await task
            assert h.flow.registry.get((PRIVATE, USER)) == newer
        finally:
            barrier.release.set()
            await task


async def test_stale_insert_reports_once_without_keyboard() -> None:
    """An already committed insert stays store-only and cannot touch the target."""
    async with concurrent_harness() as h:
        barrier = ServiceBarrier(h.services, "add_product", after=True)
        task = await start_add(h, "add_product")
        try:
            await barrier.wait()
            assert len(h.services.writes) == 1
            await h.press(PRIVATE, USER, "p:1:tg")
            newer = h.flow.registry.get((PRIVATE, USER))
            generation = h.flow.registry.generation((PRIVATE, USER))
            prompt = h.last_prompt(PRIVATE)
            before = len(h.request.calls)
            barrier.release.set()
            await task
            assert len(h.services.writes) == 1
            assert h.flow.registry.get((PRIVATE, USER)) == newer
            assert h.flow.registry.generation((PRIVATE, USER)) == generation
            assert h.last_prompt(PRIVATE).message_id == prompt.message_id
            calls = h.request.calls[before:]
            assert [c.method for c in calls] == ["sendMessage"]
            assert "reply_markup" not in calls[0].params
            assert calls[0].params["text"] == "Added. Kept: only on shop.example."
        finally:
            barrier.release.set()
            await task


@pytest.mark.parametrize("interruption", ["cancel", "add", "link"])
async def test_stale_scrape_is_silent_and_never_inserts(interruption: str) -> None:
    async with concurrent_harness() as h:
        barrier = ServiceBarrier(h.services, "prepare_add")
        task = asyncio.create_task(h.text(PRIVATE, USER, f"/add {URL_EUR}"))
        try:
            await barrier.wait()
            text = {"cancel": "/cancel", "add": f"/add {URL_NOCUR}", "link": URL_NOCUR}
            await h.text(PRIVATE, USER, text[interruption])
            before = len(h.request.calls)
            newer = h.flow.registry.get((PRIVATE, USER))
            generation = h.flow.registry.generation((PRIVATE, USER))
            barrier.release.set()
            await task
            assert h.services.writes == []
            assert not any(c[0] == "add_product" for c in h.services.calls)
            assert h.request.calls[before:] == []
            assert h.flow.registry.get((PRIVATE, USER)) == newer
            assert h.flow.registry.generation((PRIVATE, USER)) == generation
        finally:
            barrier.release.set()
            await task


@pytest.mark.parametrize("phase", ["prepare_add", "add_product"])
async def test_current_continuation_opens_prompt(phase: str) -> None:
    async with concurrent_harness() as h:
        barrier = ServiceBarrier(h.services, phase)
        task = await start_add(h, phase)
        try:
            await barrier.wait()
            before = len(h.sent(PRIVATE))
            barrier.release.set()
            await task
            assert len(h.sent(PRIVATE)) == before + 1
            assert h.sent(PRIVATE)[-1].callback_data()
            assert h.flow.registry.get((PRIVATE, USER)) is not None
            assert len(h.services.writes) == (phase == "add_product")
        finally:
            barrier.release.set()
            await task


async def test_claim_invalidates_pending_entry_without_another_open() -> None:
    """A target answer claims while an interval entry waits for its product name."""
    async with concurrent_harness() as h:
        await h.press(PRIVATE, USER, "p:1:tg")
        barrier = ServiceBarrier(h.services, "product_name")
        task = asyncio.create_task(h.press(PRIVATE, USER, "p:1:iv"))
        try:
            await barrier.wait()
            await h.text(PRIVATE, USER, "30")
            assert len(h.services.writes) == 1
            assert h.flow.registry.get((PRIVATE, USER)) is None
            before = len(h.request.calls)
            barrier.release.set()
            await task
            assert h.request.calls[before:] == []
            assert h.flow.registry.get((PRIVATE, USER)) is None
            assert len(h.services.writes) == 1
        finally:
            barrier.release.set()
            await task


async def test_stale_scope_default_never_starts_another_write() -> None:
    async with concurrent_harness() as h:
        h.services.scope_defaults[USER] = "other_stores"
        barrier = ServiceBarrier(h.services, "add_scope_default")
        task = asyncio.create_task(h.text(PRIVATE, USER, f"/add {URL_EUR}"))
        try:
            await barrier.wait()
            await h.press(PRIVATE, USER, "p:1:tg")
            before = len(h.request.calls)
            barrier.release.set()
            await task
            assert [w[0] for w in h.services.writes] == ["insert"]
            assert not any(c[0] == "set_product_scope" for c in h.services.calls)
            calls = h.request.calls[before:]
            assert [c.method for c in calls] == ["sendMessage"]
            assert "reply_markup" not in calls[0].params
            assert calls[0].params["text"] == "Added. Kept: only on shop.example."
        finally:
            barrier.release.set()
            await task


@pytest.mark.parametrize("phase", ["apply_value", "set_product_scope"])
async def test_claimed_write_finishes_with_plain_outcome(phase: str) -> None:
    async with concurrent_harness() as h:
        if phase == "apply_value":
            await h.press(PRIVATE, USER, "p:1:tg")
            answer = h.text(PRIVATE, USER, "30")
            expected = "Saved."
        else:
            await h.text(PRIVATE, USER, f"/add {URL_EUR}")
            token = _token_of(h.last_prompt(PRIVATE).callback_data())
            answer = h.press(PRIVATE, USER, f"p:{token}:sc:world")
            expected = "Other stores will be followed at this level from a later version."
        barrier = ServiceBarrier(h.services, phase, after=True)
        task = asyncio.create_task(answer)
        try:
            await barrier.wait()
            writes = list(h.services.writes)
            await h.press(PRIVATE, USER, "p:1:tg")
            newer = h.flow.registry.get((PRIVATE, USER))
            before = len(h.request.calls)
            barrier.release.set()
            await task
            assert h.services.writes == writes
            assert h.flow.registry.get((PRIVATE, USER)) == newer
            calls = h.request.calls[before:]
            assert [c.method for c in calls] == ["sendMessage"]
            assert "reply_markup" not in calls[0].params
            assert calls[0].params["text"] == expected
        finally:
            barrier.release.set()
            await task


@pytest.mark.parametrize(
    ("url", "choice"),
    [(URL_NOCUR, "cur:type"), (URL_NOCUR, "cur:USD"), (URL_EUR, "sc"), (URL_EUR, "sc:world")],
)
async def test_stale_callback_after_ack_does_not_edit_or_write(url: str, choice: str) -> None:
    async with concurrent_harness() as h:
        await h.text(PRIVATE, USER, f"/add {url}")
        token = _token_of(h.last_prompt(PRIVATE).callback_data())
        barrier = ServiceBarrier(h.request, "do_request", after=True)
        task = asyncio.create_task(h.press(PRIVATE, USER, f"p:{token}:{choice}"))
        try:
            await barrier.wait()
            await h.press(PRIVATE, USER, "p:1:tg")
            before = len(h.request.calls)
            writes = list(h.services.writes)
            newer = h.flow.registry.get((PRIVATE, USER))
            barrier.release.set()
            await task
            assert h.request.calls[before:] == []
            assert h.services.writes == writes
            assert h.flow.registry.get((PRIVATE, USER)) == newer
        finally:
            barrier.release.set()
            await task
