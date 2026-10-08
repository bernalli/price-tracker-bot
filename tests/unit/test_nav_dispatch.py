"""Navigation callbacks: the decode-first hook, the closed dispatch table and the
settings actions, against a real in-memory repository and a mocked Telegram query.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, call

import aiosqlite
import pytest
import pytest_asyncio
from freezegun import freeze_time
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from telegram.error import BadRequest, RetryAfter

import price_tracker
from price_tracker.app.views import HomeView
from price_tracker.bot.callbacks import (
    ID_MAX,
    REGISTRY,
    Action,
    BackArg,
    Choice,
    FlowTokenArg,
    IdArg,
    InvalidCallback,
    decode,
    encode,
)
from price_tracker.bot.handlers import cmd_menu
from price_tracker.bot.handlers._cards import list_view, screen_markup
from price_tracker.bot.handlers.callbacks import _nav, handle_callback
from price_tracker.bot.handlers.settings import (
    digest_mode_command,
    mute_command,
    quiet_hours_command,
    unmute_command,
)
from price_tracker.bot.messages import set_locale
from price_tracker.bot.ui.cards import list_page
from price_tracker.bot.ui.panels import home_screen
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.models import NotificationPrefs
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from price_tracker.app.views import ListFilter
    from price_tracker.bot.ui.screens import Screen

MIGRATIONS_DIR = Path(price_tracker.__file__).resolve().parent / "db" / "migrations"
USER = 10

# Written by hand, independently of the table in _nav.
HANDLED = frozenset(
    {
        "noop",
        "settings",
        "settings.section",
        "settings.mute",
        "settings.digest",
        "settings.quiet",
        "settings.language",
        "list.page",
        "list.open",
        "product.card",
        "home",
    }
)


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repository = Repository(conn)
    await repository.ensure_user(USER)
    try:
        yield repository
    finally:
        await conn.close()


def make_query(data: str | None, user_id: int = USER) -> MagicMock:
    query = MagicMock()
    query.data = data
    query.from_user.id = user_id
    query.from_user.language_code = "en"
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def make_context(db: Any) -> MagicMock:
    context = MagicMock()
    context.bot_data = {
        "db": db,
        "repository": db,
        "config": SimpleNamespace(check_interval_minutes=360),
    }
    context.user_data = {}
    return context


def make_update(query: MagicMock) -> MagicMock:
    update = MagicMock()
    update.callback_query = query
    update.effective_user.id = query.from_user.id
    update.effective_user.language_code = "en"
    return update


def sample_args(name: str) -> tuple[int | str, ...]:
    args: list[int | str] = []
    for kind in REGISTRY.spec(name).kinds:
        if isinstance(kind, IdArg):
            args.append(1)
        elif isinstance(kind, Choice):
            args.append(kind.values[0])
        elif isinstance(kind, BackArg):
            args.append("h")
        elif isinstance(kind, FlowTokenArg):
            args.append("0" * 32)
    return tuple(args)


async def press(repo: Repository, data: str | None) -> MagicMock:
    query = make_query(data)
    await handle_callback(make_update(query), make_context(repo))
    return query


# --- T3-9 the hook only forwards registered navigation actions ---------------


@pytest.mark.parametrize("data", ["menu_main", "check_1", "delete_all"])
async def test_legacy_payloads_never_reach_the_navigation_handlers(
    repo: Repository, monkeypatch: pytest.MonkeyPatch, data: str
) -> None:
    async def fail(*args: object, **kwargs: object) -> bool:
        raise AssertionError("a legacy payload reached _nav.handle_action")

    monkeypatch.setattr(_nav, "handle_action", fail)
    await press(repo, data)


@pytest.mark.parametrize("data", ["l:a:0", "garbage", "s:tz"])
async def test_garbage_edits_nothing_and_is_logged(
    repo: Repository, caplog: pytest.LogCaptureFixture, data: str
) -> None:
    with caplog.at_level(logging.INFO):
        query = await press(repo, data)
    query.edit_message_text.assert_not_called()
    assert "Unhandled callback data" in caplog.text


INVALID_SETTINGS = st.one_of(
    st.sampled_from(("s:zz", "s:mu:2", "s:dg:30", "s:qh:0007")),
    st.text(alphabet=":mudgqhoffn012248z", max_size=18).map(lambda tail: f"s{tail}"),
).filter(lambda data: isinstance(decode(data), InvalidCallback))


@settings(
    max_examples=200,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(data=INVALID_SETTINGS)
async def test_invalid_settings_never_dispatch_or_write(repo: Repository, data: str) -> None:
    before = await repo.get_notification_prefs(user_id=USER, product_id=None)
    query = await press(repo, data)
    query.edit_message_text.assert_not_called()
    assert await repo.get_notification_prefs(user_id=USER, product_id=None) == before


# --- T3-10 a user who is not allowed gets nothing ----------------------------


async def test_a_disallowed_user_reads_nothing_beyond_the_allow_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = AsyncMock()
    db.get_user = AsyncMock(return_value=None)
    db.is_user_allowed = AsyncMock(return_value=False)
    handler = AsyncMock(return_value=True)
    monkeypatch.setattr(_nav, "handle_action", handler)
    query = make_query(encode(Action("settings")))
    await handle_callback(make_update(query), make_context(db))
    # The reply language is read first; an unknown user gets no row written.
    assert db.method_calls == [call.get_user(USER), call.is_user_allowed(USER)]
    handler.assert_not_called()
    query.edit_message_text.assert_not_called()


# --- T3-16 the dispatch table is closed --------------------------------------


def test_table_keys_are_registered_actions() -> None:
    assert set(_nav._HANDLERS) <= REGISTRY.names
    assert set(_nav._HANDLERS) == HANDLED


@pytest.mark.parametrize("name", sorted(REGISTRY.names))
async def test_handle_action_answers_true_only_for_the_table(repo: Repository, name: str) -> None:
    query = make_query(None)
    handled = await _nav.handle_action(
        query, make_context(repo), repo, USER, Action(name, sample_args(name))
    )
    assert handled is (name in HANDLED)
    if name not in HANDLED:
        query.edit_message_text.assert_not_called()


async def test_noop_is_silent(repo: Repository, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO):
        query = await press(repo, "noop")
    query.edit_message_text.assert_not_called()
    query.answer.assert_awaited_once()
    assert "Unhandled callback data" not in caplog.text


# --- T3-6 editing the message tolerates what Telegram answers ----------------


async def _press_with_edit_error(repo: Repository, error: Exception) -> MagicMock:
    query = make_query(encode(Action("settings")))
    query.edit_message_text = AsyncMock(side_effect=error)
    await handle_callback(make_update(query), make_context(repo))
    return query


async def test_message_not_modified_is_ignored(
    repo: Repository, caplog: pytest.LogCaptureFixture
) -> None:
    error = BadRequest("Message is not modified: specified new message content is the same")
    with caplog.at_level(logging.WARNING):
        query = await _press_with_edit_error(repo, error)
    query.edit_message_text.assert_awaited_once()
    assert caplog.records == []


async def test_any_other_bad_request_is_logged_and_sends_nothing(
    repo: Repository, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        query = await _press_with_edit_error(repo, BadRequest("Message to edit not found"))
    query.edit_message_text.assert_awaited_once()
    query.message.reply_text.assert_not_called()
    assert [record.levelno for record in caplog.records] == [logging.WARNING]


async def test_retry_after_is_logged_and_ignored(
    repo: Repository, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        query = await _press_with_edit_error(repo, RetryAfter(3))
    query.edit_message_text.assert_awaited_once()
    assert [record.levelno for record in caplog.records] == [logging.WARNING]


# --- T3-11 a button writes exactly what its command writes --------------------

CASES = [
    ("s:mu:1", ["/mute", "all", "1"]),
    ("s:mu:8", ["/mute", "all", "8"]),
    ("s:mu:24", ["/mute", "all", "24"]),
    ("s:mu:0", ["/mute", "all", "forever"]),
    ("s:mu:off", ["/unmute", "all"]),
    ("s:dg:on", ["/digest_mode", "on"]),
    ("s:dg:off", ["/digest_mode", "off"]),
    ("s:qh:2208", ["/quiet_hours", "22:00-08:00"]),
    ("s:qh:off", ["/quiet_hours", "off"]),
]
COMMANDS = {
    "/mute": mute_command,
    "/unmute": unmute_command,
    "/digest_mode": digest_mode_command,
    "/quiet_hours": quiet_hours_command,
}
CUSTOM = NotificationPrefs(
    user_id=0,
    mute=True,
    mute_until=datetime(2030, 1, 1, tzinfo=UTC),
    digest_mode=True,
    digest_interval_minutes=30,
    quiet_hours_start="23:00",
    quiet_hours_end="07:00",
    throttle_per_hour=5,
    timezone="Europe/Berlin",
)
FROZEN = "2026-03-01 12:00:00"
OTHER = 11


async def run_command(repo: Repository, user_id: int, words: list[str]) -> None:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.first_name = "U"
    update.effective_user.username = "u"
    update.effective_user.language_code = "en"
    update.message.reply_text = AsyncMock()
    context = make_context(repo)
    context.args = words[1:]
    await COMMANDS[words[0]](update, context)


async def global_row(repo: Repository, user_id: int) -> NotificationPrefs | None:
    row = await repo.get_notification_prefs(user_id=user_id, product_id=None)
    return None if row is None else dataclasses.replace(row, user_id=0, updated_at=None)


async def row_count(repo: Repository, user_id: int) -> int:
    cursor = await repo._conn.execute(
        "SELECT COUNT(*) FROM notification_prefs WHERE user_id = ? AND product_id IS NULL",
        (user_id,),
    )
    row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.parametrize("existing", [False, True], ids=["no-row", "row"])
@pytest.mark.parametrize(("wire", "words"), CASES, ids=[wire for wire, _ in CASES])
async def test_button_writes_what_the_command_writes(
    repo: Repository, wire: str, words: list[str], existing: bool
) -> None:
    await repo.ensure_user(OTHER)
    if existing:
        for user_id in (USER, OTHER):
            await repo.upsert_notification_prefs(dataclasses.replace(CUSTOM, user_id=user_id))
    with freeze_time(FROZEN, real_asyncio=True):
        await press(repo, wire)
        await run_command(repo, OTHER, words)
    button_row = await global_row(repo, USER)
    assert button_row is not None
    assert button_row == await global_row(repo, OTHER)


@pytest.mark.parametrize(("wire", "_words"), CASES, ids=[wire for wire, _ in CASES])
async def test_pressing_twice_leaves_one_row_with_the_same_content(
    repo: Repository, wire: str, _words: list[str]
) -> None:
    with freeze_time(FROZEN, real_asyncio=True):
        await press(repo, wire)
        first = await global_row(repo, USER)
        await press(repo, wire)
    assert await global_row(repo, USER) == first
    assert await row_count(repo, USER) == 1


async def test_setting_mute_keeps_the_quiet_hours(repo: Repository) -> None:
    await press(repo, "s:qh:2208")
    await press(repo, "s:mu:8")
    row = await global_row(repo, USER)
    assert row is not None
    assert (row.quiet_hours_start, row.quiet_hours_end, row.mute) == ("22:00", "08:00", True)


async def test_a_button_acts_on_the_presser_only(repo: Repository) -> None:
    await repo.ensure_user(OTHER)
    await press(repo, "s:mu:0")
    assert await global_row(repo, OTHER) is None


# --- T3-12 any order of presses ends where an independent model says ---------

WIRES = [wire for wire, _ in CASES]


def model_step(state: dict[str, object], wire: str) -> None:
    """The expected effect of one press, written from the spec table, field by field."""
    _, section, value = wire.split(":")
    if section == "mu":
        if value == "off":
            state.update(mute=False, mute_until=None)
        elif value == "0":
            state.update(mute=True, mute_until=None)
        else:
            state.update(
                mute=True,
                mute_until=datetime(2026, 3, 1, 12, tzinfo=UTC) + timedelta(hours=int(value)),
            )
    elif section == "dg":
        state.update(digest_mode=value == "on", digest_interval_minutes=60)
    elif value == "off":
        state.update(quiet_hours_start=None, quiet_hours_end=None)
    else:
        state.update(quiet_hours_start="22:00", quiet_hours_end="08:00")


async def run_sequence(wires: list[str]) -> tuple[NotificationPrefs | None, int]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repository = Repository(conn)
    await repository.ensure_user(USER)
    try:
        with freeze_time(FROZEN, real_asyncio=True):
            for wire in wires:
                await press(repository, wire)
        return await global_row(repository, USER), await row_count(repository, USER)
    finally:
        await conn.close()


@settings(max_examples=60, deadline=None)
@given(st.lists(st.sampled_from(WIRES), min_size=1, max_size=12))
def test_any_sequence_of_presses_matches_the_model(wires: list[str]) -> None:
    state: dict[str, object] = dataclasses.asdict(NotificationPrefs(user_id=0))
    for wire in wires:
        model_step(state, wire)
    row, count = asyncio.run(run_sequence(wires))
    assert count == 1
    assert row is not None
    assert dataclasses.asdict(row) == {**state, "updated_at": None}


# --- the list: pages, cards in place, stale buttons ---------------------------

OTHER_USER = 12
ADMIN = 13
NAMES = [f"Item {i}" for i in range(1, 13)]


async def add_product(repo: Repository, user_id: int, name: str) -> int:
    return await repo.add_product(
        user_id=user_id,
        url=f"https://shop.example.com/{name.replace(' ', '-')}",
        name=name,
        domain="shop.example.com",
        initial_price=Decimal("10"),
        currency="EUR",
    )


@pytest_asyncio.fixture
async def twelve(repo: Repository) -> list[int]:
    return [await add_product(repo, USER, name) for name in NAMES]


async def render_list(repo: Repository, list_filter: ListFilter, page: int) -> Screen:
    records = await repo.get_all_products(USER)
    set_locale("en")
    return list_page(list_view(records, list_filter, page, default_interval_minutes=360))


def shown(query: MagicMock) -> tuple[str, Any]:
    """The text and keyboard of the last edit of ``query``."""
    call = query.edit_message_text.await_args
    return call.args[0], call.kwargs["reply_markup"]


def assert_shows(query: MagicMock, screen: Screen) -> None:
    text, markup = shown(query)
    assert text == screen.text
    assert markup == screen_markup(screen)


async def test_walking_the_pages_renders_each_one_and_keeps_no_state(
    repo: Repository, twelve: list[int]
) -> None:
    context = make_context(repo)
    bot_data = dict(context.bot_data)
    for page in (1, 2, 3):
        query = make_query(f"l:a:{page}")
        await handle_callback(make_update(query), context)
        assert_shows(query, await render_list(repo, "a", page))
    assert context.user_data == {}
    assert context.bot_data == bot_data


async def test_a_card_opened_from_the_list_returns_to_the_same_page_and_filter(
    repo: Repository, twelve: list[int]
) -> None:
    for pid in twelve[:3]:
        await repo.increment_errors(pid)
    query = await press(repo, f"l:e:2:{twelve[1]}")
    _, markup = shown(query)
    back = [b for row in markup.inline_keyboard for b in row if b.text == "◀️ List"]
    assert [decode(b.callback_data) for b in back] == [Action("list.page", ("e", 2))]
    returned = await press(repo, back[0].callback_data)
    assert_shows(returned, await render_list(repo, "e", 2))


@pytest.mark.parametrize("viewer", [USER, ADMIN], ids=["user", "admin"])
async def test_a_product_of_another_user_is_not_found_and_not_revealed(
    repo: Repository, monkeypatch: pytest.MonkeyPatch, viewer: int
) -> None:
    await repo.ensure_user(OTHER_USER)
    await repo.ensure_user(ADMIN, is_admin=True)
    await add_product(repo, viewer, "Mine")
    foreign = await add_product(repo, OTHER_USER, "Secret Fan")
    asked: list[tuple[int, int]] = []
    original = repo.get_product_for_user

    async def spy(product_id: int, user_id: int) -> Any:
        asked.append((product_id, user_id))
        return await original(product_id, user_id)

    monkeypatch.setattr(repo, "get_product_for_user", spy)
    query = make_query(f"l:a:1:{foreign}", user_id=viewer)
    await handle_callback(make_update(query), make_context(repo))
    text, markup = shown(query)
    assert asked == [(foreign, viewer)]
    assert text.startswith("Product not found.")
    assert "Secret Fan" not in text
    assert f"check_{foreign}" not in {b.callback_data for r in markup.inline_keyboard for b in r}
    assert "Mine" in text


async def test_a_product_deleted_after_the_page_was_drawn_gives_a_notice_and_the_page(
    repo: Repository, twelve: list[int]
) -> None:
    await repo.delete_product(twelve[0], user_id=USER)
    query = await press(repo, f"l:a:1:{twelve[0]}")
    text, _ = shown(query)
    expected = await render_list(repo, "a", 1)
    assert text == f"Product not found.\n\n{expected.text}"


@pytest.mark.parametrize("asked", [4, ID_MAX])
async def test_a_page_past_the_end_is_clamped_to_the_last(
    repo: Repository, twelve: list[int], asked: int
) -> None:
    query = await press(repo, f"l:a:{asked}")
    assert_shows(query, await render_list(repo, "a", 3))
    assert "page 3/3" in shown(query)[0]


async def test_a_filter_that_emptied_says_so(repo: Repository) -> None:
    only = await add_product(repo, USER, "Kettle")
    await repo.pause_product(only)
    query = await press(repo, "l:a:1")
    text, _ = shown(query)
    assert text.endswith("Nothing here.")


async def test_the_sold_out_filter_lists_only_active_sold_out_products(repo: Repository) -> None:
    await add_product(repo, USER, "Kettle")
    gone = await add_product(repo, USER, "Toaster")
    paused_gone = await add_product(repo, USER, "Fan")
    await repo.set_availability(gone, available=False)
    await repo.set_availability(paused_gone, available=False)
    await repo.pause_product(paused_gone)
    query = await press(repo, "l:o:1")
    text, markup = shown(query)
    assert_shows(query, await render_list(repo, "o", 1))
    assert "Toaster" in text
    assert "Kettle" not in text
    assert "Fan" not in text
    opens = [b.callback_data for r in markup.inline_keyboard for b in r if b.text.startswith("#")]
    assert opens == [f"l:o:1:{gone}"]


async def test_an_empty_sold_out_filter_says_so(repo: Repository) -> None:
    await add_product(repo, USER, "Kettle")
    text, _ = shown(await press(repo, "l:o:1"))
    assert text.endswith("Nothing here.")


async def test_nothing_left_shows_the_empty_text_and_home(repo: Repository) -> None:
    query = await press(repo, "l:a:1")
    text, markup = shown(query)
    assert "Non hai prodotti tracciati" in text
    assert [b.callback_data for r in markup.inline_keyboard for b in r] == ["h"]


async def test_pressing_pages_out_of_order_always_renders_the_page_asked(
    repo: Repository, twelve: list[int]
) -> None:
    for page in (3, 1, 3):
        query = await press(repo, f"l:a:{page}")
        assert_shows(query, await render_list(repo, "a", page))


async def test_a_double_tap_is_two_edits_and_the_second_failure_is_ignored(
    repo: Repository, twelve: list[int]
) -> None:
    context = make_context(repo)
    first = make_query("l:a:2")
    second = make_query("l:a:2")
    second.edit_message_text = AsyncMock(side_effect=BadRequest("Message is not modified"))
    await handle_callback(make_update(first), context)
    await handle_callback(make_update(second), context)
    first.edit_message_text.assert_awaited_once()
    second.edit_message_text.assert_awaited_once()
    assert_shows(first, await render_list(repo, "a", 2))


@pytest.mark.parametrize(
    ("error", "levels"),
    [
        (BadRequest("Message is not modified: the same"), []),
        (BadRequest("Message to edit not found"), [logging.WARNING]),
        (RetryAfter(3), [logging.WARNING]),
    ],
    ids=["not-modified", "not-found", "retry-after"],
)
async def test_the_list_tolerates_what_telegram_answers_to_an_edit(
    repo: Repository,
    twelve: list[int],
    caplog: pytest.LogCaptureFixture,
    error: Exception,
    levels: list[int],
) -> None:
    query = make_query("l:a:1")
    query.edit_message_text = AsyncMock(side_effect=error)
    with caplog.at_level(logging.WARNING):
        await handle_callback(make_update(query), make_context(repo))
    query.edit_message_text.assert_awaited_once()
    query.message.reply_text.assert_not_called()
    assert [record.levelno for record in caplog.records] == levels


# --- Home ----------------------------------------------------------------------


def make_command_update(user_id: int) -> MagicMock:
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.language_code = "en"
    update.effective_user.first_name = "U"
    update.effective_user.username = "u"
    update.message.reply_text = AsyncMock()
    return update


async def home_of(repo: Repository, user_id: int) -> list[tuple[str, Any]]:
    """What /menu, ``h`` and ``menu_main`` show to ``user_id``."""
    context = make_context(repo)
    shown_by: list[tuple[str, Any]] = []
    update = make_command_update(user_id)
    await cmd_menu(update, context)
    call = update.message.reply_text.await_args
    shown_by.append((call.args[0], call.kwargs["reply_markup"]))
    for data in ("h", "menu_main"):
        query = make_query(data, user_id=user_id)
        await handle_callback(make_update(query), context)
        shown_by.append(shown(query))
    return shown_by


async def test_every_way_to_the_home_shows_the_same_screen(repo: Repository) -> None:
    await add_product(repo, USER, "Kettle")
    paused = await add_product(repo, USER, "Fan")
    await add_product(repo, USER, "Lamp")
    await repo.pause_product(paused)
    screens = await home_of(repo, USER)
    assert len({text for text, _ in screens}) == 1
    assert len({str(markup) for _, markup in screens}) == 1
    set_locale("en")
    expected = home_screen(HomeView(active=2, paused=1, is_admin=False))
    assert screens[0][0] == expected.text
    assert screens[0][1] == screen_markup(expected)


@pytest.mark.parametrize(("user_id", "admin"), [(USER, False), (ADMIN, True)])
async def test_the_admin_button_is_only_for_admins_on_every_path(
    repo: Repository, user_id: int, admin: bool
) -> None:
    await repo.ensure_user(ADMIN, is_admin=True)
    for _text, markup in await home_of(repo, user_id):
        wires = {b.callback_data for row in markup.inline_keyboard for b in row}
        assert ("menu_admin" in wires) is admin


# --- the language section: the choice is stored and the section redrawn in it ---


async def stored_language(repo: Repository, user_id: int = USER) -> str | None:
    row = await repo.get_user(user_id)
    assert row is not None
    return row.language


def edited_text(query: MagicMock) -> str:
    text, _ = shown(query)
    return str(text)


async def test_choosing_italian_stores_it_and_redraws_the_section_in_italian(
    repo: Repository,
) -> None:
    query = await press(repo, "s:lang:it")
    assert await stored_language(repo) == "it"
    text = edited_text(query)
    assert text.startswith("🗣 <b>Lingua</b>")
    assert text.endswith("Attuale: Italiano")
    set_locale("en")


async def test_choosing_automatic_clears_the_choice(repo: Repository) -> None:
    await repo.set_user_language(USER, "it")
    query = await press(repo, "s:lang:auto")
    assert await stored_language(repo) is None
    assert edited_text(query).endswith("Current: Automatic (English)")


async def test_a_registered_language_without_a_catalogue_writes_nothing_and_redraws(
    repo: Repository,
) -> None:
    await repo.set_user_language(USER, "it")
    query = await press(repo, "s:lang:de")
    assert await stored_language(repo) == "it"
    query.edit_message_text.assert_awaited_once()
    assert edited_text(query).endswith("Attuale: Italiano")
    set_locale("en")


async def test_the_render_language_does_not_leak_past_the_press(repo: Repository) -> None:
    set_locale("en")
    await press(repo, "s:lang:it")
    await press(repo, "s:lang:auto")
    assert edited_text(await press(repo, "s")).startswith("⚙️ <b>Settings</b>")


@pytest.mark.parametrize(
    "wires",
    [
        ["s:lang:it", "s:lang:it"],
        ["s:lang:en", "s:lang:it", "s:lang:auto", "s:lang:it"],
        ["s:lang:it", "s:lang", "s:lang:de", "s"],
    ],
)
async def test_double_and_out_of_order_taps_end_on_the_last_written_choice(
    repo: Repository, wires: list[str]
) -> None:
    for wire in wires:
        query = await press(repo, wire)
        query.edit_message_text.assert_awaited_once()
    assert await stored_language(repo) == "it"
    set_locale("en")


async def test_the_overview_and_the_section_show_the_stored_choice(repo: Repository) -> None:
    await repo.set_user_language(USER, "en")
    assert "🗣 Language: English" in edited_text(await press(repo, "s"))
    section = edited_text(await press(repo, "s:lang"))
    assert section.endswith("Current: English")


async def test_a_user_who_is_not_allowed_changes_nothing(repo: Repository) -> None:
    stranger = 404
    query = make_query("s:lang:it", user_id=stranger)
    await handle_callback(make_update(query), make_context(repo))
    query.edit_message_text.assert_not_called()
    assert await repo.get_user(stranger) is None
    assert await stored_language(repo) is None
