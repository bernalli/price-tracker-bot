"""List presentation uses bounded requests and keeps uncertain remote state separate."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest
from telegram import CallbackQuery, InaccessibleMessage
from telegram.error import Forbidden, InvalidToken

from price_tracker.bot import list_media as media
from price_tracker.bot.list_grid import GridRegistry, TileKey
from price_tracker.bot.telegram_outcome import Outcome, OutcomeKind, attempt
from price_tracker.bot.ui.screens import Button, Screen
from price_tracker.core.domain_spacing import DomainSpacing
from price_tracker.media.cache import ThumbnailCache
from price_tracker.media.decode import make_thumbnail
from price_tracker.media.thumbnails import ThumbnailService
from tests.support.fake_telegram import (
    FakeRequest,
    LoseResponse,
    Reject,
    TimeoutBeforeApply,
    callback_update,
    make_application,
    message_update,
    overlapping,
)
from tests.support.images import jpeg_bytes
from tests.support.list_variants import view

if TYPE_CHECKING:
    from telegram import Message

URL = "https://shop.com/product.jpg"
SCREEN = Screen("<b>Products</b>", ((Button("1", callback="l:a:1:1"),),))
KINDS = ["success", "reject", "rate", "lost", "timeout"]


def script(request: FakeRequest, method: str, kind: str) -> None:
    values: dict[str, Reject | LoseResponse | TimeoutBeforeApply] = {
        "reject": Reject(400, "Bad Request: refused"),
        "rate": Reject(429, "Too Many Requests", retry_after=30),
        "lost": LoseResponse(),
        "timeout": TimeoutBeforeApply(),
    }
    if kind != "success":
        request.script(method, values[kind])


@pytest.fixture
async def rig(tmp_path, monkeypatch):
    clock = [0.0]
    cache = ThumbnailCache(tmp_path / "thumbs")
    cache.ensure_dir()

    async def refs():
        return []

    def forbidden(*args, **kwargs):
        pytest.fail("presentation must never request a download")

    monkeypatch.setattr("price_tracker.core.http_client.public_request", forbidden)
    monkeypatch.setattr("price_tracker.media.fetch.download_image", forbidden)
    monkeypatch.setattr(ThumbnailService, "request", forbidden)
    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
        service = ThumbnailService(
            cache,
            client=client,
            job_pacing=DomainSpacing(clock=lambda: clock[0]),
            referenced_urls=refs,
        )
        mode = media.TwoMessageMode(service, GridRegistry(clock=lambda: clock[0]))
        request = FakeRequest()
        async with make_application(request) as app:
            yield SimpleNamespace(mode=mode, request=request, app=app, clock=clock, cache=cache)
        assert request.violations == []


def incoming(rig: Any) -> Message:
    message = message_update(rig.app.bot, 1, 1, "/list").message
    assert message is not None
    return message


def query(rig: Any, message_id: int) -> CallbackQuery:
    result = callback_update(rig.app.bot, 1, 1, "l:a:1", message_id=message_id).callback_query
    assert result is not None
    return result


async def pair(rig: Any) -> tuple[int, int]:
    await media.send_list(incoming(rig), rig.mode, SCREEN, (view(1),))
    photos = [key[1] for key, m in rig.request.remote.items() if m.kind == "photo"]
    texts = [key[1] for key, m in rig.request.remote.items() if m.kind == "text"]
    return photos[-1], texts[-1]


async def test_mode_requires_both_concrete_services(rig):
    for context in [
        object(),
        SimpleNamespace(bot_data=MagicMock()),
        SimpleNamespace(bot_data={}),
        SimpleNamespace(bot_data={"thumbnails": rig.mode.thumbnails}),
        SimpleNamespace(bot_data={"list_grids": rig.mode.grids}),
        SimpleNamespace(bot_data={"thumbnails": object(), "list_grids": rig.mode.grids}),
    ]:
        assert media.two_message_mode(context) is None
    assert (
        media.two_message_mode(
            SimpleNamespace(
                bot_data={"thumbnails": rig.mode.thumbnails, "list_grids": rig.mode.grids}
            )
        )
        == rig.mode
    )


async def test_grid_fingerprint_cache_and_unchanged_render(rig, monkeypatch):
    thumbnail = make_thumbnail(jpeg_bytes())
    rig.cache.write(URL, thumbnail)
    items = (replace(view(7), image_url=URL), view(8))
    image = await media.grid_image(items, rig.mode.thumbnails)
    assert image is not None
    assert image.jpeg is not None
    assert image.fingerprint == tuple(
        TileKey(n, v.id, media.tile_price(v), v.image_url, n == 1) for n, v in enumerate(items, 1)
    )
    assert image.jpeg.startswith(b"\xff\xd8")

    def fail(*args):
        pytest.fail("unchanged grid must not render")

    monkeypatch.setattr(media, "render_grid", fail)
    same = await media.grid_image(items, rig.mode.thumbnails, unchanged_from=image.fingerprint)
    assert same is not None
    assert same.fingerprint == image.fingerprint
    assert same.jpeg is None


@pytest.mark.parametrize("source", ["render", "cache", "size"])
async def test_grid_preparation_failures_are_logged(rig, monkeypatch, caplog, source):
    def fail(*args):
        raise RuntimeError("broken rendering")

    if source == "render":
        monkeypatch.setattr(media, "render_grid", fail)
    elif source == "cache":
        monkeypatch.setattr(rig.mode.thumbnails, "cached_bytes", fail)
    items = tuple(view(n) for n in range(1, 8)) if source == "size" else (view(1),)
    assert await media.grid_image(items, rig.mode.thumbnails) is None
    assert "product grid rendering failed" in caplog.text


async def test_grid_cancellation_propagates_and_tiles_receive_bytes(rig, monkeypatch):
    rig.cache.write(URL, make_thumbnail(jpeg_bytes()))

    def cancel(tiles):
        assert isinstance(tiles[0].thumb, bytes)
        assert tiles[1].thumb is None
        raise asyncio.CancelledError

    monkeypatch.setattr(media, "render_grid", cancel)
    with pytest.raises(asyncio.CancelledError):
        await media.grid_image((replace(view(1), image_url=URL), view(2)), rig.mode.thumbnails)


def test_prices_handle_missing_values_and_babel_rejection(monkeypatch):
    assert media.tile_price(replace(view(1), current=None)) == "—"

    def fail(*args, **kwargs):
        raise ValueError("unknown currency")

    monkeypatch.setattr(media, "money", fail)
    assert media.tile_price(replace(view(1), current=Decimal("12.345"))) == "12.34"


@pytest.mark.parametrize("photo_kind", KINDS)
@pytest.mark.parametrize("text_kind", KINDS)
async def test_creation_outcome_matrix(rig, photo_kind, text_kind):
    script(rig.request, "sendPhoto", photo_kind)
    script(rig.request, "sendMessage", text_kind)
    await media.send_list(incoming(rig), rig.mode, SCREEN, (view(1),))
    calls = [c for c in rig.request.calls if c.method != "getMe"]
    assert [c.method for c in calls] == ["sendPhoto", "sendMessage"]
    assert not calls[0].has_keyboard()
    assert not calls[0].params.get("caption")
    assert calls[1].has_keyboard()
    assert calls[1].link_preview() == {"is_disabled": True}
    assert calls[1].params["parse_mode"] == "HTML"
    remote = list(rig.request.remote.values())
    assert [m.kind for m in remote] == (["photo"] if photo_kind in ("success", "lost") else []) + (
        ["text"] if text_kind in ("success", "lost") else []
    )
    assert len(rig.mode.grids) == int(photo_kind == text_kind == "success")
    if photo_kind == text_kind == "success":
        assert (
            rig.mode.grids.get(list(rig.request.remote)[1]).grid_message_id
            == list(rig.request.remote)[0][1]
        )
    assert rig.mode.grids.cooling_down() is ("rate" in (photo_kind, text_kind))
    rig.clock[0] += 4000
    for _ in range(5):
        await asyncio.sleep(0)
    assert len([c for c in rig.request.calls if c.method != "getMe"]) == 2


@pytest.mark.parametrize("omitted", ["render", "pause", "empty"])
@pytest.mark.parametrize("text_kind", KINDS)
async def test_creation_without_a_photo_still_attempts_text(rig, monkeypatch, omitted, text_kind):
    if omitted == "render":

        async def missing(*args):
            return None

        monkeypatch.setattr(media, "grid_image", missing)
    elif omitted == "pause":
        rig.mode.grids.note_retry_after(10)
    script(rig.request, "sendMessage", text_kind)
    await media.send_list(incoming(rig), rig.mode, SCREEN, () if omitted == "empty" else (view(1),))
    assert [c.method for c in rig.request.calls if c.method != "getMe"] == ["sendMessage"]
    assert len(rig.mode.grids) == 0


async def test_photo_and_text_rate_limits_keep_the_longest_pause(rig):
    rig.request.script("sendPhoto", Reject(429, "Too Many Requests", retry_after=60))
    rig.request.script("sendMessage", Reject(429, "Too Many Requests", retry_after=10))
    await media.send_list(incoming(rig), rig.mode, SCREEN, (view(1),))
    rig.clock[0] = 59
    assert rig.mode.grids.cooling_down()
    rig.clock[0] = 60
    assert not rig.mode.grids.cooling_down()
    assert len(rig.request.remote) == 0


@pytest.mark.parametrize("missing", ["sendPhoto", "sendMessage"])
async def test_creation_never_guesses_missing_message_ids(rig, monkeypatch, missing):
    await pair(rig)
    original = attempt

    async def classified(awaitable):
        result = await original(awaitable)
        if result.message and ((bool(result.message.photo)) == (missing == "sendPhoto")):
            return Outcome(OutcomeKind.APPLIED)
        return result

    monkeypatch.setattr(media, "attempt", classified)
    await media.send_list(incoming(rig), rig.mode, SCREEN, (view(2),))
    assert len(rig.mode.grids) == 1


@pytest.mark.parametrize("kind", KINDS)
async def test_list_edits_text_before_photo_and_only_after_applied_text(rig, kind):
    photo_id, text_id = await pair(rig)
    start = len(rig.request.calls)
    script(rig.request, "editMessageText", kind)
    await media.edit_list(query(rig, text_id), rig.mode, SCREEN, (view(2),))
    calls = rig.request.calls[start:]
    assert [c.method for c in calls] == ["editMessageText"] + (
        ["editMessageMedia"] if kind == "success" else []
    )
    if kind == "success":
        assert calls[1].params["message_id"] == photo_id
        assert not calls[1].has_keyboard()
    assert rig.mode.grids.cooling_down() is (kind == "rate")


@pytest.mark.parametrize("kind", ["reject", "lost", "timeout", "rate"])
async def test_failed_photo_edits_update_registry_knowledge(rig, kind):
    photo_id, text_id = await pair(rig)
    script(rig.request, "editMessageMedia", kind)
    await media.edit_list(query(rig, text_id), rig.mode, SCREEN, (view(2),))
    entry = rig.mode.grids.get((1, text_id))
    if kind == "reject":
        assert entry is None
    else:
        assert entry.grid_message_id == photo_id
        assert entry.fingerprint is None
    assert rig.mode.grids.cooling_down() is (kind == "rate")


@pytest.mark.parametrize("condition", ["same", "unknown", "pause", "empty", "deleted"])
async def test_list_grid_skips_and_empty_canvas(rig, condition):
    photo_id, text_id = await pair(rig)
    if condition == "unknown":
        rig.mode.grids.forget((1, text_id))
    elif condition == "pause":
        rig.mode.grids.note_retry_after(20)
    elif condition == "deleted":
        rig.request.delete_remote(1, photo_id)
    start = len(rig.request.calls)
    items = () if condition == "empty" else (view(1 if condition == "same" else 2),)
    await media.edit_list(query(rig, text_id), rig.mode, SCREEN, items)
    assert [c.method for c in rig.request.calls[start:]] == ["editMessageText"] + (
        ["editMessageMedia"] if condition in ("empty", "deleted") else []
    )
    if condition == "empty":
        assert rig.mode.grids.get((1, text_id)).fingerprint == ()
    if condition == "deleted":
        assert rig.mode.grids.get((1, text_id)) is None


@pytest.mark.parametrize("url", [None, URL])
async def test_cards_only_edit_text_with_explicit_preview(rig, url):
    _, text_id = await pair(rig)
    start = len(rig.request.calls)
    await media.edit_card(query(rig, text_id), rig.mode, SCREEN, url)
    calls = rig.request.calls[start:]
    assert [c.method for c in calls] == ["editMessageText"]
    assert calls[0].link_preview() == (
        {"url": URL, "prefer_large_media": True, "show_above_text": True}
        if url
        else {"is_disabled": True}
    )


@pytest.mark.parametrize("second", KINDS)
async def test_bad_preview_has_only_one_plain_text_fallback(rig, second):
    _, text_id = await pair(rig)
    start = len(rig.request.calls)
    rig.request.script("editMessageText", Reject(400, "Bad Request: wrong web page url"))
    script(rig.request, "editMessageText", second)
    await media.edit_card(query(rig, text_id), rig.mode, SCREEN, URL)
    calls = rig.request.calls[start:]
    assert [c.method for c in calls] == ["editMessageText"] * 2
    assert calls[1].link_preview() == {"is_disabled": True}
    assert calls[1].params["text"] == calls[0].params["text"]
    assert calls[1].params["reply_markup"] == calls[0].params["reply_markup"]
    assert rig.mode.grids.preview_refused(URL) is (second == "success")
    assert rig.mode.grids.cooling_down() is (second == "rate")
    if second == "success":
        await media.edit_card(query(rig, text_id), rig.mode, SCREEN, URL)
        assert rig.request.calls[-1].link_preview() == {"is_disabled": True}


@pytest.mark.parametrize("reason", ["missing", "forbidden", "invalid", "lost", "timeout", "rate"])
async def test_other_card_failures_never_retry_or_refuse_preview(rig, monkeypatch, reason):
    _, text_id = await pair(rig)
    start = len(rig.request.calls)
    if reason in ("forbidden", "invalid"):
        original = rig.request.do_request

        async def fail(**kwargs):
            raise Forbidden("denied") if reason == "forbidden" else InvalidToken("denied")

        monkeypatch.setattr(rig.request, "do_request", fail)
        await media.edit_card(query(rig, text_id), rig.mode, SCREEN, URL)
        monkeypatch.setattr(rig.request, "do_request", original)
        assert len(rig.request.calls) == start
    else:
        if reason == "missing":
            rig.request.script(
                "editMessageText", Reject(400, "Bad Request: message to edit not found")
            )
        else:
            script(rig.request, "editMessageText", reason)
        await media.edit_card(query(rig, text_id), rig.mode, SCREEN, URL)
        assert [c.method for c in rig.request.calls[start:]] == ["editMessageText"]
    assert not rig.mode.grids.preview_refused(URL)


@pytest.mark.parametrize("operation", ["list", "card"])
@pytest.mark.parametrize("inaccessible", [False, True])
async def test_absent_and_inaccessible_callback_messages(rig, operation, inaccessible):
    sender = incoming(rig).from_user
    assert sender is not None
    q = CallbackQuery(
        "q",
        sender,
        "chat",
        message=InaccessibleMessage(incoming(rig).chat, 999) if inaccessible else None,
    )
    q.set_bot(rig.app.bot)
    start = len(rig.request.calls)
    if operation == "list":
        await media.edit_list(q, rig.mode, SCREEN, (view(1),))
    else:
        await media.edit_card(q, rig.mode, SCREEN, URL)
    assert [c.method for c in rig.request.calls[start:]] == (
        ["editMessageText"] if inaccessible else []
    )
    assert len(rig.mode.grids) == 0


async def test_concurrent_edits_hold_one_presentation_lock(rig):
    _, text_id = await pair(rig)
    start = len(rig.request.calls)
    held = rig.request.hold_next("editMessageText")
    first = asyncio.create_task(media.edit_list(query(rig, text_id), rig.mode, SCREEN, (view(2),)))
    for _ in range(20):
        await asyncio.sleep(0)
    second = asyncio.create_task(media.edit_list(query(rig, text_id), rig.mode, SCREEN, (view(3),)))
    for _ in range(20):
        await asyncio.sleep(0)
    assert [c.method for c in rig.request.calls[start:]] == ["editMessageText"]
    held.set()
    await asyncio.gather(first, second)
    calls = rig.request.calls[start:]
    assert [c.method for c in calls] == ["editMessageText", "editMessageMedia"] * 2
    assert not overlapping(calls)
    assert rig.mode.grids.get((1, text_id)).fingerprint[0].product_id == 3


async def test_invalid_send_arguments_fail_before_requests(rig):
    for screen, items in [(Screen("x"), (view(1),)), (SCREEN, tuple(view(n) for n in range(1, 8)))]:
        with pytest.raises(ValueError, match="list requires"):
            await media.send_list(incoming(rig), rig.mode, screen, items)
    assert not rig.request.attempts("sendMessage")
    assert not rig.request.attempts("sendPhoto")
