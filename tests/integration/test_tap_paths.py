"""Every command can be reached from the main menu by tapping buttons.

The table below is written by hand from the command list: for each command, the
buttons to tap after ``/menu``, in order. Each button must be on the keyboard of the
most recent message before it is pressed. A path ends on a prompt (a Cancel button),
on a screen with a way Home, on a written preference, or on the button that runs the
command. Screens that are new in the tree must also offer a way back.
"""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal

import pytest
from freezegun import freeze_time

from price_tracker.bot.commands import COMMANDS
from tests.support.legacy_harness import (
    ADMIN,
    FROZEN_NOW,
    OWNER,
    LegacyWorld,
    build_world,
    close_world,
    seed_product,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

End = Literal["visible", "panel", "prompt", "prefs"]

PROMPT_CANCEL: Final = re.compile(r"^p:[0-9a-f]{32}:x$")
CARD: Final = ("l:a:1", "l:a:1:{P}")
RULE: Final = (*CARD, "edit_{P}")
NOTIFICATIONS: Final = (*RULE, "p:{P}:pr")

# Commands reached without a button: /start and /menu open the menu itself.
NO_PATH: Final = frozenset({"start", "menu"})

PATHS: Final[dict[str, tuple[int, tuple[str, ...], End]]] = {
    "help": (OWNER, ("menu_info", "hp"), "panel"),
    "cancel": (OWNER, (*RULE, "track_threshold_{P}", "@cancel"), "visible"),
    "add": (OWNER, ("l:a:1", "ad"), "panel"),
    "list": (OWNER, ("l:a:1",), "panel"),
    "check": (OWNER, (*CARD, "check_{P}"), "visible"),
    "checkall": (OWNER, ("menu_prezzi", "menu_checkall"), "visible"),
    "refresh": (OWNER, (*CARD, "setrefresh_{P}"), "visible"),
    "pause": (OWNER, (*CARD, "pause_{P}"), "visible"),
    "reactivate": (OWNER, ("l:a:1", "l:p:1", "l:p:1:{Q}", "reactivate_{Q}"), "visible"),
    "delete": (OWNER, (*CARD, "remove_{P}"), "visible"),
    "history": (OWNER, (*CARD, "chart_{P}"), "visible"),
    "reset": (OWNER, (*RULE, "reset_{P}"), "visible"),
    "target": (OWNER, (*RULE, "track_target_{P}"), "prompt"),
    "threshold": (OWNER, (*RULE, "track_threshold_{P}"), "prompt"),
    "mute": (OWNER, ("s", "s:mu", "s:mu:24"), "prefs"),
    "unmute": (OWNER, ("s", "s:mu", "s:mu:off"), "prefs"),
    "digest_mode": (OWNER, ("s", "s:dg", "s:dg:on"), "prefs"),
    "quiet_hours": (OWNER, ("s", "s:qh", "s:qh:2208"), "prefs"),
    "timezone": (OWNER, ("s", "s:tz", "s:ask:tz"), "prompt"),
    "throttle": (OWNER, ("s", "s:th", "s:ask:th"), "prompt"),
    "prefs": (OWNER, ("s",), "panel"),
    "digest_now": (OWNER, ("s", "s:dg", "s:dn"), "visible"),
    "export": (OWNER, ("menu_dati", "menu_esporta"), "visible"),
    "import": (OWNER, ("menu_dati", "menu_importa_info"), "panel"),
    "status": (OWNER, ("menu_info",), "panel"),
    "errors": (OWNER, ("menu_info", "er"), "panel"),
    "adduser": (ADMIN, ("menu_admin", "menu_admin_adduser"), "visible"),
    "removeuser": (ADMIN, ("menu_admin", "menu_admin_removeuser"), "visible"),
    "users": (ADMIN, ("menu_admin", "menu_admin_users"), "visible"),
    "nick": (ADMIN, ("menu_admin", "menu_admin_nick"), "visible"),
    "setinterval": (ADMIN, ("menu_admin", "menu_admin_interval"), "visible"),
    "debug": (ADMIN, ("menu_admin", "menu_admin_debug"), "prompt"),
    "health": (ADMIN, ("menu_admin", "a:hl"), "panel"),
}

# The values a command takes as an argument, reached by tapping too.
VALUE_PATHS: Final[dict[str, tuple[int, tuple[str, ...], End]]] = {
    "mute for a typed time": (OWNER, ("s", "s:mu", "s:ask:mu"), "prompt"),
    "digest interval": (OWNER, ("s", "s:dg", "s:ask:dg"), "prompt"),
    "typed quiet hours": (OWNER, ("s", "s:qh", "s:ask:qh"), "prompt"),
    "notifications of one product": (OWNER, NOTIFICATIONS, "panel"),
    "mute one product": (OWNER, (*NOTIFICATIONS, "p:{P}:mu:8"), "prefs"),
    "unmute one product": (OWNER, (*NOTIFICATIONS, "p:{P}:mu:8", "p:{P}:mu:off"), "prefs"),
    "mute one product for a typed time": (OWNER, (*NOTIFICATIONS, "p:{P}:mua"), "prompt"),
}

# A screen new in the tree -> the button that goes back from it.
NEW_NODES: Final = {
    "s:tz": "s",
    "s:th": "s",
    "s:dn": "s",
    "p:{P}:pr": "p:{P}:c",
    "er": "menu_info",
    "a:hl": "menu_admin",
    "ad": "l:a:1",
}


def test_every_command_has_a_path_or_a_reason() -> None:
    assert set(PATHS) | NO_PATH == {spec.name for spec in COMMANDS}
    assert not set(PATHS) & NO_PATH


@contextlib.asynccontextmanager
async def _world(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[LegacyWorld, int, int]]:
    real_now = datetime.now(UTC)
    with freeze_time(FROZEN_NOW, real_asyncio=True):
        world = await build_world("it", monkeypatch=monkeypatch, real_now=real_now)
        try:
            active = await seed_product(
                world,
                OWNER,
                "https://shop.example.com/item/1",
                "Kettle",
                initial="100",
                current="80",
            )
            paused = await seed_product(
                world, OWNER, "https://shop.example.com/item/2", "Fan", initial="50", active=False
            )
            yield world, active, paused
        finally:
            await close_world(world)


def _latest(world: LegacyWorld, chat_id: int) -> tuple[int, list[str]]:
    """The most recent message of ``chat_id`` that carries a keyboard, and its buttons."""
    keyboards = {mid: data for mid, data in world.current_keyboards(chat_id).items() if data}
    assert keyboards, "no message with buttons"
    latest = max(keyboards)
    return latest, keyboards[latest]


async def _prefs_rows(world: LegacyWorld) -> int:
    cursor = await world.conn.execute("SELECT COUNT(*) FROM notification_prefs")
    row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _walk(
    world: LegacyWorld, user: int, steps: tuple[str, ...], end: End, ids: dict[str, int]
) -> None:
    await world.run_command(user, "/menu")
    wires = [step.format(**{k: str(v) for k, v in ids.items()}) for step in steps]
    news = {
        k.format(**{n: str(v) for n, v in ids.items()}): v.format(
            **{n: str(x) for n, x in ids.items()}
        )
        for k, v in NEW_NODES.items()
    }
    for index, wire in enumerate(wires):
        message_id, buttons = _latest(world, user)
        if wire == "@cancel":
            assert any(PROMPT_CANCEL.match(data) for data in buttons), buttons
            continue
        assert wire in buttons, (wire, buttons)
        last = index == len(wires) - 1
        if last and end == "visible":
            return
        await world.press_message(user, wire, message_id)
        if wire in news:
            _, shown = _latest(world, user)
            assert news[wire] in shown, (wire, shown)
    if end == "visible":
        return
    _, shown = _latest(world, user)
    if end == "prompt":
        assert any(PROMPT_CANCEL.match(data) for data in shown), shown
    elif end == "panel":
        assert "h" in shown or "menu_main" in shown, shown
    else:
        assert await _prefs_rows(world) == 1


@pytest.mark.parametrize("command", sorted(PATHS))
async def test_the_command_is_reached_by_tapping(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    user, steps, end = PATHS[command]
    async with _world(monkeypatch) as (world, active, paused):
        await _walk(world, user, steps, end, {"P": active, "Q": paused})
        assert world.errors == []
        assert world.faults == []


@pytest.mark.parametrize("name", sorted(VALUE_PATHS))
async def test_the_value_is_reached_by_tapping(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    user, steps, end = VALUE_PATHS[name]
    async with _world(monkeypatch) as (world, active, paused):
        await _walk(world, user, steps, end, {"P": active, "Q": paused})
        assert world.errors == []
        assert world.faults == []
