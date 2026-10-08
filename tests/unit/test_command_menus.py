"""The short Telegram command menu and the startup reconciliation of admins."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
import pytest_asyncio
from hypothesis import given, settings
from hypothesis import strategies as st
from structlog.testing import capture_logs
from telegram.error import NetworkError

from price_tracker.bot.command_menus import menu_commands, sync_command_menus
from price_tracker.bot.commands import COMMANDS
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from price_tracker.main import reconcile_admins
from tests.support.fake_telegram import FakeRequest, make_application

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

MIGRATIONS = Path(__file__).resolve().parents[2] / "src/price_tracker/db/migrations"
ADMIN_NAMES = {spec.name for spec in COMMANDS if spec.admin}
Key = tuple[str, str, int | None, str | None]


@pytest_asyncio.fixture
async def conn() -> AsyncIterator[aiosqlite.Connection]:
    async with aiosqlite.connect(":memory:") as connection:
        connection.row_factory = aiosqlite.Row
        await apply_migrations(connection, MIGRATIONS)
        yield connection


@pytest.fixture
def request_log() -> FakeRequest:
    return FakeRequest()


MENU_METHODS = {"setMyCommands", "deleteMyCommands", "setChatMenuButton"}
LANGUAGES: tuple[str | None, ...] = (None, "it")
ENGLISH_MENU = [
    ("menu", "Open the main menu"),
    ("list", "List your products"),
    ("checkall", "Check all your products now"),
    ("status", "Show your statistics"),
    ("help", "Show every command"),
]
ITALIAN_MENU = [
    ("menu", "Apri il menu principale"),
    ("list", "Elenca i tuoi prodotti"),
    ("checkall", "Controlla subito tutti i tuoi prodotti"),
    ("status", "Mostra le tue statistiche"),
    ("help", "Mostra tutti i comandi"),
]
SHORT_NAMES = [name for name, _ in ENGLISH_MENU]
USER_NAMES_1_5 = [spec.name for spec in COMMANDS if not spec.admin]
ADMIN_LIST_1_5 = [spec.name for spec in COMMANDS]
# Users 1-4 below: two default lists, the menu button, two deletions per user.
FULL_SYNC_CALLS = 2 + 1 + 2 * 4


def _decode(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _menu_calls(request: FakeRequest) -> list[dict[str, Any]]:
    return [
        {"method": call.method, **call.params}
        for call in request.calls
        if call.method in MENU_METHODS and not call.failed
    ]


def _key(call: dict[str, Any]) -> Key:
    if call["method"] == "setChatMenuButton":
        return (call["method"], "button", call.get("chat_id"), None)
    scope = _decode(call["scope"])
    return (call["method"], scope["type"], scope.get("chat_id"), call.get("language_code"))


def _pairs(call: dict[str, Any]) -> list[tuple[str, str]]:
    return [(entry["command"], entry["description"]) for entry in _decode(call["commands"])]


async def _sync(
    request: FakeRequest, repo: Repository, user_id: int | None = None
) -> list[dict[str, Any]]:
    bot = make_application(request).bot
    await sync_command_menus(bot, repo, user_id)
    return _menu_calls(request)


async def _seed(conn: aiosqlite.Connection) -> Repository:
    """1 active admin, 2 demoted admin, 3 removed (inactive), 4 plain user."""
    repo = Repository(conn)
    await repo.add_user(1, is_admin=True)
    await repo.add_user(2, is_admin=True)
    await repo.add_user(3)
    await repo.remove_user(3)
    await repo.add_user(4)
    await reconcile_admins(repo, (1,))
    return repo


class _CommandStore:
    """Telegram's stored command lists, resolved for a private chat as the Bot API does.

    Chat scope before Default; at each level the user's language before the
    no-language fallback; the first list found wins, lists are never merged.
    """

    def __init__(self) -> None:
        self.lists: dict[tuple[str, int | None, str | None], list[str]] = {}

    @classmethod
    def as_of_1_5(cls, admins: tuple[int, ...]) -> _CommandStore:
        store = cls()
        for code in LANGUAGES:
            store.lists[("default", None, code)] = list(USER_NAMES_1_5)
            for uid in admins:
                store.lists[("chat", uid, code)] = list(ADMIN_LIST_1_5)
        return store

    def apply(self, request: FakeRequest) -> None:
        for call in _menu_calls(request):
            method, kind, chat_id, code = _key(call)
            if method == "setMyCommands":
                self.lists[(kind, chat_id, code)] = [name for name, _ in _pairs(call)]
            elif method == "deleteMyCommands":
                self.lists.pop((kind, chat_id, code), None)

    def effective(self, chat_id: int, code: str | None) -> list[str]:
        for key in (
            ("chat", chat_id, code),
            ("chat", chat_id, None),
            ("default", None, code),
            ("default", None, None),
        ):
            if key in self.lists:
                return self.lists[key]
        return []


def test_menu_commands_are_the_five_short_entries_in_each_language() -> None:
    assert [(c.command, c.description) for c in menu_commands("en")] == ENGLISH_MENU
    assert [(c.command, c.description) for c in menu_commands("it")] == ITALIAN_MENU


def test_the_short_menu_names_registered_user_commands_once() -> None:
    from price_tracker.bot.commands import MENU_COMMANDS

    by_name = {spec.name: spec for spec in COMMANDS}
    assert list(MENU_COMMANDS) == SHORT_NAMES
    assert len(set(MENU_COMMANDS)) == len(MENU_COMMANDS)
    assert all(name in by_name and not by_name[name].admin for name in MENU_COMMANDS)


@pytest.mark.asyncio
async def test_full_sync_sends_the_exact_set_of_calls(
    conn: aiosqlite.Connection, request_log: FakeRequest
) -> None:
    repo = await _seed(conn)
    calls = await _sync(request_log, repo)
    expected = Counter(
        [
            ("setMyCommands", "default", None, None),
            ("setMyCommands", "default", None, "it"),
            ("setChatMenuButton", "button", None, None),
            *(
                ("deleteMyCommands", "chat", uid, code)
                for uid in (1, 2, 3, 4)
                for code in LANGUAGES
            ),
        ]
    )
    assert Counter(_key(call) for call in calls) == expected
    assert len(calls) == FULL_SYNC_CALLS
    methods = [call["method"] for call in calls]
    assert methods[:3] == ["setMyCommands", "setMyCommands", "setChatMenuButton"]
    assert set(methods[3:]) == {"deleteMyCommands"}
    lists = {
        call.get("language_code"): _pairs(call)
        for call in calls
        if call["method"] == "setMyCommands"
    }
    assert lists == {None: ENGLISH_MENU, "it": ITALIAN_MENU}
    (button,) = [call for call in calls if call["method"] == "setChatMenuButton"]
    assert "chat_id" not in button
    assert _decode(button["menu_button"])["type"] == "commands"
    for call in calls:
        if call["method"] == "setMyCommands":
            assert not ADMIN_NAMES & {name for name, _ in _pairs(call)}


@pytest.mark.asyncio
async def test_after_a_sync_every_known_and_unknown_user_sees_the_five_commands(
    conn: aiosqlite.Connection, request_log: FakeRequest
) -> None:
    repo = await _seed(conn)
    await conn.execute("INSERT INTO users(user_id, is_admin, is_active) VALUES (9, 2, 2)")
    await conn.commit()
    # 3 is inactive but still carries a per-chat list (e.g. deactivated outside /removeuser).
    store = _CommandStore.as_of_1_5(admins=(1, 2, 3, 9))
    assert store.effective(1, None) == ADMIN_LIST_1_5
    assert store.effective(4, "it") == USER_NAMES_1_5

    await _sync(request_log, repo)
    store.apply(request_log)

    for uid in (1, 2, 3, 4, 9, 99):
        for code in LANGUAGES:
            assert store.effective(uid, code) == SHORT_NAMES, (uid, code)
    after_one = dict(store.lists)
    second = FakeRequest()
    await _sync(second, repo)
    store.apply(second)
    assert store.lists == after_one


_FLAG = st.sampled_from((0, 1, 2))


@settings(max_examples=30, deadline=None)
@given(
    users=st.dictionaries(
        st.integers(min_value=1, max_value=50),
        st.tuples(_FLAG, _FLAG, st.booleans()),
        max_size=6,
    )
)
def test_a_sync_migrates_any_stored_users_to_the_short_menu(
    users: dict[int, tuple[int, int, bool]],
) -> None:
    """Whatever the flags and the 1.5 per-chat lists, every chat ends on the five commands."""

    async def scenario() -> _CommandStore:
        async with aiosqlite.connect(":memory:") as connection:
            connection.row_factory = aiosqlite.Row
            await apply_migrations(connection, MIGRATIONS)
            for uid, (admin, active, _) in users.items():
                await connection.execute(
                    "INSERT INTO users(user_id, is_admin, is_active) VALUES (?, ?, ?)",
                    (uid, admin, active),
                )
            await connection.commit()
            had_admin_list = tuple(uid for uid, (_, _, listed) in users.items() if listed)
            store = _CommandStore.as_of_1_5(admins=had_admin_list)
            request = FakeRequest()
            await _sync(request, Repository(connection))
            store.apply(request)
            return store

    store = asyncio.run(scenario())
    for uid in [*users, 999]:
        for code in LANGUAGES:
            assert store.effective(uid, code) == SHORT_NAMES


@pytest.mark.asyncio
async def test_one_user_sync_touches_only_that_chat(
    conn: aiosqlite.Connection, request_log: FakeRequest
) -> None:
    repo = await _seed(conn)
    calls = await _sync(request_log, repo, 2)
    assert Counter(_key(call) for call in calls) == Counter(
        [("deleteMyCommands", "chat", 2, None), ("deleteMyCommands", "chat", 2, "it")]
    )
    calls = await _sync(FakeRequest(), repo, 1)
    assert {call["method"] for call in calls} == {"deleteMyCommands"}
    calls = await _sync(FakeRequest(), repo, 77)
    assert {_key(call)[2] for call in calls} == {77}
    assert {call["method"] for call in calls} == {"deleteMyCommands"}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, True, "5", 1.0])
async def test_a_malformed_user_id_is_refused(
    conn: aiosqlite.Connection, request_log: FakeRequest, bad: Any
) -> None:
    with pytest.raises(ValueError, match="user_id"):
        await _sync(request_log, Repository(conn), bad)
    assert _menu_calls(request_log) == []


@pytest.mark.asyncio
async def test_no_users_means_only_the_global_lists_and_the_button(
    conn: aiosqlite.Connection, request_log: FakeRequest
) -> None:
    calls = await _sync(request_log, Repository(conn))
    assert Counter(_key(call) for call in calls) == Counter(
        [
            ("setMyCommands", "default", None, None),
            ("setMyCommands", "default", None, "it"),
            ("setChatMenuButton", "button", None, None),
        ]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [2, -1, "x"])
async def test_a_damaged_flag_row_gets_no_admin_menu(
    conn: aiosqlite.Connection, request_log: FakeRequest, value: object
) -> None:
    await conn.execute(
        "INSERT INTO users(user_id, is_admin, is_active) VALUES (9, ?, ?)", (value, value)
    )
    await conn.commit()
    calls = await _sync(request_log, Repository(conn), 9)
    assert {call["method"] for call in calls} == {"deleteMyCommands"}


class _FailingNthRequest(FakeRequest):
    """Raises a network error on the ``fail_at``-th command-menu call, answers the rest."""

    def __init__(self, fail_at: int) -> None:
        super().__init__()
        self.fail_at = fail_at
        self.menu_attempts = 0

    async def do_request(self, url: str, method: str, *args: Any, **kwargs: Any) -> Any:
        if url.rsplit("/", 1)[-1] in MENU_METHODS:
            self.menu_attempts += 1
            if self.menu_attempts == self.fail_at:
                raise NetworkError("down")
        return await super().do_request(url, method, *args, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", range(1, FULL_SYNC_CALLS + 1))
async def test_a_telegram_error_does_not_stop_the_other_calls_and_the_next_sync_recovers(
    conn: aiosqlite.Connection, caplog: pytest.LogCaptureFixture, fail_at: int
) -> None:
    repo = await _seed(conn)
    store = _CommandStore.as_of_1_5(admins=(1, 2))
    flaky = _FailingNthRequest(fail_at)
    with caplog.at_level(logging.WARNING):
        await _sync(flaky, repo)
    assert flaky.menu_attempts == FULL_SYNC_CALLS
    assert len(_menu_calls(flaky)) == FULL_SYNC_CALLS - 1
    assert any(
        record.levelno == logging.WARNING and "command menu update failed" in record.getMessage()
        for record in caplog.records
    )
    store.apply(flaky)
    healthy = FakeRequest()
    await _sync(healthy, repo)
    store.apply(healthy)
    for uid in (1, 2, 3, 4):
        for code in LANGUAGES:
            assert store.effective(uid, code) == SHORT_NAMES


async def _reconcile_state(repo: Repository) -> dict[int, tuple[bool, bool]]:
    return {u.user_id: (u.is_admin, u.is_active) for u in await repo.list_users()}


@pytest.mark.asyncio
async def test_an_admin_dropped_from_the_configuration_is_demoted_and_stays_active(
    conn: aiosqlite.Connection,
) -> None:
    repo = Repository(conn)
    await repo.add_user(1, is_admin=True)
    await repo.add_user(2, is_admin=True)
    await reconcile_admins(repo, (2, 5))
    assert await _reconcile_state(repo) == {1: (False, True), 2: (True, True), 5: (True, True)}
    assert await repo.is_user_admin(1) is False


@pytest.mark.asyncio
async def test_an_empty_configuration_touches_no_admin_and_warns(
    conn: aiosqlite.Connection,
) -> None:
    repo = Repository(conn)
    await repo.add_user(1, is_admin=True)
    with capture_logs() as logs:
        await reconcile_admins(repo, ())
    assert await _reconcile_state(repo) == {1: (True, True)}
    assert [entry["log_level"] for entry in logs] == ["warning"]


@pytest.mark.asyncio
async def test_a_demoted_admin_loses_the_menu_and_the_authorization(
    conn: aiosqlite.Connection, request_log: FakeRequest
) -> None:
    repo = Repository(conn)
    await repo.add_user(1, is_admin=True)
    await repo.add_user(2, is_admin=True)
    await reconcile_admins(repo, (2,))
    calls = await _sync(request_log, repo, 1)
    assert Counter(_key(call) for call in calls) == Counter(
        [("deleteMyCommands", "chat", 1, None), ("deleteMyCommands", "chat", 1, "it")]
    )
    assert await repo.is_user_admin(1) is False
    assert await repo.is_user_allowed(1) is True


@pytest.mark.asyncio
async def test_reconcile_is_idempotent(conn: aiosqlite.Connection) -> None:
    repo = Repository(conn)
    await repo.add_user(1, is_admin=True)
    await reconcile_admins(repo, (1, 2))
    first = await _reconcile_state(repo)
    await reconcile_admins(repo, (1, 2))
    assert await _reconcile_state(repo) == first


@pytest.mark.asyncio
async def test_startup_completes_when_every_menu_call_fails(tmp_path: Path) -> None:
    from price_tracker.config import Config
    from price_tracker.core.registry import ScraperRegistry
    from price_tracker.main import post_init

    config = Config(
        telegram_bot_token="123456:TEST-TOKEN",  # noqa: S106 — test fixture
        admin_users=(1,),
        check_interval_minutes=360,
        database_path=str(tmp_path / "t.db"),
        default_threshold_type="percentage",
        default_threshold_value="10",
        max_consecutive_errors=10,
        check_delay_seconds=5.0,
        notification_cooldown_hours=24,
        request_timeout=30,
        log_level="INFO",
        lang="en",
        metrics_enabled=False,
    )
    db_conn = await aiosqlite.connect(config.database_path)
    db_conn.row_factory = aiosqlite.Row
    application = make_application(FakeRequest())
    application.bot_data["config"] = config
    application.bot_data["db_conn"] = db_conn
    application.bot_data["registry"] = ScraperRegistry()
    failing = AsyncMock(side_effect=NetworkError("down"))
    try:
        with (
            patch.object(type(application.bot), "set_my_commands", failing),
            patch.object(type(application.bot), "delete_my_commands", failing),
            patch.object(type(application.bot), "set_chat_menu_button", failing),
        ):
            await post_init(application)
        assert failing.await_count >= 2
        assert "digest_service" in application.bot_data
    finally:
        client = application.bot_data.get("http_client")
        if client is not None:
            await client.aclose()
        await db_conn.close()
