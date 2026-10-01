"""Characterization snapshots of the legacy Telegram handlers.

Each scenario drives a real application (every production handler, the real
scheduler and notifier, a migrated in-memory database, a fake Telegram
transport) and renders what the user sees, step by step: every Bot API call with
its text, keyboard and parameters, and the per-table database diff. The
rendering is compared with a committed file under ``tests/snapshots/legacy``;
``LEGACY_SNAPSHOTS_UPDATE=1`` rewrites the files outside CI.

The tests before the scenarios check the harness itself: the rendering grammar,
the clock and network isolation, the database diff, the update switch, and the
completeness of the scenario catalogue against the registered handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import hashlib
import io
import json
import re
import shutil
import time
import warnings
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from io import BytesIO
from typing import TYPE_CHECKING, Any, Final, Literal

import httpx
import pytest
import pytest_asyncio
from _pytest.outcomes import Failed
from freezegun import freeze_time
from hypothesis import example, given, settings
from hypothesis import strategies as st
from telegram import (
    File,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    ReplyKeyboardRemove,
)
from telegram.error import NetworkError
from telegram.ext import CommandHandler

from price_tracker.bot import messages
from price_tracker.bot.handlers import register_handlers
from price_tracker.bot.handlers.debug import status_command
from price_tracker.core.exceptions import HTTPBlockStatus, ListingGone, ParseError
from price_tracker.core.scraper_base import ProductInfo
from tests.support.fake_telegram import Call, FakeRequest, make_application
from tests.support.legacy_harness import (
    ADMIN,
    FROZEN_NOW,
    LOCALES,
    OTHER,
    OWNER,
    SNAPSHOT_ROOT,
    STRANGER,
    HarnessFault,
    LegacySnapshotUpdated,
    NonDeterministicOutput,
    Snapshot,
    TableDump,
    build_world,
    close_world,
    diff_dumps,
    reanchor,
    render_db_value,
    seed_config,
    seed_product,
    seed_user,
    wall_clock_violation,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from tests.support.legacy_harness import LegacyWorld

    ScenarioFn = Callable[[LegacyWorld], Awaitable[None]]

SCENARIOS: dict[str, ScenarioFn] = {}

COMMANDS_IN: Final = frozenset(
    {
        "start",
        "menu",
        "help",
        "adduser",
        "removeuser",
        "users",
        "utenti",
        "nick",
        "add",
        "aggiungi",
        "elimina",
        "delete",
        "target",
        "soglia",
        "threshold",
        "lista",
        "list",
        "esporta",
        "export",
        "importa",
        "check",
        "controlla",
        "checkall",
        "refresh",
        "riattiva",
        "reactivate",
        "pausa",
        "pause",
        "storia",
        "history",
        "reset",
        "azzera",
        "intervallo",
        "setinterval",
        "mute",
        "unmute",
        "digest_mode",
        "quiet_hours",
        "timezone",
        "throttle",
        "prefs",
        "digest_now",
        "debug",
        "stato",
        "status",
        "health",
        "errori",
        "errors",
    }
)

GrammarKind = Literal["exact", "prefix"]

CALLBACK_GRAMMAR: Final[frozenset[tuple[GrammarKind, str]]] = frozenset(
    {
        ("prefix", "ops_react_"),
        ("prefix", "ops_delok_"),
        ("prefix", "ops_del_"),
        ("prefix", "confirm_delete_"),
        ("exact", "cancel_delete"),
        ("exact", "delete_all"),
        ("exact", "confirmdeleteall"),
        ("prefix", "check_"),
        ("prefix", "chart_"),
        ("prefix", "pref_new_"),
        ("prefix", "pref_used_"),
        ("prefix", "pref_amazon_"),
        ("prefix", "pref_anyseller_"),
        ("prefix", "pref_default_"),
        ("prefix", "track_any_"),
        ("prefix", "track_default_"),
        ("prefix", "edit_"),
        ("prefix", "pause_"),
        ("prefix", "remove_"),
        ("prefix", "reset_"),
        ("prefix", "reactivate_"),
        ("exact", "cmd_lista"),
        ("exact", "menu_main"),
        ("exact", "menu_prodotti"),
        ("exact", "menu_paused"),
        ("exact", "menu_prezzi"),
        ("exact", "menu_checkall"),
        ("exact", "menu_storia"),
        ("exact", "menu_notifiche"),
        ("exact", "menu_dati"),
        ("exact", "menu_esporta"),
        ("exact", "menu_importa_info"),
        ("exact", "menu_info"),
        ("exact", "menu_admin"),
        ("exact", "menu_admin_users"),
        ("exact", "menu_admin_adduser"),
        ("exact", "menu_admin_removeuser"),
        ("prefix", "admin_rm_"),
        ("exact", "menu_admin_nick"),
        ("prefix", "admin_nick_"),
        ("exact", "menu_admin_interval"),
        ("exact", "menu_admin_debug"),
    }
)

CALLBACK_OUT: Final = (
    "setsoglia_",
    "settarget_",
    "setrefresh_",
    "track_threshold_",
    "track_target_",
)

LEGACY_AREAS: Final = frozenset(
    {
        "home",
        "lista",
        "menu",
        "product",
        "add",
        "data",
        "monitor",
        "history",
        "settings",
        "admin",
        "status",
        "text",
        "alert",
        "ops",
    }
)

SCENARIO_ID_RE: Final = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
PRESS_LINE_RE: Final = re.compile(r'^## step \d+: press "([^"]*)"', re.MULTILINE)
COMMAND_LINE_RE: Final = re.compile(r'^## step \d+: command "/([^\s"]+)', re.MULTILINE)
GENERATED_WITH: Final = "_generated_with.txt"
PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"
KETTLE_URL: Final = "https://shop.example.com/item/1"

# Hosts whose name (not a literal IP) the scenario resolves through the URL guard.
NETWORK_SCENARIOS: Final[dict[str, frozenset[str]]] = {
    "add.success_generic": frozenset({"shop.example.com"}),
    "data.import_csv": frozenset({"shop.example.com"}),
    "admin.cmd_debug": frozenset(),
    "alert.price_drop": frozenset(),
}


# ── fixtures and helpers ─────────────────────────────────────────────


@contextlib.asynccontextmanager
async def open_world(locale: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[LegacyWorld]:
    """A world built and closed inside the frozen clock."""
    real_now = datetime.now(UTC)
    with freeze_time(FROZEN_NOW, real_asyncio=True):
        world = await build_world(locale, monkeypatch=monkeypatch, real_now=real_now)
        try:
            yield world
        finally:
            await close_world(world)


@pytest_asyncio.fixture(params=LOCALES)
async def frozen_world(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[LegacyWorld]:
    """A world per locale; no Telegram limit may be violated by the end of the test."""
    locale: str = request.param
    async with open_world(locale, monkeypatch) as world:
        yield world
        assert world.request.violations == []
        assert world.faults == []


def snapshot_path(root: Path, scenario_id: str, locale: str) -> Path:
    """Where the snapshot of ``scenario_id`` in ``locale`` lives under ``root``."""
    area, name = scenario_id.split(".")
    return root / area / f"{name}.{locale}.txt"


async def run_to_disk(
    world: LegacyWorld,
    scenario_id: str,
    scenario: Callable[[LegacyWorld], Awaitable[None]],
    root: Path,
) -> None:
    """Run ``scenario`` and compare (or update) its snapshot under ``root``."""
    await scenario(world)
    path = snapshot_path(root, scenario_id, world.locale)
    world.recorder.snapshot(scenario_id).compare_or_update(path, root=root)


def committed_snapshots() -> dict[str, str]:
    """Every committed snapshot file (relative path -> content), bookkeeping file excluded."""
    if not SNAPSHOT_ROOT.is_dir():
        return {}
    return {
        path.relative_to(SNAPSHOT_ROOT).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(SNAPSHOT_ROOT.rglob("*.txt"))
        if path.name != GENERATED_WITH
    }


def grammar_matches(kind: GrammarKind, value: str, data: str) -> bool:
    """Whether a pressed ``data`` is an instance of one callback grammar entry."""
    if kind == "exact":
        return data == value
    return data.startswith(value) and data[len(value) :].isdigit()


def sample_snapshot(body: tuple[str, ...] = ('## step 1: command "/lista" user=10',)) -> Snapshot:
    """A small snapshot for the update-mechanism tests."""
    return Snapshot(
        scenario_id="harness.update", locale="it", body=body, real_now=datetime.now(UTC)
    )


def keyboard(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """An inline keyboard of callback buttons, one list of (label, data) per row."""
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(label, callback_data=data) for label, data in row] for row in rows]
    )


# ── T-H: harness ─────────────────────────────────────────────────────


async def test_h1_rendering_of_text_keyboard_and_toast(frozen_world: LegacyWorld) -> None:
    """A message with a 2x2 keyboard and a URL button, an edit and a toast render exactly."""
    w = frozen_world
    bot = w.app.bot
    markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("A", callback_data="a"),
                InlineKeyboardButton("B", callback_data="b"),
            ],
            [
                InlineKeyboardButton("C", callback_data="c"),
                InlineKeyboardButton("D", callback_data="d"),
            ],
            [InlineKeyboardButton("Site", url="https://shop.example.com/")],
        ]
    )

    async def calls() -> None:
        await bot.send_message(chat_id=OWNER, text="hello\n\nworld", reply_markup=markup)
        await bot.edit_message_text(chat_id=OWNER, message_id=1000, text="edited")
        await bot.answer_callback_query("42", text="x", show_alert=True)

    await w.recorder.capture("hand-built calls", calls())

    expected = "\n".join(
        [
            "# legacy snapshot v1",
            "# scenario: harness.rendering",
            "# locale: it",
            "# frozen_now: 2026-03-01T12:00:00Z",
            '## step 1: capture "hand-built calls"',
            "-> sendMessage chat=10",
            "   text:",
            "   | hello",
            "   |",
            "   | world",
            "   keyboard:",
            "   | [A → a] [B → b]",
            "   | [C → c] [D → d]",
            "   | [Site → url:https://shop.example.com/]",
            "-> editMessageText chat=10 message_id=1000",
            "   text:",
            "   | edited",
            "-> answerCallbackQuery",
            "   param show_alert=true",
            '   param text="x"',
            "## db after step 1",
            "(no changes)",
        ]
    )
    assert w.recorder.snapshot("harness.rendering").render() == expected + "\n"


def test_h2_db_timestamp_normalization_examples() -> None:
    """Only a UTC timestamp within 600 s of the real clock becomes ``<now:SHAPE>``."""
    real_now = datetime.now(UTC)
    naive = real_now.replace(tzinfo=None)

    def sqlite_form(moment: datetime) -> str:
        return moment.isoformat(sep=" ", timespec="seconds")

    near = naive + timedelta(seconds=30)
    assert render_db_value(sqlite_form(near), real_now) == "<now:9999-99-99 99:99:99>"
    assert render_db_value(sqlite_form(naive - timedelta(seconds=30)), real_now) == (
        "<now:9999-99-99 99:99:99>"
    )
    fixed = "2026-03-01 10:00:00"
    assert render_db_value(fixed, real_now) == json.dumps(fixed)
    far = sqlite_form(naive - timedelta(seconds=900))
    assert render_db_value(far, real_now) == json.dumps(far)

    iso_near = (real_now + timedelta(seconds=30)).replace(microsecond=0).isoformat()
    assert iso_near.endswith("+00:00")
    assert render_db_value(iso_near, real_now) == "<now:9999-99-99T99:99:99+99:99>"

    z_form = (real_now + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert render_db_value(z_form, real_now) == "<now:9999-99-99T99:99:99Z>"
    millis = near.isoformat(sep=" ", timespec="milliseconds")
    assert render_db_value(millis, real_now) == "<now:9999-99-99 99:99:99.999>"

    today = naive.date().isoformat()
    this_minute = naive.strftime("%H:%M")
    in_rome = (real_now + timedelta(seconds=5)).astimezone(timezone(timedelta(hours=2)))
    local = in_rome.replace(microsecond=0).isoformat()
    assert local.endswith("+02:00")
    for verbatim in (
        "2026-02-30 10:00:00",
        f"{today} {this_minute}:60",
        f"{today} 24:00:00",
        local,
        " " + sqlite_form(near),
        sqlite_form(near) + "\n",
        sqlite_form(near) + "x",
        "città",
    ):
        assert render_db_value(verbatim, real_now) == json.dumps(verbatim, ensure_ascii=False)

    blob = b"\x00\xff"
    assert render_db_value(None, real_now) == "NULL"
    assert render_db_value(7, real_now) == "7"
    assert render_db_value("7", real_now) == '"7"'
    assert render_db_value(1.5, real_now) == "float:1.5"
    assert render_db_value(blob, real_now) == f"blob:2:{hashlib.sha256(blob).hexdigest()[:12]}"


_REAL_NOW_FOR_PROPERTY: Final = datetime.now(UTC)
_TIMESTAMP_FORMS: Final = ("sqlite", "sqlite_millis", "iso", "iso_utc", "iso_z")


def _timestamp_text(moment: datetime, form: str) -> tuple[str, datetime]:
    """``moment`` (naive, UTC) written in ``form``, and the instant the text denotes."""
    if form == "sqlite_millis":
        instant = moment.replace(microsecond=moment.microsecond // 1000 * 1000)
        return instant.isoformat(sep=" ", timespec="milliseconds"), instant
    instant = moment.replace(microsecond=0)
    suffix = {"sqlite": "", "iso": "", "iso_utc": "+00:00", "iso_z": "Z"}[form]
    return instant.isoformat(sep=" " if form == "sqlite" else "T") + suffix, instant


@given(
    moment=st.one_of(
        st.datetimes(),
        st.integers(min_value=-1200, max_value=1200).map(
            lambda s: _REAL_NOW_FOR_PROPERTY.replace(tzinfo=None) + timedelta(seconds=s)
        ),
    ),
    form=st.sampled_from(_TIMESTAMP_FORMS),
)
def test_h2_db_timestamp_normalization_property(moment: datetime, form: str) -> None:
    """``<now:SHAPE>`` if and only if the instant is within 600 s of the real clock."""
    real_now = _REAL_NOW_FOR_PROPERTY
    text, instant = _timestamp_text(moment, form)
    near = abs((instant.replace(tzinfo=UTC) - real_now).total_seconds()) <= 600
    rendered = render_db_value(text, real_now)
    shape = "".join("9" if ch.isdigit() else ch for ch in text)
    if near:
        assert rendered == f"<now:{shape}>"
    else:
        assert rendered == json.dumps(text)


async def test_h3_same_scene_renders_identically_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same scenario run in two fresh worlds renders byte for byte the same."""
    assert "lista.rich" in SCENARIOS, "scenario lista.rich is not registered"
    renders: list[str] = []
    for _ in range(2):
        async with open_world(LOCALES[0], monkeypatch) as world:
            await SCENARIOS["lista.rich"](world)
            renders.append(world.recorder.snapshot("lista.rich").render())
    assert renders[0] == renders[1]


@pytest.mark.parametrize("catalogues", [(), ("en",)], ids=["none", "english_only"])
async def test_h3b_i18n_canary_refuses_an_untranslated_catalogue(
    catalogues: tuple[str, ...], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A locale without its own catalogue stops the world, even when English loads."""
    for name in catalogues:
        shutil.copytree(messages._LOCALE_DIR / name, tmp_path / name)
        assert (tmp_path / name / "LC_MESSAGES" / "messages.mo").is_file()
    monkeypatch.setattr(messages, "_LOCALE_DIR", tmp_path)
    messages.get_translation.cache_clear()
    try:
        with pytest.raises(HarnessFault, match=r"\bit\b"):
            async with open_world(LOCALES[0], monkeypatch):
                pass
    finally:
        messages.get_translation.cache_clear()


async def test_h4_db_diff_insert_update_cascade_and_seed_steps(frozen_world: LegacyWorld) -> None:
    """Inserts, column updates and cascading deletes are diffed; seed steps emit no diff."""
    w = frozen_world
    history = [("2026-02-01 10:00:00", "100.00"), ("2026-02-20 10:00:00", "90.00")]
    await w.recorder.capture(
        "seed product",
        seed_product(w, OWNER, KETTLE_URL, "Kettle", initial="100.00", history=history),
    )
    await w.recorder.seed("rename", reanchor(w, "products", {"id": 1}, "name", "Kettle 2"))
    await w.recorder.capture("pause", w.repo.pause_product(1))
    await w.recorder.capture("delete", w.repo.delete_product(1, user_id=OWNER))

    rendered = w.recorder.snapshot("harness.db_diff").render()
    lines = rendered.splitlines()
    step1 = rendered.split("## db after step 1\n", 1)[1].split("## step 2", 1)[0]
    assert "+ products id=1 {" in step1
    assert sum(line.startswith("+ price_history ") for line in step1.splitlines()) == 2
    assert '## step 2: seed "rename"' in lines
    assert "## db after step 2" not in lines
    step3 = rendered.split("## db after step 3\n", 1)[1].split("## step 4", 1)[0].splitlines()
    assert "~ products id=1 is_active: 1 -> 0" in step3
    assert [line for line in step3 if line.startswith("~ products id=1 name:")] == []
    step4 = rendered.split("## db after step 4\n", 1)[1]
    assert "- products id=1" in step4.splitlines()
    assert sum(line.startswith("- price_history ") for line in step4.splitlines()) == 2

    async def null_prices(product_id: int) -> tuple[object, ...]:
        cursor = await w.conn.execute(
            "SELECT current_price IS NULL, lowest_price IS NULL, highest_price IS NULL "
            "FROM products WHERE id = ?",
            (product_id,),
        )
        row = await cursor.fetchone()
        assert row is not None
        return tuple(row)

    kept = await seed_product(w, OWNER, "https://shop.example.com/item/2", "Kept", initial="5.00")
    nulled = await seed_product(
        w,
        OWNER,
        "https://shop.example.com/item/3",
        "Nulled",
        initial="5.00",
        current=None,
        lowest=None,
        highest=None,
    )
    assert await null_prices(kept) == (0, 0, 0)
    assert await null_prices(nulled) == (1, 1, 1)


async def test_h5_photo_and_document_uploads_and_downloads(frozen_world: LegacyWorld) -> None:
    """Uploads render by filename and content; downloads serve registered bytes only."""
    w = frozen_world
    bot = w.app.bot
    photo = PNG_MAGIC + b"\0" * (2048 - len(PNG_MAGIC))

    async def uploads() -> None:
        await bot.send_photo(chat_id=OWNER, photo=BytesIO(photo))
        await bot.send_document(
            chat_id=OWNER, document=InputFile(b"a,b\r\n1,2\r\n", filename="x.csv")
        )

    await w.recorder.capture("uploads", uploads())
    rendered = w.recorder.snapshot("harness.multipart").render()
    lines = rendered.splitlines()
    assert "-> sendPhoto chat=10 photo=png filename=application.octet-stream" in lines
    assert "-> sendDocument chat=10 filename=x.csv" in lines
    assert "   content:\n   | a,b\n   | 1,2\n" in rendered
    assert not any(line.startswith(("   param photo=", "   param document=")) for line in lines)
    sent = [
        i for i, call in enumerate(w.request.calls) if call.method in {"sendPhoto", "sendDocument"}
    ]
    assert [sorted(w.request.files_by_call[i]) for i in sent] == [["photo"], ["document"]]
    assert all(
        "photo" not in w.request.calls[i].params and "document" not in w.request.calls[i].params
        for i in sent
    )

    assert w.faults is w.request.faults
    file_id = w.upload("urls.csv", b"URL\r\nhttps://shop.example.com/item/7\r\n")
    assert w.upload("other.csv", b"x") != file_id
    telegram_file = await bot.get_file(file_id)
    out = BytesIO()
    await telegram_file.download_to_memory(out)
    assert out.getvalue() == b"URL\r\nhttps://shop.example.com/item/7\r\n"

    async def get_unknown_file() -> None:
        await bot.get_file("never-uploaded")

    async def download_unknown_file() -> None:
        ghost = File(file_id="ghost", file_unique_id="ughost", file_path="documents/ghost")
        ghost.set_bot(bot)
        await ghost.download_to_memory(BytesIO())

    for attempt in (get_unknown_file, download_unknown_file):
        with pytest.raises((HarnessFault, NetworkError)) as raised:
            await attempt()
        fault = raised.value if isinstance(raised.value, HarnessFault) else raised.value.__cause__
        assert isinstance(fault, HarnessFault), attempt.__name__
        assert w.faults, attempt.__name__
        w.faults.clear()


async def test_h6_parameters_outside_the_known_blocks(frozen_world: LegacyWorld) -> None:
    """Unknown methods and extra parameters render as sorted ``param`` lines."""
    w = frozen_world
    bot = w.app.bot

    async def calls() -> None:
        await bot.send_chat_action(chat_id=OWNER, action="typing")
        await bot.send_message(chat_id=OWNER, text="p", protect_content=True)
        await bot.answer_callback_query("99")

    await w.recorder.capture("extras", calls())
    await w.recorder.press(OWNER, "zz_unknown")
    rendered = w.recorder.snapshot("harness.params").render()
    lines = rendered.splitlines()
    action = lines.index("-> sendChatAction")
    assert lines[action + 1 : action + 3] == ['   param action="typing"', "   param chat_id=10"]
    send = lines.index("-> sendMessage chat=10")
    assert lines[send + 1] == "   param protect_content=true"
    assert "callback_query_id" not in rendered


async def test_h7_clock_is_frozen_but_sqlite_and_the_loop_are_not(
    frozen_world: LegacyWorld,
) -> None:
    """The process clock is frozen, asyncio sleeps really, SQLite writes the real clock."""
    w = frozen_world
    assert datetime.now(UTC).isoformat().startswith("2026-03-01T12:00:00")
    await asyncio.wait_for(asyncio.sleep(0.01), timeout=5)
    cursor = await w.conn.execute("SELECT datetime('now')")
    row = await cursor.fetchone()
    assert row is not None
    sqlite_now = datetime.fromisoformat(str(row[0])).replace(tzinfo=UTC)
    assert abs((sqlite_now - w.real_now).total_seconds()) <= 600
    first = time.monotonic()
    await asyncio.sleep(0.01)
    assert time.monotonic() == first


@pytest.mark.parametrize("scenario_id", sorted(NETWORK_SCENARIOS))
async def test_h8_scenarios_never_touch_the_network(
    scenario_id: str, frozen_world: LegacyWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Network scenarios complete with sockets disabled; DNS sees only the named hosts."""
    import socket

    assert scenario_id in SCENARIOS, f"scenario {scenario_id} is not registered"
    w = frozen_world

    def refuse(*args: object, **kwargs: object) -> None:
        fault = HarnessFault("network")
        w.faults.append(fault)
        raise fault

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    await SCENARIOS[scenario_id](w)
    assert w.faults == []
    assert set(w.dns_lookups) == NETWORK_SCENARIOS[scenario_id]


async def test_h9_wall_clock_guards(frozen_world: LegacyWorld) -> None:
    """Captured text near the real clock or a negative relative time fails; the rest passes.

    The guard covers message text, captions, button labels and document lines.
    """
    w = frozen_world
    bot = w.app.bot
    stamp = w.real_now.strftime("%Y-%m-%d %H:%M")
    iso_stamp = w.real_now.strftime("%Y-%m-%dT%H:%M")
    within_a_day = (w.real_now - timedelta(hours=23)).strftime("%Y-%m-%d %H:%M")
    over_a_day = (w.real_now - timedelta(hours=25)).strftime("%Y-%m-%d %H:%M")

    await w.recorder.capture("seeded", bot.send_message(chat_id=OWNER, text="at 2026-03-01 10:00"))
    await w.recorder.capture("day old", bot.send_message(chat_id=OWNER, text=f"at {over_a_day}"))
    await w.recorder.capture("positive", bot.send_message(chat_id=OWNER, text="🕐 5min fa"))
    lines = w.recorder.snapshot("harness.guards").render().splitlines()
    assert "   | at 2026-03-01 10:00" in lines
    assert f"   | at {over_a_day}" in lines
    assert "   | 🕐 5min fa" in lines

    photo = PNG_MAGIC + b"\0" * (2048 - len(PNG_MAGIC))
    csv = f"created_at\r\n{stamp}:00\r\n".encode()
    offending: list[tuple[str, Callable[[], Awaitable[object]]]] = [
        (stamp, lambda: bot.send_message(chat_id=OWNER, text=f"at {stamp}")),
        (iso_stamp, lambda: bot.send_message(chat_id=OWNER, text=f"at {iso_stamp}:00")),
        (within_a_day, lambda: bot.send_message(chat_id=OWNER, text=f"at {within_a_day}")),
        (
            stamp,
            lambda: bot.send_photo(chat_id=OWNER, photo=BytesIO(photo), caption=f"at {stamp}"),
        ),
        (
            stamp,
            lambda: bot.send_message(
                chat_id=OWNER, text="k", reply_markup=keyboard([(f"at {stamp}", "zz_k")])
            ),
        ),
        (
            stamp,
            lambda: bot.send_document(chat_id=OWNER, document=InputFile(csv, filename="x.csv")),
        ),
        ("-5min fa", lambda: bot.send_message(chat_id=OWNER, text="🕐 -5min fa")),
        ("-2h fa", lambda: bot.send_message(chat_id=OWNER, text="🕐 -2h fa")),
        ("-1g fa", lambda: bot.send_message(chat_id=OWNER, text="🕐 -1g fa")),
    ]
    for fragment, send in offending:
        with pytest.raises(NonDeterministicOutput) as raised:
            await w.recorder.capture(fragment, send())
        raised.match(r"step \d+")
        raised.match(re.escape(fragment))


async def test_h10_malformed_markup_and_text_render_without_errors(
    frozen_world: LegacyWorld,
) -> None:
    """Odd keyboards, CRLF text, an empty text and a non-inline markup still render."""
    w = frozen_world
    bot = w.app.bot

    async def calls() -> None:
        await bot.send_message(
            chat_id=OWNER, text="empty row", reply_markup=InlineKeyboardMarkup([[]])
        )
        await bot.send_message(
            chat_id=OWNER,
            text="text only",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Plain")]]),
        )
        await bot.send_message(chat_id=OWNER, text="first\r\nsecond")
        await bot.send_message(chat_id=OWNER, text="")
        await bot.send_message(chat_id=OWNER, text="gone", reply_markup=ReplyKeyboardRemove())

    await w.recorder.capture("malformed", calls())
    assert w.request.violations == ["sendMessage: text length 0"]
    w.request.violations.clear()
    await w.recorder.text(OWNER, 'say "hi"\nthere')
    lines = w.recorder.snapshot("harness.malformed").render().splitlines()
    step1_end = lines.index("## db after step 1")
    heads = [i for i, line in enumerate(lines[:step1_end]) if line == "-> sendMessage chat=10"]
    ends = [*heads[1:], step1_end]
    blocks = [lines[start + 1 : end] for start, end in zip(heads, ends, strict=True)]
    assert blocks == [
        ["   text:", "   | empty row", "   keyboard:", "   | []"],
        ["   text:", "   | text only", "   keyboard:", "   | [Plain → ?]"],
        ["   text:", "   | first\\r", "   | second"],
        ["   text:", "   |"],
        ["   text:", "   | gone", '   markup: {"remove_keyboard": true}'],
    ]
    assert '## step 2: text "say \\"hi\\"\\nthere" user=10' in lines


async def test_h11_duplicate_keys_render_as_suffixed_inserts(frozen_world: LegacyWorld) -> None:
    """Two rows with the same nullable key are two suffixed inserts, never an update."""
    w = frozen_world

    async def duplicates() -> None:
        await w.conn.execute("DROP INDEX ux_notification_prefs_global")
        await w.conn.execute(
            "INSERT INTO notification_prefs(user_id, product_id) VALUES (10, NULL)"
        )
        await w.conn.execute(
            "INSERT INTO notification_prefs(user_id, product_id) VALUES (10, NULL)"
        )
        await w.conn.commit()

    await w.recorder.capture("duplicates", duplicates())
    lines = w.recorder.snapshot("harness.duplicates").render().splitlines()
    inserts = [line for line in lines if line.startswith("+ notification_prefs ")]
    assert len(inserts) == 2
    assert inserts[0].startswith("+ notification_prefs user_id=10,product_id=NULL {")
    assert inserts[1].startswith("+ notification_prefs user_id=10,product_id=NULL#2 {")
    assert "!! duplicate key notification_prefs user_id=10,product_id=NULL ×2" in lines
    assert not any(line.startswith("~ notification_prefs") for line in lines)


async def test_h11_real_and_blob_columns_render(frozen_world: LegacyWorld) -> None:
    """A table created in the step, with REAL and BLOB columns, renders its rows."""
    w = frozen_world
    blob = b"\x00\xff"

    async def create() -> None:
        await w.conn.execute("CREATE TABLE extra (id INTEGER PRIMARY KEY, r REAL, b BLOB)")
        await w.conn.execute("INSERT INTO extra(id, r, b) VALUES (1, 1.5, ?)", (blob,))
        await w.conn.commit()

    await w.recorder.capture("create", create())
    lines = w.recorder.snapshot("harness.real_blob").render().splitlines()
    digest = hashlib.sha256(blob).hexdigest()[:12]
    assert f"+ extra id=1 {{r=float:1.5, b=blob:2:{digest}}}" in lines


async def test_h11_delete_and_reinsert_is_an_update(frozen_world: LegacyWorld) -> None:
    """Deleting and reinserting a key in one step shows as column updates."""
    w = frozen_world
    await seed_config(w, "k", "v1")

    async def swap() -> None:
        await w.conn.execute("DELETE FROM bot_config WHERE key = 'k'")
        await w.conn.execute("INSERT INTO bot_config(key, value) VALUES ('k', 'v2')")
        await w.conn.commit()

    await w.recorder.capture("swap", swap())
    lines = w.recorder.snapshot("harness.reinsert").render().splitlines()
    assert '~ bot_config key="k" value: "v1" -> "v2"' in lines
    assert not any(line.startswith(("+ bot_config", "- bot_config")) for line in lines)


_KEY_PART = st.one_of(st.none(), st.integers(min_value=0, max_value=2))
_ROW = st.tuples(_KEY_PART, _KEY_PART, st.one_of(st.none(), st.integers(min_value=0, max_value=2)))
_COLUMNS: Final = ("user_id", "product_id", "value")
_KEY: Final = ("user_id", "product_id")


@given(before=st.lists(_ROW, max_size=6), data=st.data())
def test_h11_diff_is_empty_iff_row_multisets_match(
    before: list[tuple[object, ...]], data: st.DataObject
) -> None:
    """With repeated and NULL key parts, no change is reported iff the multisets coincide."""
    after = data.draw(st.one_of(st.permutations(before), st.lists(_ROW, max_size=6)))
    real_now = datetime.now(UTC)
    lines = diff_dumps(
        {"t": TableDump(_COLUMNS, _KEY, tuple(before))},
        {"t": TableDump(_COLUMNS, _KEY, tuple(after))},
        real_now,
    )
    equal = Counter(before) == Counter(after)
    changes = [line for line in lines if line.startswith(("+ ", "- ", "~ "))]
    assert (not changes) == equal
    assert ("(no changes)" in lines) == equal


async def test_h13_pressed_message_follows_the_current_keyboards(frozen_world: LegacyWorld) -> None:
    """A press lands on the newest message still showing the button, else on a synthetic id."""
    w = frozen_world
    bot = w.app.bot
    await w.recorder.capture(
        "old keyboard",
        bot.send_message(chat_id=OWNER, text="a", reply_markup=keyboard([("x", "zz_old")])),
    )
    await w.recorder.capture(
        "replace keyboard",
        bot.edit_message_text(
            chat_id=OWNER, message_id=1000, text="b", reply_markup=keyboard([("y", "zz_new")])
        ),
    )
    await w.recorder.capture(
        "to remove",
        bot.send_message(chat_id=OWNER, text="c", reply_markup=keyboard([("z", "zz_rm")])),
    )
    await w.recorder.capture(
        "remove keyboard", bot.edit_message_text(chat_id=OWNER, message_id=1001, text="d")
    )
    await w.recorder.seed(
        "dup 1", bot.send_message(chat_id=OWNER, text="e", reply_markup=keyboard([("w", "zz_dup")]))
    )
    await w.recorder.seed(
        "dup 2", bot.send_message(chat_id=OWNER, text="f", reply_markup=keyboard([("w", "zz_dup")]))
    )
    await w.recorder.seed(
        "other chat",
        bot.send_message(chat_id=ADMIN, text="g", reply_markup=keyboard([("w", "zz_dup")])),
    )
    first_send = w.request.calls_of("sendMessage")[0]
    await w.recorder.press(OWNER, "zz_old")
    await w.recorder.press(OWNER, "zz_new")
    await w.recorder.press(OWNER, "zz_rm")
    await w.recorder.press(OWNER, "zz_never")
    await w.recorder.press(OWNER, "zz_dup")
    await w.recorder.press(OWNER, "zz_old", on=first_send)

    lines = w.recorder.snapshot("harness.resolver").render().splitlines()
    assert '## step 8: press "zz_old" user=10 on=1 synthetic' in lines
    assert '## step 9: press "zz_new" user=10 on=1000' in lines
    assert '## step 10: press "zz_rm" user=10 on=1 synthetic' in lines
    assert '## step 11: press "zz_never" user=10 on=1 synthetic' in lines
    assert '## step 12: press "zz_dup" user=10 on=1003' in lines
    assert '## step 13: press "zz_old" user=10 on=1000' in lines


async def test_h13_deleted_message_loses_its_keyboard(frozen_world: LegacyWorld) -> None:
    """A deleted message can no longer be pressed without naming it."""
    w = frozen_world
    bot = w.app.bot
    await w.recorder.capture(
        "keyboard",
        bot.send_message(chat_id=OWNER, text="a", reply_markup=keyboard([("x", "zz_del")])),
    )
    await w.recorder.capture("delete", bot.delete_message(chat_id=OWNER, message_id=1000))
    await w.recorder.press(OWNER, "zz_del")
    lines = w.recorder.snapshot("harness.deleted").render().splitlines()
    assert "-> deleteMessage chat=10 message_id=1000" in lines
    assert '## step 3: press "zz_del" user=10 on=1 synthetic' in lines


async def test_h13_seed_commands_are_listed_but_not_captured(frozen_world: LegacyWorld) -> None:
    """A command run as a seed shows only its step line, yet its keyboard stays pressable."""
    w = frozen_world
    await w.recorder.seed('"/digest_mode on"', w.run_command(OWNER, "/digest_mode on"))
    await w.recorder.seed('"/menu"', w.run_command(OWNER, "/menu"))
    menu = w.request.calls_of("sendMessage")[-1]
    assert "menu_prodotti" in menu.callback_data()
    await w.recorder.seed('press "menu_dati"', w.run_press(OWNER, "menu_dati"))
    await w.recorder.press(OWNER, "menu_importa_info")

    rendered = w.recorder.snapshot("harness.seed_commands").render()
    lines = rendered.splitlines()
    assert lines[4:7] == [
        '## step 1: seed "\\"/digest_mode on\\""',
        '## step 2: seed "\\"/menu\\""',
        '## step 3: seed "press \\"menu_dati\\""',
    ]
    assert "-> sendMessage chat=10" not in lines
    assert "Digest mode on" not in rendered
    assert f'## step 4: press "menu_importa_info" user=10 on={menu.message_id}' in lines
    step4_db = rendered.split("## db after step 4\n", 1)[1].splitlines()
    assert not any("notification_prefs" in line for line in step4_db)


async def test_h5_photo_size_out_of_bounds_is_a_violation(frozen_world: LegacyWorld) -> None:
    """A photo under 1 KiB or over 2 MiB is recorded as a violation, never raised."""
    w = frozen_world
    bot = w.app.bot

    def png(size: int) -> BytesIO:
        return BytesIO(PNG_MAGIC + b"\0" * (size - len(PNG_MAGIC)))

    async def photos() -> None:
        for size in (1023, 1024, 2 * 1024 * 1024, 2 * 1024 * 1024 + 1):
            await bot.send_photo(chat_id=OWNER, photo=png(size))

    await w.recorder.capture("photo sizes", photos())
    assert w.request.violations == [
        "sendPhoto: photo size 1023 bytes out of 1024..2097152",
        "sendPhoto: photo size 2097153 bytes out of 1024..2097152",
    ]
    w.request.violations.clear()
    lines = w.recorder.snapshot("harness.photo_sizes").render().splitlines()
    heads = "-> sendPhoto chat=10 photo=png filename=application.octet-stream"
    assert lines.count(heads) == 4


async def _unscripted_add(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/add https://shop.example.com/item/77")


async def _unscripted_job(w: LegacyWorld) -> None:
    await seed_product(
        w, OWNER, "https://shop.example.com/item/78", "Ghost", initial="10.00", current="10.00"
    )
    await w.recorder.job("run_check_all")


@pytest.mark.parametrize(
    "scenario",
    [pytest.param(_unscripted_add, id="handler"), pytest.param(_unscripted_job, id="scheduler")],
)
async def test_h14_stub_used_off_script_fails_and_writes_nothing(
    scenario: Callable[[LegacyWorld], Awaitable[None]],
    frozen_world: LegacyWorld,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A scrape without a script is a harness fault even when the product swallows it."""
    monkeypatch.setenv("LEGACY_SNAPSHOTS_UPDATE", "1")
    monkeypatch.delenv("CI", raising=False)
    with pytest.raises(HarnessFault, match=r"\bstep 1\b"):
        await run_to_disk(frozen_world, "harness.fault", scenario, tmp_path)
    assert list(tmp_path.rglob("*")) == []
    frozen_world.faults.clear()


async def test_h14_handler_errors_render_and_assertions_fail(frozen_world: LegacyWorld) -> None:
    """A handler error renders as a ``!!`` line; an assertion raised in a handler is a fault."""
    w = frozen_world

    async def handler_error() -> None:
        w.errors.append(RuntimeError("boom"))

    async def handler_assertion() -> None:
        w.errors.append(AssertionError("stub off script"))

    await w.recorder.capture("handler error", handler_error())
    await w.recorder.call_error_handler(OWNER, RuntimeError("boom"))
    lines = w.recorder.snapshot("harness.errors").render().splitlines()
    step1 = lines[lines.index('## step 1: capture "handler error"') :]
    assert "   !! error_handler: RuntimeError: boom" in step1[: step1.index("## db after step 1")]
    assert '## step 2: call error_handler RuntimeError("boom") user=10' in lines
    with pytest.raises(HarnessFault, match=r"\bstep 3\b"):
        await w.recorder.capture("handler assertion", handler_assertion())


async def test_h10_line_breaks_cannot_split_or_forge_snapshot_lines(
    frozen_world: LegacyWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CR and LF in labels, file names, document lines and errors are escaped.

    The rendering then survives writing and reading the file back unchanged.
    """
    w = frozen_world
    bot = w.app.bot
    forged = '## step 9: press "menu_admin" user=1 on=1'

    async def calls() -> None:
        await bot.send_message(
            chat_id=OWNER, text="k", reply_markup=keyboard([(f"A\n{forged}", "zz\rx")])
        )
        await bot.send_document(
            chat_id=OWNER, document=InputFile(b"a\rb\r\nc\r\n", filename="x\ny.csv")
        )

    async def handler_error() -> None:
        w.errors.append(RuntimeError(f"line one\n{forged}"))

    await w.recorder.capture("breaks", calls())
    await w.recorder.capture("error", handler_error())
    snapshot = w.recorder.snapshot("harness.breaks")
    rendered = snapshot.render()
    lines = rendered.splitlines()
    assert "\r" not in rendered
    assert not any(line.startswith("## step 9") for line in lines)
    assert PRESS_LINE_RE.findall(rendered) == []
    assert f"   | [A\\n{forged} → zz\\rx]" in lines
    assert "-> sendDocument chat=10 filename=x\\ny.csv" in lines
    assert "   | a\\rb" in lines
    assert "   | c" in lines
    assert f"   !! error_handler: RuntimeError: line one\\n{forged}" in lines

    path = tmp_path / "harness" / "breaks.it.txt"
    monkeypatch.setenv("LEGACY_SNAPSHOTS_UPDATE", "1")
    monkeypatch.delenv("CI", raising=False)
    with pytest.warns(LegacySnapshotUpdated):
        snapshot.compare_or_update(path, root=tmp_path)
    monkeypatch.delenv("LEGACY_SNAPSHOTS_UPDATE")
    snapshot.compare_or_update(path, root=tmp_path)


async def test_h10_empty_document_differs_from_a_line_break(frozen_world: LegacyWorld) -> None:
    """An empty document has no content line; a lone line break has one empty line."""
    w = frozen_world
    bot = w.app.bot
    await w.recorder.capture(
        "empty", bot.send_document(chat_id=OWNER, document=InputFile(b"", filename="e.csv"))
    )
    await w.recorder.capture(
        "break", bot.send_document(chat_id=OWNER, document=InputFile(b"\n", filename="e.csv"))
    )
    lines = w.recorder.snapshot("harness.empty_document").render().splitlines()
    first = lines.index('## step 1: capture "empty"')
    second = lines.index('## step 2: capture "break"')
    assert lines[first + 1 : first + 4] == [
        "-> sendDocument chat=10 filename=e.csv",
        "   content:",
        "## db after step 1",
    ]
    assert lines[second + 1 : second + 5] == [
        "-> sendDocument chat=10 filename=e.csv",
        "   content:",
        "   |",
        "## db after step 2",
    ]


_GUARD_NOW: Final = datetime(2026, 9, 25, 10, 30, 30, tzinfo=UTC)
"""A fixed stand-in for the real clock: the guard is a pure function of it, and a
whole second at mid-minute makes the edge examples below exact."""
_DAY: Final = timedelta(days=1)
_OFFSET_MINUTES: Final = st.integers(min_value=-14 * 4, max_value=14 * 4).map(lambda q: q * 15)


def _around_a_day(width: int) -> st.SearchStrategy[int]:
    """Seconds from the real clock within ``width`` of one day, before or after it."""
    return st.tuples(st.sampled_from((-1, 1)), st.integers(min_value=-width, max_value=width)).map(
        lambda pair: pair[0] * (86_400 + pair[1])
    )


# Uniform deltas rarely land on the one-day edge: most draws are aimed at it, at
# the widths where a wrong offset, lost seconds or a lost fraction flip G1.
_GUARD_DELTAS: Final = st.one_of(
    st.integers(min_value=-200_000, max_value=200_000),
    _around_a_day(60_000),
    _around_a_day(120),
    _around_a_day(2),
)


@settings(max_examples=400)
@example(  # the fraction counts: 86399.5 s before is within a day
    moment=_GUARD_NOW - _DAY,
    microsecond=500_000,
    separator=" ",
    precision="fraction",
    digits=1,
    zone=None,
    colon=True,
)
@example(  # the seconds count: 86399 s before is within a day
    moment=_GUARD_NOW - _DAY + timedelta(seconds=1),
    microsecond=0,
    separator=" ",
    precision="seconds",
    digits=1,
    zone=None,
    colon=True,
)
@example(  # exactly one day is outside
    moment=_GUARD_NOW - _DAY,
    microsecond=0,
    separator="T",
    precision="seconds",
    digits=1,
    zone="Z",
    colon=True,
)
@example(  # the offset sign counts: 23 h ahead, written in +02:00
    moment=_GUARD_NOW + timedelta(hours=23),
    microsecond=0,
    separator="T",
    precision="minutes",
    digits=1,
    zone=120,
    colon=False,
)
@given(
    moment=st.one_of(
        _GUARD_DELTAS.map(lambda s: _GUARD_NOW + timedelta(seconds=s)),
        st.datetimes(min_value=datetime(1001, 1, 1), max_value=datetime(9998, 12, 31)).map(
            lambda d: d.replace(tzinfo=UTC)
        ),
    ),
    microsecond=st.integers(min_value=0, max_value=999_999),
    separator=st.sampled_from((" ", "T")),
    precision=st.sampled_from(("minutes", "seconds", "fraction")),
    digits=st.integers(min_value=1, max_value=6),
    zone=st.one_of(st.none(), st.just("Z"), _OFFSET_MINUTES),
    colon=st.booleans(),
)
def test_h9_wall_clock_guard_fires_iff_a_timestamp_is_within_a_day(
    moment: datetime,
    microsecond: int,
    separator: str,
    precision: str,
    digits: int,
    zone: int | str | None,
    colon: bool,
) -> None:
    """G1 fires if and only if the written instant is less than a day from the real clock.

    The oracle builds the text from a known instant with ``strftime`` and never
    reads it back with the implementation's regex.
    """
    moment = moment.replace(microsecond=microsecond)
    if precision == "minutes":
        moment = moment.replace(second=0, microsecond=0)
    elif precision == "seconds":
        moment = moment.replace(microsecond=0)
    else:
        moment = moment.replace(
            microsecond=moment.microsecond // 10 ** (6 - digits) * 10 ** (6 - digits)
        )
    offset = timedelta(minutes=zone) if isinstance(zone, int) else timedelta(0)
    local = (moment + offset).replace(tzinfo=None)
    clock = "%H:%M" if precision == "minutes" else "%H:%M:%S"
    written = local.strftime(f"%Y-%m-%d{separator}{clock}")
    if precision == "fraction":
        written += f".{local.microsecond:06d}"[: digits + 1]
    if zone == "Z":
        written += "Z"
    elif isinstance(zone, int):
        sign = "-" if zone < 0 else "+"
        hours, minutes = divmod(abs(zone), 60)
        written += f"{sign}{hours:02d}{':' if colon else ''}{minutes:02d}"
    near = abs((moment - _GUARD_NOW).total_seconds()) < 86400
    violation = wall_clock_violation(f"at {written} ok", _GUARD_NOW)
    assert (violation is not None) == near, written


@pytest.mark.parametrize(
    "text",
    [
        "{date} 24:00",
        "{date} 12:60",
        "2026-02-30 10:00",
        "{date} {time}+25:00",
        "{date}",
        "🕐 5min fa",
        "🕐 0min fa",
        "prezzo -5 fa",
    ],
)
def test_h9_impossible_or_partial_timestamps_do_not_fire(text: str) -> None:
    """Dates without a time, impossible times and positive relative times pass quietly."""
    filled = text.format(date=_GUARD_NOW.date().isoformat(), time=_GUARD_NOW.strftime("%H:%M"))
    assert wall_clock_violation(filled, _GUARD_NOW) is None


@given(amount=st.integers(min_value=1, max_value=10**7), unit=st.sampled_from(("min", "h", "g")))
def test_h9_negative_relative_time_always_fires(amount: int, unit: str) -> None:
    """G2 fires on every negative relative time and on none of the positive ones."""
    assert wall_clock_violation(f"🕐 -{amount}{unit} fa", _GUARD_NOW) is not None
    assert wall_clock_violation(f"🕐 {amount}{unit} fa", _GUARD_NOW) is None


async def test_h4_reanchor_refuses_anything_but_exactly_one_row(
    frozen_world: LegacyWorld,
) -> None:
    """An empty key, no match or two matches fail; a NULL key part matches with ``IS``."""
    w = frozen_world
    history = [("2026-02-01 10:00:00", "100.00"), ("2026-02-20 10:00:00", "90.00")]
    product = await seed_product(w, OWNER, KETTLE_URL, "Kettle", initial="100.00", history=history)
    with pytest.raises(HarnessFault, match="empty key"):
        await reanchor(w, "products", {}, "name", "x")
    with pytest.raises(HarnessFault, match="0 rows matched"):
        await reanchor(w, "products", {"id": 999}, "name", "x")
    with pytest.raises(HarnessFault, match="2 rows matched"):
        await reanchor(w, "price_history", {"product_id": product}, "price", "1.00")
    await w.conn.execute("INSERT INTO notification_prefs(user_id, product_id) VALUES (10, NULL)")
    await w.conn.commit()
    await reanchor(
        w, "notification_prefs", {"user_id": OWNER, "product_id": None}, "timezone", "UTC"
    )
    cursor = await w.conn.execute(
        "SELECT timezone FROM notification_prefs WHERE user_id = 10 AND product_id IS NULL"
    )
    assert [tuple(row) for row in await cursor.fetchall()] == [("UTC",)]


async def test_h4_seeded_product_timestamps_are_anchored(frozen_world: LegacyWorld) -> None:
    """A seeded row carries fixed timestamps, so a later ``now`` write shows as a change."""
    w = frozen_world
    product = await seed_product(w, OWNER, KETTLE_URL, "Kettle", initial="100.00")
    paused = await seed_product(
        w, OWNER, "https://shop.example.com/item/2", "Fan", initial="5.00", active=False
    )
    cursor = await w.conn.execute(
        "SELECT id, created_at, updated_at, is_active FROM products ORDER BY id"
    )
    assert [tuple(row) for row in await cursor.fetchall()] == [
        (product, "2026-02-28 09:00:00", "2026-02-28 09:00:00", 1),
        (paused, "2026-02-28 09:00:00", "2026-02-28 09:00:00", 0),
    ]
    await w.recorder.command(OWNER, f"/pausa {product}")
    lines = w.recorder.snapshot("harness.anchored").render().splitlines()
    step1_db = lines[lines.index("## db after step 1") + 1 :]
    assert any(
        re.fullmatch(
            rf'~ products id={product} updated_at: "2026-02-28 09:00:00" -> <now:[9 :-]+>', line
        )
        for line in step1_db
    )


async def test_h1_exchange_rates_do_not_leak_into_a_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rate loaded into the process before the world is built is not seen inside it."""
    from price_tracker.bot import decorators

    monkeypatch.setitem(decorators._ECB_RATES, "USD", Decimal("2"))
    async with open_world(LOCALES[0], monkeypatch):
        assert decorators._ECB_RATES == {}
        assert decorators._convert_display(Decimal("10"), "USD") == "USD 10.00 (~€9.20)"


async def test_h14_malformed_recorder_and_stub_calls_are_refused(
    frozen_world: LegacyWorld,
) -> None:
    """Wrong job arguments, empty scripts, unanchored presses and non-exceptions fail loudly."""
    w = frozen_world
    r = w.recorder
    with pytest.raises(TypeError, match="run_check_all takes no arguments"):
        await r.job("run_check_all", interval_minutes=60)
    with pytest.raises(TypeError, match="takes only interval_minutes"):
        await r.job("digest_flush_due")
    with pytest.raises(TypeError, match="takes only interval_minutes"):
        await r.job("digest_flush_due", interval_minutes=60, limit=1)
    with pytest.raises(ValueError, match="unknown job"):
        await r.job("digest_flush_all")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="at least one outcome"):
        w.scraper.script(KETTLE_URL)
    with pytest.raises(HarnessFault, match="has no message_id"):
        await r.press(OWNER, "zz", on=Call("answerCallbackQuery", {}))
    with pytest.raises(TypeError, match="only receives exceptions"):
        await r.call_error_handler(OWNER, KeyboardInterrupt())
    assert r.snapshot("harness.refused").render().count("## step") == 0

    async def seed_error() -> None:
        w.errors.append(RuntimeError("seed boom"))

    await r.seed("failing seed", seed_error())
    lines = r.snapshot("harness.refused").render().splitlines()
    assert lines[4:] == [
        '## step 1: seed "failing seed"',
        "   !! error_handler: RuntimeError: seed boom",
    ]


# ── T-U: update mechanism ────────────────────────────────────────────


def test_u1_missing_snapshot_fails_with_the_command_to_create_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without the switch, a missing file fails naming the path and the update switch."""
    monkeypatch.delenv("LEGACY_SNAPSHOTS_UPDATE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    path = tmp_path / "harness" / "update.it.txt"
    with pytest.raises(Failed) as failed:
        sample_snapshot().compare_or_update(path, root=tmp_path)
    assert str(path) in str(failed.value)
    assert "LEGACY_SNAPSHOTS_UPDATE=1 pytest '" in str(failed.value)
    assert "::test_u1_missing_snapshot_fails_with_the_command_to_create_it'" in str(failed.value)
    assert not path.exists()


def test_u2_update_creates_once_then_leaves_the_file_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The switch writes the rendering once; an identical second run does not touch it."""
    monkeypatch.setenv("LEGACY_SNAPSHOTS_UPDATE", "1")
    monkeypatch.delenv("CI", raising=False)
    path = tmp_path / "harness" / "update.it.txt"
    snapshot = sample_snapshot()
    with pytest.warns(LegacySnapshotUpdated):
        snapshot.compare_or_update(path, root=tmp_path)
    assert path.read_text(encoding="utf-8") == snapshot.render()
    assert (tmp_path / GENERATED_WITH).is_file()
    first_mtime = path.stat().st_mtime_ns
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        snapshot.compare_or_update(path, root=tmp_path)
    assert not [w for w in caught if issubclass(w.category, LegacySnapshotUpdated)]
    assert path.stat().st_mtime_ns == first_mtime
    assert path.read_text(encoding="utf-8") == snapshot.render()

    changed = sample_snapshot(('## step 1: command "/list" user=10',))
    with pytest.warns(LegacySnapshotUpdated):
        changed.compare_or_update(path, root=tmp_path)
    assert path.read_text(encoding="utf-8") == changed.render()


def test_u3_update_is_refused_under_ci(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With ``CI`` set, the update switch raises before anything reaches the disk."""
    monkeypatch.setenv("LEGACY_SNAPSHOTS_UPDATE", "1")
    monkeypatch.setenv("CI", "true")
    path = tmp_path / "harness" / "update.it.txt"
    with pytest.raises(RuntimeError, match="refusing to rewrite legacy snapshots under CI"):
        sample_snapshot().compare_or_update(path, root=tmp_path)
    assert not path.exists()
    assert list(tmp_path.rglob("*")) == []


def test_u4_different_content_fails_with_a_unified_diff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A committed file that differs fails with the full unified diff."""
    monkeypatch.delenv("LEGACY_SNAPSHOTS_UPDATE", raising=False)
    monkeypatch.delenv("CI", raising=False)
    path = tmp_path / "harness" / "update.it.txt"
    old = sample_snapshot(('## step 1: command "/lista" user=10',))
    new = sample_snapshot(('## step 1: command "/list" user=10',))
    path.parent.mkdir(parents=True)
    path.write_text(old.render(), encoding="utf-8", newline="\n")
    with pytest.raises(Failed) as failed:
        new.compare_or_update(path, root=tmp_path)
    message = str(failed.value)
    assert f"--- {path}" in message
    assert "+++ actual" in message
    assert '-## step 1: command "/lista" user=10' in message
    assert '+## step 1: command "/list" user=10' in message
    assert "LEGACY_SNAPSHOTS_UPDATE=1" in message
    assert path.read_text(encoding="utf-8") == old.render()
    unchanged_mtime = path.stat().st_mtime_ns
    old.compare_or_update(path, root=tmp_path)
    assert path.stat().st_mtime_ns == unchanged_mtime


@pytest.mark.parametrize("value", ["0", "yes"])
def test_u5_only_the_value_one_enables_updating(
    value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Any value other than ``1`` behaves as an unset switch."""
    monkeypatch.setenv("LEGACY_SNAPSHOTS_UPDATE", value)
    monkeypatch.delenv("CI", raising=False)
    path = tmp_path / "harness" / "update.it.txt"
    with pytest.raises(Failed, match="missing snapshot"):
        sample_snapshot().compare_or_update(path, root=tmp_path)
    assert not path.exists()


# ── scenarios ────────────────────────────────────────────────────────
#
# One function per scenario, registered in SCENARIOS under "<area>.<name>". A
# scenario seeds what it needs, then drives the steps in order; it asserts
# nothing, the snapshot file is the oracle. Product ids come from the seeds.

FAN_URL: Final = "https://shop.example.com/item/2"
LAMP_URL: Final = "https://shop.example.com/item/3"
ITEM4_URL: Final = "https://shop.example.com/item/4"
ITEM5_URL: Final = "https://shop.example.com/item/5"
PAUSED_URL: Final = "https://shop.example.com/item/8"
WIDGET_URL: Final = "https://shop.example.com/item/9"
AMAZON_URL: Final = "https://www.amazon.com/dp/B0FIXTURE1"
AMAZON_URLS: Final = (AMAZON_URL, *(f"https://www.amazon.com/dp/B0FIXTURE{n}" for n in (2, 3)))
ITEM7_URL: Final = "https://shop.example.com/item/7"

KETTLE_HISTORY: Final = (
    ("2026-01-15 10:00:00", "100.00"),
    ("2026-02-01 10:00:00", "100.00"),
    ("2026-02-10 10:00:00", "100.00"),
    ("2026-02-20 10:00:00", "90.00"),
    ("2026-02-28 10:00:00", "80.00"),
)
"""Five points of history: the outlier filter's minimum."""

LN81: Final = "Long " + "N" * 76
"""An 81-character name: every truncation of a displayed name falls on an ``N``."""
LN61: Final = "Long " + "N" * 56
"""A 61-character name, one past the ``> 60`` boundary of ``/lista``."""


def item_url(n: int) -> str:
    """The example-shop URL of item ``n``."""
    return f"https://shop.example.com/item/{n}"


async def seed_cheap_and_near(w: LegacyWorld) -> tuple[int, int]:
    """Cheap (0.80 -> 0.64, under one euro) and Near (100.00 -> 99.50, under 1 %).

    Both pages read their current price again; returns their ids.
    """
    cheap = await seed_product(w, OWNER, item_url(72), "Cheap", initial="0.80", current="0.64")
    w.scraper.script(item_url(72), info("0.64", name="Cheap"))
    near = await seed_product(w, OWNER, item_url(73), "Near", initial="100.00", current="99.50")
    w.scraper.script(item_url(73), info("99.50", name="Near"))
    return cheap, near


def scenario(scenario_id: str) -> Callable[[ScenarioFn], ScenarioFn]:
    """Register the decorated function as scenario ``scenario_id``."""

    def register(fn: ScenarioFn) -> ScenarioFn:
        assert scenario_id not in SCENARIOS, f"duplicate scenario {scenario_id}"
        SCENARIOS[scenario_id] = fn
        return fn

    return register


async def seed_p1(w: LegacyWorld, **state: Any) -> int:
    """P1: OWNER's Kettle, 100.00 -> 80.00 with five points of history."""
    defaults: dict[str, Any] = {
        "initial": "100.00",
        "current": "80.00",
        "lowest": "80.00",
        "highest": "100.00",
        "last_checked_at": "2026-03-01 10:00:00",
        "history": KETTLE_HISTORY,
    }
    return await seed_product(w, OWNER, KETTLE_URL, "Kettle", **(defaults | state))


async def seed_long_p1(w: LegacyWorld) -> int:
    """P1's state and history under the 81-character name, read again at 80.00."""
    product = await seed_product(
        w,
        OWNER,
        KETTLE_URL,
        LN81,
        initial="100.00",
        current="80.00",
        lowest="80.00",
        highest="100.00",
        last_checked_at="2026-03-01 10:00:00",
        history=KETTLE_HISTORY,
    )
    w.scraper.script(KETTLE_URL, info("80.00", name=LN81))
    return product


async def seed_p2(w: LegacyWorld, **state: Any) -> int:
    """P2: OWNER's Fan in USD, without a current price."""
    defaults: dict[str, Any] = {"initial": "50.00", "current": None, "currency": "USD"}
    return await seed_product(w, OWNER, FAN_URL, "Fan", **(defaults | state))


async def seed_p3(w: LegacyWorld) -> int:
    """P3: OTHER's Lamp at 30.00."""
    return await seed_product(w, OTHER, LAMP_URL, "Lamp", initial="30.00", current="30.00")


async def seed_pp(w: LegacyWorld) -> int:
    """PP: OWNER's paused Old Fan."""
    return await seed_product(w, OWNER, PAUSED_URL, "Old Fan", initial="45.00", active=False)


def info(price: str, *, name: str = "Kettle", currency: str = "EUR") -> ProductInfo:
    """A scraped product page with a price."""
    return ProductInfo(name=name, price=Decimal(price), currency=currency)


def script_drop(w: LegacyWorld) -> None:
    """DROP: P1's page now reads 64.00, a 20 % drop the outlier filter accepts."""
    w.scraper.script(KETTLE_URL, info("64.00"))


async def seed_command(w: LegacyWorld, user: int, text: str) -> None:
    """Run a command as a listed, uncaptured seed step."""
    await w.recorder.seed(json.dumps(text, ensure_ascii=False), w.run_command(user, text))


async def open_menu(w: LegacyWorld, user: int) -> Call:
    """Send ``/menu`` as ``user`` and return the menu message, to press buttons on it."""
    await w.recorder.command(user, "/menu")
    return w.request.calls_of("sendMessage")[-1]


async def seed_lock_until(w: LegacyWorld, domain: str, until: str) -> None:
    """Move the lock expiry of ``domain`` and reload the health manager (seed steps)."""
    await w.recorder.seed(
        f"lock {domain} until {until}",
        reanchor(w, "scraper_health", {"domain": domain}, "locked_until", until),
    )
    await w.recorder.seed("reload domain health", w.health.load())


async def seed_locked_and_half_open(w: LegacyWorld) -> None:
    """example.com locked (P1 must not be read); amazon.com half-open with two products.

    The first Amazon probe fails without a block, so the domain stays half-open
    and the second Amazon product must not be read either.
    """
    await seed_p1(w)
    script_drop(w)
    first, second = AMAZON_URLS[:2]
    await seed_product(w, OWNER, first, "Amazon 1", initial="25.00")
    w.scraper.script(first, ParseError("no price"))
    await seed_product(w, OWNER, second, "Amazon 2", initial="25.00")
    w.scraper.script(second, info("20.00", name="Amazon 2"))
    await seed_blocks(w, "example.com", "http_403")
    await seed_blocks(w, "amazon.com", "http_403")
    await seed_lock_until(w, "amazon.com", "2026-03-01T11:00:00+00:00")


async def seed_job(w: LegacyWorld) -> None:
    """Run a scheduler sweep as a listed, uncaptured seed step."""
    await w.recorder.seed("job run_check_all", w.scheduler.run_check_all())


# home ─────────────────────────────────────────────────────────────────


@scenario("home.start")
async def scenario_home_start(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/start")


@scenario("home.menu_user")
async def scenario_home_menu_user(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/menu")


@scenario("home.menu_admin")
async def scenario_home_menu_admin(w: LegacyWorld) -> None:
    await w.recorder.command(ADMIN, "/menu")


@scenario("home.help_alias")
async def scenario_home_help_alias(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/help")


@scenario("home.unauthorized")
async def scenario_home_unauthorized(w: LegacyWorld) -> None:
    await w.recorder.command(STRANGER, "/start")
    await w.recorder.command(STRANGER, "/lista")


@scenario("home.admin_only_refused")
async def scenario_home_admin_only_refused(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/adduser 99")
    await w.recorder.command(OWNER, "/intervallo")
    await w.recorder.command(OWNER, "/health")
    await w.recorder.command(OWNER, "/debug x")


@scenario("home.error_handler")
async def scenario_home_error_handler(w: LegacyWorld) -> None:
    await w.recorder.call_error_handler(OWNER, RuntimeError("boom"))
    # A handler whose reply is refused (the user blocked the bot) reaches the
    # error handler, which apologizes; when the apology is refused too, it is
    # swallowed instead of escaping the error handler.
    w.request.fail_next_call_to(OWNER)
    await w.recorder.command(OWNER, "/start")
    w.request.fail_next_call_to(OWNER)
    await w.recorder.call_error_handler(OWNER, RuntimeError("boom"))


# lista ────────────────────────────────────────────────────────────────


@scenario("lista.empty")
async def scenario_lista_empty(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/lista")


@scenario("lista.rich")
async def scenario_lista_rich(w: LegacyWorld) -> None:
    await seed_p1(w, target="70.00", check_interval=90, errors=2)
    await seed_p2(w)
    await w.recorder.command(OWNER, "/lista")


@scenario("lista.increase_and_min")
async def scenario_lista_increase_and_min(w: LegacyWorld) -> None:
    await seed_p1(w, initial="60.00", current="80.00", lowest="55.00")
    await w.recorder.command(OWNER, "/lista")


@scenario("lista.alias_list")
async def scenario_lista_alias_list(w: LegacyWorld) -> None:
    await seed_p1(w)
    await w.recorder.command(OWNER, "/list")


@scenario("lista.other_user_sees_own_only")
async def scenario_lista_other_user_sees_own_only(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p3(w)
    await w.recorder.command(OTHER, "/lista")


@scenario("lista.short_interval")
async def scenario_lista_short_interval(w: LegacyWorld) -> None:
    await seed_p1(w, check_interval=30)
    await w.recorder.command(OWNER, "/lista")


@scenario("lista.edges")
async def scenario_lista_edges(w: LegacyWorld) -> None:
    # A 61-character name, a drop under 1 %, a whole-hour interval, one error;
    # and a product under one euro.
    await seed_product(
        w,
        OWNER,
        item_url(74),
        LN61,
        initial="100.00",
        current="99.50",
        check_interval=60,
        errors=1,
    )
    await seed_product(w, OWNER, item_url(75), "Sticker", initial="0.90", current="0.80")
    await w.recorder.command(OWNER, "/lista")


# menu ─────────────────────────────────────────────────────────────────
# Every scenario opens the menu with a command and presses on its messages.


@scenario("menu.main")
async def scenario_menu_main(w: LegacyWorld) -> None:
    await seed_p1(w)
    menu = await open_menu(w, OWNER)
    await w.recorder.press(OWNER, "menu_main", on=menu)


@scenario("menu.main_admin")
async def scenario_menu_main_admin(w: LegacyWorld) -> None:
    menu = await open_menu(w, ADMIN)
    await w.recorder.press(ADMIN, "menu_main", on=menu)


@scenario("menu.prodotti")
async def scenario_menu_prodotti(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p2(w)
    await seed_pp(w)
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_prodotti")


@scenario("menu.prodotti_empty")
async def scenario_menu_prodotti_empty(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_prodotti")


@scenario("menu.prodotti_overflow")
async def scenario_menu_prodotti_overflow(w: LegacyWorld) -> None:
    for n in range(1, 13):
        price = f"{9 + n}.00"
        await seed_product(
            w,
            OWNER,
            f"https://shop.example.com/item/{n}",
            f"Item {n:02d}",
            initial=price,
            current=price,
        )
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_prodotti")
    # Every list view has its own page size; each one overflows here.
    for data in ("menu_prezzi", "menu_storia", "menu_notifiche"):
        menu = await open_menu(w, OWNER)
        await w.recorder.press(OWNER, data, on=menu)
    # One product fewer: the product view shows exactly one overflow row.
    await seed_command(w, OWNER, "/pausa 12")
    menu = await open_menu(w, OWNER)
    await w.recorder.press(OWNER, "menu_prodotti", on=menu)


@scenario("menu.cmd_lista")
async def scenario_menu_cmd_lista(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p2(w)
    menu = await open_menu(w, OWNER)
    await w.recorder.press(OWNER, "menu_prodotti")
    await w.recorder.press(OWNER, "cmd_lista", on=menu)


@scenario("menu.cmd_lista_empty")
async def scenario_menu_cmd_lista_empty(w: LegacyWorld) -> None:
    menu = await open_menu(w, OWNER)
    await w.recorder.press(OWNER, "cmd_lista", on=menu)


@scenario("menu.paused")
async def scenario_menu_paused(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_pp(w)
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_prodotti")
    await w.recorder.press(OWNER, "menu_paused")
    # Eleven paused products: the paused view shows ten.
    for n in range(80, 90):
        await seed_product(w, OWNER, item_url(n), f"Paused {n}", initial="5.00", active=False)
    menu = await open_menu(w, OWNER)
    await w.recorder.press(OWNER, "menu_prodotti", on=menu)
    await w.recorder.press(OWNER, "menu_paused", on=menu)


@scenario("menu.prezzi")
async def scenario_menu_prezzi(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p2(w)
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_prezzi")


@scenario("menu.prezzi_empty")
async def scenario_menu_prezzi_empty(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_prezzi")


@scenario("menu.checkall")
async def scenario_menu_checkall(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p2(w)
    script_drop(w)
    w.scraper.script(FAN_URL, info("50.00", name="Fan", currency="USD"))
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_prezzi")
    await w.recorder.press(OWNER, "menu_checkall")


@scenario("menu.checkall_empty")
async def scenario_menu_checkall_empty(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_prezzi")
    await w.recorder.press(OWNER, "menu_checkall")


@scenario("menu.storia")
async def scenario_menu_storia(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p2(w)
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_prezzi")
    await w.recorder.press(OWNER, "menu_storia")


@scenario("menu.notifiche")
async def scenario_menu_notifiche(w: LegacyWorld) -> None:
    await seed_p1(w, target="70.00")
    await seed_p2(w, threshold=("absolute", "5"))
    await seed_product(w, OWNER, ITEM4_URL, "Speaker", initial="20.00", threshold=("any_drop", "0"))
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_notifiche")


@scenario("menu.dati")
async def scenario_menu_dati(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_pp(w)
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_dati")


@scenario("menu.esporta")
async def scenario_menu_esporta(w: LegacyWorld) -> None:
    await seed_p1(w, target="70.00")
    await seed_pp(w)
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_dati")
    await w.recorder.press(OWNER, "menu_esporta")


@scenario("menu.esporta_empty")
async def scenario_menu_esporta_empty(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_dati")
    await w.recorder.press(OWNER, "menu_esporta")


@scenario("menu.importa_info")
async def scenario_menu_importa_info(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_dati")
    await w.recorder.press(OWNER, "menu_importa_info")


@scenario("menu.info_user")
async def scenario_menu_info_user(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_pp(w)
    await w.recorder.command(OWNER, "/menu")
    await w.recorder.press(OWNER, "menu_info")


@scenario("menu.info_admin_saved_interval")
async def scenario_menu_info_admin_saved_interval(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p3(w)
    await seed_config(w, "check_interval_minutes", "90")
    await w.recorder.command(ADMIN, "/menu")
    await w.recorder.press(ADMIN, "menu_info")


@scenario("menu.unknown_callback")
async def scenario_menu_unknown_callback(w: LegacyWorld) -> None:
    menu = await open_menu(w, OWNER)
    await w.recorder.press(OWNER, "does_not_exist", on=menu)


@scenario("menu.not_allowed_user")
async def scenario_menu_not_allowed_user(w: LegacyWorld) -> None:
    await w.recorder.press(STRANGER, "menu_main")


@scenario("menu.checkall_no_change")
async def scenario_menu_checkall_no_change(w: LegacyWorld) -> None:
    await seed_p1(w)
    w.scraper.script(KETTLE_URL, info("80.00"))
    await open_menu(w, OWNER)
    await w.recorder.press(OWNER, "menu_prezzi")
    await w.recorder.press(OWNER, "menu_checkall")
    await seed_cheap_and_near(w)
    menu = await open_menu(w, OWNER)
    await w.recorder.press(OWNER, "menu_prezzi", on=menu)
    await w.recorder.press(OWNER, "menu_checkall", on=menu)


@scenario("menu.long_names")
async def scenario_menu_long_names(w: LegacyWorld) -> None:
    # Every menu list cuts the name at its own width.
    await seed_long_p1(w)
    await seed_product(w, OWNER, item_url(74), LN81, initial="45.00", active=False)
    for data in ("menu_prodotti", "menu_prezzi", "menu_storia", "menu_notifiche"):
        menu = await open_menu(w, OWNER)
        await w.recorder.press(OWNER, data, on=menu)
        if data == "menu_prodotti":
            await w.recorder.press(OWNER, "menu_paused", on=menu)
        elif data == "menu_prezzi":
            await w.recorder.press(OWNER, "menu_checkall", on=menu)


# product ──────────────────────────────────────────────────────────────


@scenario("product.edit")
async def scenario_product_edit(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, f"edit_{p1}")


@scenario("product.edit_no_reset")
async def scenario_product_edit_no_reset(w: LegacyWorld) -> None:
    p1 = await seed_p1(w, current="100.00")
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, f"edit_{p1}")


@scenario("product.edit_not_found")
async def scenario_product_edit_not_found(w: LegacyWorld) -> None:
    await seed_p1(w)
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, "edit_999")
    await w.recorder.press(OWNER, "edit_abc")


@scenario("product.ownership")
async def scenario_product_ownership(w: LegacyWorld) -> None:
    await seed_p1(w)
    p3 = await seed_p3(w)
    await w.recorder.press(OWNER, f"edit_{p3}")
    await w.recorder.press(ADMIN, f"edit_{p3}")


@scenario("product.pause_button")
async def scenario_product_pause_button(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, f"pause_{p1}")


@scenario("product.remove_button")
async def scenario_product_remove_button(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, f"remove_{p1}")


@scenario("product.confirm_delete")
async def scenario_product_confirm_delete(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, f"remove_{p1}")
    await w.recorder.press(OWNER, f"confirm_delete_{p1}")


@scenario("product.confirm_delete_not_found")
async def scenario_product_confirm_delete_not_found(w: LegacyWorld) -> None:
    await seed_p1(w)
    await w.recorder.press(OWNER, "confirm_delete_999")
    await w.recorder.press(OWNER, "confirm_delete_x")


@scenario("product.cancel_delete")
async def scenario_product_cancel_delete(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, f"remove_{p1}")
    await w.recorder.press(OWNER, "cancel_delete")


@scenario("product.delete_all_prompt")
async def scenario_product_delete_all_prompt(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p2(w)
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, "delete_all")


@scenario("product.delete_all_empty")
async def scenario_product_delete_all_empty(w: LegacyWorld) -> None:
    await w.recorder.press(OWNER, "delete_all")


@scenario("product.confirmdeleteall")
async def scenario_product_confirmdeleteall(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p2(w)
    await seed_pp(w)
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, "delete_all")
    await w.recorder.press(OWNER, "confirmdeleteall")


@scenario("product.reset_button")
async def scenario_product_reset_button(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, f"edit_{p1}")
    await w.recorder.press(OWNER, f"reset_{p1}")


@scenario("product.reset_button_no_current")
async def scenario_product_reset_button_no_current(w: LegacyWorld) -> None:
    p2 = await seed_p2(w)
    await w.recorder.press(OWNER, f"reset_{p2}")


@scenario("product.reactivate_button")
async def scenario_product_reactivate_button(w: LegacyWorld) -> None:
    pp = await seed_pp(w)
    await w.recorder.command(OWNER, "/riattiva")
    await w.recorder.press(OWNER, f"reactivate_{pp}")


@scenario("product.check_button_drop")
async def scenario_product_check_button_drop(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    script_drop(w)
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, f"check_{p1}")


@scenario("product.check_button_no_change")
async def scenario_product_check_button_no_change(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    w.scraper.script(KETTLE_URL, info("80.00"))
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, f"check_{p1}")
    cheap, near = await seed_cheap_and_near(w)
    # The press above wrote SQLite's real clock; the card below prints its age.
    await w.recorder.seed(
        "reanchor last_checked_at",
        reanchor(w, "products", {"id": p1}, "last_checked_at", "2026-03-01 11:00:00"),
    )
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, f"check_{cheap}")
    await w.recorder.press(OWNER, f"check_{near}")


@scenario("product.check_button_scrape_error")
async def scenario_product_check_button_scrape_error(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    w.scraper.script(KETTLE_URL, ParseError("no price"))
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, f"check_{p1}")


@scenario("product.check_button_not_found")
async def scenario_product_check_button_not_found(w: LegacyWorld) -> None:
    await seed_p1(w)
    await w.recorder.press(OWNER, "check_999")
    await w.recorder.press(OWNER, "check_x")


@scenario("product.chart_button")
async def scenario_product_chart_button(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.command(OWNER, "/lista")
    await w.recorder.press(OWNER, f"chart_{p1}")


@scenario("product.chart_insufficient")
async def scenario_product_chart_insufficient(w: LegacyWorld) -> None:
    p2 = await seed_p2(w, history=[("2026-02-01 10:00:00", "50.00")])
    await w.recorder.press(OWNER, f"chart_{p2}")


@scenario("product.pref_buttons")
async def scenario_product_pref_buttons(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    for prefix in ("pref_new_", "pref_used_", "pref_amazon_", "pref_anyseller_", "pref_default_"):
        await w.recorder.press(OWNER, f"{prefix}{p1}")


@scenario("product.pref_invalid_id")
async def scenario_product_pref_invalid_id(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.press(OWNER, "pref_new_x")
    await w.recorder.press(OTHER, f"pref_new_{p1}")


@scenario("product.track_any")
async def scenario_product_track_any(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.press(OWNER, f"track_any_{p1}")
    await w.recorder.press(OTHER, f"track_any_{p1}")
    await w.recorder.press(OWNER, "track_any_999")


@scenario("product.track_default")
async def scenario_product_track_default(w: LegacyWorld) -> None:
    p1 = await seed_p1(w, threshold=("absolute", "5"))
    await w.recorder.press(OWNER, f"track_default_{p1}")
    await w.recorder.press(OTHER, f"track_default_{p1}")


@scenario("product.cmd_delete")
async def scenario_product_cmd_delete(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    p2 = await seed_p2(w)
    await w.recorder.command(OWNER, "/elimina")
    await w.recorder.command(OWNER, f"/elimina {p1}")
    await w.recorder.command(OWNER, "/elimina abc")
    await w.recorder.command(OWNER, "/elimina 999")
    await w.recorder.command(OWNER, f"/delete {p2}")
    long_id = await seed_product(w, OWNER, item_url(71), LN81, initial="80.00")
    await w.recorder.command(OWNER, "/elimina")
    await w.recorder.command(OWNER, f"/elimina {long_id}")


@scenario("product.cmd_delete_empty")
async def scenario_product_cmd_delete_empty(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/elimina")


@scenario("product.cmd_target")
async def scenario_product_cmd_target(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.command(OWNER, "/target")
    for args in (f"{p1}", f"{p1} 49,90", f"{p1} 90", f"{p1} 0", f"{p1} abc", "abc 1", "999 1"):
        await w.recorder.command(OWNER, f"/target {args}")
    # The smallest positive target, then a long name in the picker and the reply.
    await w.recorder.command(OWNER, f"/target {p1} 1")
    long_id = await seed_product(w, OWNER, item_url(70), LN81, initial="80.00", current="80.00")
    await w.recorder.command(OWNER, "/target")
    await w.recorder.command(OWNER, f"/target {long_id} 50")


@scenario("product.cmd_threshold")
async def scenario_product_cmd_threshold(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.command(OWNER, "/soglia")
    for args in (f"{p1}", f"{p1} 20%", f"{p1} 50", f"{p1} ogni"):
        await w.recorder.command(OWNER, f"/soglia {args}")
    await w.recorder.command(OWNER, f"/threshold {p1} abc")
    await w.recorder.command(OWNER, "/soglia 999 5")
    await w.recorder.command(OWNER, "/soglia abc 5")
    await w.recorder.command(OWNER, f"/soglia {p1} abc%")
    # A signed percentage is stored without its sign; a long name is cut at 80.
    await w.recorder.command(OWNER, f"/soglia {p1} -20%")
    long_id = await seed_product(w, OWNER, item_url(62), LN81, initial="80.00")
    await w.recorder.command(OWNER, f"/soglia {long_id} 20%")


@scenario("product.buttons_bad_ids")
async def scenario_product_buttons_bad_ids(w: LegacyWorld) -> None:
    await seed_p1(w)
    for prefix in ("pause_", "remove_", "reset_", "reactivate_", "chart_"):
        await w.recorder.press(OWNER, f"{prefix}x")
        await w.recorder.press(OWNER, f"{prefix}999")
    await w.recorder.press(OWNER, "track_any_x")
    await w.recorder.press(OWNER, "track_default_x")


@scenario("product.check_button_no_drop")
async def scenario_product_check_button_no_drop(w: LegacyWorld) -> None:
    p1 = await seed_p1(w, current="100.00")
    w.scraper.script(KETTLE_URL, info("100.00"), info("120.00"))
    await w.recorder.command(OWNER, "/lista")
    card = next(c for c in w.request.calls_of("sendMessage") if f"check_{p1}" in c.callback_data())
    await w.recorder.press(OWNER, f"check_{p1}", on=card)
    await w.recorder.press(OWNER, f"check_{p1}", on=card)


@scenario("product.cmd_delete_single")
async def scenario_product_cmd_delete_single(w: LegacyWorld) -> None:
    await seed_p1(w)
    await w.recorder.command(OWNER, "/elimina")


@scenario("product.cmd_target_no_current")
async def scenario_product_cmd_target_no_current(w: LegacyWorld) -> None:
    p2 = await seed_p2(w)
    await w.recorder.command(OWNER, f"/target {p2} 30")


@scenario("product.long_name")
async def scenario_product_long_name(w: LegacyWorld) -> None:
    # Every product button that names the product, on one 81-character name;
    # the delete confirmation last.
    product = await seed_long_p1(w)
    for prefix in (
        "edit_",
        "reset_",
        "pause_",
        "reactivate_",
        "remove_",
        "check_",
        "chart_",
        "pref_new_",
        "track_any_",
        "track_default_",
        "confirm_delete_",
    ):
        await w.recorder.press(OWNER, f"{prefix}{product}")


# add ──────────────────────────────────────────────────────────────────


@scenario("add.usage")
async def scenario_add_usage(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/add")


@scenario("add.success_generic")
async def scenario_add_success_generic(w: LegacyWorld) -> None:
    w.scraper.script(WIDGET_URL, info("19.90", name="Widget"))
    await w.recorder.command(OWNER, f"/add {WIDGET_URL}")


@scenario("add.success_amazon")
async def scenario_add_success_amazon(w: LegacyWorld) -> None:
    w.scraper.script(AMAZON_URL, ProductInfo(name="Amazon Widget", price=Decimal("25.00")))
    await w.recorder.command(OWNER, f"/aggiungi {AMAZON_URL}")


@scenario("add.duplicate_active")
async def scenario_add_duplicate_active(w: LegacyWorld) -> None:
    await seed_p1(w)
    await w.recorder.command(OWNER, f"/add {KETTLE_URL}")


@scenario("add.duplicate_paused")
async def scenario_add_duplicate_paused(w: LegacyWorld) -> None:
    await seed_pp(w)
    await w.recorder.command(OWNER, f"/add {PAUSED_URL}")


@scenario("add.unsafe_url")
async def scenario_add_unsafe_url(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/add http://127.0.0.1/x")
    await w.recorder.command(OWNER, "/add ftp://shop.example.com/x")


@scenario("add.no_scraper")
async def scenario_add_no_scraper(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/add https://unknown.example.org/p")


@scenario("add.blocked")
async def scenario_add_blocked(w: LegacyWorld) -> None:
    w.scraper.script(WIDGET_URL, HTTPBlockStatus(status=403, url=WIDGET_URL))
    await w.recorder.command(OWNER, f"/add {WIDGET_URL}")


@scenario("add.price_none")
async def scenario_add_price_none(w: LegacyWorld) -> None:
    w.scraper.script(WIDGET_URL, ProductInfo(error="Prezzo non trovato (test)"))
    await w.recorder.command(OWNER, f"/add {WIDGET_URL}")


@scenario("add.success_long_name")
async def scenario_add_success_long_name(w: LegacyWorld) -> None:
    # 81 characters: one past the 80 the confirmation shows before "...".
    w.scraper.script(WIDGET_URL, ProductInfo(name=LN81, price=Decimal("19.90"), currency="EUR"))
    await w.recorder.command(OWNER, f"/add {WIDGET_URL}")


@scenario("add.paste_link")
async def scenario_add_paste_link(w: LegacyWorld) -> None:
    w.scraper.script(WIDGET_URL, info("19.90", name="Widget"))
    await w.recorder.text(OWNER, f"guarda {WIDGET_URL}.")


@scenario("add.paste_link_unauthorized")
async def scenario_add_paste_link_unauthorized(w: LegacyWorld) -> None:
    await w.recorder.text(STRANGER, WIDGET_URL)


# data ─────────────────────────────────────────────────────────────────

CSV_HEADER: Final = (
    "ID",
    "Nome",
    "URL",
    "Prezzo Iniziale",
    "Prezzo Attuale",
    "Prezzo Min",
    "Target",
    "Soglia",
    "Attivo",
    "Valuta",
)
"""The header ``/esporta`` writes and ``/importa`` reads."""


def csv_document(*rows: tuple[str, str, str]) -> bytes:
    """A CSV in the export layout; each row gives its URL, target and threshold."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for url, target, threshold in rows:
        writer.writerow(["", "", url, "", "", "", target, threshold, "Si", "EUR"])
    return buf.getvalue().encode("utf-8")


@scenario("data.export")
async def scenario_data_export(w: LegacyWorld) -> None:
    await seed_p1(w, target="70.00")
    await seed_pp(w)
    await w.recorder.command(OWNER, "/esporta")
    await w.recorder.command(OWNER, "/export")


@scenario("data.export_empty")
async def scenario_data_export_empty(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/esporta")


@scenario("data.import_help")
async def scenario_data_import_help(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/importa")


@scenario("data.import_csv")
async def scenario_data_import_csv(w: LegacyWorld) -> None:
    await seed_p1(w)
    new_url = "https://shop.example.com/item/20"
    w.scraper.script(new_url, ProductInfo(name="Imported", price=Decimal("12.00")))
    content = csv_document(
        (new_url, "9.5", "absolute:2"),
        (KETTLE_URL, "", "percentage:10"),
        ("http://127.0.0.1/x", "", "percentage:10"),
    )
    await w.recorder.document(OWNER, "prodotti.csv", content)


@scenario("data.import_csv_malformed")
async def scenario_data_import_csv_malformed(w: LegacyWorld) -> None:
    await w.recorder.document(OWNER, "x.csv", b"\xff\xfe garbage")


@scenario("data.import_csv_semicolon")
async def scenario_data_import_csv_semicolon(w: LegacyWorld) -> None:
    w.scraper.script(item_url(21), ProductInfo(name="X", price=Decimal("3.00")))
    content = b"ID;Nome;URL\r\n1;X;https://shop.example.com/item/21\r\n"
    await w.recorder.document(OWNER, "excel.csv", content)


@scenario("data.import_csv_bad_fields")
async def scenario_data_import_csv_bad_fields(w: LegacyWorld) -> None:
    # One bad field per row: a bad Target alone is ignored (the row imports without
    # a target), a bad threshold alone is an error, a row without URL is skipped.
    bad_target_url = "https://shop.example.com/item/22"
    bad_threshold_url = "https://shop.example.com/item/24"
    w.scraper.script(bad_target_url, ProductInfo(name="Bad", price=Decimal("5.00")))
    w.scraper.script(bad_threshold_url, ProductInfo(name="Worse", price=Decimal("6.00")))
    content = b"\xef\xbb\xbf" + csv_document(
        (bad_target_url, "abc", "percentage:10"),
        (bad_threshold_url, "", "absolute:abc"),
        ("", "", "percentage:10"),
    )
    await w.recorder.document(OWNER, "bad.csv", content)


@scenario("data.import_csv_threshold_colon")
async def scenario_data_import_csv_threshold_colon(w: LegacyWorld) -> None:
    # A threshold with two colons: its value "15:5" is not a number.
    url = item_url(25)
    w.scraper.script(url, ProductInfo(name="Colon", price=Decimal("7.00")))
    content = b"\xef\xbb\xbf" + csv_document((url, "", "percentage:15:5"))
    await w.recorder.document(OWNER, "colon.csv", content)


@scenario("data.import_csv_empty")
async def scenario_data_import_csv_empty(w: LegacyWorld) -> None:
    await w.recorder.document(OWNER, "empty.csv", b"")


@scenario("data.import_not_csv")
async def scenario_data_import_not_csv(w: LegacyWorld) -> None:
    await w.recorder.document(OWNER, "note.txt", b"x")


@scenario("data.import_uppercase_extension")
async def scenario_data_import_uppercase_extension(w: LegacyWorld) -> None:
    await w.recorder.document(OWNER, "PRODOTTI.CSV", csv_document())


@scenario("data.import_csv_edge_rows")
async def scenario_data_import_csv_edge_rows(w: LegacyWorld) -> None:
    new_url = "https://shop.example.com/item/23"
    w.scraper.script(new_url, ProductInfo(name="Plain", price=Decimal("7.00")))
    content = csv_document(
        ("https://unknown.example.org/p", "", "percentage:10"), (new_url, "", "absolute")
    )
    await w.recorder.document(OWNER, "edge.csv", content)


# monitor ──────────────────────────────────────────────────────────────


@scenario("monitor.check_picker")
async def scenario_monitor_check_picker(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p2(w)
    await w.recorder.command(OWNER, "/check")
    await seed_product(w, OWNER, item_url(65), LN81, initial="80.00", current="80.00")
    await w.recorder.command(OWNER, "/check")


@scenario("monitor.check_no_change")
async def scenario_monitor_check_no_change(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    w.scraper.script(KETTLE_URL, info("80.00"))
    await w.recorder.command(OWNER, f"/check {p1}")
    # A rise on a long name: the fresh price is shown, the name cut at 80.
    long_id = await seed_product(w, OWNER, item_url(64), LN81, initial="100.00", current="80.00")
    w.scraper.script(item_url(64), info("85.00", name=LN81))
    await w.recorder.command(OWNER, f"/check {long_id}")


@scenario("monitor.check_drop_photo")
async def scenario_monitor_check_drop_photo(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    script_drop(w)
    await w.recorder.command(OWNER, f"/controlla {p1}")


@scenario("monitor.check_drop_text")
async def scenario_monitor_check_drop_text(w: LegacyWorld) -> None:
    p1 = await seed_p1(w, history=())
    script_drop(w)
    await w.recorder.command(OWNER, f"/check {p1}")


@scenario("monitor.check_paused")
async def scenario_monitor_check_paused(w: LegacyWorld) -> None:
    pp = await seed_pp(w)
    await w.recorder.command(OWNER, f"/check {pp}")


@scenario("monitor.check_errors")
async def scenario_monitor_check_errors(w: LegacyWorld) -> None:
    await seed_p1(w)
    await w.recorder.command(OWNER, "/check abc")
    await w.recorder.command(OWNER, "/check 999")


@scenario("monitor.checkall")
async def scenario_monitor_checkall(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p2(w)
    script_drop(w)
    w.scraper.script(FAN_URL, info("50.00", name="Fan", currency="USD"))
    await w.recorder.command(OWNER, "/checkall")


@scenario("monitor.checkall_empty")
async def scenario_monitor_checkall_empty(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/checkall")


@scenario("monitor.refresh")
async def scenario_monitor_refresh(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.command(OWNER, "/refresh")
    for minutes in ("", " 30", " 90", " 120", " 0", " 3", " 99999", " abc"):
        await w.recorder.command(OWNER, f"/refresh {p1}{minutes}")
    await w.recorder.command(OWNER, "/refresh 999 30")
    await w.recorder.command(OWNER, "/refresh abc 30")
    # Each bound of the per-product interval, and a whole hour.
    for minutes in (" 1", " 5", " 10081", " 60"):
        await w.recorder.command(OWNER, f"/refresh {p1}{minutes}")
    long_id = await seed_product(w, OWNER, item_url(63), LN81, initial="80.00")
    for minutes in (" 0", " 30"):
        await w.recorder.command(OWNER, f"/refresh {long_id}{minutes}")


@scenario("monitor.pause")
async def scenario_monitor_pause(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await seed_p2(w)
    await w.recorder.command(OWNER, "/pausa")
    await w.recorder.command(OWNER, f"/pausa {p1}")
    await w.recorder.command(OWNER, "/pause abc")
    await w.recorder.command(OWNER, "/pause 999")
    long_id = await seed_product(w, OWNER, item_url(67), LN81, initial="80.00")
    await w.recorder.command(OWNER, f"/pausa {long_id}")


@scenario("monitor.reactivate")
async def scenario_monitor_reactivate(w: LegacyWorld) -> None:
    pp = await seed_pp(w)
    await seed_p2(w)
    await w.recorder.command(OWNER, "/riattiva")
    await w.recorder.command(OWNER, f"/riattiva {pp}")
    await w.recorder.command(OWNER, "/reactivate abc")
    await w.recorder.command(OWNER, "/reactivate 999")
    long_id = await seed_product(w, OWNER, item_url(66), LN81, initial="80.00", active=False)
    await w.recorder.command(OWNER, "/riattiva")
    await w.recorder.command(OWNER, f"/riattiva {long_id}")


@scenario("monitor.reactivate_none_paused")
async def scenario_monitor_reactivate_none_paused(w: LegacyWorld) -> None:
    await seed_p1(w)
    await w.recorder.command(OWNER, "/riattiva")


@scenario("monitor.pickers_empty")
async def scenario_monitor_pickers_empty(w: LegacyWorld) -> None:
    for text in ("/check", "/pausa", "/refresh", "/target", "/soglia"):
        await w.recorder.command(OWNER, text)


@scenario("monitor.checkall_increase")
async def scenario_monitor_checkall_increase(w: LegacyWorld) -> None:
    await seed_p1(w)
    w.scraper.script(KETTLE_URL, info("120.00"))
    await w.recorder.command(OWNER, "/checkall")


@scenario("monitor.checkall_quarantine")
async def scenario_monitor_checkall_quarantine(w: LegacyWorld) -> None:
    await seed_locked_and_half_open(w)
    await w.recorder.command(OWNER, "/checkall")


@scenario("monitor.checkall_failures")
async def scenario_monitor_checkall_failures(w: LegacyWorld) -> None:
    for n, url in enumerate(AMAZON_URLS, start=1):
        await seed_product(w, OWNER, url, f"Amazon {n}", initial="25.00")
        w.scraper.script(url, HTTPBlockStatus(status=403, url=url))
    failures: tuple[tuple[str, str, BaseException], ...] = (
        (ITEM4_URL, "Gone", ListingGone(status=410, url=ITEM4_URL)),
        (ITEM5_URL, "Offline", httpx.ConnectError("connection refused")),
        (ITEM7_URL, "Broken", RuntimeError("scraper crashed")),
    )
    for url, name, failure in failures:
        await seed_product(w, OWNER, url, name, initial="40.00")
        w.scraper.script(url, failure)
    await w.recorder.command(OWNER, "/checkall")


@scenario("monitor.checkall_edges")
async def scenario_monitor_checkall_edges(w: LegacyWorld) -> None:
    # A long name with a drop under 1 %, and a product under one euro.
    await seed_product(w, OWNER, item_url(74), LN61, initial="100.00", current="99.50")
    w.scraper.script(item_url(74), info("99.50", name=LN61))
    await seed_product(w, OWNER, item_url(75), "Sticker", initial="0.90", current="0.80")
    w.scraper.script(item_url(75), info("0.80", name="Sticker"))
    await w.recorder.command(OWNER, "/checkall")


async def seed_drop_with_history(w: LegacyWorld, url: str, name: str) -> int:
    """A product at 80.00 with P1's history whose page now reads 64.00; its id."""
    product = await seed_product(
        w,
        OWNER,
        url,
        name,
        initial="100.00",
        current="80.00",
        lowest="80.00",
        highest="100.00",
        history=KETTLE_HISTORY,
    )
    w.scraper.script(url, info("64.00", name=name))
    return product


@scenario("monitor.check_drop_chart_error")
async def scenario_monitor_check_drop_chart_error(w: LegacyWorld) -> None:
    # A dollar pair in the name is read as mathtext by the chart title, which
    # fails; the alert falls back to plain text.
    product = await seed_drop_with_history(w, item_url(90), r"Cable $\zz$")
    await w.recorder.command(OWNER, f"/check {product}")


@scenario("monitor.check_drop_long_caption")
async def scenario_monitor_check_drop_long_caption(w: LegacyWorld) -> None:
    # A caption over Telegram's 1024-character limit is cut to it.
    product = await seed_drop_with_history(w, item_url(91), "K" * 1100)
    await w.recorder.command(OWNER, f"/check {product}")


# history ──────────────────────────────────────────────────────────────


@scenario("history.picker")
async def scenario_history_picker(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p2(w)
    await w.recorder.command(OWNER, "/storia")
    await seed_product(w, OWNER, item_url(68), LN81, initial="80.00")
    await w.recorder.command(OWNER, "/storia")


@scenario("history.picker_empty")
async def scenario_history_picker_empty(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/storia")


@scenario("history.chart")
async def scenario_history_chart(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.command(OWNER, f"/history {p1}")


@scenario("history.insufficient")
async def scenario_history_insufficient(w: LegacyWorld) -> None:
    p2 = await seed_p2(w, history=[("2026-02-01 10:00:00", "50.00")])
    await w.recorder.command(OWNER, f"/storia {p2}")


@scenario("history.errors")
async def scenario_history_errors(w: LegacyWorld) -> None:
    await seed_p1(w)
    await w.recorder.command(OWNER, "/storia abc")
    await w.recorder.command(OWNER, "/storia 999")


@scenario("history.reset")
async def scenario_history_reset(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    p2 = await seed_p2(w)
    await w.recorder.command(OWNER, "/reset")
    await w.recorder.command(OWNER, f"/reset {p1}")
    await w.recorder.command(OWNER, f"/azzera {p2}")
    await w.recorder.command(OWNER, "/reset abc")
    await w.recorder.command(OWNER, "/reset 999")
    long_id = await seed_product(w, OWNER, item_url(69), LN81, initial="100.00", current="80.00")
    await w.recorder.command(OWNER, f"/reset {long_id}")


@scenario("history.chart_edges")
async def scenario_history_chart_edges(w: LegacyWorld) -> None:
    in_window = ("2026-02-01 10:00:00", "2026-02-20 10:00:00")
    # HA: 51 characters whose only closing dollar is the 51st, so the title and
    # the caption (both cut at 50) hold a single dollar; two change points.
    ha_name = r"Cable $\zz " + "x" * 39 + "$"
    # HB: two points under one euro. HC: two points, both outside the window.
    # HD: a dollar pair in the title, which the chart cannot render (a known defect).
    charts = (
        (item_url(92), ha_name, (("50.00", "40.00"), in_window)),
        (item_url(93), "Sticker", (("0.90", "0.80"), in_window)),
        (
            item_url(94),
            "Old Points",
            (("60.00", "55.00"), ("2025-10-01 10:00:00", "2025-11-01 10:00:00")),
        ),
        (item_url(95), r"Cable $\zz$", (("50.00", "40.00"), in_window)),
    )
    for url, name, (prices, stamps) in charts:
        product = await seed_product(
            w,
            OWNER,
            url,
            name,
            initial=prices[0],
            current=prices[1],
            lowest=prices[1],
            highest=prices[0],
            history=tuple(zip(stamps, prices, strict=True)),
        )
        await w.recorder.command(OWNER, f"/storia {product}")


# settings ─────────────────────────────────────────────────────────────


async def reanchor_digest(w: LegacyWorld, *stamps: str) -> None:
    """Re-anchor ``enqueued_at`` of digest rows 1, 2... to seeded instants (seed steps)."""
    for row_id, stamp in enumerate(stamps, start=1):
        await w.recorder.seed(
            "reanchor enqueued_at",
            reanchor(w, "digest_queue", {"id": row_id}, "enqueued_at", stamp),
        )


@scenario("settings.interval")
async def scenario_settings_interval(w: LegacyWorld) -> None:
    for text in ("/intervallo", "/intervallo 120", "/setinterval 3", "/intervallo 99999"):
        await w.recorder.command(ADMIN, text)
    await w.recorder.command(ADMIN, "/intervallo abc")
    await w.recorder.command(ADMIN, "/intervallo 30")
    # The minimum, a whole hour, and one past the maximum.
    for text in ("/intervallo 5", "/intervallo 60", "/intervallo 10081"):
        await w.recorder.command(ADMIN, text)


@scenario("settings.mute")
async def scenario_settings_mute(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    for text in ("/mute", f"/mute {p1} forever", "/mute abc", "/mute all x", "/mute all 0"):
        await w.recorder.command(OWNER, text)
    await w.recorder.command(OWNER, "/mute 999")
    await w.recorder.command(OWNER, "/mute all 48")
    await w.recorder.command(OWNER, "/mute all 1")


@scenario("settings.unmute")
async def scenario_settings_unmute(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    for text in ("/mute", "/unmute", f"/mute {p1} 2", f"/unmute {p1}", "/unmute abc"):
        await w.recorder.command(OWNER, text)
    await w.recorder.command(OWNER, "/unmute 999")


@scenario("settings.digest_mode")
async def scenario_settings_digest_mode(w: LegacyWorld) -> None:
    for args in ("", " on", " on 30", " on 0", " on abc", " off"):
        await w.recorder.command(OWNER, f"/digest_mode{args}")
    await w.recorder.command(OWNER, "/digest_mode on 1")


@scenario("settings.quiet_hours")
async def scenario_settings_quiet_hours(w: LegacyWorld) -> None:
    for args in ("", " 22:00-08:00", " 2200-0800", " 25:00-08:00", " 08:00-08:00", " off"):
        await w.recorder.command(OWNER, f"/quiet_hours{args}")
    await w.recorder.command(OWNER, "/quiet_hours 2200")
    await w.recorder.command(OWNER, "/quiet_hours ab:cd-08:00")
    # Out-of-range minutes and hours at each bound, a zero hour, and three times.
    for args in (" 22:75-08:00", " 00:30-08:00", " 24:00-08:00", " 22:60-08:00"):
        await w.recorder.command(OWNER, f"/quiet_hours{args}")
    await w.recorder.command(OWNER, "/quiet_hours 22:00-08:00-09:00")


@scenario("settings.timezone")
async def scenario_settings_timezone(w: LegacyWorld) -> None:
    for args in ("", " Europe/Rome", " Mars/Olympus"):
        await w.recorder.command(OWNER, f"/timezone{args}")


@scenario("settings.throttle")
async def scenario_settings_throttle(w: LegacyWorld) -> None:
    for args in ("", " 5", " abc", " 0", " off"):
        await w.recorder.command(OWNER, f"/throttle{args}")


@scenario("settings.prefs")
async def scenario_settings_prefs(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    for text in ("/prefs", f"/mute {p1} forever", f"/prefs {p1}", "/prefs abc", "/prefs 0"):
        await w.recorder.command(OWNER, text)
    # The global view after a per-product row exists, then a global and a
    # per-product row that disagree on every field the view prints.
    await w.recorder.command(OWNER, "/prefs")
    await seed_command(w, OWNER, "/digest_mode on 30")
    await seed_command(w, OWNER, "/timezone America/New_York")
    per_product: dict[str, object] = {"user_id": OWNER, "product_id": p1}
    for column, value in (
        ("quiet_hours_start", "22:00"),
        ("quiet_hours_end", "08:00"),
        ("throttle_per_hour", 3),
    ):
        await w.recorder.seed(
            f"per-product {column}", reanchor(w, "notification_prefs", per_product, column, value)
        )
    await w.recorder.command(OWNER, "/prefs")
    await w.recorder.command(OWNER, f"/prefs {p1}")


@scenario("settings.digest_now_empty")
async def scenario_settings_digest_now_empty(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/digest_now")
    # A pending row that no longer parses is flushed (quarantined), never kept.
    await w.recorder.seed(
        "enqueue unparseable row",
        w.repo.enqueue_digest(user_id=OWNER, product_id=None, payload="{not json"),
    )
    await w.recorder.command(OWNER, "/digest_now")


@scenario("settings.digest_now_flush")
async def scenario_settings_digest_now_flush(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await seed_p2(w)
    script_drop(w)
    w.scraper.script(FAN_URL, info("40.00", name="Fan", currency="USD"))
    # A 73-character name: over the digest row's 60-character budget.
    long_name = "Ultra Quiet Stainless Steel Electric Kettle With Temperature Control 1.7L"
    await seed_product(w, OWNER, ITEM4_URL, long_name, initial="40.00", current="40.00")
    w.scraper.script(ITEM4_URL, info("30.00", name=long_name))
    await seed_command(w, OWNER, "/digest_mode on")
    await w.recorder.job("run_check_all")
    await reanchor_digest(w, "2026-03-01 10:00:00", "2026-03-01 10:00:01")
    # Sparse rows: a price row without a product name, a warning and a suspension
    # without counts, and a row that no longer parses.
    sparse: tuple[tuple[int | None, dict[str, object]], ...] = (
        (
            p1,
            {
                "kind": "price",
                "old_price": "80.00",
                "new_price": "64.00",
                "currency": "EUR",
                "domain": "example.com",
            },
        ),
        (None, {"kind": "operational", "event": "warning", "domain": "example.com"}),
        (
            None,
            {
                "kind": "operational",
                "event": "suspended",
                "domain": "example.com",
                "reason": "parse_error",
            },
        ),
    )
    for product_id, payload in sparse:
        await w.recorder.seed(
            "enqueue sparse row",
            w.digest.enqueue(user_id=OWNER, product_id=product_id, payload=payload),
        )
    await w.recorder.seed(
        "enqueue unparseable row",
        w.repo.enqueue_digest(user_id=OWNER, product_id=None, payload="{not json"),
    )
    await w.recorder.command(OWNER, "/digest_now")


@scenario("settings.first_and_repeat")
async def scenario_settings_first_and_repeat(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/quiet_hours off")
    await w.recorder.command(OTHER, "/throttle off")
    await w.recorder.command(ADMIN, "/unmute")
    await w.recorder.command(OWNER, "/timezone Europe/Rome")
    await w.recorder.command(OWNER, "/throttle 5")


# admin ────────────────────────────────────────────────────────────────


@scenario("admin.cmd_adduser")
async def scenario_admin_cmd_adduser(w: LegacyWorld) -> None:
    for text in ("/adduser", "/adduser abc", "/adduser 99", f"/adduser {OWNER}"):
        await w.recorder.command(ADMIN, text)
    # The new user has blocked the bot: the welcome is lost, the user is added.
    w.request.fail_next_call_to(98)
    await w.recorder.command(ADMIN, "/adduser 98")


@scenario("admin.cmd_removeuser")
async def scenario_admin_cmd_removeuser(w: LegacyWorld) -> None:
    await seed_user(w, 2, admin=True)
    for args in ("", " abc", f" {ADMIN}", " 2", f" {OWNER}", " 12345"):
        await w.recorder.command(ADMIN, f"/removeuser{args}")


@scenario("admin.cmd_users")
async def scenario_admin_cmd_users(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p3(w)
    await seed_user(w, OTHER, display_name="Other", username="oth")
    await w.recorder.command(ADMIN, "/users")
    await w.recorder.command(ADMIN, "/utenti")


@scenario("admin.cmd_nick")
async def scenario_admin_cmd_nick(w: LegacyWorld) -> None:
    for text in ("/nick", "/nick abc x", "/nick 12345 Bob", f"/nick {OWNER} Bob Smith"):
        await w.recorder.command(ADMIN, text)


@scenario("admin.menu")
async def scenario_admin_menu(w: LegacyWorld) -> None:
    await open_menu(w, ADMIN)
    await w.recorder.press(ADMIN, "menu_admin")


@scenario("admin.menu_saved_interval")
async def scenario_admin_menu_saved_interval(w: LegacyWorld) -> None:
    await seed_config(w, "check_interval_minutes", "90")
    await open_menu(w, ADMIN)
    await w.recorder.press(ADMIN, "menu_admin")


@scenario("admin.menu_non_admin")
async def scenario_admin_menu_non_admin(w: LegacyWorld) -> None:
    for data in ("menu_admin", "menu_admin_users", f"admin_rm_{OTHER}"):
        await w.recorder.press(OWNER, data)


@scenario("admin.users")
async def scenario_admin_users(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p3(w)
    await open_menu(w, ADMIN)
    await w.recorder.press(ADMIN, "menu_admin")
    await w.recorder.press(ADMIN, "menu_admin_users")


@scenario("admin.adduser_prompt")
async def scenario_admin_adduser_prompt(w: LegacyWorld) -> None:
    await open_menu(w, ADMIN)
    await w.recorder.press(ADMIN, "menu_admin")
    await w.recorder.press(ADMIN, "menu_admin_adduser")
    await w.recorder.text(ADMIN, "abc")
    await w.recorder.text(ADMIN, "99")
    # Re-armed prompt, the new user has blocked the bot: still added.
    await w.recorder.press(ADMIN, "menu_admin_adduser")
    w.request.fail_next_call_to(98)
    await w.recorder.text(ADMIN, "98")


@scenario("admin.adduser_prompt_existing")
async def scenario_admin_adduser_prompt_existing(w: LegacyWorld) -> None:
    await w.recorder.press(ADMIN, "menu_admin_adduser")
    await w.recorder.text(ADMIN, str(OWNER))


@scenario("admin.adduser_prompt_cancel")
async def scenario_admin_adduser_prompt_cancel(w: LegacyWorld) -> None:
    await w.recorder.press(ADMIN, "menu_admin_adduser")
    await w.recorder.text(ADMIN, "annulla")
    await w.recorder.text(ADMIN, "99")


@scenario("admin.removeuser_menu")
async def scenario_admin_removeuser_menu(w: LegacyWorld) -> None:
    await open_menu(w, ADMIN)
    await w.recorder.press(ADMIN, "menu_admin")
    await w.recorder.press(ADMIN, "menu_admin_removeuser")


@scenario("admin.removeuser_menu_none")
async def scenario_admin_removeuser_menu_none(w: LegacyWorld) -> None:
    await reanchor(w, "users", {"user_id": OWNER}, "is_admin", 1)
    await reanchor(w, "users", {"user_id": OTHER}, "is_admin", 1)
    await w.recorder.press(ADMIN, "menu_admin_removeuser")


@scenario("admin.rm_user")
async def scenario_admin_rm_user(w: LegacyWorld) -> None:
    await w.recorder.press(ADMIN, "menu_admin_removeuser")
    await w.recorder.press(ADMIN, f"admin_rm_{OTHER}")
    await w.recorder.press(ADMIN, "admin_rm_x")


@scenario("admin.nick_menu")
async def scenario_admin_nick_menu(w: LegacyWorld) -> None:
    await w.recorder.press(ADMIN, "menu_admin_nick")


@scenario("admin.nick_prompt")
async def scenario_admin_nick_prompt(w: LegacyWorld) -> None:
    await w.recorder.press(ADMIN, "menu_admin_nick")
    await w.recorder.press(ADMIN, f"admin_nick_{OWNER}")
    await w.recorder.text(ADMIN, "Bob")
    await w.recorder.press(ADMIN, "admin_nick_x")
    await w.recorder.press(ADMIN, f"admin_nick_{OWNER}")
    await w.recorder.text(ADMIN, "   ")


@scenario("admin.interval_prompt")
async def scenario_admin_interval_prompt(w: LegacyWorld) -> None:
    await w.recorder.press(ADMIN, "menu_admin_interval")
    for text in ("abc", "3", "99999", "60"):
        await w.recorder.text(ADMIN, text)
    await w.recorder.press(ADMIN, "menu_admin_interval")
    await w.recorder.text(ADMIN, "30")
    # One past the maximum is refused and keeps the prompt; the minimum is taken.
    await w.recorder.press(ADMIN, "menu_admin_interval")
    await w.recorder.text(ADMIN, "10081")
    await w.recorder.text(ADMIN, "5")
    # A whole hour, then the info screen that reads the saved interval back.
    await w.recorder.press(ADMIN, "menu_admin_interval")
    await w.recorder.text(ADMIN, "60")
    await w.recorder.command(ADMIN, "/menu")
    await w.recorder.press(ADMIN, "menu_info")


@scenario("admin.debug_prompt")
async def scenario_admin_debug_prompt(w: LegacyWorld) -> None:
    w.scraper.script(KETTLE_URL, ProductInfo(name="Kettle", price=Decimal("80.00")))
    await w.recorder.press(ADMIN, "menu_admin_debug")
    await w.recorder.text(ADMIN, "notaurl")
    await w.recorder.press(ADMIN, "menu_admin_debug")
    await w.recorder.text(ADMIN, KETTLE_URL)
    await w.recorder.text(ADMIN, "http:/x")


@scenario("admin.cmd_debug")
async def scenario_admin_cmd_debug(w: LegacyWorld) -> None:
    w.scraper.script(KETTLE_URL, ProductInfo(name="Kettle", price=Decimal("80.00")))
    await w.recorder.command(ADMIN, "/debug")
    await w.recorder.command(ADMIN, f"/debug {KETTLE_URL}")
    await w.recorder.command(ADMIN, "/debug https://unknown.example.org/p")
    # A long URL cut in the header and in the error line, and a long scraped name.
    await w.recorder.command(ADMIN, "/debug http:/" + "x" * 90)
    long_url = "https://shop.example.com/item/44"
    w.scraper.script(long_url, ProductInfo(name=LN81, price=Decimal("80.00")))
    await w.recorder.command(ADMIN, f"/debug {long_url}")


@scenario("admin.submenus_non_admin")
async def scenario_admin_submenus_non_admin(w: LegacyWorld) -> None:
    for data in (
        "menu_admin_adduser",
        "menu_admin_removeuser",
        "menu_admin_nick",
        f"admin_nick_{OWNER}",
        "menu_admin_interval",
        "menu_admin_debug",
    ):
        await w.recorder.press(OWNER, data)


# status ───────────────────────────────────────────────────────────────


async def seed_blocks(w: LegacyWorld, domain: str, reason: str) -> None:
    """Three block events on ``domain``, as seed steps: the domain enters quarantine."""
    for n in range(1, 4):
        await w.recorder.seed(f"record block {n}", w.health.record_block(domain, reason=reason))


@scenario("status.user")
async def scenario_status_user(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_pp(w)
    await w.recorder.command(OWNER, "/stato")


@scenario("status.admin")
async def scenario_status_admin(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_p3(w)
    await seed_config(w, "check_interval_minutes", "90")
    await w.recorder.command(ADMIN, "/status")


@scenario("status.health_empty")
async def scenario_status_health_empty(w: LegacyWorld) -> None:
    await w.recorder.command(ADMIN, "/health")


@scenario("status.health_locked")
async def scenario_status_health_locked(w: LegacyWorld) -> None:
    await seed_blocks(w, "blocked.example.com", "http_403")
    await w.recorder.command(ADMIN, "/health")


@scenario("status.errori_none")
async def scenario_status_errori_none(w: LegacyWorld) -> None:
    await seed_p1(w)
    await w.recorder.command(OWNER, "/errori")


@scenario("status.errori")
async def scenario_status_errori(w: LegacyWorld) -> None:
    await seed_p1(
        w, errors=3, last_error="parse_error: no price", last_error_at="2026-03-01 11:30:00"
    )
    await seed_p2(w, errors=1, last_error="http_error: 503", last_error_at="2026-02-28 11:00:00")
    await seed_blocks(w, "example.com", "http_403")
    await w.recorder.command(OWNER, "/errori")
    await w.recorder.command(OWNER, "/errors")
    # A long name with a long error an hour old, and an error exactly a day old.
    await seed_product(
        w,
        OWNER,
        item_url(60),
        LN81,
        initial="80.00",
        errors=1,
        last_error="parse_error: " + "e" * 140,
        last_error_at="2026-03-01 11:00:00",
    )
    await seed_product(
        w,
        OWNER,
        item_url(61),
        "P4",
        initial="50.00",
        errors=1,
        last_error="x",
        last_error_at="2026-02-28 12:00:00",
    )
    await w.recorder.command(OWNER, "/errori")


@scenario("status.user_short_interval")
async def scenario_status_user_short_interval(w: LegacyWorld) -> None:
    await seed_config(w, "check_interval_minutes", "30")
    await w.recorder.command(OWNER, "/stato")


@scenario("status.health_half_open")
async def scenario_status_health_half_open(w: LegacyWorld) -> None:
    await seed_p1(
        w, errors=1, last_error="parse_error: no price", last_error_at="2026-03-01 11:30:00"
    )
    await seed_product(w, OWNER, AMAZON_URL, "Amazon Widget", initial="25.00", errors=2)
    await seed_blocks(w, "example.com", "http_403")
    await seed_blocks(w, "blocked.example.com", "http_403")
    await seed_lock_until(w, "example.com", "2026-03-01T11:00:00+00:00")
    await seed_lock_until(w, "blocked.example.com", "2026-03-01T12:30:00+00:00")
    await w.recorder.command(ADMIN, "/health")
    await w.recorder.command(OWNER, "/errori")


@scenario("status.user_hour_interval")
async def scenario_status_user_hour_interval(w: LegacyWorld) -> None:
    await seed_config(w, "check_interval_minutes", "60")
    await w.recorder.command(OWNER, "/stato")


@scenario("status.health_many_domains")
async def scenario_status_health_many_domains(w: LegacyWorld) -> None:
    # Seven domains with a block: six single blocks a minute apart, and one domain
    # locked for one more second. The list of recent blocks keeps the newest five.
    for n in range(1, 7):
        domain = f"d{n}.example.net"
        await w.recorder.seed(
            f"record block {domain}", w.health.record_block(domain, reason="http_403")
        )
        await w.recorder.seed(
            f"reanchor {domain}",
            reanchor(
                w,
                "scraper_health",
                {"domain": domain},
                "last_block_at",
                f"2026-03-01T11:0{n}:00+00:00",
            ),
        )
    await w.recorder.seed("reload domain health", w.health.load())
    await seed_blocks(w, "edge.example.net", "http_403")
    await seed_lock_until(w, "edge.example.net", "2026-03-01T12:00:01+00:00")
    await w.recorder.command(ADMIN, "/health")


@scenario("status.uptime_zero")
async def scenario_status_uptime_zero(w: LegacyWorld) -> None:
    # Started at this very instant (production sets the start before polling, so
    # it never reads zero): the frozen monotonic clock gives an uptime of 0.
    w.app.bot_data["start_time"] = time.monotonic()
    await w.recorder.command(OWNER, "/stato")


# text ─────────────────────────────────────────────────────────────────


@scenario("text.no_pending")
async def scenario_text_no_pending(w: LegacyWorld) -> None:
    await w.recorder.text(OWNER, "ciao")


# alert ────────────────────────────────────────────────────────────────
# The scheduler and the notifier run for real; no Telegram update comes in,
# except the preference commands some scenarios run as seeds.


async def seed_quiet_rome(w: LegacyWorld) -> None:
    """Quiet hours 12:00-15:00 in Europe/Rome: the frozen 12:00 UTC is 13:00 there."""
    await seed_command(w, OWNER, "/timezone Europe/Rome")
    await seed_command(w, OWNER, "/quiet_hours 12:00-15:00")


async def seed_three_drops(w: LegacyWorld) -> None:
    """P1 (DROP), P2 read at 40.00 USD and a second Lamp of OWNER read at 30.00."""
    await seed_p1(w)
    await seed_p2(w)
    await seed_product(w, OWNER, ITEM4_URL, "Lamp", initial="40.00", current="40.00")
    script_drop(w)
    w.scraper.script(FAN_URL, info("40.00", name="Fan", currency="USD"))
    w.scraper.script(ITEM4_URL, info("30.00", name="Lamp"))


async def seed_two_failing(w: LegacyWorld) -> int:
    """P1 and a Speaker on the same domain whose pages no longer show a price; P1's id."""
    p1 = await seed_p1(w)
    await seed_product(w, OWNER, ITEM4_URL, "Speaker", initial="40.00", current="40.00")
    w.scraper.script(KETTLE_URL, ParseError("no price"))
    w.scraper.script(ITEM4_URL, ParseError("no price"))
    return p1


async def seed_two_drops_in_digest(w: LegacyWorld) -> None:
    """P1 (DROP) and P2 read at 40.00 USD, with digest mode on."""
    await seed_p1(w)
    await seed_p2(w)
    script_drop(w)
    w.scraper.script(FAN_URL, info("40.00", name="Fan", currency="USD"))
    await seed_command(w, OWNER, "/digest_mode on")


@scenario("alert.price_drop")
async def scenario_alert_price_drop(w: LegacyWorld) -> None:
    # One earlier failure, so the reset of the error count on a good read shows.
    await seed_p1(w, errors=1)
    await seed_p3(w)
    script_drop(w)
    w.scraper.script(LAMP_URL, info("30.00", name="Lamp"))
    await w.recorder.job("run_check_all")


@scenario("alert.cooldown")
async def scenario_alert_cooldown(w: LegacyWorld) -> None:
    await seed_p1(w)
    w.scraper.script(KETTLE_URL, info("64.00"), info("80.00"), info("70.00"), info("56.00"))
    for _ in range(4):
        await w.recorder.job("run_check_all")


@scenario("alert.target_crossing")
async def scenario_alert_target_crossing(w: LegacyWorld) -> None:
    await seed_p1(w, threshold=("percentage", "50"), target="70.00")
    w.scraper.script(KETTLE_URL, info("69.00"))
    await w.recorder.job("run_check_all")


@scenario("alert.back_in_stock")
async def scenario_alert_back_in_stock(w: LegacyWorld) -> None:
    await seed_p1(w, available=False)
    w.scraper.script(KETTLE_URL, ProductInfo(price=Decimal("80.00"), available=True))
    await w.recorder.job("run_check_all")
    # Back in stock again with digest mode on: the queued payload keeps the old price.
    await seed_command(w, OWNER, "/digest_mode on")
    await w.recorder.seed(
        "out of stock again", reanchor(w, "products", {"id": 1}, "is_available", 0)
    )
    await w.recorder.job("run_check_all")


@scenario("alert.muted")
async def scenario_alert_muted(w: LegacyWorld) -> None:
    await seed_p1(w)
    script_drop(w)
    await seed_command(w, OWNER, "/mute")
    await w.recorder.job("run_check_all")


@scenario("alert.quiet_digest")
async def scenario_alert_quiet_digest(w: LegacyWorld) -> None:
    await seed_p1(w)
    script_drop(w)
    await seed_quiet_rome(w)
    await seed_command(w, OWNER, "/digest_mode on")
    await w.recorder.job("run_check_all")


@scenario("alert.quiet_dropped")
async def scenario_alert_quiet_dropped(w: LegacyWorld) -> None:
    await seed_p1(w)
    script_drop(w)
    await seed_quiet_rome(w)
    await w.recorder.job("run_check_all")


@scenario("alert.throttled_digest")
async def scenario_alert_throttled_digest(w: LegacyWorld) -> None:
    await seed_three_drops(w)
    await seed_command(w, OWNER, "/throttle 1")
    await seed_command(w, OWNER, "/digest_mode on")
    # A send 90 minutes ago, outside the hour: evicted, so it does not count.
    await w.recorder.seed(
        "stale throttle timestamp",
        reanchor(
            w,
            "notification_prefs",
            {"user_id": OWNER, "product_id": None},
            "throttle_state_json",
            '{"ts": [1772361000.0]}',
        ),
    )
    await w.recorder.job("run_check_all")


@scenario("alert.throttled_dropped")
async def scenario_alert_throttled_dropped(w: LegacyWorld) -> None:
    await seed_three_drops(w)
    await seed_command(w, OWNER, "/throttle 1")
    await w.recorder.job("run_check_all")


@scenario("alert.quiet_operational")
async def scenario_alert_quiet_operational(w: LegacyWorld) -> None:
    await seed_p1(w)
    w.scraper.script(KETTLE_URL, ParseError("no price"))
    w.scheduler.deps.max_consecutive_errors = 4
    await seed_quiet_rome(w)
    await w.recorder.job("run_check_all")
    await w.recorder.job("run_check_all")
    await w.recorder.command(OWNER, "/digest_now")
    # A good read, then the same failures again: the repeated warning carries the
    # same event id under the frozen clock, and a queued event is not queued twice.
    w.scraper.script(KETTLE_URL, info("80.00"), ParseError("no price"))
    for _ in range(3):
        await w.recorder.job("run_check_all")
    await w.recorder.command(OWNER, "/digest_now")


@scenario("alert.digest_flush_due")
async def scenario_alert_digest_flush_due(w: LegacyWorld) -> None:
    await seed_two_drops_in_digest(w)
    await w.recorder.job("run_check_all")
    await reanchor_digest(w, "2026-03-01 10:00:00", "2026-03-01 10:00:01")
    await w.recorder.job("digest_flush_due", interval_minutes=60)


@scenario("alert.digest_not_due")
async def scenario_alert_digest_not_due(w: LegacyWorld) -> None:
    await seed_two_drops_in_digest(w)
    await seed_job(w)
    await reanchor_digest(w, "2026-03-01 11:30:00", "2026-03-01 11:30:01")
    await w.recorder.job("digest_flush_due", interval_minutes=60)
    # OWNER's own 30-minute interval makes the same rows due; OTHER has no
    # preference row, so the job's 60-minute fallback applies to a 2-hour-old row.
    await seed_command(w, OWNER, "/digest_mode on 30")
    lamp = {
        "kind": "price",
        "product_name": "Lamp",
        "old_price": "30.00",
        "new_price": "25.00",
        "currency": "EUR",
        "domain": "example.com",
    }
    await w.recorder.seed(
        "enqueue for OTHER", w.digest.enqueue(user_id=OTHER, product_id=None, payload=lamp)
    )
    await w.recorder.seed(
        "reanchor enqueued_at",
        reanchor(w, "digest_queue", {"id": 3}, "enqueued_at", "2026-03-01 10:00:00"),
    )
    await w.recorder.job("digest_flush_due", interval_minutes=60)


@scenario("alert.operational_warning")
async def scenario_alert_operational_warning(w: LegacyWorld) -> None:
    await seed_two_failing(w)
    await w.recorder.job("run_check_all")


@scenario("alert.operational_suspended")
async def scenario_alert_operational_suspended(w: LegacyWorld) -> None:
    await seed_two_failing(w)
    # A third failing product whose last good read is too wide for the notice's
    # budget, so its currency code is shortened.
    await seed_product(
        w,
        OWNER,
        ITEM5_URL,
        "Vault",
        initial="100.00",
        current="123456789012345678.00",
        currency="CHF",
        last_checked_at="2026-03-01 10:00:00",
    )
    w.scraper.script(ITEM5_URL, ParseError("no price"))
    await w.recorder.job("run_check_all")
    await w.recorder.job("run_check_all")


@scenario("alert.listing_gone")
async def scenario_alert_listing_gone(w: LegacyWorld) -> None:
    await seed_p1(w)
    w.scraper.script(KETTLE_URL, ListingGone(status=404, url=KETTLE_URL))
    w.scheduler.deps.max_consecutive_errors = 10
    for _ in range(3):
        await w.recorder.job("run_check_all")


@scenario("alert.quarantine_entry")
async def scenario_alert_quarantine_entry(w: LegacyWorld) -> None:
    await seed_p1(w)
    await seed_product(w, OWNER, ITEM4_URL, "Speaker", initial="40.00", current="40.00")
    await seed_product(w, OWNER, ITEM5_URL, "Mixer", initial="60.00", current="60.00")
    for url in (KETTLE_URL, ITEM4_URL, ITEM5_URL):
        w.scraper.script(url, HTTPBlockStatus(status=403, url=url))
    await w.recorder.job("run_check_all")


@scenario("alert.read_failures")
async def scenario_alert_read_failures(w: LegacyWorld) -> None:
    await seed_p1(w)
    w.scraper.script(KETTLE_URL, info("5000.00"))
    item6 = "https://shop.example.com/item/6"
    item10 = "https://shop.example.com/item/10"
    item11 = "https://shop.example.com/item/11"
    not_found = httpx.Response(404, request=httpx.Request("GET", item10))
    outcomes: tuple[tuple[str, str, ProductInfo | BaseException], ...] = (
        (ITEM4_URL, "No Price", ProductInfo(name="No Price", price=None)),
        (ITEM5_URL, "Dollar", info("50.00", name="Dollar", currency="USD")),
        (item6, "Used Offer", ProductInfo(price=Decimal("40.00"), condition="used")),
        (ITEM7_URL, "Offline", httpx.ConnectError("connection refused")),
        (
            item10,
            "Gone",
            httpx.HTTPStatusError("not found", request=not_found.request, response=not_found),
        ),
        (item11, "Broken", RuntimeError("scraper crashed")),
    )
    for url, name, outcome in outcomes:
        # A read in another currency is skipped but resets the error count:
        # seeded at one error so the reset shows in the diff.
        errors = 1 if name == "Dollar" else 0
        product = await seed_product(w, OWNER, url, name, initial="40.00", errors=errors)
        w.scraper.script(url, outcome)
        if name == "Used Offer":
            await reanchor(w, "products", {"id": product}, "preferred_condition", "new")
    await w.recorder.job("run_check_all")


@scenario("alert.suspicious_drop")
async def scenario_alert_suspicious_drop(w: LegacyWorld) -> None:
    await seed_p1(w)
    w.scraper.script(
        KETTLE_URL,
        info("40.00"),
        ParseError("no price"),
        info("40.00"),
        info("40.50"),
        info("40.00"),
    )
    await seed_product(
        w,
        OWNER,
        ITEM4_URL,
        "Swinging",
        initial="100.00",
        current="80.00",
        history=KETTLE_HISTORY,
    )
    w.scraper.script(ITEM4_URL, *(info(price, name="Swinging") for price in ("40.00", "30.00") * 4))
    await seed_product(
        w, OWNER, ITEM5_URL, "Mixed", initial="100.00", current="80.00", history=KETTLE_HISTORY
    )
    w.scraper.script(ITEM5_URL, info("40.00", name="Mixed"), info("50.00", currency="USD"))
    # 51 points: the 26 oldest at 10.00, the 25 newest at 100.00. The filter reads
    # the newest 50, whose median (55.00) makes a read of 20.00 suspicious.
    start = datetime(2026, 1, 1, 10, 0, 0)
    long_history = tuple(
        ((start + timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S"), "10.00" if n < 26 else "100.00")
        for n in range(51)
    )
    await seed_product(
        w,
        OWNER,
        item_url(35),
        "Long History",
        initial="100.00",
        current="100.00",
        history=long_history,
    )
    # Read once at 20.00, then back at 100.00, so the held read is never confirmed.
    w.scraper.script(
        item_url(35), info("20.00", name="Long History"), info("100.00", name="Long History")
    )
    for _ in range(8):
        await w.recorder.job("run_check_all")


@scenario("alert.threshold_kinds")
async def scenario_alert_threshold_kinds(w: LegacyWorld) -> None:
    await seed_p1(w, threshold=("any_drop", "0"))
    w.scraper.script(KETTLE_URL, info("79.00"))
    kinds = (
        (ITEM4_URL, "Absolute", ("absolute", "5"), "30.00"),
        (ITEM5_URL, "Target Type", ("target", "50"), "49.00"),
        (ITEM7_URL, "Odd Type", ("weird", "1"), "30.00"),
    )
    for url, name, threshold, price in kinds:
        await seed_product(w, OWNER, url, name, initial="60.00", threshold=threshold)
        w.scraper.script(url, info(price, name=name))
    # An any-drop read at the same price, an HTML-escaped name with a 50 % drop
    # from 1.00, a drop just short of 10 %, and a new offer with and without a
    # pinned condition.
    edges = (
        (item_url(30), "Steady", ("any_drop", "0"), "60.00", info("60.00", name="Steady")),
        (
            item_url(31),
            'Chef\'s "Penny" Pan',
            ("percentage", "10"),
            "1.00",
            info("0.50", name='Chef\'s "Penny" Pan'),
        ),
        (
            item_url(32),
            "Near Miss",
            ("percentage", "10"),
            "100.00",
            info("90.05", name="Near Miss"),
        ),
        (
            item_url(33),
            "Pinned New",
            ("percentage", "10"),
            "60.00",
            ProductInfo(price=Decimal("59.00"), condition="new"),
        ),
        (
            item_url(34),
            "Unpinned",
            ("percentage", "10"),
            "60.00",
            ProductInfo(price=Decimal("59.00"), condition="new"),
        ),
    )
    for url, name, threshold, price, page in edges:
        product = await seed_product(
            w, OWNER, url, name, initial=price, current=price, threshold=threshold
        )
        w.scraper.script(url, page)
        if name == "Pinned New":
            await reanchor(w, "products", {"id": product}, "preferred_condition", "new")
    await w.recorder.job("run_check_all")


@scenario("alert.many_failing")
async def scenario_alert_many_failing(w: LegacyWorld) -> None:
    for n in range(1, 12):
        url = f"https://shop.example.com/item/{n}"
        await seed_product(w, OWNER, url, f"Item {n:02d}", initial="10.00")
        w.scraper.script(url, ParseError("no price"))
    await w.recorder.job("run_check_all")
    await w.recorder.job("run_check_all")


@scenario("alert.quarantine_skip")
async def scenario_alert_quarantine_skip(w: LegacyWorld) -> None:
    await seed_locked_and_half_open(w)
    # Neither may take the half-open probe: ADMIN's product is paused, and OTHER
    # is swept after OWNER (users in row order), whose probe already happened.
    await seed_product(w, ADMIN, AMAZON_URLS[2], "Amazon Paused", initial="25.00", active=False)
    w.scraper.script(AMAZON_URLS[2], info("20.00", name="Amazon Paused"))
    other_url = "https://www.amazon.com/dp/B0FIXTURE4"
    await seed_product(w, OTHER, other_url, "Amazon Other", initial="25.00", current="25.00")
    w.scraper.script(other_url, info("20.00", name="Amazon Other"))
    await w.recorder.job("run_check_all")


@scenario("alert.unsupported_site")
async def scenario_alert_unsupported_site(w: LegacyWorld) -> None:
    bare = await seed_product(w, OWNER, "https://93.184.216.34/item", "Bare IP", initial="10.00")
    await w.recorder.job("run_check_all")
    await w.recorder.command(OWNER, f"/check {bare}")


@scenario("alert.prefs_variants")
async def scenario_alert_prefs_variants(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    script_drop(w)
    await seed_product(w, OWNER, ITEM4_URL, "Lamp", initial="40.00")
    w.scraper.script(ITEM4_URL, info("30.00", name="Lamp"))
    await seed_command(w, OWNER, f"/mute {p1} forever")
    await seed_command(w, OWNER, "/timezone Europe/Rome")
    await seed_command(w, OWNER, "/quiet_hours 22:00-08:00")
    await w.recorder.job("run_check_all")
    # A window that wraps midnight and contains 13:00 in Rome: the Lamp's new low
    # is dropped (quiet, no digest), so the wrap branch is seen returning True.
    await seed_command(w, OWNER, "/quiet_hours 12:30-02:00")
    w.scraper.script(ITEM4_URL, info("25.00", name="Lamp"))
    await w.recorder.job("run_check_all")


@scenario("alert.quiet_suspension_digest")
async def scenario_alert_quiet_suspension_digest(w: LegacyWorld) -> None:
    await seed_p1(w)
    w.scraper.script(KETTLE_URL, ParseError("no price"))
    for n, url in enumerate(AMAZON_URLS, start=1):
        await seed_product(w, OWNER, url, f"Amazon {n}", initial="25.00")
        w.scraper.script(url, HTTPBlockStatus(status=403, url=url))
    await seed_quiet_rome(w)
    await w.recorder.job("run_check_all")
    await w.recorder.job("run_check_all")
    await w.recorder.command(OWNER, "/digest_now")


@scenario("alert.digest_flush_quiet")
async def scenario_alert_digest_flush_quiet(w: LegacyWorld) -> None:
    await seed_two_drops_in_digest(w)
    await seed_quiet_rome(w)
    await seed_job(w)
    await reanchor_digest(w, "2026-03-01 10:00:00", "2026-03-01 10:00:01")
    await w.recorder.job("digest_flush_due", interval_minutes=60)


@scenario("alert.prefs_expiry")
async def scenario_alert_prefs_expiry(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    script_drop(w)
    await seed_product(w, OWNER, ITEM4_URL, "Lamp", initial="40.00")
    w.scraper.script(ITEM4_URL, info("30.00", name="Lamp"))
    await seed_command(w, OWNER, "/timezone Europe/Rome")
    await w.recorder.job("run_check_all")
    # A window that does not wrap and does not contain 13:00 in Rome.
    await seed_command(w, OWNER, "/quiet_hours 14:00-16:00")
    # A per-product mute that expired an hour ago: the Kettle's new low is sent.
    await seed_command(w, OWNER, f"/mute {p1} 2")
    await w.recorder.seed(
        "expire the per-product mute",
        reanchor(
            w,
            "notification_prefs",
            {"user_id": OWNER, "product_id": p1},
            "mute_until",
            "2026-03-01T11:00:00+00:00",
        ),
    )
    # 57.50 is a 10 % drop from 64.00 that the outlier filter still accepts (at
    # least 0.6 x the history median of 95.00); 56.00 would be held as suspicious.
    w.scraper.script(KETTLE_URL, info("57.50"))
    await w.recorder.job("run_check_all")
    # A global mute that expired an hour ago: the Lamp's new low is sent.
    await seed_command(w, OWNER, "/mute all 2")
    await w.recorder.seed(
        "expire the global mute",
        reanchor(
            w,
            "notification_prefs",
            {"user_id": OWNER, "product_id": None},
            "mute_until",
            "2026-03-01T11:00:00+00:00",
        ),
    )
    w.scraper.script(ITEM4_URL, info("20.00", name="Lamp"))
    await w.recorder.job("run_check_all")


@scenario("alert.read_failures_domains")
async def scenario_alert_read_failures_domains(w: LegacyWorld) -> None:
    # Each domain's only read is skipped (another currency, a used offer when new
    # is pinned), yet the domain is seen reachable. The Dollar has no errors, so a
    # reset of its error count cannot show.
    await seed_product(w, OWNER, ITEM5_URL, "Dollar", initial="40.00", errors=0)
    w.scraper.script(ITEM5_URL, info("50.00", name="Dollar", currency="USD"))
    used = await seed_product(w, OWNER, AMAZON_URLS[2], "Used Offer", initial="40.00")
    w.scraper.script(AMAZON_URLS[2], ProductInfo(price=Decimal("40.00"), condition="used"))
    await reanchor(w, "products", {"id": used}, "preferred_condition", "new")
    await w.recorder.job("run_check_all")


# ops ──────────────────────────────────────────────────────────────────
# Each scenario starts where alert.operational_suspended ends, reproduced by
# seed sweeps: P1 and the Speaker suspended, the notice with its buttons sent.


async def seed_suspended(w: LegacyWorld, *, recovered: bool = True) -> tuple[int, Call]:
    """Suspend P1 and the Speaker with two seed sweeps; return P1's id and the notice.

    With ``recovered`` both pages read a price again afterwards.
    """
    p1 = await seed_two_failing(w)
    await seed_job(w)
    await seed_job(w)
    if recovered:
        w.scraper.script(KETTLE_URL, ProductInfo(price=Decimal("80.00")))
        w.scraper.script(ITEM4_URL, ProductInfo(price=Decimal("80.00")))
    return p1, w.request.calls_of("sendMessage")[-1]


@scenario("ops.reactivate")
async def scenario_ops_reactivate(w: LegacyWorld) -> None:
    p1, notice = await seed_suspended(w)
    await w.recorder.press(OWNER, f"ops_react_{p1}", on=notice)


@scenario("ops.reactivate_still_failing")
async def scenario_ops_reactivate_still_failing(w: LegacyWorld) -> None:
    p1, notice = await seed_suspended(w, recovered=False)
    await w.recorder.press(OWNER, f"ops_react_{p1}", on=notice)


@scenario("ops.delete_prompt")
async def scenario_ops_delete_prompt(w: LegacyWorld) -> None:
    p1, notice = await seed_suspended(w)
    await w.recorder.press(OWNER, f"ops_del_{p1}", on=notice)


@scenario("ops.delete_confirm")
async def scenario_ops_delete_confirm(w: LegacyWorld) -> None:
    p1, notice = await seed_suspended(w)
    await w.recorder.press(OWNER, f"ops_del_{p1}", on=notice)
    await w.recorder.press(OWNER, f"ops_delok_{p1}")


@scenario("ops.nothing_to_do")
async def scenario_ops_nothing_to_do(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.press(OWNER, f"ops_react_{p1}")


@scenario("ops.invalid_id")
async def scenario_ops_invalid_id(w: LegacyWorld) -> None:
    p1, notice = await seed_suspended(w)
    await w.recorder.press(OWNER, "ops_react_x", on=notice)
    await w.recorder.press(OTHER, f"ops_react_{p1}")


@scenario("ops.invalid_id_delete")
async def scenario_ops_invalid_id_delete(w: LegacyWorld) -> None:
    _, notice = await seed_suspended(w)
    await w.recorder.press(OWNER, "ops_del_x", on=notice)
    await w.recorder.press(OWNER, "ops_delok_x")


@scenario("ops.reactivate_failure_kinds")
async def scenario_ops_reactivate_failure_kinds(w: LegacyWorld) -> None:
    # Four products suspended together, then each fails the recheck differently.
    p1 = await seed_p1(w)
    group = (
        (ITEM4_URL, "Speaker", "40.00", ListingGone(status=404, url=ITEM4_URL)),
        (ITEM5_URL, "Mixer", "60.00", httpx.ConnectError("connection refused")),
        (ITEM7_URL, "Heater", "40.00", RuntimeError("scraper crashed")),
    )
    w.scraper.script(KETTLE_URL, ParseError("no price"))
    for url, name, price, _ in group:
        await seed_product(w, OWNER, url, name, initial=price, current=price)
        w.scraper.script(url, ParseError("no price"))
    await seed_job(w)
    await seed_job(w)
    w.scraper.script(KETTLE_URL, HTTPBlockStatus(status=403, url=KETTLE_URL))
    for url, _, _, failure in group:
        w.scraper.script(url, failure)
    await w.recorder.press(OWNER, f"ops_react_{p1}")


@scenario("ops.reactivate_name_edges")
async def scenario_ops_reactivate_name_edges(w: LegacyWorld) -> None:
    # A product without a name shows its URL; a long name is cut at 60.
    nameless = await seed_product(w, OWNER, item_url(75), "", initial="80.00", current="80.00")
    await seed_product(w, OWNER, item_url(76), LN81, initial="80.00", current="80.00")
    for n in (75, 76):
        w.scraper.script(item_url(n), ParseError("no price"))
    await seed_job(w)
    await seed_job(w)
    for n in (75, 76):
        w.scraper.script(item_url(n), ProductInfo(price=Decimal("80.00")))
    await w.recorder.press(OWNER, f"ops_react_{nameless}")


# ── T-S: scenarios ───────────────────────────────────────────────────


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
async def test_legacy_snapshot(scenario_id: str, frozen_world: LegacyWorld) -> None:
    """The scenario renders exactly as its committed snapshot."""
    await run_to_disk(frozen_world, scenario_id, SCENARIOS[scenario_id], SNAPSHOT_ROOT)


# ── T-C: completeness ────────────────────────────────────────────────


def test_c1_registered_commands_match_the_inventory() -> None:
    """Every legacy command (aliases included) is in the inventory, and nothing else."""
    app = make_application(FakeRequest(), with_job_queue=True)
    register_handlers(app)
    legacy = app.handlers[2]
    commands = {c for h in legacy if isinstance(h, CommandHandler) for c in h.commands}
    assert commands == COMMANDS_IN
    assert len(COMMANDS_IN) == 48
    assert len(legacy) == 52
    assert all(h.callback is not status_command for h in legacy)


def test_c2_every_callback_grammar_entry_is_pressed_and_none_out() -> None:
    """Each callback datum of the inventory is pressed in some committed snapshot."""
    pressed = {
        data for text in committed_snapshots().values() for data in PRESS_LINE_RE.findall(text)
    }
    missing = sorted(
        f"{kind}:{value}"
        for kind, value in CALLBACK_GRAMMAR
        if not any(grammar_matches(kind, value, data) for data in pressed)
    )
    assert missing == []
    assert sorted(data for data in pressed if data.startswith(CALLBACK_OUT)) == []


def test_c3_snapshot_files_match_the_scenario_catalogue() -> None:
    """No orphan and no missing snapshot file; every scenario id is well formed."""
    expected = {
        snapshot_path(SNAPSHOT_ROOT, sid, locale).relative_to(SNAPSHOT_ROOT).as_posix()
        for sid in SCENARIOS
        for locale in LOCALES
    }
    assert set(committed_snapshots()) == expected
    assert [sid for sid in SCENARIOS if not SCENARIO_ID_RE.fullmatch(sid)] == []
    assert {sid.split(".", 1)[0] for sid in SCENARIOS} == LEGACY_AREAS
    print(f"legacy snapshot scenarios: {len(SCENARIOS)}")


def test_c4_every_command_is_sent_in_some_snapshot() -> None:
    """Each command of the inventory is the first token of a captured command step."""
    sent = {cmd for text in committed_snapshots().values() for cmd in COMMAND_LINE_RE.findall(text)}
    assert sorted(COMMANDS_IN - sent) == []
