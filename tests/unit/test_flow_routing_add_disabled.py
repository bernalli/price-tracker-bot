"""Routing of the coordinator when the add flow stays with the legacy handlers.

With ``FlowConfig(add_entry=False)`` a pasted link or ``/add <url>`` is never an
add entry: without an open prompt the coordinator leaves the update alone, with
one it closes the prompt and passes the update on, like any other command. Legacy
entry buttons (``setsoglia_<id>`` and friends) open the matching prompt through
the same entry route as registry buttons.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from price_tracker.bot.callbacks import Action
from price_tracker.bot.flows import FlowConfig, Route, RouteKind
from tests.support.fake_telegram import (
    FakeServices,
    callback_update,
    message_update,
)
from tests.support.flow_harness import Harness

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

USER = 10
PRIVATE = 100
URL = "https://shop.example/item/1"
ADD_TEXTS = [
    URL,
    f"look at this {URL} please",
    f"/add {URL}",
    f"/aggiungi {URL}",
    f"/add@test_bot {URL}",
]


def _services() -> FakeServices:
    return FakeServices(active={USER}, products={1: (USER, "Kettle")})


async def _harness(*, add_entry: bool) -> Harness:
    harness = Harness(_services(), config=FlowConfig(add_entry=add_entry))
    await harness.start()
    return harness


@pytest.fixture
async def h() -> AsyncIterator[Harness]:
    harness = await _harness(add_entry=False)
    yield harness
    await harness.stop()


@pytest.fixture
async def h_add() -> AsyncIterator[Harness]:
    harness = await _harness(add_entry=True)
    yield harness
    await harness.stop()


async def _open_prompt(h: Harness) -> None:
    await h.press(PRIVATE, USER, "p:1:th")
    assert h.flow.registry.keys() == {(PRIVATE, USER)}


def _route(h: Harness, text: str) -> Route | None:
    return h.flow.check_update(message_update(h.app.bot, PRIVATE, USER, text))


@pytest.mark.parametrize("text", ADD_TEXTS)
async def test_link_or_add_command_without_prompt_is_not_routed(h: Harness, text: str) -> None:
    assert _route(h, text) is None


@pytest.mark.parametrize("text", ADD_TEXTS)
async def test_link_or_add_command_with_prompt_closes_it_as_another_command(
    h: Harness, text: str
) -> None:
    await _open_prompt(h)
    flow = h.flow.registry.get((PRIVATE, USER))
    assert flow is not None

    route = _route(h, text)

    assert route is not None
    assert route.kind is RouteKind.OTHER_COMMAND
    assert route.snapshot == flow.snapshot((PRIVATE, USER))


async def test_add_command_for_another_bot_is_not_routed_even_with_a_prompt(h: Harness) -> None:
    await _open_prompt(h)

    assert _route(h, f"/add@OtherBot {URL}") is None


@pytest.mark.parametrize("text", ADD_TEXTS)
async def test_add_entry_stays_available_when_enabled(h_add: Harness, text: str) -> None:
    route = _route(h_add, text)

    assert route is not None
    assert route.kind is RouteKind.ADD_ENTRY
    assert route.url == URL


LEGACY_ENTRIES = [
    ("setsoglia_1", Action("product.threshold", (1,))),
    ("track_threshold_1", Action("product.threshold", (1,))),
    ("settarget_1", Action("product.target", (1,))),
    ("track_target_1", Action("product.target", (1,))),
    ("setrefresh_1", Action("product.interval", (1,))),
]


@pytest.mark.parametrize(("data", "action"), LEGACY_ENTRIES)
@pytest.mark.parametrize("with_prompt", [False, True])
async def test_legacy_entry_button_routes_as_entry_callback(
    h: Harness, data: str, action: Action, with_prompt: bool
) -> None:
    if with_prompt:
        await _open_prompt(h)

    route = h.flow.check_update(callback_update(h.app.bot, PRIVATE, USER, data))

    assert route is not None
    assert route.kind is RouteKind.ENTRY_CALLBACK
    assert route.action == action


MALFORMED = ["setsoglia_abc", "setsoglia_0", "setsoglia_01", "settarget_-1", "track_any_1"]


@pytest.mark.parametrize("data", MALFORMED)
async def test_malformed_legacy_data_is_foreign_with_prompt_and_ignored_without(
    h: Harness, data: str
) -> None:
    assert h.flow.check_update(callback_update(h.app.bot, PRIVATE, USER, data)) is None

    await _open_prompt(h)
    route = h.flow.check_update(callback_update(h.app.bot, PRIVATE, USER, data))

    assert route is not None
    assert route.kind is RouteKind.FOREIGN_CALLBACK


@pytest.mark.parametrize("language_code", ["it", "en", "pt-br", None])
async def test_route_carries_the_sender_language(h: Harness, language_code: str | None) -> None:
    await _open_prompt(h)
    updates = [
        callback_update(h.app.bot, PRIVATE, USER, "p:1:tg", language_code=language_code),
        message_update(h.app.bot, PRIVATE, USER, "20%", language_code=language_code),
        message_update(h.app.bot, PRIVATE, USER, "/cancel", language_code=language_code),
    ]

    routes = [h.flow.check_update(update) for update in updates]

    assert [route.language_code if route is not None else "missing" for route in routes] == [
        language_code
    ] * 3
