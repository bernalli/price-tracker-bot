"""Fake Telegram transport and builders for real ``Update`` objects.

``FakeRequest`` replaces the HTTP layer of a real ``telegram.Bot``: every Bot API
call goes through PTB's own request/response code and returns real ``telegram``
objects, while this class records the call and answers without any network. It
can be told to answer the next call to a given chat with ``403 Forbidden``, and it
records (instead of accepting silently) any text, toast or callback data outside
the Telegram limits.

The helpers build updates with ``Update.de_json`` from the JSON shapes the Bot API
sends, and ``make_application`` builds a real ``Application`` around the fake.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from telegram import Update
from telegram.ext import Application, ApplicationBuilder
from telegram.request import BaseRequest

from price_tracker.bot.flows import (
    AddResult,
    AddScopeDefault,
    ApplyStatus,
    FlowKind,
    FlowSnapshot,
    PreparedProduct,
    PrepareResult,
    PrepareStatus,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from telegram.request import RequestData

BOT_ID = 777
MAX_TEXT = 4096
MAX_TOAST = 200
MAX_CALLBACK_BYTES = 64


@dataclass
class Call:
    """One Bot API call as the fake saw it."""

    method: str
    params: dict[str, Any]
    failed: bool = False
    message_id: int | None = None

    @property
    def chat_id(self) -> int | None:
        value = self.params.get("chat_id")
        return int(value) if value is not None else None

    def callback_data(self) -> list[str]:
        """Every ``callback_data`` of the inline keyboard sent with this call."""
        markup = self.params.get("reply_markup")
        if markup is None:
            return []
        if isinstance(markup, str):
            markup = json.loads(markup)
        return [
            button["callback_data"]
            for row in markup.get("inline_keyboard", [])
            for button in row
            if "callback_data" in button
        ]


class FakeRequest(BaseRequest):
    """Records Bot API calls and answers them like Telegram would."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.violations: list[str] = []
        self._fail_next: set[int] = set()
        self._message_ids = itertools.count(1000)

    @property
    def read_timeout(self) -> float | None:
        return None

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def fail_next_call_to(self, chat_id: int) -> None:
        """Answer the next call addressed to ``chat_id`` with 403 Forbidden."""
        self._fail_next.add(chat_id)

    def clear_failures(self) -> None:
        self._fail_next.clear()

    def calls_of(self, method: str) -> list[Call]:
        return [call for call in self.calls if call.method == method and not call.failed]

    def _check_limits(self, method: str, params: dict[str, Any]) -> None:
        text = params.get("text")
        is_text_call = method in {"sendMessage", "editMessageText"} and isinstance(text, str)
        if is_text_call and not 1 <= len(str(text)) <= MAX_TEXT:
            self.violations.append(f"{method}: text length {len(str(text))}")
        if method == "answerCallbackQuery" and isinstance(text, str) and len(text) > MAX_TOAST:
            self.violations.append(f"toast length {len(text)}")
        for data in Call(method, params).callback_data():
            if not 1 <= len(data.encode("utf-8")) <= MAX_CALLBACK_BYTES:
                self.violations.append(f"callback_data {data!r} out of 1..64 bytes")

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
        del method, read_timeout, write_timeout, connect_timeout, pool_timeout
        api_method = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data is not None else {}
        call = Call(api_method, params)
        self.calls.append(call)
        self._check_limits(api_method, params)
        chat_id = call.chat_id
        if chat_id is not None and chat_id in self._fail_next:
            self._fail_next.discard(chat_id)
            call.failed = True
            body = {
                "ok": False,
                "error_code": 403,
                "description": "Forbidden: bot was blocked by the user",
            }
            return 403, json.dumps(body).encode()
        result = self._result(api_method, params)
        if isinstance(result, dict) and "message_id" in result:
            call.message_id = int(result["message_id"])
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def _result(self, api_method: str, params: dict[str, Any]) -> Any:
        if api_method == "getMe":
            return {
                "id": BOT_ID,
                "is_bot": True,
                "first_name": "Bot",
                "username": "test_bot",
                "can_join_groups": True,
                "can_read_all_group_messages": False,
                "supports_inline_queries": False,
            }
        if api_method in {"sendMessage", "editMessageText"}:
            message_id = params.get("message_id")
            if message_id is None:
                message_id = next(self._message_ids)
            message: dict[str, Any] = {
                "message_id": int(message_id),
                "date": int(datetime.now(tz=UTC).timestamp()),
                "chat": {"id": int(params["chat_id"]), "type": "private"},
                "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot"},
                "text": params.get("text", ""),
            }
            markup = params.get("reply_markup")
            if markup is not None:
                message["reply_markup"] = json.loads(markup) if isinstance(markup, str) else markup
            return message
        return True


def make_application(
    request: FakeRequest, *, with_job_queue: bool = False, concurrent_updates: int = 1
) -> Application[Any, Any, Any, Any, Any, Any]:
    """A real ``Application`` whose bot talks to ``request``."""
    builder = ApplicationBuilder().token("123456:TEST-TOKEN").request(request).updater(None)
    if not with_job_queue:
        # PTB documents None as "no job queue"; its stub types only the JobQueue case.
        builder = builder.job_queue(None)  # type: ignore[arg-type]
    return builder.concurrent_updates(concurrent_updates).build()


_update_ids = itertools.count(1)
_query_ids = itertools.count(1)


def _user(user_id: int) -> dict[str, Any]:
    return {"id": user_id, "is_bot": False, "first_name": f"U{user_id}", "language_code": "en"}


def _chat(chat_id: int, chat_type: str) -> dict[str, Any]:
    chat: dict[str, Any] = {"id": chat_id, "type": chat_type}
    if chat_type != "private":
        chat["title"] = "group"
    return chat


def message_update(
    bot: Any,
    chat_id: int,
    user_id: int | None,
    text: str,
    *,
    kind: str = "message",
    chat_type: str | None = None,
) -> Update:
    """A text (or command) update of ``kind`` message/channel_post/edited_channel_post."""
    resolved_type = chat_type or ("channel" if kind != "message" else "private")
    message: dict[str, Any] = {
        "message_id": next(_update_ids) + 50_000,
        "date": int(datetime.now(tz=UTC).timestamp()),
        "chat": _chat(chat_id, resolved_type),
        "text": text,
    }
    if user_id is not None:
        message["from"] = _user(user_id)
    if kind == "edited_channel_post":
        message["edit_date"] = message["date"]
    if text.startswith("/"):
        command = text.split()[0]
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(command)}]
    update = Update.de_json({"update_id": next(_update_ids), kind: message}, bot)
    assert update is not None
    return update


def callback_update(
    bot: Any,
    chat_id: int,
    user_id: int,
    data: str,
    *,
    message_id: int = 1,
    chat_type: str = "private",
) -> Update:
    """A button press on bot message ``message_id`` in ``chat_id``."""
    payload = {
        "update_id": next(_update_ids),
        "callback_query": {
            "id": str(next(_query_ids)),
            "from": _user(user_id),
            "chat_instance": f"ci-{chat_id}",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": int(datetime.now(tz=UTC).timestamp()),
                "chat": _chat(chat_id, chat_type),
                "from": {"id": BOT_ID, "is_bot": True, "first_name": "Bot"},
                "text": "prompt",
            },
        },
    }
    update = Update.de_json(payload, bot)
    assert update is not None
    return update


@dataclass
class Armed:
    """One timeout the coordinator armed."""

    snapshot: FlowSnapshot
    delay: float
    callback: Callable[[FlowSnapshot], Awaitable[None]]
    disarmed: bool = False


@dataclass
class ManualTimer:
    """A ``FlowTimer`` whose timeouts fire only when a test says so.

    ``fire`` runs a timeout even if it was disarmed: a disarm can lose the race
    against a job that already started, so the snapshot check in the coordinator
    is the defence under test, not the disarm.
    """

    armed: list[Armed] = field(default_factory=list)

    def arm(
        self,
        snapshot: FlowSnapshot,
        delay: float,
        callback: Callable[[FlowSnapshot], Awaitable[None]],
    ) -> None:
        self.armed.append(Armed(snapshot, delay, callback))

    def disarm(self, snapshot: FlowSnapshot) -> None:
        for entry in self.armed:
            if entry.snapshot == snapshot:
                entry.disarmed = True

    def live(self) -> list[Armed]:
        return [entry for entry in self.armed if not entry.disarmed]

    async def fire(self, entry: Armed) -> None:
        await entry.callback(entry.snapshot)


@dataclass
class FakeServices:
    """The ``FlowServices`` boundary, with an explicit log of every write."""

    active: set[int] = field(default_factory=set)
    products: dict[int, tuple[int, str]] = field(default_factory=dict)
    scope_defaults: dict[int, AddScopeDefault] = field(default_factory=dict)
    prepare_by_url: dict[str, PrepareResult] = field(default_factory=dict)
    writes: list[tuple[Any, ...]] = field(default_factory=list)
    calls: list[tuple[Any, ...]] = field(default_factory=list)
    next_product_id: int = 1000

    async def is_active(self, user_id: int) -> bool:
        self.calls.append(("is_active", user_id))
        return user_id in self.active

    async def product_name(self, user_id: int, product_id: int) -> str | None:
        self.calls.append(("product_name", user_id, product_id))
        owner_name = self.products.get(product_id)
        if owner_name is None or owner_name[0] != user_id:
            return None
        return owner_name[1]

    async def apply_value(
        self, user_id: int, kind: FlowKind, product_id: int, value: object
    ) -> ApplyStatus:
        self.calls.append(("apply_value", user_id, kind, product_id, value))
        if user_id not in self.active:
            return ApplyStatus.NOT_AUTHORISED
        self.writes.append(("value", user_id, kind, product_id, value))
        return ApplyStatus.OK

    async def prepare_add(self, user_id: int, url: str) -> PrepareResult:
        self.calls.append(("prepare_add", user_id, url))
        if user_id not in self.active:
            return PrepareResult(PrepareStatus.NOT_AUTHORISED)
        return self.prepare_by_url.get(url, PrepareResult(PrepareStatus.FAILED))

    async def add_product(self, user_id: int, product: PreparedProduct, currency: str) -> AddResult:
        self.calls.append(("add_product", user_id, product.url, currency))
        if user_id not in self.active:
            return AddResult(ApplyStatus.NOT_AUTHORISED)
        product_id = self.next_product_id
        self.next_product_id += 1
        self.products[product_id] = (user_id, product.name)
        self.writes.append(("insert", user_id, product_id, product.url, currency))
        return AddResult(ApplyStatus.OK, product_id)

    async def add_scope_default(self, user_id: int) -> AddScopeDefault:
        self.calls.append(("add_scope_default", user_id))
        return self.scope_defaults.get(user_id, "ask")

    async def set_product_scope(
        self, user_id: int, product_id: int, *, cross_store: bool, scope_override: str | None
    ) -> ApplyStatus:
        self.calls.append(("set_product_scope", user_id, product_id, cross_store, scope_override))
        if user_id not in self.active:
            return ApplyStatus.NOT_AUTHORISED
        self.writes.append(("scope", user_id, product_id, cross_store, scope_override))
        return ApplyStatus.OK


def ready(url: str, *, currency: str | None, store: str = "shop.example") -> PrepareResult:
    """A scrape that succeeded for ``url``."""
    return PrepareResult(
        PrepareStatus.READY,
        PreparedProduct(
            url=url, name="Sample kettle", store=store, price=Decimal("19.90"), currency=currency
        ),
    )
