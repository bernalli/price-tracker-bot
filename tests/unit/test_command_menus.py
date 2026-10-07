"""Per-language, per-role command menus and the startup reconciliation of admins."""

from __future__ import annotations

import json
import logging
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
import pytest_asyncio
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


def _menu_calls(request: FakeRequest) -> list[dict[str, Any]]:
    return [
        {"method": call.method, **call.params}
        for call in request.calls
        if call.method in {"setMyCommands", "deleteMyCommands"}
    ]


def _key(call: dict[str, Any]) -> Key:
    scope = call["scope"]
    scope = json.loads(scope) if isinstance(scope, str) else scope
    return (call["method"], scope["type"], scope.get("chat_id"), call.get("language_code"))


def _names(call: dict[str, Any]) -> set[str]:
    commands = call["commands"]
    commands = json.loads(commands) if isinstance(commands, str) else commands
    return {entry["command"] for entry in commands}


async def _sync(
    request: FakeRequest, repo: Repository, user_id: int | None = None
) -> list[dict[str, Any]]:
    bot = make_application(request).bot
    await sync_command_menus(bot, repo, user_id)
    return _menu_calls(request)


async def _seed(conn: aiosqlite.Connection) -> Repository:
    repo = Repository(conn)
    await repo.add_user(1, is_admin=True)
    await repo.add_user(2, is_admin=True)
    await repo.remove_user(2)
    await repo.add_user(3)
    return repo


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
            ("setMyCommands", "chat", 1, None),
            ("setMyCommands", "chat", 1, "it"),
            ("deleteMyCommands", "chat", 2, None),
            ("deleteMyCommands", "chat", 2, "it"),
            ("deleteMyCommands", "chat", 3, None),
            ("deleteMyCommands", "chat", 3, "it"),
        ]
    )
    assert Counter(_key(call) for call in calls) == expected


@pytest.mark.asyncio
async def test_only_the_active_admin_receives_the_admin_commands(
    conn: aiosqlite.Connection, request_log: FakeRequest
) -> None:
    repo = await _seed(conn)
    for call in await _sync(request_log, repo):
        if call["method"] != "setMyCommands":
            continue
        scope = _key(call)[1:3]
        names = _names(call)
        if scope == ("chat", 1):
            assert names >= ADMIN_NAMES
        else:
            assert not ADMIN_NAMES & names
            assert names, "the global list is never empty"


def test_menu_commands_follow_the_language_and_leave_aliases_out() -> None:
    english = menu_commands("en", admin=False)
    italian = menu_commands("it", admin=False)
    assert [c.command for c in english] == [c.command for c in italian]
    assert [c.description for c in english] != [c.description for c in italian]
    assert "lista" not in {c.command for c in english}
    full = menu_commands("en", admin=True)
    assert {c.command for c in full} == {c.command for c in english} | ADMIN_NAMES
    assert not ADMIN_NAMES & {c.command for c in english}


@pytest.mark.asyncio
async def test_one_user_sync_touches_only_that_chat(
    conn: aiosqlite.Connection, request_log: FakeRequest
) -> None:
    repo = await _seed(conn)
    calls = await _sync(request_log, repo, 2)
    assert Counter(_key(call) for call in calls) == Counter(
        [("deleteMyCommands", "chat", 2, None), ("deleteMyCommands", "chat", 2, "it")]
    )
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
    assert request_log.calls_of("setMyCommands") == []


@pytest.mark.asyncio
async def test_no_users_means_only_the_global_lists(
    conn: aiosqlite.Connection, request_log: FakeRequest
) -> None:
    calls = await _sync(request_log, Repository(conn))
    assert Counter(_key(call) for call in calls) == Counter(
        [("setMyCommands", "default", None, None), ("setMyCommands", "default", None, "it")]
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


@pytest.mark.asyncio
async def test_a_telegram_error_does_not_stop_the_other_calls(
    conn: aiosqlite.Connection, request_log: FakeRequest, caplog: pytest.LogCaptureFixture
) -> None:
    repo = await _seed(conn)
    bot = make_application(request_log).bot
    real = type(bot).set_my_commands
    attempts: list[Any] = []

    async def flaky(self: Any, *args: Any, **kwargs: Any) -> Any:
        attempts.append(kwargs.get("scope"))
        if len(attempts) == 1:
            raise NetworkError("down")
        return await real(self, *args, **kwargs)

    with (
        patch.object(type(bot), "set_my_commands", flaky),
        caplog.at_level(logging.WARNING),
    ):
        await sync_command_menus(bot, repo)
    assert len(attempts) == 4
    assert len(request_log.calls_of("deleteMyCommands")) == 4
    assert any(record.levelno == logging.WARNING for record in caplog.records)


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
        ):
            await post_init(application)
        assert failing.await_count >= 2
        assert "digest_service" in application.bot_data
    finally:
        client = application.bot_data.get("http_client")
        if client is not None:
            await client.aclose()
        await db_conn.close()
