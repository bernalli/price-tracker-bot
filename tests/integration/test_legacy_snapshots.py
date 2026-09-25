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
from price_tracker.core.exceptions import HTTPBlockStatus, ParseError
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

KETTLE_HISTORY: Final = (
    ("2026-01-15 10:00:00", "100.00"),
    ("2026-02-01 10:00:00", "100.00"),
    ("2026-02-10 10:00:00", "100.00"),
    ("2026-02-20 10:00:00", "90.00"),
    ("2026-02-28 10:00:00", "80.00"),
)
"""Five points of history: the outlier filter's minimum."""


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


@scenario("product.cmd_delete_empty")
async def scenario_product_cmd_delete_empty(w: LegacyWorld) -> None:
    await w.recorder.command(OWNER, "/elimina")


@scenario("product.cmd_target")
async def scenario_product_cmd_target(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.command(OWNER, "/target")
    for args in (f"{p1}", f"{p1} 49,90", f"{p1} 90", f"{p1} 0", f"{p1} abc", "abc 1", "999 1"):
        await w.recorder.command(OWNER, f"/target {args}")


@scenario("product.cmd_threshold")
async def scenario_product_cmd_threshold(w: LegacyWorld) -> None:
    p1 = await seed_p1(w)
    await w.recorder.command(OWNER, "/soglia")
    for args in (f"{p1}", f"{p1} 20%", f"{p1} 50", f"{p1} ogni"):
        await w.recorder.command(OWNER, f"/soglia {args}")
    await w.recorder.command(OWNER, f"/threshold {p1} abc")
    await w.recorder.command(OWNER, "/soglia 999 5")


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
    content = b"ID;Nome;URL\r\n1;X;https://shop.example.com/item/21\r\n"
    await w.recorder.document(OWNER, "excel.csv", content)


@scenario("data.import_csv_bad_fields")
async def scenario_data_import_csv_bad_fields(w: LegacyWorld) -> None:
    bad_url = "https://shop.example.com/item/22"
    w.scraper.script(bad_url, ProductInfo(name="Bad", price=Decimal("5.00")))
    content = b"\xef\xbb\xbf" + csv_document(
        (bad_url, "abc", "absolute:abc"), ("", "", "percentage:10")
    )
    await w.recorder.document(OWNER, "bad.csv", content)


@scenario("data.import_csv_empty")
async def scenario_data_import_csv_empty(w: LegacyWorld) -> None:
    await w.recorder.document(OWNER, "empty.csv", b"")


@scenario("data.import_not_csv")
async def scenario_data_import_not_csv(w: LegacyWorld) -> None:
    await w.recorder.document(OWNER, "note.txt", b"x")


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
