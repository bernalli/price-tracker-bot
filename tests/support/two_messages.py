"""Offline wiring and presentation invariants for paired list messages."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx

from price_tracker.bot.list_grid import GridRegistry
from price_tracker.bot.list_media import TwoMessageMode
from price_tracker.core.domain_spacing import DomainSpacing
from price_tracker.media.cache import ThumbnailCache
from price_tracker.media.thumbnails import ThumbnailService
from tests.support.fake_telegram import callback_update

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from tests.integration.test_guided_flow_wired import Wired
    from tests.support.fake_telegram import Call, FakeRequest


def wire_two_messages(w: Wired, tmp_path: Path, *, clock: Callable[[], float]) -> TwoMessageMode:
    def forbidden(request: httpx.Request) -> httpx.Response:
        raise AssertionError("list navigation must not use the network")

    async def referenced() -> list[str]:
        return list(await w.repo.list_image_urls())

    client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
    w.app.bot_data["http_client"] = client
    cache = ThumbnailCache(tmp_path / "thumbs")
    cache.ensure_dir()
    spacing = DomainSpacing(clock=clock)
    service = ThumbnailService(
        cache, client=client, job_pacing=spacing, referenced_urls=referenced, clock=clock
    )
    grids = GridRegistry(clock=clock)
    w.app.bot_data.update(thumbnails=service, list_grids=grids)
    return TwoMessageMode(service, grids)


async def press_on(
    w: Wired, chat_id: int, user_id: int, data: str, *, message_id: int
) -> list[Call]:
    start = len(w.request.calls)
    await w.process(callback_update(w.app.bot, chat_id, user_id, data, message_id=message_id))
    return w.request.calls[start:]


def assert_two_message_invariants(
    request: FakeRequest,
    callback_calls: Sequence[Sequence[Call]],
    *,
    command_calls: Sequence[Sequence[Call]] = (),
    grids: GridRegistry | None = None,
) -> None:
    photo_ids = {
        (call.chat_id, call.message_id)
        for call in request.calls
        if call.method == "sendPhoto" and call.message_id is not None
    }
    for call in request.calls:
        assert call.method not in {"deleteMessage", "editMessageReplyMarkup", "editMessageCaption"}
        if call.method in {"sendPhoto", "editMessageMedia"}:
            assert not call.has_keyboard()
            assert not call.params.get("caption")
            if call.method == "editMessageMedia":
                assert (call.chat_id, call.params["message_id"]) in photo_ids
                media = call.params["media"]
                if isinstance(media, str):
                    media = json.loads(media)
                assert not media.get("caption")
        if call.method == "editMessageText":
            assert (call.chat_id, call.params["message_id"]) not in photo_ids
    for calls in callback_calls:
        assert all(call.method not in {"sendPhoto", "sendMessage"} for call in calls)
    confirmed: dict[tuple[int | None, int | None], int | None] = {}
    for calls in command_calls:
        sends = [call for call in calls if call.method in {"sendPhoto", "sendMessage"}]
        assert [call.method for call in sends] in (["sendMessage"], ["sendPhoto", "sendMessage"])
        if len(sends) == 2 and not sends[0].failed and not sends[1].failed:
            confirmed[(sends[1].chat_id, sends[1].message_id)] = sends[0].message_id
    if grids is not None:
        for key, entry in grids._entries.items():
            assert confirmed.get(key) == entry.grid_message_id
    assert request.violations == []
