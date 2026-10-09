"""Guided-flow coordinator: one correlation record and one timer per prompt.

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
* G4 - every event in every state has a defined outcome (see the event x state
  test matrix in ``tests/integration/test_guided_flow.py``).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, TelegramError
from telegram.ext import (
    ApplicationHandlerStop,
    BaseHandler,
    filters,
)

from price_tracker.app.inputs import (
    Cancel,
    InputError,
    InputErrorCode,
    parse_currency_code,
)
from price_tracker.bot.callbacks import (
    REGISTRY,
    Action,
    ActionRegistry,
    InvalidCallback,
    decode_legacy_entry,
)
from price_tracker.bot.flow_rendering import (
    _APPLY_TEXTS,
    _HINTS,
    _PREPARE_TEXTS,
    _PROMPTS,
    _SCOPE_LEVEL_LABELS,
    _WHOLE_NUMBER_KINDS,
    LABEL_BACK_NOTIFICATIONS,
    LABEL_BACK_SETTINGS,
    LABEL_CANCEL,
    LABEL_HOME,
    LABEL_ONLY_ON,
    LABEL_OTHER_STORES,
    LABEL_TYPE_CODE,
    PROMPT_DEBUG,
    TEXT_ADDED,
    TEXT_ADDED_OTHER_STORES,
    TEXT_CANCELLED,
    TEXT_CHOOSE_LEVEL,
    TEXT_CURRENCY_CHOSEN,
    TEXT_EXPIRED,
    TEXT_NOT_AUTHORISED,
    TEXT_NOT_FOUND,
    TEXT_RESUMED,
    TEXT_SCOPE_NOTICE,
    TEXT_SCRAPE_FAILED,
    TEXT_SUPERSEDED,
    TEXT_TOO_MANY,
    TEXT_TYPE_CODE,
    TEXT_WHERE_FOLLOWED,
    TEXT_WHICH_CURRENCY,
    TEXT_WHOLE_MINUTES,
    TEXT_WHOLE_NUMBER,
    _kept_text,
    _parse_for,
    closing_text,
)
from price_tracker.bot.flow_state import (
    ADD_COMMANDS,
    CANCEL_COMMAND,
    CURRENCY_BUTTONS,
    ENTRY_ACTIONS,
    LEGACY_DEBUG_ENTRY,
    LEGACY_PENDING_KEY,
    SETTING_KINDS,
    URL_PATTERN,
    ActiveFlow,
    AnyContext,
    ApplyStatus,
    FlowConfig,
    FlowKey,
    FlowKind,
    FlowRegistry,
    FlowServices,
    FlowSnapshot,
    FlowState,
    FlowTimer,
    PreparedProduct,
    PrepareStatus,
    Route,
    RouteKind,
    entry_kind,
)
from price_tracker.bot.messages import _, reset_locale, set_locale

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from telegram import Bot, Message
    from telegram.ext import Application


logger = logging.getLogger("price_tracker.bot.flows")
_MESSAGE_ONLY = filters.UpdateType.MESSAGE


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


async def _telegram_language(user_id: int, language_code: str | None) -> str | None:
    """Default resolver: the Telegram language of the update, without any read."""
    del user_id
    return language_code


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
        locale_resolver: Callable[[int, str | None], Awaitable[str | None]] | None = None,
        debug_runner: Callable[[Update, AnyContext, str], Awaitable[None]] | None = None,
    ) -> None:
        """``locale_resolver(user_id, telegram_language)`` gives the language to answer in.

        Without one, the Telegram language of the update is used as it is.
        ``debug_runner(update, context, url)`` analyses the link sent to the admin debug
        prompt; without one that prompt never opens.
        """
        super().__init__(_ignore)
        self.services = services
        self.timer = timer
        self.config = config or FlowConfig()
        self.registry = registry if registry is not None else FlowRegistry()
        self.codec = codec
        self._locale_resolver = locale_resolver or _telegram_language
        self._debug_runner = debug_runner
        self._bot: Bot | None = None

    def attach(self, bot: Bot) -> None:
        """Give the coordinator the bot its timeouts use."""
        self._bot = bot

    # -- routing (pure: reads the registry, mutates nothing) --------------

    def check_update(self, update: object) -> Route | None:
        """Decide whether and how this update belongs to a guided flow."""
        if not isinstance(update, Update):
            return None
        route = self._route(update)
        if route is None:
            return None
        user = update.effective_user
        return replace(route, language_code=None if user is None else user.language_code)

    def _route(self, update: Update) -> Route | None:
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
        if flow is not None and flow.kind is FlowKind.DEBUG:
            # The debug prompt asks for a link: it takes one before the add entry does.
            return Route(RouteKind.ANSWER, key, snapshot, text=text)
        url = URL_PATTERN.search(text)
        if url is not None:
            if not self.config.add_entry:
                return None if snapshot is None else Route(RouteKind.OTHER_COMMAND, key, snapshot)
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
        if self.config.add_entry and name in ADD_COMMANDS and len(parts) > 1:
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
        decoded: Action | InvalidCallback | None = self.codec.decode(query.data)
        if isinstance(decoded, InvalidCallback):
            decoded = decode_legacy_entry(query.data)
        if decoded is None and query.data == LEGACY_DEBUG_ENTRY:
            decoded = Action("admin.debug", ())
        if isinstance(decoded, Action):
            if self.codec.is_flow_action(decoded):
                return Route(RouteKind.FLOW_CALLBACK, key, snapshot, action=decoded)
            if entry_kind(decoded) is not None:
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
        # Only the Telegram language here, with no I/O: the stored one is read by
        # _localise after the route's registry step, so a ticket or claim is never
        # taken behind a pending read.
        locale = set_locale(check_result.language_code)
        try:
            if self._bot is None:
                self._bot = application.bot
            transport = _Transport(application.bot)
            consumed = await self._dispatch(update, check_result, context, transport)
        finally:
            reset_locale(locale)
        if consumed:
            raise ApplicationHandlerStop

    async def _dispatch(
        self, update: Update, route: Route, context: AnyContext, transport: _Transport
    ) -> bool:
        if route.kind is RouteKind.ENTRY_CALLBACK:
            action = route.action
            kind = None if action is None else entry_kind(action)
            if action is None or kind is None:  # pragma: no cover - routing contract
                return False
            if kind is FlowKind.DEBUG or action.name == "settings.ask":
                return await self._on_global_entry(update, route, context, transport, kind)
            return await self._on_entry_callback(update, route, context, transport)
        if route.kind is RouteKind.FLOW_CALLBACK:
            return await self._on_flow_callback(update, route, transport)
        if route.kind is RouteKind.ADD_ENTRY:
            return await self._on_add_entry(route, context, transport)
        if route.kind is RouteKind.ANSWER:
            return await self._on_answer(update, route, context, transport)
        if route.kind is RouteKind.CANCEL:
            return await self._end_by_user(route, transport, consume=True)
        return await self._end_by_user(route, transport, consume=False)

    # -- shared steps --------------------------------------------------------

    async def _localise(self, route: Route) -> str | None:
        """Read the user's language, make it current for this update and return it.

        Awaited only after the route's synchronous registry step (ticket, claim or
        replace), so a ``/cancel`` processed during the read still invalidates the
        route. ``handle_update`` restores the caller's language when the update ends.
        """
        language = await self._locale_resolver(route.key[1], route.language_code)
        set_locale(language)
        return language

    def _new_token(self) -> str:
        return uuid.uuid4().hex

    def _button(self, label: str, action: Action) -> InlineKeyboardButton:
        return InlineKeyboardButton(label, callback_data=self.codec.encode(action))

    def _cancel_row(self, token: str) -> list[InlineKeyboardButton]:
        return [self._button(_(LABEL_CANCEL), Action("flow.cancel", (token,)))]

    def _nav_markup(self, flow: ActiveFlow) -> InlineKeyboardMarkup | None:
        """The way back and Home under a message that ends a setting prompt; none otherwise."""
        if flow.kind not in SETTING_KINDS:
            return None
        if flow.product_id is None:
            back = self._button(_(LABEL_BACK_SETTINGS), Action("settings"))
        else:
            back = self._button(
                _(LABEL_BACK_NOTIFICATIONS), Action("product.prefs", (flow.product_id,))
            )
        return InlineKeyboardMarkup([[back, self._button(_(LABEL_HOME), Action("home"))]])

    async def _close_superseded(
        self, key: FlowKey, flow: ActiveFlow, transport: _Transport
    ) -> None:
        self.timer.disarm(flow.snapshot(key))
        await transport.edit(flow.prompt_chat_id, flow.prompt_message_id, _(TEXT_SUPERSEDED))

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
        language = await self._localise(route)
        if not self.registry.is_current(route.key, ticket):
            await transport.answer(query.id)
            return True
        active = await self.services.is_active(user_id)
        if not self.registry.is_current(route.key, ticket):
            # Superseded while waiting: open nothing, but still dismiss the
            # button's loading state (every callback query gets one answer).
            await transport.answer(query.id)
            return True
        if not active:
            await transport.answer(query.id, _(TEXT_NOT_AUTHORISED))
            return True
        name = await self.services.product_name(user_id, product_id)
        if not self.registry.is_current(route.key, ticket):
            await transport.answer(query.id)
            return True
        if name is None:
            await transport.answer(query.id, _(TEXT_NOT_FOUND))
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
            language_code=language,
        )
        self._supersede_legacy(context)
        await transport.answer(query.id)
        markup = InlineKeyboardMarkup([self._cancel_row(token)])
        await self._show_prompt(
            route.key, flow, f"{name}\n{_(_PROMPTS[kind])}", markup, transport, ticket
        )
        return True

    async def _on_global_entry(
        self,
        update: Update,
        route: Route,
        context: AnyContext,
        transport: _Transport,
        kind: FlowKind,
    ) -> bool:
        """Open a prompt that belongs to no product: the admin debug or a setting.

        Only an active administrator gets the debug prompt; any active user a setting one.
        """
        query = update.callback_query
        if query is None:  # pragma: no cover - routing contract
            return False
        query_id = query.id
        ticket = self.registry.generation(route.key)
        language = await self._localise(route)
        if not self.registry.is_current(route.key, ticket):
            await transport.answer(query_id)
            return True
        if kind is FlowKind.DEBUG:
            allowed = self._debug_runner is not None and await self.services.is_admin(route.key[1])
        else:
            allowed = await self.services.is_active(route.key[1])
        if not self.registry.is_current(route.key, ticket):
            await transport.answer(query_id)
            return True
        if not allowed:
            await transport.answer(query_id, _(TEXT_NOT_AUTHORISED))
            return True
        flow = ActiveFlow(
            kind=kind,
            state=FlowState.AWAIT_VALUE,
            token=self._new_token(),
            product_id=None,
            prompt_chat_id=route.key[0],
            started_at=time.monotonic(),
            language_code=language,
        )
        self._supersede_legacy(context)
        await transport.answer(query_id)
        markup = InlineKeyboardMarkup([self._cancel_row(flow.token)])
        text = _(PROMPT_DEBUG if kind is FlowKind.DEBUG else _PROMPTS[kind])
        await self._show_prompt(route.key, flow, text, markup, transport, ticket)
        return True

    async def _on_add_entry(self, route: Route, context: AnyContext, transport: _Transport) -> bool:
        key = route.key
        user_id = key[1]
        if route.url is None:  # pragma: no cover - routing contract
            return False
        ticket = self.registry.advance(key)
        language = await self._localise(route)
        if not self.registry.is_current(key, ticket):
            return True
        active = await self.services.is_active(user_id)
        if not self.registry.is_current(key, ticket):
            return True
        if not active:
            await transport.send(key[0], _(TEXT_NOT_AUTHORISED))
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
                    await transport.send(key[0], _(TEXT_RESUMED))
                return True
            await transport.send(key[0], _(_PREPARE_TEXTS.get(result.status, TEXT_SCRAPE_FAILED)))
            return True
        product = result.product
        if product.currency is not None:
            await self._insert_and_branch(
                key, product, product.currency, transport, ticket, language
            )
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
            language_code=language,
        )
        await self._show_prompt(
            key,
            flow,
            f"{product.name}\n{_(TEXT_WHICH_CURRENCY)}",
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
                [self._button(_(LABEL_TYPE_CODE), Action("flow.currency", (token, "type")))],
                [self._button(_(LABEL_CANCEL), Action("flow.currency", (token, "cancel")))],
            ]
        )

    def _scope_markup(self, token: str, store: str, *, picker: bool) -> InlineKeyboardMarkup:
        only = self._button(
            _(LABEL_ONLY_ON).format(store=store)[:60], Action("flow.scope", (token, "store"))
        )
        if not picker:
            other = self._button(_(LABEL_OTHER_STORES), Action("flow.scope_picker", (token,)))
            return InlineKeyboardMarkup([[only], [other]])
        levels = [
            [self._button(_(label), Action("flow.scope", (token, level)))]
            for level, label in _SCOPE_LEVEL_LABELS
        ]
        return InlineKeyboardMarkup([*levels, [only]])

    async def _insert_and_branch(
        self,
        key: FlowKey,
        product: PreparedProduct,
        currency: str,
        transport: _Transport,
        ticket: int,
        language: str | None,
    ) -> None:
        """Insert the product with an explicit currency, then run the scope branch.

        ``language`` is the one already read for this update; the scope prompt keeps it.
        """
        user_id = key[1]
        if not self.registry.is_current(key, ticket):
            return
        added = await self.services.add_product(user_id, product, currency)
        if added.status is not ApplyStatus.OK or added.product_id is None:
            if not self.registry.is_current(key, ticket):
                return
            await transport.send(key[0], _(_APPLY_TEXTS[added.status]))
            return
        product_id = added.product_id
        if not self.config.add_scope_step:
            text = (
                _(TEXT_ADDED)
                if self.registry.is_current(key, ticket)
                else f"{_(TEXT_ADDED)} {_kept_text(product.store)}"
            )
            await transport.send(key[0], text)
            return
        default = await self.services.add_scope_default(user_id)
        if default == "store_only":
            text = (
                _(TEXT_ADDED)
                if self.registry.is_current(key, ticket)
                else f"{_(TEXT_ADDED)} {_kept_text(product.store)}"
            )
            await transport.send(key[0], text)
            return
        if default == "other_stores":
            if not self.registry.is_current(key, ticket):
                await transport.send(key[0], f"{_(TEXT_ADDED)} {_kept_text(product.store)}")
                return
            status = await self.services.set_product_scope(
                user_id, product_id, cross_store=True, scope_override=None
            )
            text = _(TEXT_ADDED_OTHER_STORES if status is ApplyStatus.OK else _APPLY_TEXTS[status])
            if status is not ApplyStatus.OK and not self.registry.is_current(key, ticket):
                text = f"{_(TEXT_ADDED)} {_kept_text(product.store)}"
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
            language_code=language,
        )
        shown = await self._show_prompt(
            key,
            flow,
            f"{product.name}\n{_(TEXT_WHERE_FOLLOWED)}",
            self._scope_markup(token, product.store, picker=False),
            transport,
            ticket,
        )
        if not shown:
            await transport.send(key[0], f"{_(TEXT_ADDED)} {_kept_text(product.store)}")

    async def _on_flow_callback(self, update: Update, route: Route, transport: _Transport) -> bool:
        query = update.callback_query
        action = route.action
        if query is None or action is None:  # pragma: no cover - routing contract
            return False
        token = action.args[0]
        flow = self.registry.get(route.key)
        if flow is None or flow.token != token or not _action_fits(action, flow):
            await self._localise(route)
            await transport.answer(query.id, _(TEXT_EXPIRED))
            return True
        snapshot = flow.snapshot(route.key)
        choice = action.args[1] if len(action.args) > 1 else None
        if action.name == "flow.cancel" or choice == "cancel":
            await self._close_by_button(route, snapshot, query.id, transport)
            return True
        if action.name == "flow.currency" and choice == "type":
            if self.registry.replace(snapshot, replace(flow, typing=True)):
                ticket = self.registry.generation(route.key)
                await self._localise(route)
                await transport.answer(query.id)
                if not self.registry.is_current(route.key, ticket):
                    return True
                markup = InlineKeyboardMarkup([self._cancel_row(flow.token)])
                await transport.edit(
                    flow.prompt_chat_id, flow.prompt_message_id, _(TEXT_TYPE_CODE), markup
                )
            return True
        if action.name == "flow.currency" and isinstance(choice, str):
            return await self._on_currency_chosen(route, snapshot, query.id, choice, transport)
        if action.name == "flow.scope_picker":
            if self.registry.replace(snapshot, replace(flow, picker_open=True)):
                ticket = self.registry.generation(route.key)
                await self._localise(route)
                await transport.answer(query.id)
                if not self.registry.is_current(route.key, ticket):
                    return True
                markup = self._scope_markup(flow.token, flow.store, picker=True)
                await transport.edit(
                    flow.prompt_chat_id, flow.prompt_message_id, _(TEXT_CHOOSE_LEVEL), markup
                )
            return True
        if isinstance(choice, str):
            return await self._on_scope_chosen(route, snapshot, query.id, choice, transport)
        return True  # pragma: no cover - registry contract

    async def _close_by_button(
        self, route: Route, snapshot: FlowSnapshot, query_id: str, transport: _Transport
    ) -> None:
        claimed = self.registry.claim(snapshot)
        if claimed is None:
            await self._localise(route)
            await transport.answer(query_id, _(TEXT_EXPIRED))
            return
        ticket = self.registry.generation(snapshot.key)
        self.timer.disarm(snapshot)
        await self._localise(route)
        await transport.answer(query_id)
        if not self.registry.is_current(snapshot.key, ticket):
            return
        await transport.edit(
            claimed.prompt_chat_id,
            claimed.prompt_message_id,
            closing_text(claimed),
            self._nav_markup(claimed),
        )

    async def _on_currency_chosen(
        self,
        route: Route,
        snapshot: FlowSnapshot,
        query_id: str,
        code: str,
        transport: _Transport,
    ) -> bool:
        claimed = self.registry.claim(snapshot)
        if claimed is None or claimed.payload is None:
            await self._localise(route)
            await transport.answer(query_id, _(TEXT_EXPIRED))
            return True
        ticket = self.registry.generation(snapshot.key)
        self.timer.disarm(snapshot)
        language = await self._localise(route)
        await transport.answer(query_id)
        if not self.registry.is_current(snapshot.key, ticket):
            return True
        await transport.edit(
            claimed.prompt_chat_id,
            claimed.prompt_message_id,
            _(TEXT_CURRENCY_CHOSEN).format(code=code),
        )
        await self._insert_and_branch(
            snapshot.key, claimed.payload, code, transport, ticket, language
        )
        return True

    async def _on_scope_chosen(
        self,
        route: Route,
        snapshot: FlowSnapshot,
        query_id: str,
        choice: str,
        transport: _Transport,
    ) -> bool:
        claimed = self.registry.claim(snapshot)
        if claimed is None or claimed.product_id is None:
            await self._localise(route)
            await transport.answer(query_id, _(TEXT_EXPIRED))
            return True
        ticket = self.registry.generation(snapshot.key)
        self.timer.disarm(snapshot)
        await self._localise(route)
        await transport.answer(query_id)
        if not self.registry.is_current(snapshot.key, ticket):
            return True
        if choice == "store":
            text = _kept_text(claimed.store)
        else:
            status = await self.services.set_product_scope(
                snapshot.key[1], claimed.product_id, cross_store=True, scope_override=choice
            )
            text = _(TEXT_SCOPE_NOTICE if status is ApplyStatus.OK else _APPLY_TEXTS[status])
        if self.registry.is_current(snapshot.key, ticket):
            await transport.edit(claimed.prompt_chat_id, claimed.prompt_message_id, text)
        elif choice != "store" and status is ApplyStatus.OK:
            await transport.send(snapshot.key[0], text)
        return True

    async def _on_answer(
        self, update: Update, route: Route, context: AnyContext, transport: _Transport
    ) -> bool:
        snapshot = route.snapshot
        flow = None if snapshot is None else self.registry.get(snapshot.key)
        if snapshot is None or flow is None or flow.token != snapshot.token or route.text is None:
            return False  # the flow ended since routing: let the fallbacks answer
        chat_id = snapshot.key[0]
        if flow.state is FlowState.AWAIT_CURRENCY:
            parsed_code = parse_currency_code(route.text)
            if isinstance(parsed_code, InputError):
                await self._reject(route, snapshot, flow, parsed_code, transport)
                return True
            claimed = self.registry.claim(snapshot)
            if claimed is None or claimed.payload is None:  # pragma: no cover - sync above
                return False
            self.timer.disarm(snapshot)
            ticket = self.registry.generation(snapshot.key)
            language = await self._localise(route)
            await self._insert_and_branch(
                snapshot.key, claimed.payload, parsed_code, transport, ticket, language
            )
            return True
        value = _parse_for(flow.kind, route.text)
        if isinstance(value, InputError):
            await self._reject(route, snapshot, flow, value, transport)
            return True
        claimed = self.registry.claim(snapshot)
        if claimed is None:  # pragma: no cover - sync above
            return False
        self.timer.disarm(snapshot)
        ticket = self.registry.generation(snapshot.key)
        await self._localise(route)
        if not self.registry.is_current(snapshot.key, ticket):
            return True
        if claimed.kind is FlowKind.DEBUG and isinstance(value, str):
            await self._run_debug(update, context, value, transport)
            return True
        if claimed.kind in SETTING_KINDS:
            status = await self.services.apply_setting(
                snapshot.key[1], claimed.kind, claimed.product_id, value
            )
            if status is not ApplyStatus.OK and not self.registry.is_current(snapshot.key, ticket):
                return True
            await transport.send(chat_id, _(_APPLY_TEXTS[status]), self._nav_markup(claimed))
            return True
        if claimed.product_id is None:  # pragma: no cover - only the debug flow has none
            return False
        if isinstance(value, Cancel):
            await transport.send(chat_id, _(TEXT_CANCELLED))
            return True
        status = await self.services.apply_value(
            snapshot.key[1], claimed.kind, claimed.product_id, value
        )
        if status is not ApplyStatus.OK and not self.registry.is_current(snapshot.key, ticket):
            return True
        await transport.send(chat_id, _(_APPLY_TEXTS[status]))
        return True

    async def _run_debug(
        self, update: Update, context: AnyContext, url: str, transport: _Transport
    ) -> None:
        """Run the debug of ``url`` if the sender is still an active administrator.

        A failure of the admin check or of the run goes to the error handlers here:
        raised, it would let the later handler groups take the link as a product to add.
        """
        user = update.effective_user
        chat = update.effective_chat
        if user is None or chat is None:  # pragma: no cover - routing contract
            return
        try:
            if self._debug_runner is None or not await self.services.is_admin(user.id):
                await transport.send(chat.id, _(TEXT_NOT_AUTHORISED))
                return
            await self._debug_runner(update, context, url)
        except Exception as exc:  # noqa: BLE001 - reported to the error handlers below
            await context.application.process_error(update, exc)

    async def _reject(
        self,
        route: Route,
        snapshot: FlowSnapshot,
        flow: ActiveFlow,
        error: InputError,
        transport: _Transport,
    ) -> None:
        attempts = flow.attempts + 1
        if attempts >= self.config.max_attempts:
            claimed = self.registry.claim(snapshot)
            if claimed is None:
                return
            self.timer.disarm(snapshot)
            ticket = self.registry.generation(snapshot.key)
            await self._localise(route)
            if not self.registry.is_current(snapshot.key, ticket):
                return
            await transport.send(snapshot.key[0], _(TEXT_TOO_MANY), self._nav_markup(claimed))
            return
        self.registry.replace(snapshot, replace(flow, attempts=attempts))
        ticket = self.registry.generation(snapshot.key)
        await self._localise(route)
        if not self.registry.is_current(snapshot.key, ticket):
            return
        hint = _HINTS[error.code]
        if flow.kind is FlowKind.INTERVAL and error.code is InputErrorCode.NOT_A_NUMBER:
            hint = TEXT_WHOLE_MINUTES
        if flow.kind in _WHOLE_NUMBER_KINDS and error.code is InputErrorCode.NOT_A_NUMBER:
            hint = TEXT_WHOLE_NUMBER
        await transport.send(snapshot.key[0], _(hint))

    async def _end_by_user(self, route: Route, transport: _Transport, *, consume: bool) -> bool:
        """``/cancel`` (consumed), another command or a foreign callback (passed on).

        The claim, or the advance of a ``/cancel`` with no flow, happens before any
        await: a ``/cancel`` wins over an opening that is still waiting.
        """
        snapshot = route.snapshot
        claimed = None if snapshot is None else self.registry.claim(snapshot)
        if snapshot is None or claimed is None:
            if route.kind is RouteKind.CANCEL:
                self.registry.advance(route.key)
            return False
        self.timer.disarm(snapshot)
        await self._localise(route)
        await transport.edit(
            claimed.prompt_chat_id,
            claimed.prompt_message_id,
            closing_text(claimed),
            self._nav_markup(claimed),
        )
        return consume

    async def on_timeout(self, snapshot: FlowSnapshot) -> None:
        """Timer callback: acts only if the registry still holds ``snapshot``."""
        claimed = self.registry.claim(snapshot)
        if claimed is None or self._bot is None:
            return
        locale = set_locale(claimed.language_code)
        try:
            transport = _Transport(self._bot)
            await transport.edit(
                claimed.prompt_chat_id,
                claimed.prompt_message_id,
                closing_text(claimed, expired=True),
                self._nav_markup(claimed),
            )
        finally:
            reset_locale(locale)


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


# --- fallbacks (groups 1 and 3) --------------------------------------------
