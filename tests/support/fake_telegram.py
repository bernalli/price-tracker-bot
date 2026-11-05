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

import asyncio
import hashlib
import itertools
import json
import warnings
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from typing import TYPE_CHECKING, Any

from PIL import Image, UnidentifiedImageError
from telegram import Update
from telegram.error import TimedOut
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
from price_tracker.db.models import UserRecord

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

    started: int = 0
    finished: int | None = None

    def link_preview(self) -> dict[str, Any] | None:
        """The explicit link preview options, if any."""
        return _json_dict(self.params.get("link_preview_options"))

    def has_keyboard(self) -> bool:
        """Whether an inline keyboard has at least one row."""
        markup = _json_dict(self.params.get("reply_markup"))
        return bool(markup and markup.get("inline_keyboard"))

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


@dataclass
class RemoteMessage:
    """Visible remote state, independent of the request history."""

    kind: str
    text: str | None = None
    caption: str | None = None
    reply_markup: dict[str, Any] | None = None
    link_preview: dict[str, Any] | None = None
    photo_size: tuple[int, int] | None = None
    photo_sha256: str | None = None


@dataclass(frozen=True)
class Reject:
    status: int
    description: str
    retry_after: int | None = None


@dataclass(frozen=True)
class LoseResponse:
    """Apply the operation, then lose its response."""


@dataclass(frozen=True)
class TimeoutBeforeApply:
    """Lose the request before it can change remote state."""


def _json_dict(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else None


def overlapping(calls: list[Call]) -> bool:
    """Whether any two recorded request intervals intersect."""
    ordered = sorted(calls, key=lambda call: call.started)
    return any(
        left.finished is None or left.finished > right.started
        for left, right in zip(ordered, ordered[1:], strict=False)
    )


class FakeRequest(BaseRequest):
    """Records Bot API calls and answers them like Telegram would."""

    def __init__(self) -> None:
        self.calls: list[Call] = []
        self.violations: list[str] = []
        self._fail_next: set[int] = set()
        self._message_ids = itertools.count(1000)
        self.remote: dict[tuple[int, int], RemoteMessage] = {}
        self._deleted: set[tuple[int, int]] = set()
        self.simulate_not_modified = False
        self._scripts: dict[str, deque[Reject | LoseResponse | TimeoutBeforeApply]] = defaultdict(
            deque
        )
        self._holds: dict[str, deque[asyncio.Event]] = defaultdict(deque)
        self._sequence = itertools.count(1)
        self._checked_photo_size: tuple[int, int] = (1, 1)

    def remote_of(self, chat_id: int, message_id: int) -> RemoteMessage | None:
        return self.remote.get((chat_id, message_id))

    def delete_remote(self, chat_id: int, message_id: int) -> None:
        key = (chat_id, message_id)
        self.remote.pop(key, None)
        self._deleted.add(key)

    def script(self, method: str, behaviour: Reject | LoseResponse | TimeoutBeforeApply) -> None:
        self._scripts[method].append(behaviour)

    def attempts(self, method: str) -> list[Call]:
        return [call for call in self.calls if call.method == method]

    def hold_next(self, method: str) -> asyncio.Event:
        event = asyncio.Event()
        self._holds[method].append(event)
        return event

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

    def _check_limits(
        self, method: str, params: dict[str, Any], request_data: RequestData | None = None
    ) -> None:
        text = params.get("text")
        is_text_call = method in {"sendMessage", "editMessageText"} and isinstance(text, str)
        if is_text_call and not 1 <= len(str(text)) <= MAX_TEXT:
            self.violations.append(f"{method}: text length {len(str(text))}")
        if method == "answerCallbackQuery" and isinstance(text, str) and len(text) > MAX_TOAST:
            self.violations.append(f"toast length {len(text)}")
        for callback in Call(method, params).callback_data():
            if not 1 <= len(callback.encode("utf-8")) <= MAX_CALLBACK_BYTES:
                self.violations.append(f"callback_data {callback!r} out of 1..64 bytes")

        media = _json_dict(params.get("media"))
        self._checked_photo_size = (1, 1)
        if method == "sendPhoto" or (
            method == "editMessageMedia" and media is not None and media.get("type") == "photo"
        ):
            data = self._photo_bytes(params, request_data)
            if len(data) > 10_000_000:
                self.violations.append(f"{method}: photo size {len(data)}")
            previous_pixel_cap = Image.MAX_IMAGE_PIXELS
            try:
                Image.MAX_IMAGE_PIXELS = None
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", Image.DecompressionBombWarning)
                    with Image.open(BytesIO(data), formats=("JPEG", "PNG")) as image:
                        image.load()
                        width, height = image.size
                self._checked_photo_size = (width, height)
                if width + height > 10000:
                    self.violations.append(f"{method}: photo dimensions {width}x{height}")
                if max(width, height) / min(width, height) > 20:
                    self.violations.append(f"{method}: photo ratio {width}x{height}")
            except (OSError, ValueError, UnidentifiedImageError):
                self.violations.append(f"{method}: photo decode failed")
            finally:
                Image.MAX_IMAGE_PIXELS = previous_pixel_cap

    @staticmethod
    def _photo_bytes(params: dict[str, Any], request_data: RequestData | None) -> bytes:
        if request_data is None:
            return b""
        media = _json_dict(params.get("media"))
        reference = media.get("media") if media is not None else params.get("photo")
        uploads = request_data.multipart_data
        part_name = "photo"
        if isinstance(reference, str) and reference.startswith("attach://"):
            part_name = reference.removeprefix("attach://")
        part = uploads.get(part_name)
        if part is None:
            return b""
        content = part[1]
        if isinstance(content, bytes):
            return content
        position = content.tell()
        try:
            return bytes(content.read())
        finally:
            content.seek(position)

    @staticmethod
    def _error(
        call: Call, status: int, description: str, retry_after: int | None = None
    ) -> tuple[int, bytes]:
        call.failed = True
        body: dict[str, Any] = {"ok": False, "error_code": status, "description": description}
        if retry_after is not None:
            body["parameters"] = {"retry_after": retry_after}
        return status, json.dumps(body).encode()

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
        params: dict[str, Any] = dict(request_data.parameters) if request_data is not None else {}
        call = Call(api_method, params, started=next(self._sequence))
        self.calls.append(call)
        behaviour = self._scripts[api_method].popleft() if self._scripts[api_method] else None
        hold = self._holds[api_method].popleft() if self._holds[api_method] else None
        try:
            if hold is not None:
                await hold.wait()
            self._check_limits(api_method, params, request_data)
            chat_id = call.chat_id
            if chat_id is not None and chat_id in self._fail_next:
                self._fail_next.discard(chat_id)
                return self._error(call, 403, "Forbidden: bot was blocked by the user")
            if isinstance(behaviour, Reject):
                return self._error(
                    call, behaviour.status, behaviour.description, behaviour.retry_after
                )
            if isinstance(behaviour, TimeoutBeforeApply):
                call.failed = True
                raise TimedOut("Timed out")
            key = (int(params.get("chat_id", 0)), int(params.get("message_id", 0)))
            previous = self.remote.get(key)
            if api_method in {"editMessageText", "editMessageMedia"} and key in self._deleted:
                return self._error(call, 400, "Bad Request: message to edit not found")
            if api_method == "editMessageText" and previous is not None:
                if previous.kind == "photo":
                    return self._error(
                        call, 400, "Bad Request: there is no text in the message to edit"
                    )
                if (
                    self.simulate_not_modified
                    and previous.text == params.get("text")
                    and previous.reply_markup == _json_dict(params.get("reply_markup"))
                    and previous.link_preview == call.link_preview()
                ):
                    return self._error(
                        call,
                        400,
                        "Bad Request: message is not modified: specified new message content and "
                        "reply markup are exactly the same as a current content and reply markup "
                        "of the message",
                    )
            result = (
                self._message_result(api_method, params, request_data)
                if api_method in {"sendPhoto", "editMessageMedia"}
                else self._result(api_method, params)
            )
            if isinstance(result, dict) and "message_id" in result:
                call.message_id = int(result["message_id"])
            if isinstance(behaviour, LoseResponse):
                call.failed = True
                raise TimedOut("Timed out")
            return 200, json.dumps({"ok": True, "result": result}).encode()
        finally:
            call.finished = next(self._sequence)

    def _result(self, api_method: str, params: dict[str, Any]) -> Any:
        return self._message_result(api_method, params)

    def _message_result(
        self, api_method: str, params: dict[str, Any], request_data: RequestData | None = None
    ) -> Any:
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
        if api_method in {"sendMessage", "editMessageText", "sendPhoto", "editMessageMedia"}:
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
            if api_method in {"sendPhoto", "editMessageMedia"}:
                data = self._photo_bytes(params, request_data)
                size = self._checked_photo_size
                media = _json_dict(params.get("media")) or params
                caption = media.get("caption")
                message.pop("text", None)
                message["photo"] = [
                    {
                        "file_id": f"f{message_id}",
                        "file_unique_id": f"u{message_id}",
                        "width": size[0],
                        "height": size[1],
                    }
                ]
                if caption is not None:
                    message["caption"] = caption
                remote = RemoteMessage(
                    "photo",
                    caption=caption,
                    reply_markup=_json_dict(markup),
                    photo_size=size,
                    photo_sha256=hashlib.sha256(data).hexdigest(),
                )
            else:
                remote = RemoteMessage(
                    "text",
                    text=params.get("text", ""),
                    reply_markup=_json_dict(markup),
                    link_preview=_json_dict(params.get("link_preview_options")),
                )
            self.remote[(int(params["chat_id"]), int(message_id))] = remote
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


def _user(user_id: int, language_code: str | None = "en") -> dict[str, Any]:
    user: dict[str, Any] = {"id": user_id, "is_bot": False, "first_name": f"U{user_id}"}
    if language_code is not None:
        user["language_code"] = language_code
    return user


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
    language_code: str | None = "en",
) -> Update:
    """A text (or command) update of ``kind`` message/channel_post/edited_channel_post.

    ``language_code`` is the sender's Telegram language; ``None`` omits the field.
    """
    resolved_type = chat_type or ("channel" if kind != "message" else "private")
    message: dict[str, Any] = {
        "message_id": next(_update_ids) + 50_000,
        "date": int(datetime.now(tz=UTC).timestamp()),
        "chat": _chat(chat_id, resolved_type),
        "text": text,
    }
    if user_id is not None:
        message["from"] = _user(user_id, language_code)
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
    language_code: str | None = "en",
) -> Update:
    """A button press on bot message ``message_id`` in ``chat_id``.

    ``language_code`` is the presser's Telegram language; ``None`` omits the field.
    """
    payload = {
        "update_id": next(_update_ids),
        "callback_query": {
            "id": str(next(_query_ids)),
            "from": _user(user_id, language_code),
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
    languages: dict[int, str] = field(default_factory=dict)
    language_reads: list[int] = field(default_factory=list)
    admins: set[int] = field(default_factory=set)

    async def get_user(self, user_id: int) -> UserRecord | None:
        """The stored row the reply language is read from; a row only when a language is set.

        The harness stores this object as ``bot_data["db"]``, so the coordinator and the
        ``/cancel`` fallback read the language through the production ``user_locale``.
        """
        self.language_reads.append(user_id)
        language = self.languages.get(user_id)
        if language is None:
            return None
        return UserRecord(user_id=user_id, is_admin=False, is_active=True, language=language)

    async def is_active(self, user_id: int) -> bool:
        self.calls.append(("is_active", user_id))
        return user_id in self.active

    async def is_admin(self, user_id: int) -> bool:
        self.calls.append(("is_admin", user_id))
        return user_id in self.admins and user_id in self.active

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

    async def apply_setting(
        self, user_id: int, kind: FlowKind, product_id: int | None, value: object
    ) -> ApplyStatus:
        self.calls.append(("apply_setting", user_id, kind, product_id, value))
        if user_id not in self.active:
            return ApplyStatus.NOT_AUTHORISED
        self.writes.append(("setting", user_id, kind, product_id, value))
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
