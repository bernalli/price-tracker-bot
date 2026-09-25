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
import hashlib
import json
import re
import time
import warnings
from collections import Counter
from datetime import UTC, datetime, timedelta, timezone
from io import BytesIO
from typing import TYPE_CHECKING, Final, Literal

import pytest
import pytest_asyncio
from _pytest.outcomes import Failed
from freezegun import freeze_time
from hypothesis import given
from hypothesis import strategies as st
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, ReplyKeyboardRemove
from telegram.error import NetworkError
from telegram.ext import CommandHandler

from price_tracker.bot import messages
from price_tracker.bot.handlers import register_handlers
from price_tracker.bot.handlers.debug import status_command
from tests.support.fake_telegram import FakeRequest, make_application
from tests.support.legacy_harness import (
    FROZEN_NOW,
    LOCALES,
    OWNER,
    SNAPSHOT_ROOT,
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
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from tests.support.legacy_harness import LegacyWorld

SCENARIOS: dict[str, Callable[[LegacyWorld], Awaitable[None]]] = {}

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
    return data.startswith(value) and len(data) > len(value)


def sample_snapshot(body: tuple[str, ...] = ('## step 1: command "/lista" user=10',)) -> Snapshot:
    """A small snapshot for the update-mechanism tests."""
    return Snapshot(scenario_id="harness.update", locale="it", body=body)


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

    today = naive.date().isoformat()
    in_rome = (real_now + timedelta(seconds=5)).astimezone(timezone(timedelta(hours=2)))
    local = in_rome.replace(microsecond=0).isoformat()
    assert local.endswith("+02:00")
    for verbatim in (
        "2026-02-30 10:00:00",
        f"{today} 10:00:60",
        local,
        " " + sqlite_form(near),
    ):
        assert render_db_value(verbatim, real_now) == json.dumps(verbatim, ensure_ascii=False)


_REAL_NOW_FOR_PROPERTY: Final = datetime.now(UTC)


@given(
    moment=st.one_of(
        st.datetimes(),
        st.integers(min_value=-1200, max_value=1200).map(
            lambda s: _REAL_NOW_FOR_PROPERTY.replace(tzinfo=None) + timedelta(seconds=s)
        ),
    )
)
def test_h2_db_timestamp_normalization_property(moment: datetime) -> None:
    """``<now:SHAPE>`` if and only if the instant is within 600 s of the real clock."""
    real_now = _REAL_NOW_FOR_PROPERTY
    text = moment.isoformat(sep=" ", timespec="seconds")
    truncated = moment.replace(microsecond=0, tzinfo=UTC)
    near = abs((truncated - real_now).total_seconds()) <= 600
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


async def test_h3b_i18n_canary_refuses_an_untranslated_catalogue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A locale whose catalogue cannot be loaded stops the world before any step."""
    monkeypatch.setattr(messages, "_LOCALE_DIR", tmp_path)
    messages.get_translation.cache_clear()
    try:
        with pytest.raises(HarnessFault, match="it"):
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
    assert "~ products id=1 is_active: 1 -> 0" in lines
    step4 = rendered.split("## db after step 4\n", 1)[1]
    assert "- products id=1" in step4.splitlines()
    assert sum(line.startswith("- price_history ") for line in step4.splitlines()) == 2


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

    w.upload("doc7", b"URL\r\nhttps://shop.example.com/item/7\r\n")
    telegram_file = await bot.get_file("doc7")
    out = BytesIO()
    await telegram_file.download_to_memory(out)
    assert out.getvalue() == b"URL\r\nhttps://shop.example.com/item/7\r\n"

    with pytest.raises((HarnessFault, NetworkError)) as raised:
        await bot.get_file("never-uploaded")
    fault = raised.value if isinstance(raised.value, HarnessFault) else raised.value.__cause__
    assert isinstance(fault, HarnessFault)
    assert w.faults
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
    """A text near the real clock or a negative relative time fails; seeded times pass."""
    w = frozen_world
    bot = w.app.bot
    stamp = w.real_now.strftime("%Y-%m-%d %H:%M")
    with pytest.raises(NonDeterministicOutput):
        await w.recorder.capture("real clock", bot.send_message(chat_id=OWNER, text=f"at {stamp}"))
    with pytest.raises(NonDeterministicOutput):
        await w.recorder.capture("negative", bot.send_message(chat_id=OWNER, text="🕐 -5min fa"))
    await w.recorder.capture("seeded", bot.send_message(chat_id=OWNER, text="at 2026-03-01 10:00"))
    assert "   | at 2026-03-01 10:00" in w.recorder.snapshot("harness.guards").render()


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
    rendered = w.recorder.snapshot("harness.malformed").render()
    assert "Plain" in rendered
    assert "first" in rendered
    assert "second" in rendered
    markup_lines = [line for line in rendered.splitlines() if line.startswith("   markup: ")]
    assert len(markup_lines) == 1
    assert json.loads(markup_lines[0].removeprefix("   markup: ")) == {"remove_keyboard": True}


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
    rendered = w.recorder.snapshot("harness.duplicates").render()
    assert "+ notification_prefs (10, NULL)#2" in rendered
    assert "!! duplicate key" in rendered
    assert not any(line.startswith("~ notification_prefs") for line in rendered.splitlines())


async def test_h11_real_and_blob_columns_render(frozen_world: LegacyWorld) -> None:
    """A table created in the step, with REAL and BLOB columns, renders its rows."""
    w = frozen_world
    blob = b"\x00\xff"

    async def create() -> None:
        await w.conn.execute("CREATE TABLE extra (id INTEGER PRIMARY KEY, r REAL, b BLOB)")
        await w.conn.execute("INSERT INTO extra(id, r, b) VALUES (1, 1.5, ?)", (blob,))
        await w.conn.commit()

    await w.recorder.capture("create", create())
    rendered = w.recorder.snapshot("harness.real_blob").render()
    assert "float:1.5" in rendered
    assert f"blob:2:{hashlib.sha256(blob).hexdigest()[:12]}" in rendered


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
    assert any(line.startswith("~ bot_config") and '"v1" -> "v2"' in line for line in lines)
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
    await w.recorder.press(OWNER, "zz_old")
    await w.recorder.press(OWNER, "zz_new")
    await w.recorder.press(OWNER, "zz_rm")
    await w.recorder.press(OWNER, "zz_never")
    await w.recorder.press(OWNER, "zz_dup")

    lines = w.recorder.snapshot("harness.resolver").render().splitlines()
    assert '## step 7: press "zz_old" user=10 on=1 synthetic' in lines
    assert '## step 8: press "zz_new" user=10 on=1000' in lines
    assert '## step 9: press "zz_rm" user=10 on=1 synthetic' in lines
    assert '## step 10: press "zz_never" user=10 on=1 synthetic' in lines
    assert '## step 11: press "zz_dup" user=10 on=1003' in lines


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
    with pytest.raises(HarnessFault):
        await run_to_disk(frozen_world, "harness.fault", scenario, tmp_path)
    assert list(tmp_path.rglob("*")) == []


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
    assert "LEGACY_SNAPSHOTS_UPDATE=1" in str(failed.value)
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
    print(f"legacy snapshot scenarios: {len(SCENARIOS)}")


def test_c4_every_command_is_sent_in_some_snapshot() -> None:
    """Each command of the inventory is the first token of a captured command step."""
    sent = {cmd for text in committed_snapshots().values() for cmd in COMMAND_LINE_RE.findall(text)}
    assert sorted(COMMANDS_IN - sent) == []
