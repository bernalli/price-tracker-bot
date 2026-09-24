"""Guided flows: one coordinator, one correlation record, one timer per prompt.

:class:`GuidedFlow` is a single handler registered in group 0 that owns every
guided interaction (threshold, target and interval prompts; the add flow with its
currency and scope steps). :class:`FlowRegistry` is the only flow state: one
:class:`ActiveFlow` per ``(chat_id, user_id)`` key, each carrying a fresh
``uuid4().hex`` token per prompt.

Correlation is immutable and never lives in ``user_data`` (which PTB shares
between every chat of the same user):

* a flow callback carries the full token of the prompt that emitted it and acts
  only when ``registry[(chat_id, user_id)].token`` equals it;
* a timeout is armed with a :class:`FlowSnapshot` ``(key, token)`` taken when the
  prompt is shown, and acts only if the registry still holds that exact token;
* a text answer is bound, at routing time, to the snapshot of the flow open for
  its own key, and is re-checked when the handler runs.

Guarantees (proved by ``tests/integration/test_guided_flow*.py``):

* G1 - at most one flow per ``(chat_id, user_id)``: the registry is a dict keyed
  by that pair, and opening a flow supersedes the previous one in the same step.
* G2 - an update is consumed by at most one flow: every consumed update ends with
  ``ApplicationHandlerStop``; updates the flow does not consume are returned
  without it so that later groups run.
* G3 - every check-and-update of the registry is synchronous (no ``await`` between
  the token comparison and the mutation); side effects run only after the entry
  was claimed.
* G4 - every event in every state has a defined outcome (see the prototype
  document ``docs/plans/2026-09-23-guided-flow-prototype.md``).
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    ApplicationHandlerStop,
    BaseHandler,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from price_tracker.app.inputs import (
    Cancel,
    InputError,
    InputErrorCode,
    parse_currency_code,
    parse_product_interval,
    parse_target,
    parse_threshold,
)
from price_tracker.bot.callbacks import REGISTRY, Action, ActionRegistry, InvalidCallback

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from decimal import Decimal

    from telegram import Bot, Message
    from telegram.ext import Application, JobQueue


logger = logging.getLogger(__name__)

FlowKey = tuple[int, int]
AnyContext = CallbackContext[Any, Any, Any, Any]
AddScopeDefault = Literal["ask", "store_only", "other_stores"]

URL_PATTERN: Final = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
ADD_COMMANDS: Final = frozenset({"add", "aggiungi"})
CANCEL_COMMAND: Final = "cancel"
LEGACY_PENDING_KEY: Final = "pending_action"
CURRENCY_BUTTONS: Final = ("EUR", "USD", "GBP")
_MESSAGE_ONLY: Final = filters.UpdateType.MESSAGE


class FlowKind(StrEnum):
    """What a flow edits."""

    THRESHOLD = "threshold"
    TARGET = "target"
    INTERVAL = "interval"
    ADD = "add"


class FlowState(StrEnum):
    """Where a flow is waiting."""

    AWAIT_VALUE = "await_value"
    AWAIT_CURRENCY = "await_currency"
    AWAIT_SCOPE = "await_scope"


ENTRY_ACTIONS: Final = {
    "product.threshold": FlowKind.THRESHOLD,
    "product.target": FlowKind.TARGET,
    "product.interval": FlowKind.INTERVAL,
}


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

    async def product_name(self, user_id: int, product_id: int) -> str | None: ...

    async def apply_value(
        self, user_id: int, kind: FlowKind, product_id: int, value: object
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


# --- texts (English; catalogues are out of scope for the prototype) --------

TEXT_EXPIRED: Final = "This button has expired."
TEXT_NOT_AUTHORISED: Final = "Not authorised."
TEXT_NOT_FOUND: Final = "Product not found."
TEXT_SUPERSEDED: Final = "Replaced by a newer prompt."
TEXT_NO_OPEN_PROMPT: Final = "No open prompt - use the buttons or /menu."
TEXT_NOTHING_TO_CANCEL: Final = "Nothing to cancel."
TEXT_TOO_MANY: Final = "Too many invalid answers - cancelled, nothing changed."
TEXT_SAVED: Final = "Saved."
TEXT_ADDED: Final = "Added."
TEXT_ADDED_OTHER_STORES: Final = "Added - other stores will be followed too."
TEXT_SCRAPE_FAILED: Final = "Could not read this page. Nothing was added."
TEXT_ALREADY_TRACKED: Final = "Already tracked."
TEXT_RESUMED: Final = "Tracking resumed."
TEXT_TYPE_CODE: Final = "Type a three-letter currency code, for example USD."
TEXT_CANCELLED: Final = "Cancelled - nothing changed."
TEXT_NO_PRODUCT: Final = "Cancelled - no product was added."
TEXT_EXPIRED_VALUE: Final = "Expired - nothing changed."
TEXT_EXPIRED_ADD: Final = "Expired - no product was added."
TEXT_SCOPE_NOTICE: Final = "Other stores will be followed at this level from a later version."

_PROMPTS: Final = {
    FlowKind.THRESHOLD: "Send the drop threshold: 20% or 5.50 (one dot or comma).",
    FlowKind.TARGET: "Send the target price, e.g. 49.90 (0 clears it).",
    FlowKind.INTERVAL: "Send the check interval in minutes (5-10080, 0 resets).",
}

_HINTS: Final = {
    InputErrorCode.EMPTY: "Please send a value.",
    InputErrorCode.TOO_LONG: "That is too long.",
    InputErrorCode.CONTROL_CHARACTER: "That contains invisible characters.",
    InputErrorCode.NOT_A_NUMBER: "Use digits with one dot or comma, e.g. 1299.99.",
    InputErrorCode.OUT_OF_RANGE: "That value is out of range.",
    InputErrorCode.NOT_A_CURRENCY: "Unknown currency code.",
    InputErrorCode.NOT_A_TIMEZONE: "Unknown time zone.",
    InputErrorCode.NOT_A_TIME_RANGE: "Use HH:MM-HH:MM.",
    InputErrorCode.NOT_A_URL: "That is not a link.",
    InputErrorCode.UNSAFE_URL: "That link is not allowed.",
}


def _kept_text(store: str) -> str:
    return f"Kept: only on {store}."


def closing_text(flow: ActiveFlow, *, expired: bool = False) -> str:
    """The text a prompt is edited to when its flow ends without an answer."""
    if flow.state is FlowState.AWAIT_SCOPE:
        return _kept_text(flow.store)
    if flow.state is FlowState.AWAIT_CURRENCY:
        return TEXT_EXPIRED_ADD if expired else TEXT_NO_PRODUCT
    return TEXT_EXPIRED_VALUE if expired else TEXT_CANCELLED


class _Transport:
    """Bot calls for one update or timeout; never retries a blocked chat."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self.blocked: set[int] = set()

    def _failed(self, chat_id: int | None, exc: TelegramError) -> None:
        if isinstance(exc, Forbidden) and chat_id is not None:
            self.blocked.add(chat_id)
        logger.warning("guided-flow transport failure (chat %s): %s", chat_id, exc)

    async def send(
        self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> Message | None:
        if chat_id in self.blocked:
            return None
        try:
            return await self._bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        except TelegramError as exc:
            self._failed(chat_id, exc)
            return None

    async def edit(
        self,
        chat_id: int,
        message_id: int | None,
        text: str,
        markup: InlineKeyboardMarkup | None = None,
    ) -> bool:
        if message_id is None or chat_id in self.blocked:
            return False
        try:
            await self._bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup
            )
        except TelegramError as exc:
            self._failed(chat_id, exc)
            return False
        return True

    async def answer(self, query_id: str, text: str | None = None) -> None:
        try:
            await self._bot.answer_callback_query(callback_query_id=query_id, text=text)
        except TelegramError as exc:
            self._failed(None, exc)


async def _ignore(update: object, context: AnyContext) -> None:
    """Placeholder callback: :class:`GuidedFlow` overrides ``handle_update``."""
    del update, context


class GuidedFlow(BaseHandler[Update, AnyContext, None]):
    """The single coordinator of every guided flow (group 0)."""

    def __init__(
        self,
        services: FlowServices,
        timer: FlowTimer,
        *,
        config: FlowConfig | None = None,
        registry: FlowRegistry | None = None,
        codec: ActionRegistry = REGISTRY,
    ) -> None:
        super().__init__(_ignore)
        self.services = services
        self.timer = timer
        self.config = config or FlowConfig()
        self.registry = registry if registry is not None else FlowRegistry()
        self.codec = codec
        self._bot: Bot | None = None

    def attach(self, bot: Bot) -> None:
        """Give the coordinator the bot its timeouts use."""
        self._bot = bot

    # -- routing (pure: reads the registry, mutates nothing) --------------

    def check_update(self, update: object) -> Route | None:
        """Decide whether and how this update belongs to a guided flow."""
        if not isinstance(update, Update):
            return None
        if update.callback_query is not None:
            return self._route_callback(update)
        if not _MESSAGE_ONLY.check_update(update):
            return None
        message = update.message
        if message is None or message.from_user is None or message.text is None:
            return None
        key = (message.chat.id, message.from_user.id)
        flow = self.registry.get(key)
        snapshot = None if flow is None else flow.snapshot(key)
        text = message.text
        if filters.COMMAND.check_update(update):
            return self._route_command(key, snapshot, text)
        url = URL_PATTERN.search(text)
        if url is not None:
            return Route(RouteKind.ADD_ENTRY, key, snapshot, url=url.group(0).rstrip(".,;:!?)"))
        if flow is None:
            return None
        if flow.state is FlowState.AWAIT_VALUE or (
            flow.state is FlowState.AWAIT_CURRENCY and flow.typing
        ):
            return Route(RouteKind.ANSWER, key, snapshot, text=text)
        return None

    def _route_command(
        self, key: FlowKey, snapshot: FlowSnapshot | None, text: str
    ) -> Route | None:
        parts = text.split()
        name, separator, recipient = parts[0][1:].partition("@")
        if separator:
            try:
                bot_username = self._bot.username if self._bot is not None else None
            except RuntimeError:
                bot_username = None
            if not bot_username or recipient.lower() != bot_username.lower():
                return None
        name = name.lower()
        if name in ADD_COMMANDS and len(parts) > 1:
            url = URL_PATTERN.fullmatch(parts[1])
            if url is not None:
                return Route(RouteKind.ADD_ENTRY, key, snapshot, url=url.group(0))
        if name == CANCEL_COMMAND:
            return Route(RouteKind.CANCEL, key, snapshot)
        if snapshot is None:
            return None
        return Route(RouteKind.OTHER_COMMAND, key, snapshot)

    def _route_callback(self, update: Update) -> Route | None:
        query = update.callback_query
        if query is None or query.message is None:
            return None
        key = (query.message.chat.id, query.from_user.id)
        flow = self.registry.get(key)
        snapshot = None if flow is None else flow.snapshot(key)
        decoded = self.codec.decode(query.data)
        if isinstance(decoded, Action):
            if self.codec.is_flow_action(decoded):
                return Route(RouteKind.FLOW_CALLBACK, key, snapshot, action=decoded)
            if decoded.name in ENTRY_ACTIONS:
                return Route(RouteKind.ENTRY_CALLBACK, key, snapshot, action=decoded)
        if snapshot is None:
            return None
        return Route(RouteKind.FOREIGN_CALLBACK, key, snapshot)

    # -- handling ----------------------------------------------------------

    async def handle_update(
        self,
        update: Update,
        application: Application[Any, AnyContext, Any, Any, Any, Any],
        check_result: object,
        context: AnyContext,
    ) -> None:
        """Run the route; raise ``ApplicationHandlerStop`` iff the update was consumed."""
        if not isinstance(check_result, Route):  # pragma: no cover - PTB contract
            return
        if self._bot is None:
            self._bot = application.bot
        transport = _Transport(application.bot)
        consumed = await self._dispatch(update, check_result, context, transport)
        if consumed:
            raise ApplicationHandlerStop

    async def _dispatch(
        self, update: Update, route: Route, context: AnyContext, transport: _Transport
    ) -> bool:
        if route.kind is RouteKind.ENTRY_CALLBACK:
            return await self._on_entry_callback(update, route, context, transport)
        if route.kind is RouteKind.FLOW_CALLBACK:
            return await self._on_flow_callback(update, route, transport)
        if route.kind is RouteKind.ADD_ENTRY:
            return await self._on_add_entry(route, context, transport)
        if route.kind is RouteKind.ANSWER:
            return await self._on_answer(route, transport)
        if route.kind is RouteKind.CANCEL:
            return await self._end_by_user(route, transport, consume=True)
        return await self._end_by_user(route, transport, consume=False)

    # -- shared steps --------------------------------------------------------

    def _new_token(self) -> str:
        return uuid.uuid4().hex

    def _button(self, label: str, action: Action) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=self.codec.encode(action))

    def _cancel_row(self, token: str) -> list[InlineKeyboardButton]:
        return [self._button("Cancel", Action("flow.cancel", (token,)))]

    async def _close_superseded(
        self, key: FlowKey, flow: ActiveFlow, transport: _Transport
    ) -> None:
        self.timer.disarm(flow.snapshot(key))
        await transport.edit(flow.prompt_chat_id, flow.prompt_message_id, TEXT_SUPERSEDED)

    def _supersede_legacy(self, context: AnyContext) -> None:
        user_data = context.user_data
        if isinstance(user_data, dict):
            user_data.pop(LEGACY_PENDING_KEY, None)

    async def _show_prompt(
        self,
        key: FlowKey,
        flow: ActiveFlow,
        text: str,
        markup: InlineKeyboardMarkup,
        transport: _Transport,
        ticket: int,
    ) -> bool:
        """Open ``flow`` for ``key``, send its prompt, then arm its timer.

        The registry is written before the send (E13); a failed send ends the flow.
        """
        superseded = self.registry.get(key)
        if not self.registry.open_if_current(key, ticket, flow):
            return False
        ticket = self.registry.generation(key)
        if superseded is not None:
            await self._close_superseded(key, superseded, transport)
        snapshot = flow.snapshot(key)
        if not self.registry.is_current(key, ticket):
            return False
        message = await transport.send(key[0], text, markup)
        if not self.registry.is_current(key, ticket):
            return False
        if message is None:
            self.registry.claim(snapshot)
            return True
        if self.registry.replace(snapshot, replace(flow, prompt_message_id=message.message_id)):
            self.timer.arm(snapshot, self.config.timeout_seconds, self.on_timeout)
        return True

    # -- events ---------------------------------------------------------------

    async def _on_entry_callback(
        self, update: Update, route: Route, context: AnyContext, transport: _Transport
    ) -> bool:
        query = update.callback_query
        action = route.action
        if query is None or action is None:  # pragma: no cover - routing contract
            return False
        product_id = action.args[0]
        if not isinstance(product_id, int):  # pragma: no cover - registry contract
            return False
        user_id = route.key[1]
        ticket = self.registry.generation(route.key)
        active = await self.services.is_active(user_id)
        if not self.registry.is_current(route.key, ticket):
            return True
        if not active:
            await transport.answer(query.id, TEXT_NOT_AUTHORISED)
            return True
        name = await self.services.product_name(user_id, product_id)
        if not self.registry.is_current(route.key, ticket):
            return True
        if name is None:
            await transport.answer(query.id, TEXT_NOT_FOUND)
            return True
        kind = ENTRY_ACTIONS[action.name]
        token = self._new_token()
        flow = ActiveFlow(
            kind=kind,
            state=FlowState.AWAIT_VALUE,
            token=token,
            product_id=product_id,
            prompt_chat_id=route.key[0],
            started_at=time.monotonic(),
        )
        self._supersede_legacy(context)
        await transport.answer(query.id)
        markup = InlineKeyboardMarkup([self._cancel_row(token)])
        await self._show_prompt(
            route.key, flow, f"{name}\n{_PROMPTS[kind]}", markup, transport, ticket
        )
        return True

    async def _on_add_entry(self, route: Route, context: AnyContext, transport: _Transport) -> bool:
        key = route.key
        user_id = key[1]
        if route.url is None:  # pragma: no cover - routing contract
            return False
        ticket = self.registry.advance(key)
        active = await self.services.is_active(user_id)
        if not self.registry.is_current(key, ticket):
            return True
        if not active:
            await transport.send(key[0], TEXT_NOT_AUTHORISED)
            return True
        superseded = self.registry.discard(key)
        ticket = self.registry.generation(key)
        self._supersede_legacy(context)
        if superseded is not None:
            await self._close_superseded(key, superseded, transport)
        if not self.registry.is_current(key, ticket):
            return True
        result = await self.services.prepare_add(user_id, route.url)
        if result.status is not PrepareStatus.READY or result.product is None:
            if not self.registry.is_current(key, ticket):
                if result.status is PrepareStatus.DUPLICATE_REACTIVATED:
                    await transport.send(key[0], TEXT_RESUMED)
                return True
            await transport.send(key[0], _PREPARE_TEXTS.get(result.status, TEXT_SCRAPE_FAILED))
            return True
        product = result.product
        if product.currency is not None:
            await self._insert_and_branch(key, product, product.currency, transport, ticket)
            return True
        token = self._new_token()
        flow = ActiveFlow(
            kind=FlowKind.ADD,
            state=FlowState.AWAIT_CURRENCY,
            token=token,
            product_id=None,
            prompt_chat_id=key[0],
            payload=product,
            store=product.store,
            started_at=time.monotonic(),
        )
        await self._show_prompt(
            key,
            flow,
            f"{product.name}\nWhich currency is this price in?",
            self._currency_markup(token),
            transport,
            ticket,
        )
        return True

    def _currency_markup(self, token: str) -> InlineKeyboardMarkup:
        codes = [
            self._button(code, Action("flow.currency", (token, code))) for code in CURRENCY_BUTTONS
        ]
        return InlineKeyboardMarkup(
            [
                codes,
                [self._button("Type a code", Action("flow.currency", (token, "type")))],
                [self._button("Cancel", Action("flow.currency", (token, "cancel")))],
            ]
        )

    def _scope_markup(self, token: str, store: str, *, picker: bool) -> InlineKeyboardMarkup:
        only = self._button(f"Only on {store}"[:60], Action("flow.scope", (token, "store")))
        if not picker:
            return InlineKeyboardMarkup(
                [[only], [self._button("Other stores too", Action("flow.scope_picker", (token,)))]]
            )
        levels = [
            [self._button(label, Action("flow.scope", (token, level)))]
            for level, label in (
                ("own_country", "My country"),
                ("customs_area", "My customs area"),
                ("world", "Worldwide"),
            )
        ]
        return InlineKeyboardMarkup([*levels, [only]])

    async def _insert_and_branch(
        self,
        key: FlowKey,
        product: PreparedProduct,
        currency: str,
        transport: _Transport,
        ticket: int,
    ) -> None:
        """Insert the product with an explicit currency, then run the scope branch."""
        user_id = key[1]
        if not self.registry.is_current(key, ticket):
            return
        added = await self.services.add_product(user_id, product, currency)
        if added.status is not ApplyStatus.OK or added.product_id is None:
            if not self.registry.is_current(key, ticket):
                return
            await transport.send(key[0], _APPLY_TEXTS[added.status])
            return
        product_id = added.product_id
        if not self.config.add_scope_step:
            text = (
                TEXT_ADDED
                if self.registry.is_current(key, ticket)
                else f"{TEXT_ADDED} {_kept_text(product.store)}"
            )
            await transport.send(key[0], text)
            return
        default = await self.services.add_scope_default(user_id)
        if default == "store_only":
            text = (
                TEXT_ADDED
                if self.registry.is_current(key, ticket)
                else f"{TEXT_ADDED} {_kept_text(product.store)}"
            )
            await transport.send(key[0], text)
            return
        if default == "other_stores":
            if not self.registry.is_current(key, ticket):
                await transport.send(key[0], f"{TEXT_ADDED} {_kept_text(product.store)}")
                return
            status = await self.services.set_product_scope(
                user_id, product_id, cross_store=True, scope_override=None
            )
            text = TEXT_ADDED_OTHER_STORES if status is ApplyStatus.OK else _APPLY_TEXTS[status]
            if status is not ApplyStatus.OK and not self.registry.is_current(key, ticket):
                text = f"{TEXT_ADDED} {_kept_text(product.store)}"
            await transport.send(key[0], text)
            return
        token = self._new_token()
        flow = ActiveFlow(
            kind=FlowKind.ADD,
            state=FlowState.AWAIT_SCOPE,
            token=token,
            product_id=product_id,
            prompt_chat_id=key[0],
            store=product.store,
            started_at=time.monotonic(),
        )
        shown = await self._show_prompt(
            key,
            flow,
            f"{product.name}\nWhere should the price be followed?",
            self._scope_markup(token, product.store, picker=False),
            transport,
            ticket,
        )
        if not shown:
            await transport.send(key[0], f"{TEXT_ADDED} {_kept_text(product.store)}")

    async def _on_flow_callback(self, update: Update, route: Route, transport: _Transport) -> bool:
        query = update.callback_query
        action = route.action
        if query is None or action is None:  # pragma: no cover - routing contract
            return False
        token = action.args[0]
        flow = self.registry.get(route.key)
        if flow is None or flow.token != token or not _action_fits(action, flow):
            await transport.answer(query.id, TEXT_EXPIRED)
            return True
        snapshot = flow.snapshot(route.key)
        choice = action.args[1] if len(action.args) > 1 else None
        if action.name == "flow.cancel" or choice == "cancel":
            await self._close_by_button(snapshot, query.id, transport)
            return True
        if action.name == "flow.currency" and choice == "type":
            if self.registry.replace(snapshot, replace(flow, typing=True)):
                ticket = self.registry.generation(route.key)
                await transport.answer(query.id)
                if not self.registry.is_current(route.key, ticket):
                    return True
                markup = InlineKeyboardMarkup([self._cancel_row(flow.token)])
                await transport.edit(
                    flow.prompt_chat_id, flow.prompt_message_id, TEXT_TYPE_CODE, markup
                )
            return True
        if action.name == "flow.currency" and isinstance(choice, str):
            return await self._on_currency_chosen(snapshot, query.id, choice, transport)
        if action.name == "flow.scope_picker":
            if self.registry.replace(snapshot, replace(flow, picker_open=True)):
                ticket = self.registry.generation(route.key)
                await transport.answer(query.id)
                if not self.registry.is_current(route.key, ticket):
                    return True
                markup = self._scope_markup(flow.token, flow.store, picker=True)
                await transport.edit(
                    flow.prompt_chat_id, flow.prompt_message_id, "Choose a level:", markup
                )
            return True
        if isinstance(choice, str):
            return await self._on_scope_chosen(snapshot, query.id, choice, transport)
        return True  # pragma: no cover - registry contract

    async def _close_by_button(
        self, snapshot: FlowSnapshot, query_id: str, transport: _Transport
    ) -> None:
        claimed = self.registry.claim(snapshot)
        if claimed is None:
            await transport.answer(query_id, TEXT_EXPIRED)
            return
        ticket = self.registry.generation(snapshot.key)
        self.timer.disarm(snapshot)
        await transport.answer(query_id)
        if not self.registry.is_current(snapshot.key, ticket):
            return
        await transport.edit(
            claimed.prompt_chat_id, claimed.prompt_message_id, closing_text(claimed)
        )

    async def _on_currency_chosen(
        self, snapshot: FlowSnapshot, query_id: str, code: str, transport: _Transport
    ) -> bool:
        claimed = self.registry.claim(snapshot)
        if claimed is None or claimed.payload is None:
            await transport.answer(query_id, TEXT_EXPIRED)
            return True
        ticket = self.registry.generation(snapshot.key)
        self.timer.disarm(snapshot)
        await transport.answer(query_id)
        if not self.registry.is_current(snapshot.key, ticket):
            return True
        await transport.edit(claimed.prompt_chat_id, claimed.prompt_message_id, f"Currency: {code}")
        await self._insert_and_branch(snapshot.key, claimed.payload, code, transport, ticket)
        return True

    async def _on_scope_chosen(
        self, snapshot: FlowSnapshot, query_id: str, choice: str, transport: _Transport
    ) -> bool:
        claimed = self.registry.claim(snapshot)
        if claimed is None or claimed.product_id is None:
            await transport.answer(query_id, TEXT_EXPIRED)
            return True
        ticket = self.registry.generation(snapshot.key)
        self.timer.disarm(snapshot)
        await transport.answer(query_id)
        if not self.registry.is_current(snapshot.key, ticket):
            return True
        if choice == "store":
            text = _kept_text(claimed.store)
        else:
            status = await self.services.set_product_scope(
                snapshot.key[1], claimed.product_id, cross_store=True, scope_override=choice
            )
            text = TEXT_SCOPE_NOTICE if status is ApplyStatus.OK else _APPLY_TEXTS[status]
        if self.registry.is_current(snapshot.key, ticket):
            await transport.edit(claimed.prompt_chat_id, claimed.prompt_message_id, text)
        elif choice != "store" and status is ApplyStatus.OK:
            await transport.send(snapshot.key[0], text)
        return True

    async def _on_answer(self, route: Route, transport: _Transport) -> bool:
        snapshot = route.snapshot
        flow = None if snapshot is None else self.registry.get(snapshot.key)
        if snapshot is None or flow is None or flow.token != snapshot.token or route.text is None:
            return False  # the flow ended since routing: let the fallbacks answer
        chat_id = snapshot.key[0]
        if flow.state is FlowState.AWAIT_CURRENCY:
            parsed_code = parse_currency_code(route.text)
            if isinstance(parsed_code, InputError):
                await self._reject(snapshot, flow, parsed_code, transport)
                return True
            claimed = self.registry.claim(snapshot)
            if claimed is None or claimed.payload is None:  # pragma: no cover - sync above
                return False
            self.timer.disarm(snapshot)
            await self._insert_and_branch(
                snapshot.key,
                claimed.payload,
                parsed_code,
                transport,
                self.registry.generation(snapshot.key),
            )
            return True
        value = _parse_for(flow.kind, route.text)
        if isinstance(value, InputError):
            await self._reject(snapshot, flow, value, transport)
            return True
        claimed = self.registry.claim(snapshot)
        if claimed is None or claimed.product_id is None:  # pragma: no cover - sync above
            return False
        self.timer.disarm(snapshot)
        ticket = self.registry.generation(snapshot.key)
        if isinstance(value, Cancel):
            await transport.send(chat_id, TEXT_CANCELLED)
            return True
        status = await self.services.apply_value(
            snapshot.key[1], claimed.kind, claimed.product_id, value
        )
        if status is not ApplyStatus.OK and not self.registry.is_current(snapshot.key, ticket):
            return True
        await transport.send(
            chat_id, TEXT_SAVED if status is ApplyStatus.OK else _APPLY_TEXTS[status]
        )
        return True

    async def _reject(
        self, snapshot: FlowSnapshot, flow: ActiveFlow, error: InputError, transport: _Transport
    ) -> None:
        attempts = flow.attempts + 1
        if attempts >= self.config.max_attempts:
            if self.registry.claim(snapshot) is not None:
                self.timer.disarm(snapshot)
            await transport.send(snapshot.key[0], TEXT_TOO_MANY)
            return
        self.registry.replace(snapshot, replace(flow, attempts=attempts))
        await transport.send(snapshot.key[0], _HINTS[error.code])

    async def _end_by_user(self, route: Route, transport: _Transport, *, consume: bool) -> bool:
        """``/cancel`` (consumed), another command or a foreign callback (passed on)."""
        snapshot = route.snapshot
        claimed = None if snapshot is None else self.registry.claim(snapshot)
        if snapshot is None or claimed is None:
            if route.kind is RouteKind.CANCEL:
                self.registry.advance(route.key)
            return False
        self.timer.disarm(snapshot)
        await transport.edit(
            claimed.prompt_chat_id, claimed.prompt_message_id, closing_text(claimed)
        )
        return consume

    async def on_timeout(self, snapshot: FlowSnapshot) -> None:
        """Timer callback: acts only if the registry still holds ``snapshot``."""
        claimed = self.registry.claim(snapshot)
        if claimed is None or self._bot is None:
            return
        transport = _Transport(self._bot)
        await transport.edit(
            claimed.prompt_chat_id, claimed.prompt_message_id, closing_text(claimed, expired=True)
        )


def _action_fits(action: Action, flow: ActiveFlow) -> bool:
    if action.name == "flow.cancel":
        return True
    if action.name == "flow.currency":
        return flow.state is FlowState.AWAIT_CURRENCY and not (
            action.args[1] == "type" and flow.typing
        )
    if action.name == "flow.scope_picker":
        return flow.state is FlowState.AWAIT_SCOPE and not flow.picker_open
    return flow.state is FlowState.AWAIT_SCOPE


def _parse_for(kind: FlowKind, text: str) -> object:
    if kind is FlowKind.THRESHOLD:
        return parse_threshold(text)
    if kind is FlowKind.TARGET:
        return parse_target(text)
    return parse_product_interval(text)


_APPLY_TEXTS: Final = {
    ApplyStatus.OK: TEXT_SAVED,
    ApplyStatus.NOT_AUTHORISED: TEXT_NOT_AUTHORISED,
    ApplyStatus.NOT_FOUND: TEXT_NOT_FOUND,
}

_PREPARE_TEXTS: Final = {
    PrepareStatus.FAILED: TEXT_SCRAPE_FAILED,
    PrepareStatus.DUPLICATE_ACTIVE: TEXT_ALREADY_TRACKED,
    PrepareStatus.DUPLICATE_REACTIVATED: TEXT_RESUMED,
    PrepareStatus.NOT_AUTHORISED: TEXT_NOT_AUTHORISED,
}


# --- fallbacks (groups 1 and 3) --------------------------------------------


async def nothing_to_cancel(update: Update, context: AnyContext) -> None:
    """Group 1: ``/cancel`` outside a flow."""
    del context
    if update.message is not None:
        await update.message.reply_text(TEXT_NOTHING_TO_CANCEL)


async def no_open_prompt(update: Update, context: AnyContext) -> None:
    """Group 3: free text that no flow asked for."""
    del context
    if update.message is not None:
        await update.message.reply_text(TEXT_NO_OPEN_PROMPT)


async def expired_callback(update: Update, context: AnyContext) -> None:
    """Group 3 catch-all: callback data outside the registry gets the expiry toast."""
    del context
    if update.callback_query is not None:
        await update.callback_query.answer(TEXT_EXPIRED)


FLOW_GROUP: Final = 0
COMMAND_GROUP: Final = 1
FALLBACK_GROUP: Final = 3


def register_guided_flow(
    application: Application[Any, Any, Any, Any, Any, Any],
    flow: GuidedFlow,
    *,
    legacy_handlers_present: bool = False,
) -> None:
    """Install the coordinator (group 0), ``/cancel`` (group 1) and the fallbacks (group 3).

    While legacy handlers are registered (``legacy_handlers_present``), the two
    group-3 fallbacks are **not** installed: legacy text and callback handlers
    neither raise ``ApplicationHandlerStop`` nor report consumption, so a group-3
    fallback would answer a message or a callback query that a legacy handler
    already consumed (measured by the coexistence tests). Until the last legacy
    handler is deleted, the legacy free-text handler stays the only free-text
    consumer and unanswered callbacks keep today's behaviour.
    """
    flow.attach(application.bot)
    application.add_handler(flow, group=FLOW_GROUP)
    application.add_handler(
        CommandHandler(CANCEL_COMMAND, nothing_to_cancel, filters=_MESSAGE_ONLY),
        group=COMMAND_GROUP,
    )
    if legacy_handlers_present:
        return
    application.add_handler(
        CallbackQueryHandler(expired_callback, pattern=is_unregistered), group=FALLBACK_GROUP
    )
    application.add_handler(
        MessageHandler(_MESSAGE_ONLY & filters.TEXT & ~filters.COMMAND, no_open_prompt),
        group=FALLBACK_GROUP,
    )


def is_unregistered(data: object) -> bool:
    """Pattern of the group-3 catch-all: data outside the registered language.

    PTB evaluates every group unless a handler raises ``ApplicationHandlerStop``,
    so a pattern-less catch-all would answer a second time every callback that a
    group-1 action handler already answered (measured by the codec tests).
    """
    return isinstance(REGISTRY.decode(data), InvalidCallback)


def is_registered_non_flow(data: object) -> bool:
    """Pattern for group-1 action handlers: a registered action that is not a flow's."""
    decoded = REGISTRY.decode(data)
    return not isinstance(decoded, InvalidCallback) and not REGISTRY.is_flow_action(decoded)
