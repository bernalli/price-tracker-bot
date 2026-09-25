"""No button or command lets a user touch or see a product they may not see.

A property over every input shape the code base can produce, not over a
hand-written list:

* callbacks: every identifier-like string literal of ``price_tracker/bot`` and
  ``core/notices.py``, suffixed with ``_`` when needed, followed by the product id
  (a prefix added tomorrow is covered the day it is written), plus one encoding of
  every registry action that carries an id;
* commands: every ``CommandHandler`` the production layout registers, called with
  the product id as first argument.

A user who does not own the product sends each one. For callbacks the whole
database must be identical afterwards; for commands (which legitimately write the
caller's own rows) every row that references the product or its owner must be.
No outgoing text, caption or toast may carry the product's name or URL.

A positive control proves the oracle sees writes: the owner's press of each
known writing prefix must change the snapshot.
"""

from __future__ import annotations

import ast
import itertools
import re
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest
from telegram.ext import CommandHandler

import price_tracker
from price_tracker.bot.callbacks import REGISTRY, Action, BackArg, Choice, FlowTokenArg, IdArg
from price_tracker.bot.handlers import register_handlers
from price_tracker.bot.handlers._helpers import _get_user_product
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from tests.support.fake_telegram import (
    FakeRequest,
    callback_update,
    make_application,
    message_update,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

PACKAGE = Path(price_tracker.__file__).resolve().parent
MIGRATIONS_DIR = PACKAGE / "db" / "migrations"
SCANNED = (PACKAGE / "bot", PACKAGE / "core" / "notices.py")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*_?")

ADMIN = 1
OWNER = 10
OTHER = 11
VICTIM_NAME = "Victim-Kettle-7f3a"
VICTIM_URL = "https://shop.example/item/victim-7f3a"
# Guards the scanner itself: a scan that loses these measured nothing.
KNOWN_PRODUCT_PREFIXES = frozenset(
    {
        "track_any_",
        "track_default_",
        "track_threshold_",
        "track_target_",
        "pref_new_",
        "pref_used_",
        "pref_amazon_",
        "pref_anyseller_",
        "pref_default_",
        "edit_",
        "pause_",
        "remove_",
        "reset_",
        "reactivate_",
        "confirm_delete_",
        "check_",
        "chart_",
        "ops_react_",
        "ops_del_",
        "ops_delok_",
        "setsoglia_",
        "settarget_",
        "setrefresh_",
    }
)
KNOWN_ID_COMMANDS = frozenset(
    {"soglia", "target", "refresh", "pausa", "riattiva", "reset", "storia", "elimina", "mute"}
)
# /add fetches its argument as a URL; the product id is never its input.
URL_COMMANDS = frozenset({"add", "aggiungi"})
OWNER_WRITES = [
    "track_any_",
    "track_default_",
    "pref_new_",
    "pref_used_",
    "pref_amazon_",
    "pref_anyseller_",
    "pref_default_",
    "pause_",
    "reset_",
    "confirm_delete_",
]
_message_ids = itertools.count(1)


def candidate_prefixes() -> frozenset[str]:
    """Every identifier-like string literal of the bot package, ending in ``_``."""
    found: set[str] = set()
    for base in SCANNED:
        files = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for path in files:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and _IDENTIFIER.fullmatch(node.value)
                ):
                    found.add(node.value if node.value.endswith("_") else f"{node.value}_")
    return frozenset(found)


def registry_encodings(target_id: int) -> list[str]:
    """One encoding of every registry action that can name ``target_id``."""
    out: list[str] = []
    for name in sorted(REGISTRY.names):
        spec = REGISTRY.spec(name)
        if not any(isinstance(kind, (IdArg, BackArg)) for kind in spec.kinds):
            continue
        if any(isinstance(kind, FlowTokenArg) for kind in spec.kinds):
            continue
        args: list[int | str] = []
        for kind in spec.kinds:
            if isinstance(kind, IdArg):
                args.append(target_id)
            elif isinstance(kind, Choice):
                args.append(kind.values[0])
            elif isinstance(kind, BackArg):
                args.append(f"p{target_id}")
        out.append(REGISTRY.encode(Action(name, tuple(args))))
    return out


class Wired:
    """The production handler layout around a fake Telegram and a real repository."""

    def __init__(self, conn: aiosqlite.Connection, repo: Repository) -> None:
        self.conn = conn
        self.request = FakeRequest()
        self.app = make_application(self.request, with_job_queue=True)
        register_handlers(self.app)
        self.app.bot_data["db"] = repo
        self.app.bot_data["repository"] = repo
        self.errors: list[BaseException] = []
        self.app.add_error_handler(self._record_error)
        self.victim = 0

    async def _record_error(self, update: object, context: Any) -> None:
        del update
        self.errors.append(context.error)

    async def _process(self, update: Any) -> None:
        await self.app.update_processor.process_update(update, self.app.process_update(update))

    async def press(self, user_id: int, data: str) -> None:
        await self._process(
            callback_update(self.app.bot, user_id, user_id, data, message_id=next(_message_ids))
        )

    async def send(self, user_id: int, text: str) -> None:
        await self._process(message_update(self.app.bot, user_id, user_id, text))

    def commands(self) -> frozenset[str]:
        return frozenset(
            command
            for handlers in self.app.handlers.values()
            for handler in handlers
            if isinstance(handler, CommandHandler)
            for command in handler.commands
        )

    async def _tables(self) -> list[str]:
        cursor = await self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        return sorted(str(row[0]) for row in await cursor.fetchall())

    async def snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        """Every row of every table."""
        dump: dict[str, list[tuple[Any, ...]]] = {}
        for table in await self._tables():
            rows = await (await self.conn.execute(f'SELECT * FROM "{table}"')).fetchall()  # noqa: S608
            dump[table] = sorted(tuple(row) for row in rows)
        return dump

    async def victim_rows(self) -> dict[str, list[tuple[Any, ...]]]:
        """Every row that references the victim product or its owner."""
        dump: dict[str, list[tuple[Any, ...]]] = {}
        for table in await self._tables():
            info = await (await self.conn.execute(f'PRAGMA table_info("{table}")')).fetchall()
            columns = {str(row[1]) for row in info}
            if table == "products":
                where, param = "id = ?", self.victim
            elif table == "users":
                where, param = "user_id = ?", OWNER
            elif "product_id" in columns:
                where, param = "product_id = ?", self.victim
            else:
                continue
            sql = f'SELECT * FROM "{table}" WHERE {where}'  # noqa: S608
            rows = await (await self.conn.execute(sql, (param,))).fetchall()
            dump[table] = sorted(tuple(row) for row in rows)
        return dump

    def reveals_victim(self, since: int) -> list[str]:
        return [
            call.method
            for call in self.request.calls[since:]
            for key in ("text", "caption")
            if VICTIM_NAME in str(call.params.get(key, ""))
            or VICTIM_URL in str(call.params.get(key, ""))
        ]


@pytest.fixture
async def wired() -> AsyncIterator[Wired]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys=ON")
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(ADMIN, is_admin=True)
    await repo.ensure_user(OWNER)
    await repo.ensure_user(OTHER)
    bot = Wired(conn, repo)
    # Every field a button can write starts at a value no button writes, so any
    # write to the victim shows in the snapshot.
    bot.victim = await repo.add_product(
        user_id=OWNER,
        url=VICTIM_URL,
        name=VICTIM_NAME,
        domain="shop.example",
        initial_price=Decimal("100"),
        currency="EUR",
        threshold_type="absolute",
        threshold_value=Decimal("7"),
    )
    await repo.update_price(bot.victim, Decimal("90"))
    await repo.set_target_price(bot.victim, Decimal("55"))
    await repo.set_product_preferences(bot.victim, condition="used", seller="amazon")
    await bot.app.initialize()
    try:
        yield bot
    finally:
        await bot.app.shutdown()
        await conn.close()


def test_the_scan_finds_every_known_product_prefix() -> None:
    missing = KNOWN_PRODUCT_PREFIXES - candidate_prefixes()
    assert not missing, f"scanner lost known prefixes: {sorted(missing)}"


async def test_no_callback_lets_a_non_owner_touch_or_see_the_product(wired: Wired) -> None:
    presses = [f"{prefix}{wired.victim}" for prefix in sorted(candidate_prefixes())]
    presses += registry_encodings(wired.victim)
    violations: list[str] = []
    for data in presses:
        before = await wired.snapshot()
        sent = len(wired.request.calls)
        errors = len(wired.errors)
        await wired.press(OTHER, data)
        if await wired.snapshot() != before:
            violations.append(f"{data}: database changed")
        violations += [
            f"{data}: {method} revealed the product" for method in wired.reveals_victim(sent)
        ]
        if len(wired.errors) != errors:
            violations.append(f"{data}: raised {wired.errors[-1]!r}")
    assert violations == []


async def test_no_command_lets_a_non_owner_touch_or_see_the_product(wired: Wired) -> None:
    commands = wired.commands()
    assert commands >= KNOWN_ID_COMMANDS, f"lost commands: {sorted(KNOWN_ID_COMMANDS - commands)}"
    violations: list[str] = []
    for command in sorted(commands - URL_COMMANDS):
        text = f"/{command} {wired.victim} 5"
        before = await wired.victim_rows()
        sent = len(wired.request.calls)
        await wired.send(OTHER, text)
        if await wired.victim_rows() != before:
            violations.append(f"{text}: rows of the product or its owner changed")
        violations += [
            f"{text}: {method} revealed the product" for method in wired.reveals_victim(sent)
        ]
    assert violations == []


@pytest.mark.parametrize("prefix", OWNER_WRITES)
async def test_the_owner_press_is_visible_to_the_oracle(wired: Wired, prefix: str) -> None:
    before = await wired.snapshot()
    await wired.press(OWNER, f"{prefix}{wired.victim}")
    assert await wired.snapshot() != before
    assert wired.errors == []


async def test_the_owner_mute_is_visible_to_the_oracle(wired: Wired) -> None:
    before = await wired.victim_rows()
    await wired.send(OWNER, f"/mute {wired.victim} 5")
    assert await wired.victim_rows() != before


async def test_a_deactivated_admin_sees_no_product(wired: Wired) -> None:
    repo: Repository = wired.app.bot_data["db"]
    await repo.remove_user(ADMIN)

    class Context:
        bot_data = wired.app.bot_data

    assert await _get_user_product(Context, wired.victim, ADMIN) is None  # type: ignore[arg-type]
