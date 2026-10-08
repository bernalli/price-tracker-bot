"""The setting prompts (mute, digest, quiet hours, time zone, throttle) in the real layout.

A ``✏️`` button sends a new message with the prompt and Cancel; the answer is parsed,
written through the services and confirmed with a way back and Home. Every message
that closes one of these prompts carries the same two buttons.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from freezegun import freeze_time
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.app.inputs import (
    InputError,
    parse_digest_interval,
    parse_mute_hours,
    parse_quiet_hours,
    parse_throttle,
    parse_timezone,
)
from price_tracker.db.models import NotificationPrefs
from tests.integration.test_guided_flow_wired import (
    ADMIN,
    CANCELLED,
    EXPIRED,
    GROUP,
    NOT_AUTHORISED,
    NOT_FOUND,
    OTHER,
    OWNER,
    PRIVATE,
    SAVED,
    TOO_MANY,
    Wired,
    _open,
    _wired,
)
from tests.support.fake_telegram import message_update

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from tests.support.fake_telegram import Call

STRANGER = 12
SUPERSEDED = "Replaced by a newer prompt."
BUTTON_EXPIRED = "This button has expired."
PROMPTS = {
    "mu": "Send the mute duration in hours (1-8760), or forever.",
    "dg": "Send the digest interval in minutes (5-1440).",
    "qh": "Send the quiet hours as HH:MM-HH:MM, or off.",
    "tz": "Send your time zone, for example Europe/Rome.",
    "th": "Send the most notifications per hour, or off.",
}
WHOLE = "Use a whole number, e.g. 12."
RANGE = "That value is out of range."
EMPTY = "Please send a value."
LONG = "That is too long."
INVISIBLE = "That contains invisible characters."
ZONE = "Unknown time zone."
WINDOW = "Use HH:MM-HH:MM."
NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)
GLOBAL_NAV = [["s", "h"]]


@pytest.fixture
async def w() -> AsyncIterator[Wired]:
    async with _wired() as wired:
        yield wired


def _rows(call: Call) -> list[list[str]]:
    markup = call.params.get("reply_markup")
    if markup is None:
        return []
    markup = json.loads(markup) if isinstance(markup, str) else markup
    return [[b["callback_data"] for b in row] for row in markup["inline_keyboard"]]


def _last_sent(w: Wired, chat_id: int = PRIVATE) -> Call:
    return w.sent(chat_id)[-1]


def _closing_edit(w: Wired, prompt: Call) -> Call:
    edits = [
        c
        for c in w.request.calls_of("editMessageText")
        if int(c.params["message_id"]) == prompt.message_id
    ]
    assert len(edits) == 1, edits
    return edits[0]


async def _global(w: Wired, user_id: int = OWNER) -> NotificationPrefs | None:
    return await w.repo.get_notification_prefs(user_id=user_id, product_id=None)


async def _prefs_rows(w: Wired) -> int:
    cursor = await w.conn.execute("SELECT COUNT(*) FROM notification_prefs")
    row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


# --- opening ----------------------------------------------------------------------


@pytest.mark.parametrize("setting", sorted(PROMPTS))
async def test_each_setting_button_opens_its_prompt(w: Wired, setting: str) -> None:
    before = len(w.request.calls)
    prompt = await _open(w, f"s:ask:{setting}")

    assert prompt.params["text"] == PROMPTS[setting]
    (cancel,) = prompt.callback_data()
    flow = w.flow.registry.get((PRIVATE, OWNER))
    assert flow is not None
    assert cancel == f"p:{flow.token}:x"
    assert flow.product_id is None
    assert w.pending(OWNER) is None
    methods = [c.method for c in w.calls_since(before)]
    assert "editMessageText" not in methods
    assert methods.count("answerCallbackQuery") == 1


async def test_the_product_mute_button_opens_its_prompt(w: Wired) -> None:
    prompt = await _open(w, f"p:{w.product}:mua")

    assert prompt.params["text"] == f"Kettle\n{PROMPTS['mu']}"
    flow = w.flow.registry.get((PRIVATE, OWNER))
    assert flow is not None
    assert (flow.kind, flow.product_id) == ("mute", w.product)


@pytest.mark.parametrize("data", [*(f"s:ask:{s}" for s in PROMPTS), "p:{pid}:mua"])
async def test_a_user_who_may_not_use_the_bot_gets_no_prompt(w: Wired, data: str) -> None:
    await w.repo.remove_user(OWNER)
    await w.press(PRIVATE, OWNER, data.format(pid=w.product))
    await w.press(PRIVATE, STRANGER, data.format(pid=w.product))

    assert w.toasts() == [NOT_AUTHORISED, NOT_AUTHORISED]
    assert len(w.flow.registry) == 0
    assert w.prompts(PRIVATE) == []


@pytest.mark.parametrize("which", ["foreign", "missing"])
async def test_the_mute_of_a_foreign_or_missing_product_is_not_found(w: Wired, which: str) -> None:
    product_id = w.other_product if which == "foreign" else w.other_product + 1000
    await w.press(PRIVATE, OWNER, f"p:{product_id}:mua")

    assert w.toasts() == [NOT_FOUND]
    assert len(w.flow.registry) == 0


# --- answers ------------------------------------------------------------------------

VALID: list[tuple[str, str, dict[str, Any]]] = [
    ("mu", "1", {"mute": True, "mute_until": NOW + timedelta(hours=1)}),
    ("mu", "8760", {"mute": True, "mute_until": NOW + timedelta(hours=8760)}),
    ("mu", "forever", {"mute": True, "mute_until": None}),
    ("mu", "FOREVER", {"mute": True, "mute_until": None}),
    ("mu", " 12 ", {"mute": True, "mute_until": NOW + timedelta(hours=12)}),
    ("dg", "5", {"digest_mode": True, "digest_interval_minutes": 5}),
    ("dg", "1440", {"digest_mode": True, "digest_interval_minutes": 1440}),
    ("qh", "22:00-08:00", {"quiet_hours_start": "22:00", "quiet_hours_end": "08:00"}),
    ("qh", "00:00-23:59", {"quiet_hours_start": "00:00", "quiet_hours_end": "23:59"}),
    ("qh", "off", {"quiet_hours_start": None, "quiet_hours_end": None}),
    ("tz", "Europe/Rome", {"timezone": "Europe/Rome"}),
    ("tz", "UTC", {"timezone": "UTC"}),
    # Surrounding whitespace is stripped from every scalar answer.
    ("tz", "Asia/Tokyo\n", {"timezone": "Asia/Tokyo"}),
    ("th", "1", {"throttle_per_hour": 1}),
    ("th", "999999999", {"throttle_per_hour": 999_999_999}),
    ("th", "off", {"throttle_per_hour": None}),
    ("th", "OFF", {"throttle_per_hour": None}),
]
START: dict[str, Any] = {
    "mute": False,
    "mute_until": None,
    "digest_mode": False,
    "digest_interval_minutes": 30,
    "quiet_hours_start": "23:30",
    "quiet_hours_end": "06:30",
    "throttle_per_hour": 7,
    "timezone": "America/New_York",
}


@pytest.mark.parametrize(("setting", "answer", "fields"), VALID, ids=repr)
async def test_a_valid_answer_writes_its_fields_and_says_saved_with_a_way_back(
    w: Wired, setting: str, answer: str, fields: dict[str, Any]
) -> None:
    await w.repo.upsert_notification_prefs(NotificationPrefs(user_id=OWNER, **START))
    with freeze_time(NOW, real_asyncio=True):
        await _open(w, f"s:ask:{setting}")
        await w.text(PRIVATE, OWNER, answer)

    row = await _global(w)
    assert row is not None
    got = {key: getattr(row, key) for key in START}
    assert got == {**START, **fields}
    saved = _last_sent(w)
    assert saved.params["text"] == SAVED
    assert _rows(saved) == GLOBAL_NAV
    assert len(w.flow.registry) == 0


INVALID: list[tuple[str, str, str]] = [
    ("mu", "0", RANGE),
    ("mu", "8761", RANGE),
    ("mu", "-1", WHOLE),
    ("mu", "1.5", WHOLE),
    ("mu", "1e3", WHOLE),
    ("mu", "١٢", WHOLE),
    ("mu", "   ", EMPTY),
    ("mu", "forever!", WHOLE),
    ("mu", "1" * 65, LONG),
    ("mu", "1\x002", INVISIBLE),
    ("dg", "4", RANGE),
    ("dg", "1441", RANGE),
    ("dg", "30m", WHOLE),
    ("dg", "+30", WHOLE),
    ("qh", "22:00-22:00", WINDOW),
    ("qh", "24:00-01:00", WINDOW),
    ("qh", "22:00–08:00", WINDOW),
    ("qh", "2200-0800", WINDOW),
    ("qh", "22:00", WINDOW),
    ("qh", "22:00-08:00-09:00", WINDOW),
    ("tz", "europe/rome", ZONE),
    ("tz", "Mars/Base", ZONE),
    ("tz", "../etc/passwd", ZONE),
    ("tz", "Europe/Ro\nme", INVISIBLE),
    ("th", "0", RANGE),
    ("th", "1000000000", WHOLE),
    ("th", "-5", WHOLE),
    ("th", "off now", WHOLE),
]


@pytest.mark.parametrize(("setting", "answer", "hint"), INVALID, ids=repr)
async def test_an_invalid_answer_gets_its_hint_counts_and_writes_nothing(
    w: Wired, setting: str, answer: str, hint: str
) -> None:
    await _open(w, f"s:ask:{setting}")
    await w.text(PRIVATE, OWNER, answer)

    assert w.texts_to(PRIVATE)[-1] == hint
    flow = w.flow.registry.get((PRIVATE, OWNER))
    assert flow is not None
    assert flow.attempts == 1
    assert await _prefs_rows(w) == 0


@pytest.mark.parametrize("setting", sorted(PROMPTS))
async def test_three_invalid_answers_close_the_prompt(w: Wired, setting: str) -> None:
    await _open(w, f"s:ask:{setting}")
    for answer in ("xx", "yy", "zz"):
        await w.text(PRIVATE, OWNER, answer)

    assert w.texts_to(PRIVATE)[-1] == TOO_MANY
    assert len(w.flow.registry) == 0
    assert await _prefs_rows(w) == 0


_PARSERS = {
    "mu": parse_mute_hours,
    "dg": parse_digest_interval,
    "qh": parse_quiet_hours,
    "tz": parse_timezone,
    "th": parse_throttle,
}


@settings(max_examples=300, deadline=None)
@given(setting=st.sampled_from(sorted(_PARSERS)), answer=st.text())
def test_every_parser_answers_a_value_or_an_input_error(setting: str, answer: str) -> None:
    result = _PARSERS[setting](answer)
    assert result is not None
    if isinstance(result, InputError):
        assert result.code is not None


async def test_a_product_mute_answer_writes_the_product_row_only(w: Wired) -> None:
    await w.repo.upsert_notification_prefs(
        NotificationPrefs(user_id=OWNER, digest_mode=True, timezone="Asia/Tokyo")
    )
    await _open(w, f"p:{w.product}:mua")
    await w.text(PRIVATE, OWNER, "forever")

    row = await w.repo.get_notification_prefs(user_id=OWNER, product_id=w.product)
    assert row is not None
    assert (row.mute, row.mute_until, row.digest_mode, row.timezone) == (
        True,
        None,
        True,
        "Asia/Tokyo",
    )
    global_row = await _global(w)
    assert global_row is not None
    assert global_row.mute is False
    saved = _last_sent(w)
    assert saved.params["text"] == SAVED
    assert _rows(saved) == [[f"p:{w.product}:pr", "h"]]


async def test_a_user_deactivated_before_answering_writes_nothing(w: Wired) -> None:
    await _open(w, "s:ask:th")
    await w.repo.remove_user(OWNER)
    await w.text(PRIVATE, OWNER, "5")

    assert await _prefs_rows(w) == 0
    assert w.texts_to(PRIVATE)[-1] == NOT_AUTHORISED
    assert len(w.flow.registry) == 0


async def test_an_admin_answering_on_a_foreign_product_writes_nothing(w: Wired) -> None:
    await _open(w, f"p:{w.product}:mua", user_id=ADMIN)
    await w.text(PRIVATE, ADMIN, "8")

    assert w.texts_to(PRIVATE)[-1] == NOT_FOUND
    assert await _prefs_rows(w) == 0


# --- closing and the way back -------------------------------------------------------


async def _close_by(w: Wired, how: str, prompt: Call, user_id: int = OWNER) -> None:
    if how == "cancel":
        (cancel,) = prompt.callback_data()
        await w.press(PRIVATE, user_id, cancel)
    elif how == "command":
        await w.text(PRIVATE, user_id, "/cancel")
    elif how == "other_command":
        await w.text(PRIVATE, user_id, "/help")
    else:
        flow = w.flow.registry.get((PRIVATE, user_id))
        assert flow is not None
        await w.flow.on_timeout(flow.snapshot((PRIVATE, user_id)))


CLOSINGS = [
    ("cancel", CANCELLED),
    ("command", CANCELLED),
    ("other_command", CANCELLED),
    ("timeout", EXPIRED),
]


@pytest.mark.parametrize(("how", "text"), CLOSINGS)
@pytest.mark.parametrize("setting", sorted(PROMPTS))
async def test_a_closed_setting_prompt_leads_back_to_settings_and_home(
    w: Wired, setting: str, how: str, text: str
) -> None:
    prompt = await _open(w, f"s:ask:{setting}")
    await _close_by(w, how, prompt)

    edit = _closing_edit(w, prompt)
    assert edit.params["text"] == text
    assert _rows(edit) == GLOBAL_NAV
    assert len(w.flow.registry) == 0
    assert await _prefs_rows(w) == 0


@pytest.mark.parametrize(("how", "text"), CLOSINGS)
async def test_a_closed_product_mute_prompt_leads_back_to_its_notifications(
    w: Wired, how: str, text: str
) -> None:
    prompt = await _open(w, f"p:{w.product}:mua")
    await _close_by(w, how, prompt)

    edit = _closing_edit(w, prompt)
    assert edit.params["text"] == text
    assert _rows(edit) == [[f"p:{w.product}:pr", "h"]]


@pytest.mark.parametrize(("how", "text"), CLOSINGS)
@pytest.mark.parametrize("verb", ["th", "tg", "iv"])
async def test_the_older_prompts_still_close_without_buttons(
    w: Wired, verb: str, how: str, text: str
) -> None:
    prompt = await _open(w, f"p:{w.product}:{verb}")
    await _close_by(w, how, prompt)

    edit = _closing_edit(w, prompt)
    assert edit.params["text"] == text
    assert _rows(edit) == []


async def test_the_older_prompts_still_say_saved_without_buttons(w: Wired) -> None:
    await _open(w, f"p:{w.product}:th")
    await w.text(PRIVATE, OWNER, "20%")

    saved = _last_sent(w)
    assert saved.params["text"] == SAVED
    assert _rows(saved) == []


# --- stale and foreign ----------------------------------------------------------------


async def test_a_second_setting_prompt_replaces_the_first(w: Wired) -> None:
    first = await _open(w, "s:ask:tz")
    second = await _open(w, "s:ask:th")
    (first_cancel,) = first.callback_data()

    await w.press(PRIVATE, OWNER, first_cancel)

    assert w.edits_of(first) == [SUPERSEDED]
    assert w.toasts()[-1] == BUTTON_EXPIRED
    flow = w.flow.registry.get((PRIVATE, OWNER))
    assert flow is not None
    assert flow.kind == "throttle"
    assert second.callback_data() == [f"p:{flow.token}:x"]


async def test_a_threshold_prompt_replaces_a_time_zone_prompt(w: Wired) -> None:
    zone = await _open(w, "s:ask:tz")
    await _open(w, f"p:{w.product}:th")
    await w.text(PRIVATE, OWNER, "Europe/Rome")

    assert w.edits_of(zone) == [SUPERSEDED]
    assert await _prefs_rows(w) == 0
    assert w.texts_to(PRIVATE)[-1] != SAVED


async def test_the_cancel_of_another_user_or_chat_has_expired(w: Wired) -> None:
    prompt = await _open(w, "s:ask:th")
    (cancel,) = prompt.callback_data()

    await w.press(PRIVATE, OTHER, cancel)
    await w.press(GROUP, OWNER, cancel)

    assert w.toasts()[-2:] == [BUTTON_EXPIRED, BUTTON_EXPIRED]
    assert w.flow.registry.keys() == {(PRIVATE, OWNER)}


async def test_the_same_answer_in_another_chat_is_not_taken(w: Wired) -> None:
    await _open(w, "s:ask:th")
    await w.text(GROUP, OWNER, "5")

    assert await _prefs_rows(w) == 0
    assert w.flow.registry.keys() == {(PRIVATE, OWNER)}


@pytest.mark.parametrize("kind", ["channel_post", "edited_channel_post", "edited_message"])
async def test_posts_and_edits_are_never_taken_as_answers(w: Wired, kind: str) -> None:
    await _open(w, "s:ask:th")
    before = len(w.request.calls)

    await w.process(message_update(w.app.bot, PRIVATE, OWNER, "5", kind=kind))

    assert await _prefs_rows(w) == 0
    assert w.flow.registry.keys() == {(PRIVATE, OWNER)}
    assert w.calls_since(before) == []


async def test_a_link_at_a_setting_prompt_closes_it_and_goes_to_the_add(
    w: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    adds: list[str] = []

    async def fake_add_product(update: Any, context: Any, url: str) -> None:
        del update, context
        adds.append(url)

    monkeypatch.setattr("price_tracker.bot.handlers.product._add_product", fake_add_product)
    prompt = await _open(w, "s:ask:tz")

    await w.text(PRIVATE, OWNER, "https://shop.example/item/9")

    assert adds == ["https://shop.example/item/9"]
    assert w.edits_of(prompt) == [CANCELLED]
    assert len(w.flow.registry) == 0


async def test_the_prompts_follow_the_language(w: Wired) -> None:
    await w.press(PRIVATE, OWNER, "s:ask:tz", language_code="it")
    await w.process(message_update(w.app.bot, PRIVATE, OWNER, "Marte/Base", language_code="it"))

    assert w.texts_to(PRIVATE) == [
        "Invia il tuo fuso orario, ad esempio Europe/Rome.",
        "Fuso orario sconosciuto.",
    ]
