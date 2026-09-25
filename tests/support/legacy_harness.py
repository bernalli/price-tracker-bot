"""Characterization harness for the legacy Telegram handlers.

A world is a real ``Application`` with every production handler registered, a
migrated in-memory database, the real scheduler, notifier, digest service and
domain health manager, and a fake Telegram transport. Every step drives the app
the way Telegram would (a command, a free text, a button press, a document, a
scheduler job) and records what the user would see: each Bot API call with its
text, keyboard and parameters, plus the per-table database diff after the step.
The recording renders to a closed plain-text grammar that is compared with a
committed snapshot file.

Nothing in a world touches the network: DNS, scrapers and the HTTP client are
offline stubs, and the clock is frozen at ``FROZEN_NOW`` for the whole scenario.
"""

from __future__ import annotations

import difflib
import enum
import gettext
import hashlib
import importlib.metadata
import itertools
import json
import os
import platform
import re
import socket
import sqlite3
import time
import warnings
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, ClassVar, Final, Literal
from urllib.parse import urlparse

import httpx
import pytest
from prometheus_client import CollectorRegistry
from telegram import Update
from telegram.ext import CallbackContext

from price_tracker.bot.handlers import error_handler, register_handlers
from price_tracker.bot.messages import get_translation
from price_tracker.config import Config
from price_tracker.core.health import HealthManager
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper
from price_tracker.core.url_utils import extract_etld_plus_one
from price_tracker.db.repository import Repository
from price_tracker.main import bootstrap_database
from price_tracker.notifier.digest import DigestService
from price_tracker.notifier.preferences import PreferencesManager
from price_tracker.notifier.telegram import TelegramNotifier
from price_tracker.observability.metrics import MetricsRegistry
from tests.support.fake_telegram import (
    FakeRequest,
    callback_update,
    make_application,
    message_update,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Mapping, Sequence

    import aiosqlite
    from telegram.ext import Application
    from telegram.request import RequestData

    from price_tracker.core.scraper_base import ProductInfo
    from tests.support.fake_telegram import Call

FROZEN_NOW: Final = "2026-03-01 12:00:00"
"""UTC instant every scenario runs at."""

ADMIN: Final = 1
OWNER: Final = 10
OTHER: Final = 11
STRANGER: Final = 12
"""User ids; a private chat id equals its user id, as on Telegram."""

LOCALES: Final = ("it",)
PNG_MAGIC: Final = b"\x89PNG\r\n\x1a\n"
PHOTO_BYTES: Final = (1024, 2 * 1024 * 1024)
"""Inclusive size bounds of an uploaded photo; outside them is a ``violations`` entry."""
SNAPSHOT_ROOT: Final = Path(__file__).resolve().parents[1] / "snapshots" / "legacy"

JobName = Literal["run_check_all", "digest_flush_due"]

FIXTURE_HTML: Final = (
    Path(__file__).resolve().parents[1] / "fixtures" / "generic" / "sample_jsonld.html"
)
"""The page every GET of the world's HTTP client answers with."""

NOW_TOLERANCE_SECONDS: Final = 600
"""A database timestamp this close to the real clock was written by SQLite's ``now``."""

WALL_CLOCK_GUARD_SECONDS: Final = 86400
"""A timestamp in captured text closer than this to the real clock fails the step."""

GENERATED_WITH: Final = "_generated_with.txt"
"""Bookkeeping file, under the snapshot root, naming the environment of the last update."""

RENDERING_DISTRIBUTIONS: Final = (
    "python-telegram-bot",
    "aiosqlite",
    "httpx",
    "freezegun",
    "matplotlib",
    "tldextract",
    "babel",
)
"""Libraries whose version can change a rendering, recorded in ``GENERATED_WITH``."""

# A known message and its translation per locale: the catalogue canary.
_CANARY_MSGID: Final = "❌ Product not found."
_CANARY: Final[dict[str, str]] = {"it": "❌ Prodotto non trovato."}

# ASCII digits only: ``\d`` would also accept other Unicode digits.
_DB_TIMESTAMP_RE: Final = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})[ T]([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]+))?(Z|\+00:00)?"
)
# A timestamp inside captured text: seconds optional, any UTC offset.
_WALL_CLOCK_RE: Final = re.compile(
    r"(?<![0-9])([0-9]{4})-([0-9]{2})-([0-9]{2})[ T]([0-9]{2}):([0-9]{2})"
    r"(?::([0-9]{2})(?:\.([0-9]+))?)?(Z|[+-][0-9]{2}:?[0-9]{2})?"
)
_NEGATIVE_RELATIVE_RE: Final = re.compile(r"-[0-9]+(?:min|h|g) fa")
# Update and message ids of documents; like the fake's own counters, never rendered.
_update_ids = itertools.count(900_000)


class _Unset(enum.Enum):
    """Type of ``UNSET``."""

    UNSET = "UNSET"


UNSET: Final = _Unset.UNSET
"""Default of the ``seed_product`` prices: keep what ``Repository.add_product`` wrote."""


class NonDeterministicOutput(AssertionError):
    """A captured text depends on the wall clock instead of seeded data."""


class HarnessFault(AssertionError):
    """A harness failure (a stub used off script): never rendered, always red."""


class LegacySnapshotUpdated(UserWarning):
    """A snapshot file was created or rewritten in update mode."""

    def __init__(self, path: Path) -> None:
        super().__init__(f"legacy snapshot written: {path}")
        self.path = path


class ScriptedScraper(AbstractScraper):
    """The only scraper of a world: answers from a per-URL script, never from the network.

    ``name="scripted"``, ``priority=100``; ``can_handle`` is true when the host ends
    with ``example.com`` or ``amazon.com``. The script of a URL is a list of
    ``ProductInfo`` or exceptions consumed in order, the last one repeating. A URL
    without a script queues a ``HarnessFault`` in ``faults`` and raises it (never
    ``None``, never a default ``ProductInfo``).
    """

    name: ClassVar[str] = "scripted"
    priority: ClassVar[int] = 100

    def __init__(self, faults: list[HarnessFault]) -> None:
        self._faults = faults
        self._scripts: dict[str, list[ProductInfo | BaseException]] = {}
        self._played: dict[str, int] = {}

    def script(self, url: str, *outcomes: ProductInfo | BaseException) -> None:
        """Set the outcomes of the next scrapes of ``url``, in order; the last one repeats."""
        if not outcomes:
            raise ValueError(f"script for {url} needs at least one outcome")
        self._scripts[url] = list(outcomes)
        self._played[url] = 0

    def can_handle(self, url: str) -> bool:
        """True for hosts ending with ``example.com`` or ``amazon.com``."""
        host = urlparse(url).hostname or ""
        return host.endswith(("example.com", "amazon.com"))

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        """Play the next scripted outcome of ``url``: return it, or raise it if an exception."""
        del client
        outcomes = self._scripts.get(url)
        if outcomes is None:
            fault = HarnessFault(f"no scraper script for {url}")
            self._faults.append(fault)
            raise fault
        played = self._played[url]
        self._played[url] = played + 1
        outcome = outcomes[min(played, len(outcomes) - 1)]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RecordingRequest(FakeRequest):
    """A ``FakeRequest`` that also records uploads and serves downloads.

    It records ``request_data.multipart_data`` of each call in ``files_by_call``
    (index in ``calls`` -> name -> ``(filename, bytes, mimetype)``), answers
    ``getFile`` with a ``File`` ``{file_id, file_unique_id, file_size,
    file_path="documents/<file_id>"}`` and serves the download (``do_request`` with
    ``method == "GET"``, the ``file_id`` being the last path segment) from the bytes
    registered with ``upload(file_id, content)``. A ``getFile`` or a download of a
    ``file_id`` never uploaded queues a ``HarnessFault`` in ``faults`` and raises it
    (the bot wraps it in ``NetworkError``).
    """

    def __init__(self) -> None:
        super().__init__()
        self.files_by_call: dict[int, dict[str, tuple[str, bytes, str]]] = {}
        self.faults: list[HarnessFault] = []
        self._uploads: dict[str, bytes] = {}

    def upload(self, file_id: str, content: bytes) -> None:
        """Register ``content`` as the bytes of the Telegram file ``file_id``."""
        self._uploads[file_id] = bytes(content)

    def _uploaded(self, file_id: str) -> bytes:
        """The bytes registered for ``file_id``; a never-uploaded id is a harness fault."""
        content = self._uploads.get(file_id)
        if content is None:
            fault = HarnessFault(f"file {file_id!r} was never uploaded with world.upload")
            self.faults.append(fault)
            raise fault
        return content

    async def do_request(
        self,
        url: str,
        method: str,
        request_data: RequestData | None = None,
        read_timeout: Any = None,
        write_timeout: Any = None,
        connect_timeout: Any = None,
        pool_timeout: Any = None,
    ) -> tuple[int, bytes]:
        """Serve a file download for ``GET``; otherwise record the call and its uploads."""
        if method == "GET":
            return 200, self._uploaded(url.rstrip("/").rsplit("/", 1)[-1])
        first_call = len(self.calls)
        answer = await super().do_request(
            url, method, request_data, read_timeout, write_timeout, connect_timeout, pool_timeout
        )
        if request_data is not None and len(self.calls) > first_call:
            files = {
                name: (filename, _read_content(content), mimetype)
                for name, (filename, content, mimetype) in request_data.multipart_data.items()
            }
            if files:
                index = len(self.calls) - 1
                self.files_by_call[index] = files
                self._check_photo_size(self.calls[index].method, files)
        return answer

    def _check_photo_size(
        self, api_method: str, files: Mapping[str, tuple[str, bytes, str]]
    ) -> None:
        """Record a photo outside ``PHOTO_BYTES`` as a violation (never raised here)."""
        photo = files.get("photo")
        if api_method != "sendPhoto" or photo is None:
            return
        size = len(photo[1])
        if not PHOTO_BYTES[0] <= size <= PHOTO_BYTES[1]:
            self.violations.append(
                f"sendPhoto: photo size {size} bytes out of {PHOTO_BYTES[0]}..{PHOTO_BYTES[1]}"
            )

    def _result(self, api_method: str, params: dict[str, Any]) -> Any:
        """A ``File`` for ``getFile``; otherwise the answer of ``FakeRequest``.

        A sent message echoes only an inline keyboard, as Telegram does: any other
        ``reply_markup`` is not part of the returned ``Message``.
        """
        if api_method == "getFile":
            file_id = str(params["file_id"])
            return {
                "file_id": file_id,
                "file_unique_id": f"u{file_id}",
                "file_size": len(self._uploaded(file_id)),
                "file_path": f"documents/{file_id}",
            }
        result = super()._result(api_method, params)
        if isinstance(result, dict):
            markup = result.get("reply_markup")
            if isinstance(markup, dict) and "inline_keyboard" not in markup:
                del result["reply_markup"]
        return result


def _read_content(content: bytes | IO[bytes]) -> bytes:
    """The bytes of a multipart part; a file handle is read and rewound."""
    if isinstance(content, bytes):
        return content
    position = content.tell()
    data = content.read()
    content.seek(position)
    return data


@dataclass(frozen=True)
class TableDump:
    """Every row of one table, as read after a step.

    ``columns`` are in ``PRAGMA table_info`` order (``rowid`` first when the table
    has no primary key); ``key`` names the key columns in primary-key order;
    ``rows`` are full rows in ``columns`` order.
    """

    columns: tuple[str, ...]
    key: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]


@dataclass
class LegacyWorld:
    """One isolated application, database and fake Telegram, frozen at ``FROZEN_NOW``."""

    conn: aiosqlite.Connection
    repo: Repository
    request: RecordingRequest
    app: Application[Any, Any, Any, Any, Any, Any]
    scheduler: Scheduler
    digest: DigestService
    health: HealthManager
    registry: ScraperRegistry
    scraper: ScriptedScraper
    http_client: httpx.AsyncClient
    config: Config
    locale: str
    real_now: datetime
    errors: list[BaseException]
    faults: list[HarnessFault]
    dns_lookups: list[str]
    recorder: Recorder = field(init=False)
    _uploads_made: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self.recorder = Recorder(self)

    def upload(self, filename: str, data: bytes) -> str:
        """Make ``data`` downloadable as a new Telegram file and return its ``file_id``.

        File ids are ``doc1``, ``doc2``... in upload order, a counter shared with
        ``Recorder.document``; ``filename`` is the name the document carries.
        """
        del filename
        self._uploads_made += 1
        file_id = f"doc{self._uploads_made}"
        self.request.upload(file_id, data)
        return file_id

    async def process(self, update: Update) -> None:
        """Process ``update`` through the application, as Telegram delivery would."""
        await self.app.update_processor.process_update(update, self.app.process_update(update))

    async def run_command(self, user: int, text: str) -> None:
        """Send ``text`` from ``user`` without recording a step (for ``Recorder.seed``)."""
        await self.process(
            message_update(self.app.bot, user, user, text, language_code=self.locale)
        )

    async def run_press(self, user: int, data: str) -> None:
        """Press ``data`` as ``user`` without recording a step (for ``Recorder.seed``).

        The pressed message is resolved as ``Recorder.press`` without ``on``.
        """
        message_id, _ = self.pressed_message(user, data, None)
        await self.press_message(user, data, message_id)

    async def press_message(self, user: int, data: str, message_id: int) -> None:
        """Press ``data`` as ``user`` on the bot message ``message_id`` of their chat."""
        await self.process(
            callback_update(
                self.app.bot, user, user, data, message_id=message_id, language_code=self.locale
            )
        )

    def pressed_message(self, user: int, data: str, on: Call | None) -> tuple[int, bool]:
        """The message a press of ``data`` by ``user`` lands on, and whether it is synthetic.

        ``on`` wins when given (a ``Call`` without ``message_id`` is a ``HarnessFault``);
        otherwise the highest message id of the chat of ``user`` whose current
        keyboard carries ``data``, else the synthetic id 1.
        """
        if on is not None:
            if on.message_id is None:
                raise HarnessFault(f"press {data!r}: the {on.method} call has no message_id")
            return on.message_id, False
        showing = [mid for mid, datas in self.current_keyboards(user).items() if data in datas]
        return (max(showing), False) if showing else (1, True)

    def current_keyboards(self, chat_id: int) -> dict[int, list[str]]:
        """Message id -> callback data of its current inline keyboard, in ``chat_id``.

        Replayed over every call, seeds included: ``sendMessage`` sets a keyboard,
        an edit replaces it with its own (or removes it when it has none), and
        ``deleteMessage`` removes it.
        """
        keyboards: dict[int, list[str]] = {}
        for call in self.request.calls:
            if call.failed or call.chat_id != chat_id:
                continue
            if call.method == "sendMessage" and call.message_id is not None:
                keyboards[call.message_id] = call.callback_data()
            elif call.method in {"editMessageText", "editMessageReplyMarkup", "deleteMessage"}:
                target = call.params.get("message_id")
                if target is None:
                    continue
                if call.method == "deleteMessage":
                    keyboards.pop(int(target), None)
                else:
                    keyboards[int(target)] = call.callback_data()
        return keyboards


async def build_world(
    locale: str, *, monkeypatch: pytest.MonkeyPatch, real_now: datetime
) -> LegacyWorld:
    """Build a world for ``locale`` with the offline stubs applied through ``monkeypatch``.

    Must run inside the frozen clock; ``real_now`` is the wall-clock instant read
    before freezing. Seeds ``ADMIN`` (admin), ``OWNER`` and ``OTHER``; ``STRANGER``
    is not in the database. Raises ``HarnessFault`` naming ``locale`` if its
    catalogue does not load as a ``GNUTranslations`` or does not translate a known
    message into that language. On any failure, closes whatever it had opened
    (application, HTTP client, database) before raising.
    """
    _check_catalogue(locale)
    dns_lookups: list[str] = []

    def offline_getaddrinfo(host: object, *args: object, **kwargs: object) -> list[Any]:
        del args, kwargs
        dns_lookups.append(str(host))
        raise socket.gaierror(socket.EAI_NONAME, "offline")

    monkeypatch.setattr(socket, "getaddrinfo", offline_getaddrinfo)

    conn = await bootstrap_database(":memory:")
    http_client: httpx.AsyncClient | None = None
    app: Application[Any, Any, Any, Any, Any, Any] | None = None
    initialized = False
    try:
        repo = Repository(conn)
        request = RecordingRequest()
        app = make_application(request, with_job_queue=True)
        register_handlers(app)
        await app.initialize()
        initialized = True
        config = Config(
            telegram_bot_token="123456:TEST-TOKEN",
            admin_users=(ADMIN,),
            check_interval_minutes=360,
            database_path=":memory:",
            default_threshold_type="percentage",
            default_threshold_value="10",
            max_consecutive_errors=2,
            check_delay_seconds=0.0,
            notification_cooldown_hours=24,
            request_timeout=5,
            log_level="WARNING",
            lang=locale,
        )
        scraper = ScriptedScraper(request.faults)
        registry = ScraperRegistry()
        registry.register(scraper)
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(_fixture_html))
        health = HealthManager(repo)
        await health.load()
        digest = DigestService(repo=repo, bot=app.bot, metrics=None, lang=locale)
        notifier = TelegramNotifier(
            app.bot, metrics=None, prefs=PreferencesManager(repo), digest=digest
        )
        scheduler = Scheduler(
            SchedulerDeps(
                repo=repo,
                registry=registry,
                client=http_client,
                notifier=notifier,
                max_consecutive_errors=2,
                listing_gone_confirmations=3,
                delay_between_products=0.0,
                notification_cooldown_hours=24,
                health_mgr=health,
                metrics=None,
                lang=locale,
            )
        )
        bot_data = app.bot_data
        bot_data["db"] = bot_data["repo"] = bot_data["repository"] = repo
        bot_data["config"] = config
        bot_data["registry"] = bot_data["scraper"] = registry
        bot_data["http_client"] = http_client
        bot_data["scheduler"] = scheduler
        bot_data["digest_service"] = digest
        bot_data["health_manager"] = health
        bot_data["metrics"] = MetricsRegistry(registry=CollectorRegistry())
        # The monotonic clock is frozen too, so the uptime renders as "1h 2m 5s".
        bot_data["start_time"] = time.monotonic() - 3725.0
        world = LegacyWorld(
            conn=conn,
            repo=repo,
            request=request,
            app=app,
            scheduler=scheduler,
            digest=digest,
            health=health,
            registry=registry,
            scraper=scraper,
            http_client=http_client,
            config=config,
            locale=locale,
            real_now=real_now,
            errors=[],
            faults=request.faults,
            dns_lookups=dns_lookups,
        )

        async def record_error(
            update: object, context: CallbackContext[Any, Any, Any, Any]
        ) -> None:
            del update
            if context.error is not None:
                world.errors.append(context.error)

        app.add_error_handler(record_error)
        await seed_user(world, ADMIN, admin=True)
        await seed_user(world, OWNER)
        await seed_user(world, OTHER)
    except BaseException:
        if app is not None and initialized:
            await app.shutdown()
        if http_client is not None:
            await http_client.aclose()
        await conn.close()
        raise
    return world


def _check_catalogue(locale: str) -> None:
    """Refuse a locale whose catalogue does not load or does not translate the canary."""
    translation = get_translation(locale)
    if not isinstance(translation, gettext.GNUTranslations):
        raise HarnessFault(
            f"locale {locale!r}: no message catalogue loaded ({type(translation).__name__})"
        )
    expected = _CANARY.get(locale)
    if expected is None:
        raise HarnessFault(f"locale {locale!r}: no canary translation known to the harness")
    actual = translation.gettext(_CANARY_MSGID)
    if actual != expected:
        raise HarnessFault(
            f"locale {locale!r}: catalogue renders {_CANARY_MSGID!r} as {actual!r}, "
            f"expected {expected!r}"
        )


def _fixture_html(request: httpx.Request) -> httpx.Response:
    """Answer any request of the world's HTTP client with the JSON-LD fixture page."""
    return httpx.Response(
        200,
        content=FIXTURE_HTML.read_bytes(),
        headers={"content-type": "text/html; charset=utf-8"},
        request=request,
    )


async def close_world(world: LegacyWorld) -> None:
    """Shut the application down and close the HTTP client and the database."""
    try:
        await world.app.shutdown()
    finally:
        try:
            await world.http_client.aclose()
        finally:
            await world.conn.close()


async def seed_user(
    world: LegacyWorld,
    user_id: int,
    *,
    admin: bool = False,
    display_name: str | None = None,
    username: str | None = None,
    active: bool = True,
) -> None:
    """Insert or update a user row (never captured unless run inside a step)."""
    await world.conn.execute(
        "INSERT INTO users(user_id, is_admin, is_active, display_name, username) "
        "VALUES(?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
        "is_admin = excluded.is_admin, is_active = excluded.is_active, "
        "display_name = excluded.display_name, username = excluded.username",
        (user_id, int(admin), int(active), display_name, username),
    )
    await world.conn.commit()


async def seed_product(
    world: LegacyWorld,
    owner: int,
    url: str,
    name: str,
    *,
    initial: str,
    current: str | None | _Unset = UNSET,
    lowest: str | None | _Unset = UNSET,
    highest: str | None | _Unset = UNSET,
    target: str | None = None,
    threshold: tuple[str, str] = ("percentage", "10"),
    active: bool = True,
    currency: str = "EUR",
    check_interval: int | None = None,
    last_checked_at: str | None = None,
    errors: int = 0,
    last_error: str | None = None,
    last_error_at: str | None = None,
    available: bool = True,
    history: Sequence[tuple[str, str]] = (),
) -> int:
    """Insert a product the way ``/add`` does, then overwrite its state; return its id.

    Inserts with ``Repository.add_product(domain=extract_etld_plus_one(url),
    initial_price, currency, threshold_type, threshold_value)``, then rewrites with a
    single ``UPDATE`` current, lowest, highest, target, check_interval,
    last_checked_at, consecutive_errors, last_error, last_error_at and is_available.
    A price left at ``UNSET`` keeps what ``add_product`` wrote (the initial price);
    a price passed as ``None`` writes NULL; every other ``None`` argument writes
    NULL. ``active=False`` goes through
    ``Repository.pause_product`` (a manual pause, as ``/pausa``). ``history`` inserts
    ``price_history`` rows with an explicit ``checked_at``.
    """
    product_id = await world.repo.add_product(
        user_id=owner,
        url=url,
        name=name,
        domain=extract_etld_plus_one(url),
        initial_price=Decimal(initial),
        currency=currency,
        threshold_type=threshold[0],
        threshold_value=Decimal(threshold[1]),
    )
    assignments: list[str] = []
    values: list[object] = []
    for column, price in (
        ("current_price", current),
        ("lowest_price", lowest),
        ("highest_price", highest),
    ):
        if price is not UNSET:
            assignments.append(f"{column} = ?")
            values.append(price)
    state: tuple[tuple[str, object], ...] = (
        ("target_price", target),
        ("check_interval_minutes", check_interval),
        ("last_checked_at", last_checked_at),
        ("consecutive_errors", errors),
        ("last_error", last_error),
        ("last_error_at", last_error_at),
        ("is_available", int(available)),
    )
    assignments.extend(f"{column} = ?" for column, _ in state)
    values.extend(value for _, value in state)
    await world.conn.execute(
        f"UPDATE products SET {', '.join(assignments)} WHERE id = ?", (*values, product_id)
    )
    await world.conn.executemany(
        "INSERT INTO price_history(product_id, price, checked_at) VALUES(?, ?, ?)",
        [(product_id, price, checked_at) for checked_at, price in history],
    )
    await world.conn.commit()
    if not active:
        await world.repo.pause_product(product_id)
    return product_id


async def reanchor(
    world: LegacyWorld, table: str, pk: dict[str, object], column: str, value: object
) -> None:
    """Seed ``UPDATE``: set ``column`` to ``value`` on the row of ``table`` matching ``pk``.

    Exactly one row must match (``IS`` comparison, so a NULL key part matches NULL);
    anything else is a ``HarnessFault``.
    """
    if not pk:
        raise HarnessFault(f"reanchor {table}.{column}: empty key")
    where = " AND ".join(f"{_quote_identifier(name)} IS ?" for name in pk)
    cursor = await world.conn.execute(
        f"UPDATE {_quote_identifier(table)} SET {_quote_identifier(column)} = ? WHERE {where}",
        (value, *pk.values()),
    )
    await world.conn.commit()
    if cursor.rowcount != 1:
        raise HarnessFault(f"reanchor {table} {pk}: {cursor.rowcount} rows matched, expected 1")


async def seed_config(world: LegacyWorld, key: str, value: str) -> None:
    """Seed a ``bot_config`` entry."""
    await world.repo.set_config(key, value)


def render_db_value(value: object, real_now: datetime) -> str:
    """Render one database value for the diff.

    ``NULL``; integers in decimal; text as ``json.dumps(ensure_ascii=False)``;
    REAL as ``float:<repr>``; BLOB as ``blob:<len>:<sha256[:12]>``. A text value
    shaped as an ISO/SQLite UTC timestamp within 600 s of ``real_now`` renders as
    ``<now:SHAPE>``, SHAPE being the value with every digit replaced by ``9``.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool | int):
        return str(int(value))
    if isinstance(value, float):
        return f"float:{value!r}"
    if isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        return f"blob:{len(raw)}:{hashlib.sha256(raw).hexdigest()[:12]}"
    text = str(value)
    instant = _db_timestamp(text)
    if instant is not None and abs((instant - real_now).total_seconds()) <= NOW_TOLERANCE_SECONDS:
        shape = "".join("9" if "0" <= ch <= "9" else ch for ch in text)
        return f"<now:{shape}>"
    return json.dumps(text, ensure_ascii=False)


def _text_timestamp(match: re.Match[str]) -> datetime | None:
    """The instant of a ``_WALL_CLOCK_RE`` match (naive means UTC), else ``None``."""
    year, month, day, hour, minute = (int(part) for part in match.groups()[:5])
    second = int(match.group(6) or 0)
    microsecond = int(((match.group(7) or "") + "000000")[:6])
    offset = match.group(8)
    try:
        zone = UTC
        if offset and offset != "Z":
            digits = offset[1:].replace(":", "")
            delta = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            zone = timezone(-delta if offset[0] == "-" else delta)
        return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=zone)
    except ValueError:
        return None


def _db_timestamp(text: str) -> datetime | None:
    """The UTC instant of a timestamp written as SQLite or ISO UTC text, else ``None``."""
    match = _DB_TIMESTAMP_RE.fullmatch(text)
    if match is None:
        return None
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    fraction = match.group(7) or ""
    microsecond = int((fraction + "000000")[:6])
    try:
        return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=UTC)
    except ValueError:
        return None


def diff_dumps(
    before: Mapping[str, TableDump], after: Mapping[str, TableDump], real_now: datetime
) -> list[str]:
    """The ``## db after`` block body for two dumps: ``+``/``-`` per key, ``~`` per column.

    A row is named ``<table> <col>=<value>[,<col>=<value>...]`` over its key
    columns, values rendered by ``render_db_value``. Lines: ``+ <name> {<col>=<value>,
    ...}`` with every non-key column in ``columns`` order, separated by ``", "``;
    ``- <name>``; ``~ <name> <col>: <old> -> <new>``, one per changed column in
    ``columns`` order. Returns ``["(no changes)"]`` when nothing changed. Rows
    sharing a key are ordered by the tuple of their rendered values, then get the
    suffixes ``#2``, ``#3``... on their name, and the key adds a line
    ``!! duplicate key <name> ×<n>`` (also next to ``(no changes)``).
    """
    changes: list[str] = []
    duplicates: list[str] = []
    for table in sorted(set(before) | set(after)):
        old = before.get(table)
        new = after.get(table)
        reference = new if new is not None else old
        assert reference is not None
        old_rows, _ = _named_rows(table, old, real_now)
        new_rows, table_duplicates = _named_rows(table, new, real_now)
        duplicates.extend(table_duplicates)
        old_by_name = dict(old_rows)
        new_by_name = dict(new_rows)
        names = [name for name, _ in new_rows]
        names += [name for name, _ in old_rows if name not in new_by_name]
        columns = list(reference.columns)
        if old is not None:
            columns += [column for column in old.columns if column not in columns]
        for name in names:
            old_row = old_by_name.get(name)
            new_row = new_by_name.get(name)
            if old_row is None:
                assert new_row is not None
                values = ", ".join(
                    f"{column}={value}"
                    for column, value in new_row.items()
                    if column not in reference.key
                )
                changes.append(f"+ {name} {{{values}}}")
            elif new_row is None:
                changes.append(f"- {name}")
            else:
                for column in columns:
                    before_value = old_row.get(column, "<absent>")
                    after_value = new_row.get(column, "<absent>")
                    if before_value != after_value:
                        changes.append(f"~ {name} {column}: {before_value} -> {after_value}")
    if not changes:
        return ["(no changes)", *duplicates]
    return [*changes, *duplicates]


def _named_rows(
    table: str, dump: TableDump | None, real_now: datetime
) -> tuple[list[tuple[str, dict[str, str]]], list[str]]:
    """Rows of ``dump`` rendered and named by key, plus the duplicate-key lines."""
    if dump is None:
        return [], []
    key_positions = [dump.columns.index(column) for column in dump.key]
    groups: dict[tuple[str, ...], list[tuple[str, ...]]] = {}
    for row in dump.rows:
        rendered = tuple(render_db_value(value, real_now) for value in row)
        groups.setdefault(tuple(rendered[i] for i in key_positions), []).append(rendered)
    named: list[tuple[str, dict[str, str]]] = []
    duplicates: list[str] = []
    for key_values, rows in groups.items():
        base = f"{table} " + ",".join(
            f"{column}={value}" for column, value in zip(dump.key, key_values, strict=True)
        )
        rows.sort()
        for index, rendered in enumerate(rows):
            name = base if index == 0 else f"{base}#{index + 1}"
            named.append((name, dict(zip(dump.columns, rendered, strict=True))))
        if len(rows) > 1:
            duplicates.append(f"!! duplicate key {base} ×{len(rows)}")
    return named, duplicates


def _quote_identifier(name: str) -> str:
    """An SQLite identifier in double quotes."""
    return '"' + name.replace('"', '""') + '"'


async def dump_database(conn: aiosqlite.Connection) -> dict[str, TableDump]:
    """Every application table (not ``schema_version``, not ``sqlite_*``), read in full."""
    cursor = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\' AND name != 'schema_version' ORDER BY name"
    )
    names = [str(row[0]) for row in await cursor.fetchall()]
    dumps: dict[str, TableDump] = {}
    for name in names:
        quoted = _quote_identifier(name)
        info = await (await conn.execute(f"PRAGMA table_info({quoted})")).fetchall()
        columns = tuple(str(row[1]) for row in info)
        primary = sorted((int(row[5]), str(row[1])) for row in info if int(row[5]) > 0)
        if primary:
            key = tuple(column for _, column in primary)
            order = ", ".join(_quote_identifier(column) for column in key)
            query = f"SELECT * FROM {quoted} ORDER BY {order}"
        else:
            key = ("rowid",)
            columns = ("rowid", *columns)
            query = f"SELECT rowid, * FROM {quoted} ORDER BY rowid"
        rows = await (await conn.execute(query)).fetchall()
        dumps[name] = TableDump(columns, key, tuple(tuple(row) for row in rows))
    return dumps


# ── rendering of Bot API calls ───────────────────────────────────────

_INDENT: Final = "   "


def _canonical(value: object) -> str:
    """Canonical JSON of a parameter value."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _text_block(name: str, text: str) -> list[str]:
    """A ``name:`` block with one ``|`` line per line of ``text``."""
    lines = [f"{_INDENT}{name}:"]
    for line in text.split("\n"):
        shown = line.replace("\r", "\\r")
        lines.append(f"{_INDENT}| {shown}" if shown else f"{_INDENT}|")
    return lines


def _button(button: Mapping[str, Any]) -> str:
    """One inline button as ``[<label> → <target>]``."""
    label = button.get("text", "")
    if "callback_data" in button:
        target = str(button["callback_data"])
    elif "url" in button:
        target = f"url:{button['url']}"
    else:
        kinds = sorted(name for name in button if name != "text")
        target = kinds[0] if kinds else "?"
    return f"[{label} → {target}]"


def _markup_block(markup: object) -> list[str]:
    """``keyboard:`` for an inline keyboard, ``markup: <json>`` for anything else."""
    if isinstance(markup, dict) and isinstance(markup.get("inline_keyboard"), list):
        lines = [f"{_INDENT}keyboard:"]
        for row in markup["inline_keyboard"]:
            buttons = " ".join(_button(button) for button in row) if row else "[]"
            lines.append(f"{_INDENT}| {buttons}")
        return lines
    return [f"{_INDENT}markup: {_canonical(markup)}"]


def _content_lines(content: bytes) -> list[str]:
    """A document's lines, split on CRLF or LF (the terminator itself is not kept)."""
    text = content.decode("utf-8", errors="replace")
    lines = re.split(r"\r?\n", text)
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()
    return lines


def _upload_head(field_name: str, files: Mapping[str, tuple[str, bytes, str]]) -> str:
    """The header suffix of an uploaded photo or document, empty if nothing was uploaded."""
    upload = files.get(field_name)
    if upload is None:
        return ""
    filename, content, _ = upload
    if field_name == "photo":
        kind = "png" if content.startswith(PNG_MAGIC) else "not-png"
        return f" photo={kind} filename={filename}"
    return f" filename={filename}"


@dataclass(frozen=True)
class RenderedCall:
    """The lines of one call, and every captured text a wall-clock guard must read."""

    lines: tuple[str, ...]
    texts: tuple[str, ...]


def render_call(call: Call, files: Mapping[str, tuple[str, bytes, str]]) -> RenderedCall:
    """Render one Bot API call: its header, its ``param`` lines, then its blocks."""
    params = dict(call.params)
    params.pop("callback_query_id", None)
    method = call.method
    blocks: list[str] = []
    texts: list[str] = []

    def take_text(name: str) -> None:
        value = params.pop(name, None)
        if value is None:
            return
        texts.append(str(value))
        blocks.extend(_text_block(name, str(value)))

    def take_markup() -> None:
        markup = params.pop("reply_markup", None)
        if markup is None:
            return
        parsed = json.loads(markup) if isinstance(markup, str) else markup
        if isinstance(parsed, dict):
            for row in parsed.get("inline_keyboard") or []:
                texts.extend(str(button.get("text", "")) for button in row)
        blocks.extend(_markup_block(parsed))

    if method in {"sendMessage", "editMessageText"}:
        head = f"-> {method} chat={params.pop('chat_id', None)}"
        if method == "editMessageText":
            head += f" message_id={params.pop('message_id', None)}"
        take_text("text")
        take_markup()
    elif method in {"sendPhoto", "sendDocument"}:
        field_name = "photo" if method == "sendPhoto" else "document"
        head = f"-> {method} chat={params.pop('chat_id', None)}" + _upload_head(field_name, files)
        take_text("caption")
        upload = files.get(field_name)
        if method == "sendDocument" and upload is not None:
            content = _content_lines(upload[1])
            texts.extend(content)
            blocks.append(f"{_INDENT}content:")
            blocks.extend(f"{_INDENT}| {line}" if line else f"{_INDENT}|" for line in content)
        take_markup()
    elif method == "deleteMessage":
        head = (
            f"-> deleteMessage chat={params.pop('chat_id', None)}"
            f" message_id={params.pop('message_id', None)}"
        )
    else:
        head = f"-> {method}"
        if isinstance(params.get("text"), str):
            texts.append(params["text"])
    param_lines = [f"{_INDENT}param {name}={_canonical(params[name])}" for name in sorted(params)]
    return RenderedCall((head, *param_lines, *blocks), tuple(texts))


# ── recorder ─────────────────────────────────────────────────────────


def _quote(value: str) -> str:
    """A step argument as it appears in a step header."""
    return json.dumps(value, ensure_ascii=False)


class Recorder:
    """Runs the steps of a scenario and records what each one produced.

    A step header quotes its string argument with
    ``json.dumps(value, ensure_ascii=False)``; ``call_error_handler`` renders the
    exception as ``<TypeName>(<json.dumps(str(exc), ensure_ascii=False)>)``.
    """

    def __init__(self, world: LegacyWorld) -> None:
        self._world = world
        self._lines: list[str] = []
        self._step = 0
        self._errors_seen = 0

    async def command(self, user: int, text: str) -> None:
        """Send ``text`` (a command such as ``"/target 1 49.90"``) from ``user``."""
        await self._captured(
            f"command {_quote(text)} user={user}", self._world.run_command(user, text)
        )

    async def text(self, user: int, text: str) -> None:
        """Send a free text, possibly containing a URL, from ``user``."""
        await self._captured(
            f"text {_quote(text)} user={user}", self._world.run_command(user, text)
        )

    async def press(self, user: int, data: str, *, on: Call | None = None) -> None:
        """Press the button carrying ``data`` as ``user``.

        ``on`` is the bot message the button belongs to (a ``Call`` without a
        ``message_id`` is a ``HarnessFault``); without it the pressed message is the
        highest message id, in the chat of ``user``, whose current keyboard carries
        ``data``, or the synthetic id 1 when none does.
        """
        message_id, synthetic = self._world.pressed_message(user, data, on)
        suffix = " synthetic" if synthetic else ""
        await self._captured(
            f"press {_quote(data)} user={user} on={message_id}{suffix}",
            self._world.press_message(user, data, message_id),
        )

    async def document(self, user: int, filename: str, content: bytes) -> None:
        """Send ``content`` as a CSV document named ``filename`` from ``user``."""
        world = self._world
        file_id = world.upload(filename, content)
        payload = {
            "update_id": next(_update_ids),
            "message": {
                "message_id": next(_update_ids),
                "date": int(datetime.now(tz=UTC).timestamp()),
                "chat": {"id": user, "type": "private"},
                "from": {
                    "id": user,
                    "is_bot": False,
                    "first_name": f"U{user}",
                    "language_code": world.locale,
                },
                "document": {
                    "file_id": file_id,
                    "file_unique_id": f"u{file_id}",
                    "file_name": filename,
                    "mime_type": "text/csv",
                    "file_size": len(content),
                },
            },
        }
        update = Update.de_json(payload, world.app.bot)
        assert update is not None
        await self._captured(
            f"document {_quote(filename)} ({len(content)} bytes) user={user}",
            world.process(update),
        )

    async def job(self, name: JobName, **kw: int) -> None:
        """Run a scheduler job directly (``run_check_all`` or ``digest_flush_due``)."""
        world = self._world
        action: Awaitable[object]
        if name == "run_check_all":
            if kw:
                raise TypeError(f"run_check_all takes no arguments, got {sorted(kw)}")
            action = world.scheduler.run_check_all()
        elif name == "digest_flush_due":
            action = world.digest.flush_due(interval_minutes=kw["interval_minutes"])
        else:
            raise ValueError(f"unknown job {name!r}")
        arguments = ", ".join(f"{key}={value}" for key, value in sorted(kw.items()))
        await self._captured(f"job {name}({arguments})" if kw else f"job {name}", action)

    async def call_error_handler(self, user: int, exc: BaseException) -> None:
        """Call the application's error handler with ``exc`` for an update from ``user``."""
        if not isinstance(exc, Exception):
            raise TypeError(f"the error handler only receives exceptions, not {exc!r}")
        world = self._world
        update = message_update(world.app.bot, user, user, "error", language_code=world.locale)
        context: CallbackContext[Any, Any, Any, Any] = CallbackContext.from_error(
            update, exc, world.app
        )
        await self._captured(
            f"call error_handler {type(exc).__name__}({_quote(str(exc))}) user={user}",
            error_handler(update, context),
        )

    async def seed(self, label: str, coro: Awaitable[object]) -> None:
        """Run ``coro`` as a listed but uncaptured step: no calls and no diff rendered."""
        self._step += 1
        step = self._step
        self._lines.append(f"## step {step}: seed {_quote(label)}")
        try:
            await coro
        finally:
            self._check_faults(step)
        self._lines.extend(self._new_errors())

    async def capture(self, label: str, coro: Awaitable[object]) -> None:
        """Run ``coro`` as a captured step: its calls and database diff are rendered."""
        await self._captured(f"capture {_quote(label)}", coro)

    async def _captured(self, header: str, action: Awaitable[object]) -> None:
        """Run a captured step: header, calls, handler errors, database diff."""
        self._step += 1
        step = self._step
        world = self._world
        before = await dump_database(world.conn)
        first_call = len(world.request.calls)
        try:
            await action
        finally:
            self._check_faults(step)
        body: list[str] = []
        texts: list[str] = []
        for index in range(first_call, len(world.request.calls)):
            rendered = render_call(
                world.request.calls[index], world.request.files_by_call.get(index, {})
            )
            body.extend(rendered.lines)
            texts.extend(rendered.texts)
        self._check_guards(step, texts)
        errors = self._new_errors()
        after = await dump_database(world.conn)
        self._lines.append(f"## step {step}: {header}")
        self._lines.extend(body if body else ["(no calls)"])
        self._lines.extend(errors)
        self._lines.append(f"## db after step {step}")
        self._lines.extend(diff_dumps(before, after, world.real_now))

    def _check_faults(self, step: int) -> None:
        """Raise ``HarnessFault`` if a stub was used off script or a handler asserted."""
        world = self._world
        broken = [e for e in world.errors[self._errors_seen :] if isinstance(e, AssertionError)]
        if world.faults or broken:
            reasons = "; ".join(str(e) for e in [*world.faults, *broken])
            raise HarnessFault(f"step {step}: harness fault: {reasons}")

    def _check_guards(self, step: int, texts: Sequence[str]) -> None:
        """Refuse captured text that depends on the wall clock (guards G1 and G2).

        G1: an absolute timestamp less than a day from the real clock; G2: a
        negative relative time. Nothing in a text is normalized: such a text needs
        seeded data instead.
        """
        real_now = self._world.real_now
        for text in texts:
            for match in _WALL_CLOCK_RE.finditer(text):
                instant = _text_timestamp(match)
                if instant is None:
                    continue
                if abs((instant - real_now).total_seconds()) < WALL_CLOCK_GUARD_SECONDS:
                    raise NonDeterministicOutput(
                        f"step {step}: timestamp {match.group(0)!r} is near the real clock "
                        f"in captured text {text[:200]!r}"
                    )
            relative = _NEGATIVE_RELATIVE_RE.search(text)
            if relative is not None:
                raise NonDeterministicOutput(
                    f"step {step}: negative relative time {relative.group(0)!r} "
                    f"in captured text {text[:200]!r}"
                )

    def _new_errors(self) -> list[str]:
        """Render and consume the handler errors raised since the last step."""
        world = self._world
        new_errors = world.errors[self._errors_seen :]
        self._errors_seen = len(world.errors)
        return [f"{_INDENT}!! error_handler: {type(e).__name__}: {e}" for e in new_errors]

    def snapshot(self, scenario_id: str) -> Snapshot:
        """The rendering of every step recorded so far, as scenario ``scenario_id``."""
        return Snapshot(scenario_id, self._world.locale, tuple(self._lines), self._world.real_now)


FROZEN_NOW_ISO: Final = FROZEN_NOW.replace(" ", "T") + "Z"


@dataclass(frozen=True)
class Snapshot:
    """A recorded scenario: its header fields and the rendered step lines."""

    scenario_id: str
    locale: str
    body: tuple[str, ...]
    real_now: datetime

    def render(self) -> str:
        """The snapshot file content: the header, then every body line, newline-terminated."""
        header = (
            "# legacy snapshot v1",
            f"# scenario: {self.scenario_id}",
            f"# locale: {self.locale}",
            f"# frozen_now: {FROZEN_NOW_ISO}",
        )
        return "\n".join((*header, *self.body)) + "\n"

    def compare_or_update(self, path: Path, *, root: Path = SNAPSHOT_ROOT) -> None:
        """Compare the rendering with ``path``, or rewrite it when updating is requested.

        ``LEGACY_SNAPSHOTS_UPDATE=1`` rewrites a missing or different file (and
        ``root/_generated_with.txt``), but is refused with ``RuntimeError`` when
        ``CI`` is set. Otherwise a missing or different file fails the test with the
        path, the unified diff and the command that recreates it, whose node id is
        read from ``PYTEST_CURRENT_TEST`` (the part before its last space).
        """
        update = os.environ.get("LEGACY_SNAPSHOTS_UPDATE") == "1"
        if update and os.environ.get("CI"):
            raise RuntimeError("refusing to rewrite legacy snapshots under CI (CI is set)")
        actual = self.render()
        expected = path.read_text(encoding="utf-8") if path.is_file() else None
        if update:
            if expected != actual:
                _write_if_changed(path, actual)
                warnings.warn(LegacySnapshotUpdated(path), stacklevel=2)
            _write_if_changed(root / GENERATED_WITH, _generated_with())
            return
        command = f"LEGACY_SNAPSHOTS_UPDATE=1 pytest '{_current_test_node()}'"
        if expected is None:
            pytest.fail(f"missing snapshot {path}\nrun: {command} to create it")
        if expected != actual:
            diff = "".join(
                difflib.unified_diff(
                    expected.splitlines(keepends=True),
                    actual.splitlines(keepends=True),
                    fromfile=str(path),
                    tofile="actual",
                )
            )
            pytest.fail(
                f"snapshot differs: {path}\n{diff}{_version_drift(root)}run: {command} to update it"
            )


def _write_if_changed(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` (UTF-8, LF) unless it already holds exactly that."""
    if path.is_file() and path.read_text(encoding="utf-8") == content:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def _current_test_node() -> str:
    """The node id of the running test, from ``PYTEST_CURRENT_TEST``."""
    current = os.environ.get("PYTEST_CURRENT_TEST", "")
    return current.rsplit(" ", 1)[0] if current else "tests/integration/test_legacy_snapshots.py"


def _environment() -> dict[str, str]:
    """Versions of Python, SQLite and every library that shapes a rendering."""
    versions = {"python": platform.python_version(), "sqlite": sqlite3.sqlite_version}
    for distribution in RENDERING_DISTRIBUTIONS:
        try:
            versions[distribution] = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError:
            versions[distribution] = "absent"
    return versions


def _generated_with() -> str:
    """The content of ``_generated_with.txt``: one ``name==version`` line each."""
    return "".join(f"{name}=={version}\n" for name, version in _environment().items())


def _version_drift(root: Path) -> str:
    """The versions that differ from the ones the snapshots were generated with."""
    recorded_file = root / GENERATED_WITH
    if not recorded_file.is_file():
        return ""
    recorded: dict[str, str] = {}
    for line in recorded_file.read_text(encoding="utf-8").splitlines():
        name, separator, version = line.partition("==")
        if separator:
            recorded[name] = version
    drift = [
        f"  {name}: generated with {recorded.get(name, 'unknown')}, running {version}"
        for name, version in _environment().items()
        if recorded.get(name) != version
    ]
    if not drift:
        return ""
    lines = "\n".join(drift)
    return f"environment differs from the one that generated the snapshots:\n{lines}\n"
