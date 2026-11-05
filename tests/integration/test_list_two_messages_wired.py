"""Paired lists remain navigable through real handlers and uncertain delivery."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from telegram import CallbackQuery, InaccessibleMessage, Update

from price_tracker.bot.list_grid import GridRegistry
from price_tracker.media.cache import ThumbnailCache
from price_tracker.media.decode import make_thumbnail
from tests.integration.test_guided_flow_wired import OWNER, PRIVATE, _wired
from tests.support.fake_telegram import LoseResponse, Reject, TimeoutBeforeApply, overlapping
from tests.support.images import jpeg_bytes
from tests.support.two_messages import assert_two_message_invariants, press_on, wire_two_messages

URL = "https://shop.example/image.jpg"


@pytest.fixture
async def paired(tmp_path, monkeypatch):
    from tests.support.fake_telegram import make_application

    def concurrent(request: Any, **kwargs: Any) -> Any:
        return make_application(request, concurrent_updates=2, **kwargs)

    monkeypatch.setattr("tests.integration.test_guided_flow_wired.make_application", concurrent)
    async with _wired() as w:
        clock = [0.0]
        for n in range(2, 9):
            await w.repo.add_product(
                user_id=OWNER,
                url=f"https://shop.example/item/{n + 10}",
                name=f"Item {n}",
                domain="shop.example",
                initial_price=Decimal(10),
                currency="EUR",
            )
        await w.repo.update_price_and_image(w.product, Decimal(100), URL)
        mode = wire_two_messages(w, tmp_path, clock=lambda: clock[0])
        cache = ThumbnailCache(tmp_path / "thumbs")
        cache.write(URL, make_thumbnail(jpeg_bytes()))
        rig = SimpleNamespace(w=w, mode=mode, clock=clock, callbacks=[], cache=cache)
        yield rig
        assert_two_message_invariants(w.request, rig.callbacks)
        assert w.errors == []
        assert not any("An error occurred" in text for text in w.texts_to(PRIVATE))
        await w.app.bot_data["http_client"].aclose()


async def command(rig: Any) -> list[Any]:
    start = len(rig.w.request.calls)
    await rig.w.text(PRIVATE, OWNER, "/list")
    calls = list(rig.w.request.calls[start:])
    assert [c.method for c in calls] in (["sendPhoto", "sendMessage"], ["sendMessage"])
    return calls


async def press(rig: Any, data: str, message_id: int) -> list[Any]:
    calls = await press_on(rig.w, PRIVATE, OWNER, data, message_id=message_id)
    rig.callbacks.append(calls)
    return [c for c in calls if c.method != "answerCallbackQuery"]


async def initial(rig: Any) -> tuple[int, int]:
    calls = await command(rig)
    assert [c.method for c in calls] == ["sendPhoto", "sendMessage"]
    photo, text = calls
    assert rig.w.request.remote_of(PRIVATE, photo.message_id).photo_size == (960, 800)
    assert text.has_keyboard()
    entry = rig.mode.grids.get((PRIVATE, text.message_id))
    assert entry.grid_message_id == photo.message_id
    return photo.message_id, text.message_id


async def test_list_pagination_filters_cards_and_return(paired):
    r = paired
    photo_id, text_id = await initial(r)
    calls = await press(r, "l:a:2", text_id)
    assert [c.method for c in calls] == ["editMessageText", "editMessageMedia"]
    assert calls[0].params["message_id"] == text_id
    assert calls[1].params["message_id"] == photo_id
    r.w.request.simulate_not_modified = True
    assert [c.method for c in await press(r, "l:a:2", text_id)] == ["editMessageText"]
    calls = await press(r, "l:p:1", text_id)
    assert [c.method for c in calls] == ["editMessageText", "editMessageMedia"]
    assert r.mode.grids.get((PRIVATE, text_id)).fingerprint == ()
    await press(r, "l:a:1", text_id)
    calls = await press(r, f"l:a:1:{r.w.product}", text_id)
    assert [c.method for c in calls] == ["editMessageText"]
    assert calls[0].link_preview() == {
        "url": URL,
        "prefer_large_media": True,
        "show_above_text": True,
    }
    assert [c.method for c in await press(r, "l:a:1", text_id)] == ["editMessageText"]
    row = await r.w.repo.get_all_products(OWNER)
    no_image = next(p for p in row if p.id != r.w.product)
    calls = await press(r, f"l:a:1:{no_image.id}", text_id)
    assert calls[0].link_preview() == {"is_disabled": True}


async def test_new_thumbnail_and_deleted_product_redraw_the_grid(paired):
    r = paired
    r.cache.prune(set())
    _, text_id = await initial(r)
    await press(r, f"l:a:1:{r.w.product}", text_id)
    r.cache.write(URL, make_thumbnail(jpeg_bytes()))
    assert [c.method for c in await press(r, "l:a:1", text_id)] == [
        "editMessageText",
        "editMessageMedia",
    ]
    await r.w.repo.delete_product(r.w.product, user_id=OWNER)
    calls = await press(r, f"l:a:1:{r.w.product}", text_id)
    assert [c.method for c in calls] == ["editMessageText", "editMessageMedia"]
    assert "Product not found." in calls[0].params["text"]


@pytest.mark.parametrize("failure", ["deleted", "lost", "timeout", "rate"])
async def test_photo_failure_recovery_is_explicit(paired, failure):
    r = paired
    photo_id, text_id = await initial(r)
    if failure == "deleted":
        r.w.request.delete_remote(PRIVATE, photo_id)
    else:
        r.w.request.script(
            "editMessageMedia",
            {
                "lost": LoseResponse(),
                "timeout": TimeoutBeforeApply(),
                "rate": Reject(429, "Too Many Requests", retry_after=30),
            }[failure],
        )
    await press(r, "l:a:2", text_id)
    entry = r.mode.grids.get((PRIVATE, text_id))
    if failure == "deleted":
        assert entry is None
    else:
        assert entry.fingerprint is None
    calls = await press(r, "l:a:2", text_id)
    assert [c.method for c in calls] == ["editMessageText"] + (
        ["editMessageMedia"] if failure in ("lost", "timeout") else []
    )
    if failure == "rate":
        r.clock[0] = 30
        assert [c.method for c in await press(r, "l:a:2", text_id)] == [
            "editMessageText",
            "editMessageMedia",
        ]


async def test_restart_and_home_messages_have_no_known_grid(paired):
    r = paired
    _, text_id = await initial(r)
    r.w.app.bot_data["list_grids"] = GridRegistry()
    assert [c.method for c in await press(r, "l:a:2", text_id)] == ["editMessageText"]
    home = await r.w.app.bot.send_message(PRIVATE, "Home")
    assert [c.method for c in await press(r, "l:a:1", home.message_id)] == ["editMessageText"]


@pytest.mark.parametrize("data", ["l:a", "l:a:1:1:extra", "l:x:1", "l:a:1:9223372036854775808"])
async def test_malformed_buttons_do_not_mutate_products_or_photos(paired, data):
    r = paired
    _, text_id = await initial(r)
    before = await r.w.repo.get_all_products(OWNER)
    assert await press(r, data, text_id) == []
    assert await r.w.repo.get_all_products(OWNER) == before


@pytest.mark.parametrize("inaccessible", [False, True])
async def test_missing_and_inaccessible_callback_messages_are_safe(paired, inaccessible):
    r = paired
    _, text_id = await initial(r)
    q = CallbackQuery(
        "missing",
        (await r.w.app.bot.get_me()),
        "chat",
        data="l:a:1",
        message=InaccessibleMessage((await r.w.app.bot.send_message(PRIVATE, "Home")).chat, text_id)
        if inaccessible
        else None,
    )
    # Use an authorised sender; the callback still goes through every handler group.
    from tests.support.fake_telegram import callback_update

    callback = callback_update(r.w.app.bot, PRIVATE, OWNER, "l:a:1").callback_query
    assert callback is not None
    sender = callback.from_user
    q = CallbackQuery("missing", sender, "chat", data="l:a:1", message=q.message)
    q.set_bot(r.w.app.bot)
    start = len(r.w.request.calls)
    await r.w.process(Update(12345, callback_query=q))
    calls = r.w.request.calls[start:]
    r.callbacks.append(calls)
    assert [c.method for c in calls if c.method != "answerCallbackQuery"] == (
        ["editMessageText"] if inaccessible else []
    )


async def test_all_paused_products_send_only_text(paired):
    for product in await paired.w.repo.get_all_products(OWNER):
        await paired.w.repo.pause_product(product.id)
    assert [c.method for c in await command(paired)] == ["sendMessage"]
    assert len(paired.mode.grids) == 0


@pytest.mark.parametrize(
    "photo", [Reject(400, "Bad Request: refused"), Reject(429, "Too Many Requests", retry_after=30)]
)
async def test_failed_photo_creation_keeps_all_text_navigation(paired, photo):
    r = paired
    r.w.request.script("sendPhoto", photo)
    calls = await command(r)
    text_id = calls[1].message_id
    assert len(r.mode.grids) == 0
    for data in ("l:a:2", "l:a:1", f"l:a:1:{r.w.product}"):
        assert [c.method for c in await press(r, data, text_id)] == ["editMessageText"]


@pytest.mark.parametrize(
    "failure",
    [
        LoseResponse(),
        Reject(400, "Bad Request: refused"),
        TimeoutBeforeApply(),
        Reject(429, "Too Many Requests", retry_after=30),
    ],
)
async def test_orphan_photo_never_gets_an_invented_association(paired, failure):
    r = paired
    r.w.request.script("sendMessage", failure)
    calls = await command(r)
    old_photo = calls[0].message_id
    assert len(r.mode.grids) == 0
    if isinstance(failure, LoseResponse):
        text_id = calls[1].message_id
        assert [c.method for c in await press(r, "l:a:2", text_id)] == ["editMessageText"]
    else:
        assert len(r.w.request.remote) == 1
    before = len(r.w.request.calls)
    r.clock[0] += 4000
    for _ in range(5):
        await asyncio.sleep(0)
    assert len(r.w.request.calls) == before
    next_calls = await command(r)
    new_text = next_calls[-1].message_id
    entry = r.mode.grids.get((PRIVATE, new_text))
    assert entry.grid_message_id != old_photo
    assert [c.method for c in await press(r, "l:a:2", new_text)] == [
        "editMessageText",
        "editMessageMedia",
    ]


@pytest.mark.parametrize("photo", [LoseResponse(), TimeoutBeforeApply()])
@pytest.mark.parametrize("text", [LoseResponse(), TimeoutBeforeApply()])
async def test_both_uncertain_sends_have_independent_remote_states(paired, photo, text):
    r = paired
    r.w.request.script("sendPhoto", photo)
    r.w.request.script("sendMessage", text)
    await command(r)
    assert [m.kind for m in r.w.request.remote.values()] == (
        ["photo"] if isinstance(photo, LoseResponse) else []
    ) + (["text"] if isinstance(text, LoseResponse) else [])
    assert len(r.mode.grids) == 0
    calls = await command(r)
    assert r.mode.grids.get((PRIVATE, calls[-1].message_id)).grid_message_id == calls[0].message_id


async def test_concurrent_callbacks_do_not_overlap(paired):
    r = paired
    _, text_id = await initial(r)
    held = r.w.request.hold_next("editMessageText")
    first = asyncio.create_task(press(r, "l:a:2", text_id))
    for _ in range(20):
        await asyncio.sleep(0)
    second = asyncio.create_task(press(r, "l:a:1", text_id))
    held.set()
    await asyncio.gather(first, second)
    edits = [c for c in r.w.request.calls if c.method in ("editMessageText", "editMessageMedia")]
    assert [c.method for c in edits] == ["editMessageText", "editMessageMedia"] * 2
    assert not overlapping(edits)


async def test_an_emptied_list_has_an_empty_grid_and_add_navigation(paired):
    r = paired
    _, text_id = await initial(r)
    for product in await r.w.repo.get_all_products(OWNER):
        await r.w.repo.delete_product(product.id, user_id=OWNER)
    calls = await press(r, "l:a:1", text_id)
    assert [c.method for c in calls] == ["editMessageText", "editMessageMedia"]
    assert r.mode.grids.get((PRIVATE, text_id)).fingerprint == ()
    assert calls[0].callback_data() == ["ad", "h"]


async def test_disabling_services_preserves_the_legacy_navigation(paired):
    r = paired
    del r.w.app.bot_data["thumbnails"]
    del r.w.app.bot_data["list_grids"]
    calls = await command(r)
    assert [c.method for c in calls] == ["sendMessage"]
    text_id = calls[0].message_id
    for data in ("l:a:2", f"l:a:1:{r.w.product}", "l:a:1"):
        calls = await press(r, data, text_id)
        assert [c.method for c in calls] == ["editMessageText"]
        assert not calls[0].link_preview() or calls[0].link_preview() == {"is_disabled": True}
