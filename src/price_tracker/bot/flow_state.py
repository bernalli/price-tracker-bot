"""State, service boundaries, and timer for guided flows."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from telegram.ext import CallbackContext

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from decimal import Decimal

    from telegram.ext import JobQueue

    from price_tracker.bot.callbacks import Action

logger = logging.getLogger("price_tracker.bot.flows")

FlowKey = tuple[int, int]
AnyContext = CallbackContext[Any, Any, Any, Any]
AddScopeDefault = Literal["ask", "store_only", "other_stores"]

URL_PATTERN: Final = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
ADD_COMMANDS: Final = frozenset({"add", "aggiungi"})
CANCEL_COMMAND: Final = "cancel"
LEGACY_PENDING_KEY: Final = "pending_action"
CURRENCY_BUTTONS: Final = ("EUR", "USD", "GBP")


class FlowKind(StrEnum):
    """What a flow edits."""

    THRESHOLD = "threshold"
    TARGET = "target"
    INTERVAL = "interval"
    ADD = "add"
    DEBUG = "debug"
    MUTE = "mute"
    DIGEST = "digest"
    QUIET = "quiet"
    TIMEZONE = "timezone"
    THROTTLE = "throttle"


class FlowState(StrEnum):
    """Where a flow is waiting."""

    AWAIT_VALUE = "await_value"
    AWAIT_CURRENCY = "await_currency"
    AWAIT_SCOPE = "await_scope"


ENTRY_ACTIONS: Final = {
    "product.threshold": FlowKind.THRESHOLD,
    "product.target": FlowKind.TARGET,
    "product.interval": FlowKind.INTERVAL,
    "product.mute_ask": FlowKind.MUTE,
    "admin.debug": FlowKind.DEBUG,
}
# ``settings.ask`` opens the prompt of the setting its argument names.
SETTING_ASKS: Final = {
    "mu": FlowKind.MUTE,
    "dg": FlowKind.DIGEST,
    "qh": FlowKind.QUIET,
    "tz": FlowKind.TIMEZONE,
    "th": FlowKind.THROTTLE,
}
SETTING_KINDS: Final = frozenset(SETTING_ASKS.values())


def entry_kind(action: Action) -> FlowKind | None:
    """The kind of prompt ``action`` opens, or ``None`` when it opens none."""
    if action.name == "settings.ask":
        return SETTING_ASKS.get(str(action.args[0])) if action.args else None
    return ENTRY_ACTIONS.get(action.name)


# The admin menu's Debug button still sends its pre-registry string.
LEGACY_DEBUG_ENTRY: Final = "menu_admin_debug"


@dataclass(frozen=True, slots=True)
class FlowSnapshot:
    """Immutable correlation of one prompt: its key and its token."""

    key: FlowKey
    token: str


@dataclass(frozen=True, slots=True)
class PreparedProduct:
    """A scraped page held by the add flow until the currency is known."""

    url: str
    name: str
    store: str
    price: Decimal
    currency: str | None


@dataclass(frozen=True, slots=True)
class ActiveFlow:
    """The correlation record of one open flow."""

    kind: FlowKind
    state: FlowState
    token: str
    product_id: int | None
    prompt_chat_id: int
    prompt_message_id: int | None = None
    attempts: int = 0
    typing: bool = False
    picker_open: bool = False
    payload: PreparedProduct | None = None
    store: str = ""
    started_at: float = 0.0
    language_code: str | None = None

    def snapshot(self, key: FlowKey) -> FlowSnapshot:
        """The immutable ``(key, token)`` pair of this prompt."""
        return FlowSnapshot(key, self.token)


class FlowRegistry:
    """One :class:`ActiveFlow` per ``(chat_id, user_id)``; every method is sync.

    No method awaits, so a token comparison and the mutation it guards are one
    atomic step for the asyncio scheduler (G3).
    """

    def __init__(self) -> None:
        self._flows: dict[FlowKey, ActiveFlow] = {}
        self._generations: dict[FlowKey, int] = {}

    def __len__(self) -> int:
        return len(self._flows)

    def keys(self) -> frozenset[FlowKey]:
        """Keys with an open flow."""
        return frozenset(self._flows)

    def get(self, key: FlowKey) -> ActiveFlow | None:
        """The open flow for ``key``, if any."""
        return self._flows.get(key)

    def generation(self, key: FlowKey) -> int:
        """Current ticket, retained even when no prompt is open."""
        return self._generations.get(key, 0)

    def advance(self, key: FlowKey) -> int:
        """Invalidate older continuations for this key."""
        ticket = self.generation(key) + 1
        self._generations[key] = ticket
        return ticket

    def is_current(self, key: FlowKey, ticket: int) -> bool:
        """Check a continuation before its next effect, without yielding."""
        return self.generation(key) == ticket

    def open_if_current(self, key: FlowKey, ticket: int, flow: ActiveFlow) -> bool:
        """Check the ticket and open in the same synchronous operation."""
        if not self.is_current(key, ticket):
            return False
        self.open(key, flow)
        return True

    def open(self, key: FlowKey, flow: ActiveFlow) -> ActiveFlow | None:
        """Install ``flow`` for ``key``; return the flow it supersedes."""
        self.advance(key)
        previous = self._flows.get(key)
        self._flows[key] = flow
        return previous

    def discard(self, key: FlowKey) -> ActiveFlow | None:
        """Remove and return whatever flow ``key`` has."""
        self.advance(key)
        return self._flows.pop(key, None)

    def claim(self, snapshot: FlowSnapshot) -> ActiveFlow | None:
        """Remove the flow iff it still carries ``snapshot.token``."""
        current = self._flows.get(snapshot.key)
        if current is None or current.token != snapshot.token:
            return None
        self.advance(snapshot.key)
        del self._flows[snapshot.key]
        return current

    def replace(self, snapshot: FlowSnapshot, flow: ActiveFlow) -> bool:
        """Swap in ``flow`` iff the open flow still carries ``snapshot.token``."""
        current = self._flows.get(snapshot.key)
        if current is None or current.token != snapshot.token:
            return False
        self._flows[snapshot.key] = flow
        return True


# --- boundaries -------------------------------------------------------------


class ApplyStatus(StrEnum):
    """Outcome of a mutating service call."""

    OK = "ok"
    NOT_AUTHORISED = "not_authorised"
    NOT_FOUND = "not_found"


class PrepareStatus(StrEnum):
    """Outcome of ``prepare_add``."""

    READY = "ready"
    FAILED = "failed"
    DUPLICATE_ACTIVE = "duplicate_active"
    DUPLICATE_REACTIVATED = "duplicate_reactivated"
    NOT_AUTHORISED = "not_authorised"


@dataclass(frozen=True, slots=True)
class PrepareResult:
    """What ``prepare_add`` found for a URL."""

    status: PrepareStatus
    product: PreparedProduct | None = None


@dataclass(frozen=True, slots=True)
class AddResult:
    """Outcome of an insert."""

    status: ApplyStatus
    product_id: int | None = None


class FlowServices(Protocol):
    """The application services a flow calls. Every mutation re-checks access."""

    async def is_active(self, user_id: int) -> bool: ...

    async def is_admin(self, user_id: int) -> bool: ...

    async def product_name(self, user_id: int, product_id: int) -> str | None: ...

    async def apply_value(
        self, user_id: int, kind: FlowKind, product_id: int, value: object
    ) -> ApplyStatus: ...

    async def apply_setting(
        self, user_id: int, kind: FlowKind, product_id: int | None, value: object
    ) -> ApplyStatus: ...

    async def prepare_add(self, user_id: int, url: str) -> PrepareResult: ...

    async def add_product(
        self, user_id: int, product: PreparedProduct, currency: str
    ) -> AddResult: ...

    async def add_scope_default(self, user_id: int) -> AddScopeDefault: ...

    async def set_product_scope(
        self, user_id: int, product_id: int, *, cross_store: bool, scope_override: str | None
    ) -> ApplyStatus: ...


class FlowTimer(Protocol):
    """Arms and disarms one timeout per prompt, keyed by its snapshot."""

    def arm(
        self,
        snapshot: FlowSnapshot,
        delay: float,
        callback: Callable[[FlowSnapshot], Awaitable[None]],
    ) -> None: ...

    def disarm(self, snapshot: FlowSnapshot) -> None: ...


class JobQueueTimer:
    """:class:`FlowTimer` on PTB's job queue; the snapshot rides in ``job.data``."""

    def __init__(self, job_queue: JobQueue[Any]) -> None:
        self._job_queue = job_queue
        self._callbacks: dict[str, Callable[[FlowSnapshot], Awaitable[None]]] = {}

    @staticmethod
    def _name(snapshot: FlowSnapshot) -> str:
        return f"guided-flow:{snapshot.key[0]}:{snapshot.key[1]}:{snapshot.token}"

    def arm(
        self,
        snapshot: FlowSnapshot,
        delay: float,
        callback: Callable[[FlowSnapshot], Awaitable[None]],
    ) -> None:
        """Schedule ``callback(snapshot)`` after ``delay`` seconds."""
        self._callbacks[snapshot.token] = callback
        self._job_queue.run_once(self._run, when=delay, data=snapshot, name=self._name(snapshot))

    def disarm(self, snapshot: FlowSnapshot) -> None:
        """Cancel the job of ``snapshot`` if it is still scheduled."""
        self._callbacks.pop(snapshot.token, None)
        for job in self._job_queue.get_jobs_by_name(self._name(snapshot)):
            job.schedule_removal()

    async def _run(self, context: AnyContext) -> None:
        job = context.job
        snapshot = None if job is None else job.data
        if not isinstance(snapshot, FlowSnapshot):
            logger.warning("guided-flow timeout job without a snapshot: %r", snapshot)
            return
        callback = self._callbacks.pop(snapshot.token, None)
        if callback is not None:
            await callback(snapshot)


@dataclass(frozen=True, slots=True)
class FlowConfig:
    """Operator knobs of the coordinator."""

    timeout_seconds: float = 300.0
    max_attempts: int = 3
    add_scope_step: bool = True
    add_entry: bool = True


# --- routing ----------------------------------------------------------------


class RouteKind(StrEnum):
    """What :meth:`GuidedFlow.check_update` decided for an update."""

    ENTRY_CALLBACK = "entry_callback"
    FLOW_CALLBACK = "flow_callback"
    FOREIGN_CALLBACK = "foreign_callback"
    ADD_ENTRY = "add_entry"
    ANSWER = "answer"
    CANCEL = "cancel"
    OTHER_COMMAND = "other_command"


@dataclass(frozen=True, slots=True)
class Route:
    """The routing decision, bound to the snapshot observed at routing time."""

    kind: RouteKind
    key: FlowKey
    snapshot: FlowSnapshot | None = None
    action: Action | None = None
    url: str | None = None
    text: str | None = None
    language_code: str | None = None
