"""Guided-flow coordinator on a real python-telegram-bot ``Application``.

Every case feeds real ``Update`` objects through ``Application.process_update``;
the bot talks to a fake HTTP layer, the services are fakes that log every write,
and timeouts fire only when a test fires them. Each test name states the event of
the events x states table it proves.
"""

from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import pytest
from telegram.ext import BaseHandler, CallbackQueryHandler, CommandHandler, MessageHandler

from price_tracker.app.inputs import Percentage, SetTarget
from price_tracker.bot.flows import (
    TEXT_CANCELLED,
    TEXT_EXPIRED,
    TEXT_EXPIRED_ADD,
    TEXT_EXPIRED_VALUE,
    TEXT_NO_OPEN_PROMPT,
    TEXT_NO_PRODUCT,
    TEXT_NOTHING_TO_CANCEL,
    TEXT_SUPERSEDED,
    TEXT_TOO_MANY,
    FlowConfig,
    FlowKind,
    FlowSnapshot,
    GuidedFlow,
    JobQueueTimer,
    RouteKind,
)
from price_tracker.bot.handlers import text_input
from tests.support.fake_telegram import (
    FakeRequest,
    FakeServices,
    ManualTimer,
    callback_update,
    make_application,
    message_update,
    ready,
)
from tests.support.flow_harness import HELP_TEXT, Harness
from tests.support.input_corpus import FLOW_REJECTED

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from telegram import Update
    from telegram.ext import CallbackContext

USER = 10
OTHER_USER = 11
PRIVATE = 100
GROUP = -500
CHANNEL = -1001
URL_EUR = "https://shop.example/item/1"
URL_NOCUR = "https://shop.example/item/2"
_TOKEN_RE = re.compile(r"^p:([0-9a-f]{32}):")


def _token_of(data: list[str]) -> str:
    """Independent oracle: the flow token is the 32-hex second token of a flow button."""
    tokens = {m.group(1) for d in data if (m := _TOKEN_RE.match(d))}
    assert len(tokens) == 1, data
    return tokens.pop()


def _services() -> FakeServices:
    return FakeServices(
        active={USER, OTHER_USER},
        products={1: (USER, "Kettle"), 2: (USER, "Lamp"), 3: (OTHER_USER, "Fan")},
        prepare_by_url={
            URL_EUR: ready(URL_EUR, currency="EUR"),
            URL_NOCUR: ready(URL_NOCUR, currency=None),
        },
    )


@pytest.fixture
async def h() -> AsyncIterator[Harness]:
    harness = Harness(_services())
    await harness.start()
    yield harness
    assert harness.request.violations == []
    await harness.stop()


async def _open_value(h: Harness, chat: int, user: int, product: int, verb: str) -> tuple[str, int]:
    """Tap an entry button; return the new prompt's token and message id."""
    await h.press(chat, user, f"p:{product}:{verb}")
    prompt = h.last_prompt(chat)
    return _token_of(prompt.callback_data()), h.prompt_message_id(prompt)


# --- G1 / G2: supersede, single consumption --------------------------------


async def test_second_prompt_supersedes_first_and_one_answer_applies_once(h: Harness) -> None:
    """11.7 item 1: threshold then target, one answer."""
    _, first_message = await _open_value(h, PRIVATE, USER, 1, "th")
    second_token, _ = await _open_value(h, PRIVATE, USER, 1, "tg")

    await h.text(PRIVATE, USER, "42")

    assert h.services.writes == [("value", USER, FlowKind.TARGET, 1, SetTarget(Decimal("42")))]
    assert h.edits_of(first_message) == [TEXT_SUPERSEDED]
    assert len(h.flow.registry) == 0
    await h.text(PRIVATE, USER, "42")
    assert len(h.services.writes) == 1
    assert h.texts_to(PRIVATE)[-1] == TEXT_NO_OPEN_PROMPT
    assert second_token


async def test_replay_of_superseded_prompt_button_is_expired(h: Harness) -> None:
    """Replay after supersede: the old cancel button must not close the new flow."""
    old_token, _ = await _open_value(h, PRIVATE, USER, 1, "th")
    new_token, _ = await _open_value(h, PRIVATE, USER, 1, "tg")

    await h.press(PRIVATE, USER, f"p:{old_token}:x")

    assert h.toasts()[-1] == TEXT_EXPIRED
    open_flow = h.flow.registry.get((PRIVATE, USER))
    assert open_flow is not None
    assert open_flow.token == new_token
    await h.text(PRIVATE, USER, "5")
    assert h.services.writes == [("value", USER, FlowKind.TARGET, 1, SetTarget(Decimal("5")))]


async def test_stale_keyboard_of_same_product_with_old_token_is_expired(h: Harness) -> None:
    """A scope keyboard for product P, then a threshold prompt for the same P."""
    await h.text(PRIVATE, USER, f"/add {URL_EUR}")
    scope_prompt = h.last_prompt(PRIVATE)
    scope_token = _token_of(scope_prompt.callback_data())
    inserted = h.services.writes[-1]
    assert inserted[0] == "insert"
    product_id = inserted[2]

    await h.press(PRIVATE, USER, f"p:{product_id}:th")
    await h.press(PRIVATE, USER, f"p:{scope_token}:sc:world")

    assert h.toasts()[-1] == TEXT_EXPIRED
    assert [w for w in h.services.writes if w[0] == "scope"] == []
    open_flow = h.flow.registry.get((PRIVATE, USER))
    assert open_flow is not None
    assert open_flow.product_id == product_id
    assert open_flow.kind is FlowKind.THRESHOLD


async def test_double_tap_on_scope_writes_once(h: Harness) -> None:
    """E9: the second identical tap finds no flow and gets the expiry toast."""
    await h.text(PRIVATE, USER, f"/add {URL_EUR}")
    token = _token_of(h.last_prompt(PRIVATE).callback_data())

    await h.press(PRIVATE, USER, f"p:{token}:sc:customs_area")
    await h.press(PRIVATE, USER, f"p:{token}:sc:customs_area")

    scope_writes = [w for w in h.services.writes if w[0] == "scope"]
    assert scope_writes == [("scope", USER, 1000, True, "customs_area")]
    assert h.toasts()[-1] == TEXT_EXPIRED


async def test_three_invalid_answers_end_flow_and_empty_registry(h: Harness) -> None:
    """E3 at max_attempts: flow ends, registry entry removed, timeout disarmed."""
    await _open_value(h, PRIVATE, USER, 1, "th")
    for _ in range(3):
        await h.text(PRIVATE, USER, "NaN")

    assert h.texts_to(PRIVATE)[-1] == TEXT_TOO_MANY
    assert len(h.flow.registry) == 0
    assert h.timer.live() == []
    await h.text(PRIVATE, USER, "20%")
    assert h.services.writes == []
    assert h.texts_to(PRIVATE)[-1] == TEXT_NO_OPEN_PROMPT


# --- correlation: two chats, other users, unknown tokens -------------------


async def test_same_user_in_two_chats_has_two_independent_flows(h: Harness) -> None:
    """E15: opening a prompt in the group must not steal the private prompt's answer."""
    await _open_value(h, PRIVATE, USER, 1, "th")
    await _open_value(h, GROUP, USER, 2, "tg")

    await h.text(PRIVATE, USER, "20%")
    await h.text(GROUP, USER, "12.50")

    assert h.services.writes == [
        ("value", USER, FlowKind.THRESHOLD, 1, Percentage(20)),
        ("value", USER, FlowKind.TARGET, 2, SetTarget(Decimal("12.50"))),
    ]


async def test_timeout_in_one_chat_leaves_the_other_chat_flow_open(h: Harness) -> None:
    await _open_value(h, PRIVATE, USER, 1, "th")
    await _open_value(h, GROUP, USER, 2, "tg")
    private_timer = next(a for a in h.timer.armed if a.snapshot.key == (PRIVATE, USER))

    await h.timer.fire(private_timer)

    assert h.flow.registry.keys() == {(GROUP, USER)}
    await h.text(GROUP, USER, "3")
    assert h.services.writes == [("value", USER, FlowKind.TARGET, 2, SetTarget(Decimal("3")))]


async def test_other_user_pressing_a_prompt_button_in_a_group_is_expired(h: Harness) -> None:
    token, _ = await _open_value(h, GROUP, USER, 1, "th")

    await h.press(GROUP, OTHER_USER, f"p:{token}:x")
    await h.text(GROUP, OTHER_USER, "20%")

    assert h.toasts()[-1] == TEXT_EXPIRED
    assert h.texts_to(GROUP)[-1] == TEXT_NO_OPEN_PROMPT
    assert h.flow.registry.keys() == {(GROUP, USER)}
    await h.text(GROUP, USER, "20%")
    assert h.services.writes == [("value", USER, FlowKind.THRESHOLD, 1, Percentage(20))]


async def test_valid_token_pressed_from_another_chat_is_expired(h: Harness) -> None:
    token, _ = await _open_value(h, PRIVATE, USER, 1, "th")

    await h.press(GROUP, USER, f"p:{token}:x")

    assert h.toasts()[-1] == TEXT_EXPIRED
    assert h.flow.registry.keys() == {(PRIVATE, USER)}


@pytest.mark.parametrize("open_first", [False, True])
async def test_unknown_token_is_expired_and_changes_nothing(h: Harness, open_first: bool) -> None:
    if open_first:
        await _open_value(h, PRIVATE, USER, 1, "th")
    before = h.flow.registry.keys()

    await h.press(PRIVATE, USER, "p:" + "ab" * 16 + ":x")
    await h.press(PRIVATE, USER, "p:" + "cd" * 16 + ":cur:USD")

    assert h.toasts()[-2:] == [TEXT_EXPIRED, TEXT_EXPIRED]
    assert h.flow.registry.keys() == before
    assert h.services.writes == []


# --- timeouts --------------------------------------------------------------


async def test_answer_then_timeout_applies_once_and_never_expires(h: Harness) -> None:
    """E11, answer first: the timeout's snapshot no longer matches."""
    _, message_id = await _open_value(h, PRIVATE, USER, 1, "th")
    armed = h.timer.armed[-1]

    await h.text(PRIVATE, USER, "20%")
    await h.timer.fire(armed)

    assert h.services.writes == [("value", USER, FlowKind.THRESHOLD, 1, Percentage(20))]
    assert TEXT_EXPIRED_VALUE not in h.edits_of(message_id)


async def test_timeout_then_answer_applies_nothing(h: Harness) -> None:
    """E11, timeout first: the prompt expires and the answer finds no flow."""
    _, message_id = await _open_value(h, PRIVATE, USER, 1, "th")

    await h.timer.fire(h.timer.armed[-1])
    await h.text(PRIVATE, USER, "20%")

    assert h.edits_of(message_id) == [TEXT_EXPIRED_VALUE]
    assert h.services.writes == []
    assert h.texts_to(PRIVATE)[-1] == TEXT_NO_OPEN_PROMPT


async def test_timeout_of_superseded_prompt_does_not_touch_new_flow(h: Harness) -> None:
    await _open_value(h, PRIVATE, USER, 1, "th")
    old_timer = h.timer.armed[-1]
    new_token, new_message = await _open_value(h, PRIVATE, USER, 1, "tg")

    await h.timer.fire(old_timer)

    open_flow = h.flow.registry.get((PRIVATE, USER))
    assert open_flow is not None
    assert open_flow.token == new_token
    assert h.edits_of(new_message) == []


async def test_answer_racing_timeout_in_same_loop_iteration(h: Harness) -> None:
    """E11 without ordering: both run concurrently, exactly one of them wins."""
    _, message_id = await _open_value(h, PRIVATE, USER, 1, "th")
    armed = h.timer.armed[-1]
    answer = message_update(h.app.bot, PRIVATE, USER, "20%")

    await asyncio.gather(h.process(answer), h.timer.fire(armed))

    expired = TEXT_EXPIRED_VALUE in h.edits_of(message_id)
    assert expired != bool(h.services.writes)
    assert len(h.flow.registry) == 0


# --- restart ---------------------------------------------------------------


async def test_restart_mid_flow_old_buttons_expire_and_text_finds_no_prompt(h: Harness) -> None:
    """E14: registry and timers die with the process; old prompts stay in the chat."""
    token, _ = await _open_value(h, PRIVATE, USER, 1, "th")

    await h.restart()
    await h.press(PRIVATE, USER, f"p:{token}:x")
    await h.text(PRIVATE, USER, "20%")

    assert h.toasts()[-1] == TEXT_EXPIRED
    assert h.texts_to(PRIVATE)[-1] == TEXT_NO_OPEN_PROMPT
    assert h.services.writes == []
    assert len(h.flow.registry) == 0


# --- commands and foreign callbacks ----------------------------------------


async def test_add_addressed_to_another_bot_reaches_no_service(h: Harness) -> None:
    calls_before = len(h.request.calls)

    await h.text(PRIVATE, USER, f"/add@DifferentBot {URL_EUR}")

    assert h.services.calls == []
    assert h.services.writes == []
    assert len(h.request.calls) == calls_before
    assert len(h.flow.registry) == 0


async def test_cancel_addressed_to_another_bot_keeps_open_flow(h: Harness) -> None:
    await _open_value(h, PRIVATE, USER, 1, "th")
    flow_before = h.flow.registry.get((PRIVATE, USER))
    service_calls_before = len(h.services.calls)
    transport_calls_before = len(h.request.calls)

    await h.text(PRIVATE, USER, "/cancel@DifferentBot")

    assert h.flow.registry.get((PRIVATE, USER)) is flow_before
    assert len(h.services.calls) == service_calls_before
    assert len(h.request.calls) == transport_calls_before


async def test_non_flow_command_addressed_to_another_bot_is_not_routed(h: Harness) -> None:
    await _open_value(h, PRIVATE, USER, 1, "th")
    flow_before = h.flow.registry.get((PRIVATE, USER))
    service_calls_before = len(h.services.calls)
    transport_calls_before = len(h.request.calls)
    update = message_update(h.app.bot, PRIVATE, USER, "/help@DifferentBot")

    assert h.flow.check_update(update) is None
    await h.process(update)

    assert h.flow.registry.get((PRIVATE, USER)) is flow_before
    assert h.help_calls == 0
    assert len(h.services.calls) == service_calls_before
    assert len(h.request.calls) == transport_calls_before


async def test_add_addressed_to_our_bot_is_case_insensitive(h: Harness) -> None:
    await h.text(PRIVATE, USER, f"/AdD@TeSt_BoT {URL_EUR}")

    assert h.services.writes == [("insert", USER, 1000, URL_EUR, "EUR")]


async def test_add_without_recipient_still_works(h: Harness) -> None:
    await h.text(PRIVATE, USER, f"/add {URL_EUR}")

    assert h.services.writes == [("insert", USER, 1000, URL_EUR, "EUR")]


def test_addressed_command_is_ignored_when_bot_username_is_unknown() -> None:
    app = make_application(FakeRequest())
    flow = GuidedFlow(_services(), ManualTimer())
    flow.attach(app.bot)
    update = message_update(app.bot, PRIVATE, USER, f"/add@test_bot {URL_EUR}")

    assert flow.check_update(update) is None


async def test_cancel_inside_and_outside_a_flow(h: Harness) -> None:
    """E4: inside a flow the prompt is closed; outside, group 1 answers once."""
    _, message_id = await _open_value(h, PRIVATE, USER, 1, "th")

    await h.text(PRIVATE, USER, "/cancel")
    assert h.edits_of(message_id) == [TEXT_CANCELLED]
    assert TEXT_NOTHING_TO_CANCEL not in h.texts_to(PRIVATE)

    await h.text(PRIVATE, USER, "/cancel")
    assert h.texts_to(PRIVATE).count(TEXT_NOTHING_TO_CANCEL) == 1


async def test_other_command_ends_flow_and_still_runs(h: Harness) -> None:
    """E5: the flow ends with its default outcome and the command runs in group 1."""
    _, message_id = await _open_value(h, PRIVATE, USER, 1, "th")

    await h.text(PRIVATE, USER, "/help")

    assert h.edits_of(message_id) == [TEXT_CANCELLED]
    assert h.help_calls == 1
    assert h.texts_to(PRIVATE)[-1] == HELP_TEXT
    assert len(h.flow.registry) == 0


@pytest.mark.parametrize("data", ["h", "l:a:1", "check_5", "garbage"])
async def test_foreign_callback_ends_flow_and_passes_through(h: Harness, data: str) -> None:
    """E8: any callback that is not a flow action abandons the prompt."""
    _, message_id = await _open_value(h, PRIVATE, USER, 1, "th")
    routed_before = len(h.routed)

    await h.press(PRIVATE, USER, data)

    assert h.edits_of(message_id) == [TEXT_CANCELLED]
    assert len(h.flow.registry) == 0
    registered = data in {"h", "l:a:1"}
    assert len(h.routed) == routed_before + (1 if registered else 0)
    if not registered:
        assert h.toasts()[-1] == TEXT_EXPIRED


# --- revocation and transport failure --------------------------------------


async def test_revoked_user_answer_writes_nothing(h: Harness) -> None:
    """E12: the service re-checks access at call time."""
    await _open_value(h, PRIVATE, USER, 1, "th")
    h.services.active.discard(USER)

    await h.text(PRIVATE, USER, "20%")

    assert h.services.writes == []
    assert h.texts_to(PRIVATE)[-1] == "Not authorised."
    assert len(h.flow.registry) == 0


async def test_forbidden_on_success_screen_keeps_write_and_never_retries(h: Harness) -> None:
    """E13: the write is committed first; nothing more is sent to the blocked chat."""
    await _open_value(h, PRIVATE, USER, 1, "th")
    h.request.fail_next_call_to(PRIVATE)
    calls_before = len(h.request.calls)

    await h.text(PRIVATE, USER, "20%")

    assert h.services.writes == [("value", USER, FlowKind.THRESHOLD, 1, Percentage(20))]
    after = [c for c in h.request.calls[calls_before:] if c.chat_id == PRIVATE]
    assert [c.failed for c in after] == [True]
    assert len(h.flow.registry) == 0


async def test_forbidden_on_prompt_ends_the_flow_it_would_open(h: Harness) -> None:
    h.request.fail_next_call_to(PRIVATE)

    await h.press(PRIVATE, USER, "p:1:th")

    assert len(h.flow.registry) == 0
    assert h.timer.armed == []


async def test_forbidden_on_supersede_edit_skips_new_prompt(h: Harness) -> None:
    await _open_value(h, PRIVATE, USER, 1, "th")
    h.request.fail_next_call_to(PRIVATE)
    calls_before = len(h.request.calls)

    await h.press(PRIVATE, USER, "p:1:tg")

    after = [c for c in h.request.calls[calls_before:] if c.chat_id == PRIVATE]
    assert [(c.method, c.failed) for c in after] == [("editMessageText", True)]
    assert len(h.flow.registry) == 0


# --- add flow: currency and scope ------------------------------------------


async def test_unknown_currency_holds_no_row_until_a_choice(h: Harness) -> None:
    """11.7 item 10: AWAIT_CURRENCY inserts nothing; the button inserts with USD."""
    await h.text(PRIVATE, USER, URL_NOCUR)
    assert h.services.writes == []
    token = _token_of(h.last_prompt(PRIVATE).callback_data())

    await h.press(PRIVATE, USER, f"p:{token}:cur:USD")

    assert h.services.writes[0] == ("insert", USER, 1000, URL_NOCUR, "USD")


@pytest.mark.parametrize(
    ("typed", "expected"), [("usd", "USD"), (" gbp ", "GBP"), ("us$", None), ("XXQ", None)]
)
async def test_typed_currency_code(h: Harness, typed: str, expected: str | None) -> None:
    await h.text(PRIVATE, USER, URL_NOCUR)
    token = _token_of(h.last_prompt(PRIVATE).callback_data())
    await h.press(PRIVATE, USER, f"p:{token}:cur:type")

    await h.text(PRIVATE, USER, typed)

    inserts = [w for w in h.services.writes if w[0] == "insert"]
    if expected is None:
        assert inserts == []
        assert h.flow.registry.get((PRIVATE, USER)) is not None
    else:
        assert inserts == [("insert", USER, 1000, URL_NOCUR, expected)]


async def test_text_in_currency_step_without_type_mode_falls_through(h: Harness) -> None:
    await h.text(PRIVATE, USER, URL_NOCUR)

    await h.text(PRIVATE, USER, "USD")

    assert h.services.writes == []
    assert h.texts_to(PRIVATE)[-1] == TEXT_NO_OPEN_PROMPT
    assert h.flow.registry.get((PRIVATE, USER)) is not None


@pytest.mark.parametrize("how", ["cancel_button", "cancel_command", "timeout"])
async def test_currency_step_closed_without_choice_adds_nothing(h: Harness, how: str) -> None:
    await h.text(PRIVATE, USER, URL_NOCUR)
    prompt = h.last_prompt(PRIVATE)
    token = _token_of(prompt.callback_data())
    message_id = h.prompt_message_id(prompt)

    if how == "cancel_button":
        await h.press(PRIVATE, USER, f"p:{token}:cur:cancel")
        expected = TEXT_NO_PRODUCT
    elif how == "cancel_command":
        await h.text(PRIVATE, USER, "/cancel")
        expected = TEXT_NO_PRODUCT
    else:
        await h.timer.fire(h.timer.armed[-1])
        expected = TEXT_EXPIRED_ADD

    assert h.services.writes == []
    assert h.edits_of(message_id) == [expected]
    assert len(h.flow.registry) == 0


@pytest.mark.parametrize(
    ("default", "step", "expect_prompt", "expect_scope"),
    [
        ("ask", True, True, None),
        ("store_only", True, False, None),
        ("other_stores", True, False, ("scope", USER, 1000, True, None)),
        ("ask", False, False, None),
        ("store_only", False, False, None),
        ("other_stores", False, False, None),
    ],
)
async def test_scope_branch_follows_add_scope_default(
    default: str, step: bool, expect_prompt: bool, expect_scope: tuple[Any, ...] | None
) -> None:
    """11.7 item 11 (a): the table over add_scope_default x ADD_SCOPE_STEP."""
    services = _services()
    services.scope_defaults[USER] = default  # type: ignore[assignment]
    harness = Harness(services, config=FlowConfig(add_scope_step=step))
    await harness.start()
    try:
        await harness.text(PRIVATE, USER, f"/add {URL_EUR}")
        scope_writes = [w for w in services.writes if w[0] == "scope"]
        assert scope_writes == ([] if expect_scope is None else [expect_scope])
        assert (harness.flow.registry.get((PRIVATE, USER)) is not None) == expect_prompt
        assert not any(c[0] == "set_user_add_scope_default" for c in services.calls)
    finally:
        await harness.stop()


async def test_failed_scrape_never_opens_a_flow(h: Harness) -> None:
    await _open_value(h, PRIVATE, USER, 1, "th")

    await h.text(PRIVATE, USER, "/add https://shop.example/broken")

    assert len(h.flow.registry) == 0
    assert h.services.writes == []


async def test_scope_picker_then_level_writes_override(h: Harness) -> None:
    await h.text(PRIVATE, USER, f"/add {URL_EUR}")
    token = _token_of(h.last_prompt(PRIVATE).callback_data())

    await h.press(PRIVATE, USER, f"p:{token}:sc")
    await h.press(PRIVATE, USER, f"p:{token}:sc:world")

    assert [w for w in h.services.writes if w[0] == "scope"] == [
        ("scope", USER, 1000, True, "world")
    ]
    assert len(h.flow.registry) == 0


# --- E16: channel posts ----------------------------------------------------


def _message_handlers(h: Harness) -> list[BaseHandler[Any, Any, Any]]:
    return [
        handler
        for group in h.app.handlers.values()
        for handler in group
        if isinstance(handler, MessageHandler | CommandHandler | GuidedFlow)
    ]


@pytest.mark.parametrize("kind", ["channel_post", "edited_channel_post", "guest_message"])
@pytest.mark.parametrize("with_from", [True, False])
@pytest.mark.parametrize("text", ["20%", URL_EUR, f"/add {URL_EUR}", "/cancel", "/help"])
async def test_channel_posts_reach_no_handler(
    h: Harness, kind: str, with_from: bool, text: str
) -> None:
    """E16 / constraint F1: rejected by check_update of every message-shaped handler."""
    await _open_value(h, CHANNEL, USER, 1, "th")
    calls_before = len(h.request.calls)
    services_before = len(h.services.calls)
    update = message_update(h.app.bot, CHANNEL, USER if with_from else None, text, kind=kind)

    accepted = [handler for handler in _message_handlers(h) if handler.check_update(update)]
    await h.process(update)

    assert accepted == []
    assert len(h.request.calls) == calls_before
    assert len(h.services.calls) == services_before
    assert h.flow.registry.keys() == {(CHANNEL, USER)}


@pytest.mark.parametrize(
    ("text", "route"),
    [
        ("20%", RouteKind.ANSWER),
        (URL_EUR, RouteKind.ADD_ENTRY),
        (f"/add {URL_EUR}", RouteKind.ADD_ENTRY),
        ("/cancel", RouteKind.CANCEL),
        ("/help", RouteKind.OTHER_COMMAND),
    ],
)
async def test_ordinary_message_is_routed_positive_control(
    h: Harness, text: str, route: RouteKind
) -> None:
    await _open_value(h, CHANNEL, USER, 1, "th")
    update = message_update(h.app.bot, CHANNEL, USER, text, chat_type="group")

    decision = h.flow.check_update(update)

    assert decision is not None
    assert decision.kind is route


async def test_fallback_handlers_accept_ordinary_messages_positive_control(h: Harness) -> None:
    text_update = message_update(h.app.bot, PRIVATE, USER, "hello")
    cancel_update = message_update(h.app.bot, PRIVATE, USER, "/cancel")
    handlers = _message_handlers(h)
    fallback = next(x for x in handlers if isinstance(x, MessageHandler))
    cancel = next(x for x in handlers if isinstance(x, CommandHandler) and "cancel" in x.commands)

    assert fallback.check_update(text_update)
    assert cancel.check_update(cancel_update)


# --- coexistence with the legacy pending_action ----------------------------


def _legacy_db() -> AsyncMock:
    db = AsyncMock()
    db.is_user_allowed = AsyncMock(return_value=True)
    db.is_user_admin = AsyncMock(return_value=False)
    db.get_product_for_user = AsyncMock(
        return_value={"name": "Kettle", "current_price": None, "currency": "EUR"}
    )
    return db


async def _legacy_track_threshold(
    update: Update, context: CallbackContext[Any, Any, Any, Any]
) -> None:
    """Stand-in for the legacy `track_threshold_<id>` button: sets pending_action."""
    query = update.callback_query
    assert query is not None
    assert query.data is not None
    assert context.user_data is not None
    context.user_data["pending_action"] = ("threshold", int(query.data.rsplit("_", 1)[1]))
    await query.answer()


@pytest.fixture
async def legacy_h() -> AsyncIterator[tuple[Harness, AsyncMock]]:
    harness = Harness(_services(), legacy_handlers_present=True)
    db = _legacy_db()
    harness.app.bot_data["db"] = db
    text_input.register(harness.app)
    harness.app.add_handler(
        CallbackQueryHandler(_legacy_track_threshold, pattern=r"^track_threshold_\d+$"), group=2
    )
    await harness.start()
    yield harness, db
    await harness.stop()


async def test_new_flow_supersedes_legacy_pending_action(
    legacy_h: tuple[Harness, AsyncMock],
) -> None:
    h, db = legacy_h
    await h.press(PRIVATE, USER, "track_threshold_1")
    assert h.app.user_data[USER]["pending_action"] == ("threshold", 1)

    await _open_value(h, PRIVATE, USER, 1, "tg")
    await h.text(PRIVATE, USER, "12")

    assert "pending_action" not in h.app.user_data[USER]
    assert h.services.writes == [("value", USER, FlowKind.TARGET, 1, SetTarget(Decimal("12")))]
    db.set_threshold.assert_not_awaited()


async def test_legacy_button_ends_new_flow_and_legacy_consumes_next_text(
    legacy_h: tuple[Harness, AsyncMock],
) -> None:
    h, db = legacy_h
    _, message_id = await _open_value(h, PRIVATE, USER, 1, "tg")

    await h.press(PRIVATE, USER, "track_threshold_1")
    await h.text(PRIVATE, USER, "20")

    assert h.edits_of(message_id) == [TEXT_CANCELLED]
    assert h.services.writes == []
    db.set_threshold.assert_awaited_once()
    assert TEXT_NO_OPEN_PROMPT not in h.texts_to(PRIVATE)
    answered = [c.params["callback_query_id"] for c in h.request.calls_of("answerCallbackQuery")]
    assert len(answered) == len(set(answered))


async def test_text_without_any_prompt_is_consumed_at_most_once_during_coexistence(
    legacy_h: tuple[Harness, AsyncMock],
) -> None:
    h, db = legacy_h

    await h.text(PRIVATE, USER, "hello")

    assert h.request.calls_of("sendMessage") == []
    assert h.services.writes == []
    db.set_threshold.assert_not_awaited()


# --- the production timer --------------------------------------------------


async def test_job_queue_timer_fires_with_its_snapshot_and_disarm_cancels() -> None:
    request = FakeRequest()
    app = make_application(request, with_job_queue=True)
    await app.initialize()
    assert app.job_queue is not None
    await app.job_queue.start()
    try:
        timer = JobQueueTimer(app.job_queue)
        fired: list[FlowSnapshot] = []

        async def record(snapshot: FlowSnapshot) -> None:
            fired.append(snapshot)

        kept = FlowSnapshot((PRIVATE, USER), "a" * 32)
        cancelled = FlowSnapshot((GROUP, USER), "b" * 32)
        timer.arm(kept, 0.05, record)
        timer.arm(cancelled, 0.05, record)
        timer.disarm(cancelled)
        await asyncio.sleep(0.4)
        assert fired == [kept]
    finally:
        await app.job_queue.stop()
        await app.shutdown()


async def test_timeout_is_armed_with_the_configured_delay() -> None:
    timer = ManualTimer()
    flow = GuidedFlow(_services(), timer, config=FlowConfig(timeout_seconds=42.0))
    app = make_application(FakeRequest())
    from price_tracker.bot.flows import register_guided_flow  # noqa: PLC0415

    register_guided_flow(app, flow)
    await app.initialize()
    try:
        await app.process_update(callback_update(app.bot, PRIVATE, USER, "p:1:th"))
    finally:
        await app.shutdown()
    assert [a.delay for a in timer.armed] == [42.0]


# --- rejection => no mutation (11.7 item 13, per-parser corpus) ------------

_FLOW_VERBS = {"threshold": "th", "target": "tg", "product_interval": "iv"}


@pytest.mark.parametrize(
    ("kind", "text"),
    [(kind, text) for kind, texts in FLOW_REJECTED.items() for text in texts],
    ids=repr,
)
async def test_rejected_answer_mutates_nothing(h: Harness, kind: str, text: str) -> None:
    await _open_value(h, PRIVATE, USER, 1, _FLOW_VERBS[kind])
    calls_before = [c for c in h.services.calls if c[0] == "apply_value"]

    await h.text(PRIVATE, USER, text)

    assert h.services.writes == []
    assert [c for c in h.services.calls if c[0] == "apply_value"] == calls_before
    open_flow = h.flow.registry.get((PRIVATE, USER))
    assert open_flow is not None
    assert open_flow.attempts == 1


async def test_stale_keyboard_same_state_old_token_does_not_act_on_new_prompt(h: Harness) -> None:
    """Two currency prompts for the same page: the older keyboard must not insert."""
    await h.text(PRIVATE, USER, URL_NOCUR)
    old_token = _token_of(h.last_prompt(PRIVATE).callback_data())
    await h.text(PRIVATE, USER, URL_NOCUR)
    new_token = _token_of(h.last_prompt(PRIVATE).callback_data())

    await h.press(PRIVATE, USER, f"p:{old_token}:cur:USD")

    assert old_token != new_token
    assert h.toasts()[-1] == TEXT_EXPIRED
    assert h.services.writes == []
    open_flow = h.flow.registry.get((PRIVATE, USER))
    assert open_flow is not None
    assert open_flow.token == new_token


@pytest.mark.parametrize(("url", "suffix"), [(URL_NOCUR, "cur:type"), (URL_EUR, "sc")])
async def test_intermediate_button_replay_is_expired(h: Harness, url: str, suffix: str) -> None:
    await h.text(PRIVATE, USER, f"/add {url}")
    token = _token_of(h.last_prompt(PRIVATE).callback_data())
    wire = f"p:{token}:{suffix}"
    await h.press(PRIVATE, USER, wire)
    edits_before = len(h.request.calls_of("editMessageText"))
    await h.press(PRIVATE, USER, wire)
    assert h.toasts()[-1] == TEXT_EXPIRED
    assert len(h.request.calls_of("editMessageText")) == edits_before
