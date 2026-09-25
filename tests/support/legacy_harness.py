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

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, Literal

from price_tracker.core.scraper_base import AbstractScraper
from tests.support.fake_telegram import FakeRequest

if TYPE_CHECKING:
    from collections.abc import Awaitable, Mapping, Sequence
    from datetime import datetime

    import aiosqlite
    import httpx
    import pytest
    from telegram.ext import Application
    from telegram.request import RequestData

    from price_tracker.config import Config
    from price_tracker.core.health import HealthManager
    from price_tracker.core.registry import ScraperRegistry
    from price_tracker.core.scheduler import Scheduler
    from price_tracker.core.scraper_base import ProductInfo
    from price_tracker.db.repository import Repository
    from price_tracker.notifier.digest import DigestService
    from tests.support.fake_telegram import Call

FROZEN_NOW: Final = "2026-03-01 12:00:00"
"""UTC instant every scenario runs at."""

ADMIN: Final = 1
OWNER: Final = 10
OTHER: Final = 11
STRANGER: Final = 12
"""User ids; a private chat id equals its user id, as on Telegram."""

LOCALES: Final = ("it",)
SNAPSHOT_ROOT: Final = Path(__file__).resolve().parents[1] / "snapshots" / "legacy"

JobName = Literal["run_check_all", "digest_flush_due"]


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
        raise NotImplementedError

    def script(self, url: str, *outcomes: ProductInfo | BaseException) -> None:
        """Set the outcomes of the next scrapes of ``url``, in order; the last one repeats."""
        raise NotImplementedError

    def can_handle(self, url: str) -> bool:
        """True for hosts ending with ``example.com`` or ``amazon.com``."""
        raise NotImplementedError

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        """Play the next scripted outcome of ``url``: return it, or raise it if an exception."""
        raise NotImplementedError


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
        raise NotImplementedError

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
        raise NotImplementedError

    def _result(self, api_method: str, params: dict[str, Any]) -> Any:
        """A ``File`` for ``getFile``; otherwise the answer of ``FakeRequest``."""
        raise NotImplementedError


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

    def __post_init__(self) -> None:
        self.recorder = Recorder(self)

    def upload(self, filename: str, data: bytes) -> str:
        """Make ``data`` downloadable as a new Telegram file and return its ``file_id``.

        File ids are ``doc1``, ``doc2``... in upload order, a counter shared with
        ``Recorder.document``; ``filename`` is the name the document carries.
        """
        raise NotImplementedError


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
    raise NotImplementedError


async def close_world(world: LegacyWorld) -> None:
    """Shut the application down and close the HTTP client and the database."""
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


async def reanchor(
    world: LegacyWorld, table: str, pk: dict[str, object], column: str, value: object
) -> None:
    """Seed ``UPDATE``: set ``column`` to ``value`` on the row of ``table`` matching ``pk``."""
    raise NotImplementedError


async def seed_config(world: LegacyWorld, key: str, value: str) -> None:
    """Seed a ``bot_config`` entry."""
    raise NotImplementedError


def render_db_value(value: object, real_now: datetime) -> str:
    """Render one database value for the diff.

    ``NULL``; integers in decimal; text as ``json.dumps(ensure_ascii=False)``;
    REAL as ``float:<repr>``; BLOB as ``blob:<len>:<sha256[:12]>``. A text value
    shaped as an ISO/SQLite UTC timestamp within 600 s of ``real_now`` renders as
    ``<now:SHAPE>``, SHAPE being the value with every digit replaced by ``9``.
    """
    return ""  # contract stub: an empty rendering fails the assertions


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
    return []  # contract stub: an empty diff fails the assertions


class Recorder:
    """Runs the steps of a scenario and records what each one produced.

    A step header quotes its string argument with
    ``json.dumps(value, ensure_ascii=False)``; ``call_error_handler`` renders the
    exception as ``<TypeName>(<json.dumps(str(exc), ensure_ascii=False)>)``.
    """

    def __init__(self, world: LegacyWorld) -> None:
        self._world = world

    async def command(self, user: int, text: str) -> None:
        """Send ``text`` (a command such as ``"/target 1 49.90"``) from ``user``."""
        raise NotImplementedError

    async def text(self, user: int, text: str) -> None:
        """Send a free text, possibly containing a URL, from ``user``."""
        raise NotImplementedError

    async def press(self, user: int, data: str, *, on: Call | None = None) -> None:
        """Press the button carrying ``data`` as ``user``.

        ``on`` is the bot message the button belongs to (a ``Call`` without a
        ``message_id`` is a ``HarnessFault``); without it the pressed message is the
        highest message id, in the chat of ``user``, whose current keyboard carries
        ``data``, or the synthetic id 1 when none does.
        """
        raise NotImplementedError

    async def document(self, user: int, filename: str, content: bytes) -> None:
        """Send ``content`` as a CSV document named ``filename`` from ``user``."""
        raise NotImplementedError

    async def job(self, name: JobName, **kw: int) -> None:
        """Run a scheduler job directly (``run_check_all`` or ``digest_flush_due``)."""
        raise NotImplementedError

    async def call_error_handler(self, user: int, exc: BaseException) -> None:
        """Call the application's error handler with ``exc`` for an update from ``user``."""
        raise NotImplementedError

    async def seed(self, label: str, coro: Awaitable[object]) -> None:
        """Run ``coro`` as a listed but uncaptured step: no calls and no diff rendered."""
        raise NotImplementedError

    async def capture(self, label: str, coro: Awaitable[object]) -> None:
        """Run ``coro`` as a captured step: its calls and database diff are rendered."""
        raise NotImplementedError

    def snapshot(self, scenario_id: str) -> Snapshot:
        """The rendering of every step recorded so far, as scenario ``scenario_id``."""
        # contract stub: empty body
        return Snapshot(scenario_id, self._world.locale, (), self._world.real_now)


@dataclass(frozen=True)
class Snapshot:
    """A recorded scenario: its header fields and the rendered step lines."""

    scenario_id: str
    locale: str
    body: tuple[str, ...]
    real_now: datetime

    def render(self) -> str:
        """The snapshot file content: the header, then every body line, newline-terminated."""
        return ""  # contract stub: an empty rendering fails the assertions

    def compare_or_update(self, path: Path, *, root: Path = SNAPSHOT_ROOT) -> None:
        """Compare the rendering with ``path``, or rewrite it when updating is requested.

        ``LEGACY_SNAPSHOTS_UPDATE=1`` rewrites a missing or different file (and
        ``root/_generated_with.txt``), but is refused with ``RuntimeError`` when
        ``CI`` is set. Otherwise a missing or different file fails the test with the
        path, the unified diff and the command that recreates it, whose node id is
        read from ``PYTEST_CURRENT_TEST`` (the part before its last space).
        """
        return None  # contract stub: neither compares nor writes
