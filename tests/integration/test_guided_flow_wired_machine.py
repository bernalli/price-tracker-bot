"""Stateful property test of the coordinator wired into the real application.

Hypothesis drives arbitrary sequences of legacy entry buttons (own, foreign and
missing products), malformed entry data, answers from a labelled corpus,
``/cancel``, ``/list``, non-entry legacy buttons, presses of current and old
cancel buttons, timeouts of current and superseded snapshots, and channel posts,
over two chats and two users, through ``Application.process_update`` with the
production handler layout and a migrated in-memory repository.

The reference model never calls the code under test: it keeps its own open
flow per ``(chat_id, user_id)`` and its own copy of the three product settings.
Tokens are learned from the cancel button of the prompt the bot sent.

Invariants checked after every step:

* I1 - the registry holds exactly the model's open keys, each with the token of
  the prompt last sent for it, and no user has a product ``pending_action``;
* I2 - threshold, target and interval of every product equal the model, which
  changes them only on a valid answer to the current prompt of the key;
* I3 - the add flow is never prepared;
* I4 - at most one ``answerCallbackQuery`` per update;
* I5 - malformed entry data is a foreign callback: it opens nothing, changes no
  product, and only closes the prompt of the key that pressed it.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import aiosqlite
from hypothesis import event, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

import price_tracker.bot.handlers.product as product_handlers
from price_tracker.bot.flow_services import RepositoryFlowServices
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from tests.integration.test_guided_flow_wired import (
    ADMIN,
    GROUP,
    MIGRATIONS_DIR,
    OTHER,
    OWNER,
    PRIVATE,
    Wired,
)

if TYPE_CHECKING:
    from price_tracker.bot.flows import FlowSnapshot
    from tests.support.fake_telegram import Call

USERS = (OWNER, OTHER)
CHATS = (PRIVATE, GROUP)
KEYS = [(c, u) for c in CHATS for u in USERS]
MISSING_PRODUCT = 999
PREFIX_KIND = {
    "setsoglia": "threshold",
    "track_threshold": "threshold",
    "settarget": "target",
    "track_target": "target",
    "setrefresh": "interval",
}
MALFORMED = (
    "setsoglia_",
    "setsoglia_0",
    "setsoglia_01",
    "setsoglia_-1",
    "setsoglia_ 1",
    "setsoglia_1\n",
    "setsoglia_#1",
    "SETSOGLIA_1",
    "setsoglia1",
    "setsoglia_1_2",
    "settarget_9223372036854775808",
    "track_any_x",
)
NON_ENTRY_BUTTONS = ("menu_main", "cmd_lista")
CANCEL_BUTTON_RE = re.compile(r"^p:([0-9a-f]{32}):x$")
MAX_ATTEMPTS = 3
URL = "https://shop.example/item/9"
LINK = "link"
CANCEL = "cancel"
# text -> meaning per kind, written from the input grammar:
# None = rejected, CANCEL = cancel sentinel, LINK = closes the prompt and passes on,
# otherwise the stored columns (threshold (type, value), target value, interval).
ANSWERS: dict[str, dict[str, Any]] = {
    "20%": {"threshold": ("percentage", "20"), "target": None, "interval": None},
    "12,50": {"threshold": ("absolute", "12.50"), "target": ("12.50",), "interval": None},
    "30": {"threshold": ("absolute", "30"), "target": ("30",), "interval": (30,)},
    "4": {"threshold": ("absolute", "4"), "target": ("4",), "interval": None},
    "0": {"threshold": None, "target": (None,), "interval": (None,)},
    "any": {"threshold": ("any_drop", "0"), "target": None, "interval": None},
    "-": {"threshold": CANCEL, "target": CANCEL, "interval": None},
    "annulla": {"threshold": CANCEL, "target": CANCEL, "interval": None},
    "   ": {"threshold": None, "target": None, "interval": None},
    "5\x00": {"threshold": None, "target": None, "interval": None},
    "2​0": {"threshold": None, "target": None, "interval": None},
    "1e3": {"threshold": None, "target": None, "interval": None},
    "abc": {"threshold": None, "target": None, "interval": None},
    URL: {"threshold": LINK, "target": LINK, "interval": LINK},
}
ACCEPTED = {
    kind: [t for t, m in ANSWERS.items() if m[kind] is not None and m[kind] not in (LINK, CANCEL)]
    for kind in ("threshold", "target", "interval")
}
REJECTED = {
    kind: [t for t, m in ANSWERS.items() if m[kind] is None or m[kind] == LINK]
    for kind in ("threshold", "target", "interval")
}


@dataclass(frozen=True)
class MFlow:
    """The model's open prompt for a key."""

    kind: str
    product: int
    token: str
    attempts: int = 0


@dataclass
class MProduct:
    """The model's copy of the three settings a prompt can change."""

    threshold: tuple[str, str]
    target: str | None
    interval: int | None


class WiredFlowMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.w: Wired = self._run(self._start())
        self.owned = {self.w.product: OWNER, self.w.other_product: OTHER}
        self.products = sorted(self.owned)
        self.flows: dict[tuple[int, int], MFlow] = {}
        self.model: dict[int, MProduct] = {pid: self._run(self._read(pid)) for pid in self.products}
        self.snapshots: list[FlowSnapshot] = []
        self.tokens: list[str] = []
        self.step_start = 0

    async def _start(self) -> Wired:
        self.conn = await aiosqlite.connect(":memory:")
        self.conn.row_factory = aiosqlite.Row
        await apply_migrations(self.conn, MIGRATIONS_DIR)
        repo = Repository(self.conn)
        await repo.ensure_user(ADMIN, is_admin=True)
        for user in USERS:
            await repo.ensure_user(user)
        wired = Wired(self.conn, repo)
        wired.product = await repo.add_product(
            user_id=OWNER,
            url="https://shop.example/item/1",
            name="Kettle",
            domain="shop.example",
            initial_price=Decimal("100"),
            currency="EUR",
        )
        wired.other_product = await repo.add_product(
            user_id=OTHER,
            url="https://shop.example/item/2",
            name="Fan",
            domain="shop.example",
            initial_price=Decimal("50"),
            currency="EUR",
        )
        self.prepared: list[tuple[int, str]] = []
        self.legacy_adds: list[str] = []
        self._original_prepare = RepositoryFlowServices.prepare_add
        self._original_add = product_handlers._add_product
        machine = self

        async def prepare_spy(self: RepositoryFlowServices, user_id: int, url: str) -> Any:
            machine.prepared.append((user_id, url))
            return await machine._original_prepare(self, user_id, url)

        async def fake_add_product(update: Any, context: Any, url: str) -> None:
            del context
            machine.legacy_adds.append(url)
            await update.message.reply_text("legacy add")

        RepositoryFlowServices.prepare_add = prepare_spy  # type: ignore[method-assign]
        product_handlers._add_product = fake_add_product
        await wired.app.initialize()
        return wired

    def teardown(self) -> None:
        RepositoryFlowServices.prepare_add = self._original_prepare  # type: ignore[method-assign]
        product_handlers._add_product = self._original_add
        self._run(self.w.app.shutdown())
        self._run(self.conn.close())
        self.loop.close()

    # -- helpers -----------------------------------------------------------

    def _run(self, coro: Any) -> Any:
        return self.loop.run_until_complete(coro)

    async def _read(self, product_id: int) -> MProduct:
        row = await self.w.row(product_id)
        interval = row["check_interval_minutes"]
        target = row["target_price"]
        return MProduct(
            threshold=(str(row["threshold_type"]), str(row["threshold_value"])),
            target=None if target is None else str(target),
            interval=None if interval is None else int(interval),
        )

    def _begin(self) -> None:
        self.step_start = len(self.w.request.calls)

    def _step_calls(self) -> list[Call]:
        return self.w.request.calls[self.step_start :]

    def _learn_prompt(self, key: tuple[int, int]) -> str:
        token = None
        for call in self._step_calls():
            if call.method != "sendMessage" or call.chat_id != key[0]:
                continue
            for data in call.callback_data():
                match = CANCEL_BUTTON_RE.match(data)
                if match:
                    token = match.group(1)
        assert token is not None, f"model expected a prompt for {key}, none was sent"
        self.tokens.append(token)
        return token

    def _record_snapshot(self, key: tuple[int, int]) -> None:
        live = self.w.flow.registry.get(key)
        if live is not None:
            self.snapshots.append(live.snapshot(key))

    def _registry_tokens(self) -> dict[tuple[int, int], str]:
        registry = self.w.flow.registry
        tokens = {}
        open_keys = registry.keys()
        for key in open_keys:
            live = registry.get(key)
            assert live is not None
            tokens[key] = live.token
        return tokens

    def _visible(self, user: int, product: int) -> bool:
        return self.owned.get(product) == user

    # -- rules: entries ------------------------------------------------------

    @rule(
        key=st.sampled_from(KEYS),
        prefix=st.sampled_from(sorted(PREFIX_KIND)),
        which=st.sampled_from(("own", "foreign", "missing")),
    )
    def press_entry(self, key: tuple[int, int], prefix: str, which: str) -> None:
        user = key[1]
        if which == "missing":
            product = MISSING_PRODUCT
        else:
            product = next(p for p in self.products if (self.owned[p] == user) == (which == "own"))
        self._begin()
        self._run(self.w.press(key[0], user, f"{prefix}_{product}"))
        if not self._visible(user, product):
            event("entry refused")
            return
        event(f"entry opened {PREFIX_KIND[prefix]}")
        self.flows[key] = MFlow(PREFIX_KIND[prefix], product, self._learn_prompt(key))
        self._record_snapshot(key)

    @rule(key=st.sampled_from(KEYS), data=st.sampled_from(MALFORMED))
    def press_malformed_entry(self, key: tuple[int, int], data: str) -> None:
        self._begin()
        registry_before = self._registry_tokens()
        self._run(self.w.press(key[0], key[1], data))
        self.flows.pop(key, None)
        registry_after = self._registry_tokens()
        expected = {k: t for k, t in registry_before.items() if k != key}
        assert registry_after == expected
        assert not any(
            CANCEL_BUTTON_RE.match(d)
            for c in self._step_calls()
            if c.method == "sendMessage"
            for d in c.callback_data()
        )

    @rule(key=st.sampled_from(KEYS), data=st.sampled_from(NON_ENTRY_BUTTONS))
    def press_non_entry_legacy_button(self, key: tuple[int, int], data: str) -> None:
        self._begin()
        self._run(self.w.press(key[0], key[1], data))
        self.flows.pop(key, None)

    # -- rules: answers --------------------------------------------------------

    @rule(key=st.sampled_from(KEYS), text=st.sampled_from(sorted(ANSWERS)))
    def send_text(self, key: tuple[int, int], text: str) -> None:
        self._begin()
        self._run(self.w.text(key[0], key[1], text))
        self._model_text(key, text)

    @precondition(lambda self: bool(self.flows))
    @rule(data=st.data())
    def answer_an_open_prompt(self, data: st.DataObject) -> None:
        """Same event as ``send_text``, with a text the open prompt accepts."""
        key = data.draw(st.sampled_from(sorted(self.flows)))
        kind = self.flows[key].kind
        self.send_text(key=key, text=data.draw(st.sampled_from(ACCEPTED[kind])))

    @precondition(lambda self: bool(self.flows))
    @rule(data=st.data())
    def reject_an_open_prompt(self, data: st.DataObject) -> None:
        """Same event as ``send_text``, with a text the open prompt does not accept."""
        key = data.draw(st.sampled_from(sorted(self.flows)))
        kind = self.flows[key].kind
        self.send_text(key=key, text=data.draw(st.sampled_from(REJECTED[kind])))

    def _model_text(self, key: tuple[int, int], text: str) -> None:
        flow = self.flows.get(key)
        if flow is None:
            return
        meaning = ANSWERS[text][flow.kind]
        if meaning == LINK:
            event("link closed the prompt")
            del self.flows[key]
            return
        if meaning is None:
            attempts = flow.attempts + 1
            if attempts >= MAX_ATTEMPTS:
                event("third invalid answer")
                del self.flows[key]
            else:
                self.flows[key] = replace(flow, attempts=attempts)
            return
        del self.flows[key]
        if meaning == CANCEL:
            event("cancel sentinel")
            return
        event(f"applied {flow.kind} {text!r}")
        product = self.model[flow.product]
        if flow.kind == "threshold":
            product.threshold = meaning
        elif flow.kind == "target":
            product.target = meaning[0]
        else:
            product.interval = meaning[0]

    # -- rules: commands, buttons, time, channels -------------------------------

    @rule(key=st.sampled_from(KEYS), command=st.sampled_from(("/cancel", "/list")))
    def command(self, key: tuple[int, int], command: str) -> None:
        self._begin()
        self._run(self.w.text(key[0], key[1], command))
        self.flows.pop(key, None)

    @precondition(lambda self: bool(self.tokens))
    @rule(key=st.sampled_from(KEYS), data=st.data())
    def press_cancel_button(self, key: tuple[int, int], data: st.DataObject) -> None:
        self._press_cancel(key, data.draw(st.sampled_from(self.tokens)))

    @precondition(lambda self: bool(self.flows))
    @rule(data=st.data())
    def press_current_cancel_button(self, data: st.DataObject) -> None:
        """Same event as ``press_cancel_button``, on the prompt a key has open."""
        key = data.draw(st.sampled_from(sorted(self.flows)))
        self._press_cancel(key, self.flows[key].token)

    def _press_cancel(self, key: tuple[int, int], token: str) -> None:
        self._begin()
        self._run(self.w.press(key[0], key[1], f"p:{token}:x"))
        flow = self.flows.get(key)
        if flow is not None and flow.token == token:
            event("current cancel button")
            del self.flows[key]
        else:
            event("old cancel button")

    @precondition(lambda self: bool(self.snapshots))
    @rule(data=st.data())
    def fire_timeout(self, data: st.DataObject) -> None:
        snapshot = data.draw(st.sampled_from(self.snapshots))
        self._begin()
        self._run(self.w.flow.on_timeout(snapshot))
        flow = self.flows.get(snapshot.key)
        if flow is not None and flow.token == snapshot.token:
            event("timeout ended the prompt")
            del self.flows[snapshot.key]
        else:
            event("stale timeout")

    @rule(
        key=st.sampled_from(KEYS),
        kind=st.sampled_from(("channel_post", "edited_channel_post")),
        text=st.sampled_from(sorted(ANSWERS)),
    )
    def channel_post(self, key: tuple[int, int], kind: str, text: str) -> None:
        self._begin()
        self._run(self.w.text(key[0], key[1], text, kind=kind))
        assert self._step_calls() == []

    # -- invariants -------------------------------------------------------------

    @invariant()
    def one_flow_per_key_and_no_product_pending_action(self) -> None:
        registry = self.w.flow.registry
        assert registry.keys() == set(self.flows)
        for key, flow in self.flows.items():
            live = registry.get(key)
            assert live is not None
            assert live.token == flow.token
        for data in self.w.app.user_data.values():
            pending = data.get("pending_action")
            assert not (
                isinstance(pending, tuple)
                and pending
                and pending[0] in ("threshold", "target", "refresh")
            ), pending

    @invariant()
    def products_match_model(self) -> None:
        for pid in self.products:
            assert self._run(self._read(pid)) == self.model[pid], pid

    @invariant()
    def add_flow_never_prepared(self) -> None:
        assert self.prepared == []
        assert self.w.errors == []

    @invariant()
    def at_most_one_answer_per_update(self) -> None:
        answers = [c for c in self._step_calls() if c.method == "answerCallbackQuery"]
        assert len(answers) <= 1

    @invariant()
    def within_telegram_limits(self) -> None:
        assert self.w.request.violations == []


WiredFlowMachine.TestCase.settings = settings(
    max_examples=int(os.environ.get("GUIDED_FLOW_WIRED_EXAMPLES", "100")),
    stateful_step_count=50,
    deadline=None,
)
TestWiredFlowMachine = WiredFlowMachine.TestCase
