"""Navigation callbacks: the decode-first hook, the closed dispatch table and the
settings actions, against a real in-memory repository and a mocked Telegram query.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
from price_tracker.bot.callbacks import (
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
from price_tracker.bot.handlers.callbacks import _nav, handle_callback
from price_tracker.bot.handlers.settings import (
    digest_mode_command,
    mute_command,
    quiet_hours_command,
    unmute_command,
)
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.models import NotificationPrefs
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

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
    query.answer = AsyncMock()
    query.edit_message_text = AsyncMock()
    return query


def make_context(db: Any) -> MagicMock:
    context = MagicMock()
    context.bot_data = {"db": db, "repository": db}
    context.user_data = {}
    return context


def make_update(query: MagicMock) -> MagicMock:
    update = MagicMock()
    update.callback_query = query
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
    db.is_user_allowed = AsyncMock(return_value=False)
    handler = AsyncMock(return_value=True)
    monkeypatch.setattr(_nav, "handle_action", handler)
    query = make_query(encode(Action("settings")))
    await handle_callback(make_update(query), make_context(db))
    assert db.method_calls == [call.is_user_allowed(USER)]
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
