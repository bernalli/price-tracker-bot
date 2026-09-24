"""Stateful property test of the guided-flow coordinator (G1-G4).

Hypothesis generates arbitrary sequences of prompts, answers, current/old/
duplicate/foreign callbacks, unknown tokens, commands, timeouts (including
disarmed ones firing late), restarts, revocations, scope-default changes,
channel posts and transport failures, and drives them through a real
``Application.process_update``.

The reference model below is written from the SP1 events x states table as
corrected for S1-1. It never calls the code under test: it keeps its own flow
per ``(chat_id, user_id)``, its own expected write log and its own labelled
input corpus. Tokens are learned from what the bot *sent* (the keyboard of the
prompt the user saw), parsed with the model's own regex.

Invariants checked after every step:

* G1 - the registry holds exactly the model's open keys, each with the token of
  the prompt the user last saw for that key (at most one flow per key);
* G2 - a text is consumed by at most one consumer: the free-text fallback
  answers iff the model says no flow consumed it, and no callback query is
  answered twice;
* writes - the fake services' write log equals the model's, entry for entry, so
  no answer is applied by a flow other than the one that asked for it and
  nothing is written after a flow ended;
* E13 - after a Forbidden to a chat, no further call to that chat in that step;
* limits - no text, toast or callback data outside the Telegram limits.
"""

from __future__ import annotations

import asyncio
import os
import re
import string
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from hypothesis import event, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from price_tracker.app.inputs import (
    Absolute,
    Cancel,
    ClearTarget,
    IntervalMinutes,
    Percentage,
    ResetInterval,
    SetTarget,
)
from price_tracker.bot.flows import FlowKind
from tests.support.fake_telegram import Call, FakeServices, message_update, ready
from tests.support.flow_harness import Harness, ServiceBarrier

PRIVATE, GROUP = 100, -500
USERS = (10, 11)
CHATS = (PRIVATE, GROUP)
KEYS = [(c, u) for c in CHATS for u in USERS]
OWNED = {1: 10, 2: 10, 3: 11}
URL_EUR = "https://shop.example/item/eur"
URL_NOCUR = "https://shop.example/item/nocur"
URL_BROKEN = "https://shop.example/item/broken"
NO_OPEN_PROMPT = "No open prompt - use the buttons or /menu."
KIND_BY_VERB = {"th": "threshold", "tg": "target", "iv": "interval"}
SUT_KIND = {
    "threshold": FlowKind.THRESHOLD,
    "target": FlowKind.TARGET,
    "interval": FlowKind.INTERVAL,
}
MAX_ATTEMPTS = 3
TOKEN_RE = re.compile(r"^p:([0-9a-f]{32}):(cur|sc|x)(?::([A-Za-z_]+))?$")

# text -> meaning per value kind (None = rejected), written from the grammar.
VALUE_TEXTS: dict[str, dict[str, Any]] = {
    "20%": {"threshold": Percentage(20), "target": None, "interval": None},
    "12.50": {
        "threshold": Absolute(Decimal("12.50")),
        "target": SetTarget(Decimal("12.50")),
        "interval": None,
    },
    "30": {
        "threshold": Absolute(Decimal("30")),
        "target": SetTarget(Decimal("30")),
        "interval": IntervalMinutes(30),
    },
    "0": {"threshold": None, "target": ClearTarget(), "interval": ResetInterval()},
    "-": {"threshold": Cancel(), "target": Cancel(), "interval": None},
    "NaN": {"threshold": None, "target": None, "interval": None},
    "1e3": {"threshold": None, "target": None, "interval": None},
    "hello": {"threshold": None, "target": None, "interval": None},
}
CURRENCY_TEXTS: dict[str, str | None] = {"usd": "USD", "gbp": "GBP", "us$": None, "euro": None}
ALL_TEXTS = sorted({*VALUE_TEXTS, *CURRENCY_TEXTS})
# Mostly the buttons of the state, plus one that does not fit it.
BUTTON_CHOICES: dict[str, tuple[str, ...]] = {
    "value": ("x", "x", "sc:store"),
    "currency": ("cur:USD", "cur:EUR", "cur:GBP", "cur:type", "cur:cancel", "x", "sc:world"),
    "scope": ("sc", "sc:store", "sc:world", "sc:own_country", "sc:customs_area", "x", "cur:USD"),
}


@dataclass(frozen=True)
class MFlow:
    """The model's flow: what the table says is open for a key."""

    kind: str
    state: str  # "value" | "currency" | "scope"
    token: str
    product: int | None = None
    url: str | None = None
    attempts: int = 0
    typing: bool = False


class GuidedFlowMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.services = FakeServices(
            active=set(USERS),
            products={pid: (owner, f"Item {pid}") for pid, owner in OWNED.items()},
            prepare_by_url={
                URL_EUR: ready(URL_EUR, currency="EUR"),
                URL_NOCUR: ready(URL_NOCUR, currency=None),
            },
        )
        self.h = Harness(self.services, concurrent_updates=2)
        self.loop.run_until_complete(self.h.start())
        # the model
        self.flows: dict[tuple[int, int], MFlow] = {}
        self.writes: list[tuple[Any, ...]] = []
        self.active: set[int] = set(USERS)
        self.defaults: dict[int, str] = {}
        self.next_id = 1000
        self.seen_buttons: list[str] = []
        self.step_start = 0
        self.expect_fallback: bool | None = None
        self.steps = 0
        self.generations: dict[tuple[int, int], int] = {}
        self.pending: (
            tuple[tuple[int, int], str, int, int | None, ServiceBarrier, asyncio.Task[None]] | None
        ) = None

    def teardown(self) -> None:
        if self.pending is not None:
            self.resume_service()
        self.loop.run_until_complete(self.h.stop())
        self.loop.close()
        stats_path = os.environ.get("GUIDED_FLOW_MACHINE_STATS")
        if stats_path:
            with open(stats_path, "a", encoding="utf-8") as stats:
                stats.write(f"{self.steps}\n")

    # -- helpers -----------------------------------------------------------

    def _begin(self) -> None:
        self.steps += 1
        self.step_start = len(self.h.request.calls)
        self.expect_fallback = None

    def _step_calls(self) -> list[Call]:
        return self.h.request.calls[self.step_start :]

    def _run(self, coro: Any) -> None:
        self.loop.run_until_complete(coro)

    def _learn_prompt(self, key: tuple[int, int]) -> str | None:
        """Token of the prompt sent to ``key``'s chat in this step, if any."""
        token = None
        for call in self._step_calls():
            if call.method != "sendMessage" or call.failed or call.chat_id != key[0]:
                continue
            data = call.callback_data()
            self.seen_buttons.extend(data)
            for item in data:
                match = TOKEN_RE.match(item)
                if match:
                    token = match.group(1)
        for call in self._step_calls():
            if call.method == "editMessageText" and not call.failed:
                self.seen_buttons.extend(call.callback_data())
        return token

    def _advance(self, key: tuple[int, int]) -> int:
        self.generations[key] = self.generations.get(key, 0) + 1
        return self.generations[key]

    def _end_flow(self, key: tuple[int, int]) -> None:
        if key in self.flows:
            self._advance(key)
            del self.flows[key]

    def _open(self, key: tuple[int, int], flow: MFlow | None) -> None:
        """Install a flow whose prompt must have been sent in this step."""
        if flow is None:
            self._end_flow(key)
            return
        self._advance(key)
        token = self._learn_prompt(key)
        assert token is not None, f"model expected a prompt for {key}, none was sent"
        self.flows[key] = replace(flow, token=token)
        event(f"opened {flow.kind}/{flow.state}")

    def _insert_and_branch(self, key: tuple[int, int], url: str, currency: str) -> MFlow | None:
        """Model of the insert plus the scope branch; returns the scope flow, if any."""
        user = key[1]
        if user not in self.active:
            return None
        product = self.next_id
        self.next_id += 1
        self.writes.append(("insert", user, product, url, currency))
        default = self.defaults.get(user, "ask")
        if default == "store_only":
            return None
        if default == "other_stores":
            self.writes.append(("scope", user, product, True, None))
            return None
        return MFlow("add", "scope", "", product=product)

    # -- rules: entries ------------------------------------------------------

    @rule(
        key=st.sampled_from(KEYS),
        product=st.sampled_from(sorted(OWNED)),
        verb=st.sampled_from(sorted(KIND_BY_VERB)),
    )
    def open_value(self, key: tuple[int, int], product: int, verb: str) -> None:
        self._begin()
        self._run(self.h.press(key[0], key[1], f"p:{product}:{verb}"))
        user = key[1]
        if user not in self.active or OWNED[product] != user:
            return
        self._open(key, MFlow(KIND_BY_VERB[verb], "value", "", product=product))

    @rule(
        key=st.sampled_from(KEYS),
        url=st.sampled_from((URL_EUR, URL_NOCUR, URL_BROKEN)),
        as_command=st.booleans(),
    )
    def add(self, key: tuple[int, int], url: str, as_command: bool) -> None:
        self._begin()
        text = f"/add {url}" if as_command else f"look {url}"
        self._run(self.h.text(key[0], key[1], text))
        self._model_add(key, url)

    def _model_add(self, key: tuple[int, int], url: str) -> None:
        self._advance(key)
        user = key[1]
        if user not in self.active:
            return
        self._end_flow(key)
        if url == URL_BROKEN:
            return
        if url == URL_EUR:
            self._open(key, self._insert_and_branch(key, url, "EUR"))
            return
        self._open(key, MFlow("add", "currency", "", url=url))

    # -- rules: answers -------------------------------------------------------

    @rule(key=st.sampled_from(KEYS), text=st.sampled_from(ALL_TEXTS))
    def send_text(self, key: tuple[int, int], text: str) -> None:
        self._begin()
        self._run(self.h.text(key[0], key[1], text))
        self._model_text(key, text)

    @precondition(lambda self: bool(self.flows))
    @rule(data=st.data())
    def answer_an_open_flow(self, data: st.DataObject) -> None:
        """Same event as ``send_text``, aimed at a key that has an open flow.

        Half of the draws use a text the flow's grammar accepts, so that
        applied answers are frequent; the other half use the whole corpus.
        """
        key = data.draw(st.sampled_from(sorted(self.flows)))
        flow = self.flows[key]
        if flow.state == "value":
            accepted = [t for t, m in VALUE_TEXTS.items() if m[flow.kind] is not None]
        else:
            accepted = [t for t, c in CURRENCY_TEXTS.items() if c is not None]
        text = data.draw(st.one_of(st.sampled_from(accepted), st.sampled_from(ALL_TEXTS)))
        self.send_text(key=key, text=text)

    @precondition(lambda self: bool(self.flows))
    @rule(data=st.data())
    def press_an_open_flow(self, data: st.DataObject) -> None:
        """Same event as ``press_current``, aimed at a key that has an open flow."""
        key = data.draw(st.sampled_from(sorted(self.flows)))
        choice = data.draw(st.sampled_from(BUTTON_CHOICES[self.flows[key].state]))
        self._press(key, f"p:{self.flows[key].token}:{choice}")

    def _model_text(self, key: tuple[int, int], text: str) -> None:
        flow = self.flows.get(key)
        if flow is None or not (
            flow.state == "value" or (flow.state == "currency" and flow.typing)
        ):
            self.expect_fallback = True
            return
        self.expect_fallback = False
        if flow.state == "currency":
            code = CURRENCY_TEXTS.get(text)
            if code is None:
                self._model_reject(key, flow)
                return
            self._end_flow(key)
            assert flow.url is not None
            self._open(key, self._insert_and_branch(key, flow.url, code))
            return
        meaning = VALUE_TEXTS.get(text, {}).get(flow.kind)
        if meaning is None:
            self._model_reject(key, flow)
            return
        self._end_flow(key)
        if isinstance(meaning, Cancel) or key[1] not in self.active:
            event("value answer: cancel or revoked")
            return
        event("value answer applied")
        self.writes.append(("value", key[1], SUT_KIND[flow.kind], flow.product, meaning))

    def _model_reject(self, key: tuple[int, int], flow: MFlow) -> None:
        attempts = flow.attempts + 1
        if attempts >= MAX_ATTEMPTS:
            event("third invalid answer ended the flow")
            self._end_flow(key)
        else:
            self.flows[key] = replace(flow, attempts=attempts)

    # -- rules: callbacks -----------------------------------------------------

    @rule(
        key=st.sampled_from(KEYS),
        choice=st.sampled_from(
            (
                "x",
                "cur:USD",
                "cur:EUR",
                "cur:type",
                "cur:cancel",
                "sc",
                "sc:store",
                "sc:world",
                "sc:own_country",
                "sc:customs_area",
            )
        ),
    )
    def press_current(self, key: tuple[int, int], choice: str) -> None:
        flow = self.flows.get(key)
        token = flow.token if flow is not None else "f" * 32
        self._press(key, f"p:{token}:{choice}")

    @rule(data=st.data(), key=st.sampled_from(KEYS))
    def replay_seen_button(self, data: st.DataObject, key: tuple[int, int]) -> None:
        if not self.seen_buttons:
            return
        self._press(key, data.draw(st.sampled_from(self.seen_buttons)))

    @rule(
        key=st.sampled_from(KEYS),
        token=st.text(alphabet="0123456789abcdef", min_size=32, max_size=32),
    )
    def press_unknown_token(self, key: tuple[int, int], token: str) -> None:
        self._press(key, f"p:{token}:x")

    def _press(self, key: tuple[int, int], data: str) -> None:
        self._begin()
        self._run(self.h.press(key[0], key[1], data))
        match = TOKEN_RE.match(data)
        if match is None:  # entry or non-flow button replayed
            self._model_non_flow_press(key, data)
            return
        token, verb, arg = match.groups()
        flow = self.flows.get(key)
        fits = flow is not None and (
            verb == "x"
            or (verb == "cur" and flow.state == "currency")
            or (verb == "sc" and flow.state == "scope")
        )
        if flow is None or flow.token != token or not fits:
            event("flow button expired")
            return
        event(f"flow button {verb}:{arg} accepted")
        if verb == "x" or arg == "cancel":
            self._end_flow(key)
        elif verb == "cur" and arg == "type":
            self.flows[key] = replace(flow, typing=True)
            self._learn_prompt(key)
        elif verb == "cur":
            self._end_flow(key)
            assert flow.url is not None
            assert arg is not None
            self._open(key, self._insert_and_branch(key, flow.url, arg))
        elif arg is None:  # the scope picker keeps the flow open
            self._learn_prompt(key)
        else:
            self._end_flow(key)
            if arg != "store" and key[1] in self.active:
                self.writes.append(("scope", key[1], flow.product, True, arg))

    def _model_non_flow_press(self, key: tuple[int, int], data: str) -> None:
        entry = re.fullmatch(r"p:([0-9]+):(th|tg|iv)", data)
        if entry is not None:
            product, verb = int(entry.group(1)), entry.group(2)
            user = key[1]
            if user in self.active and OWNED.get(product) == user:
                self._open(key, MFlow(KIND_BY_VERB[verb], "value", "", product=product))
            return
        self._end_flow(key)  # any other callback abandons the prompt (E8)

    @rule(key=st.sampled_from(KEYS), data=st.sampled_from(("h", "l:a:1", "check_5", "zz")))
    def foreign_callback(self, key: tuple[int, int], data: str) -> None:
        self._begin()
        self._run(self.h.press(key[0], key[1], data))
        self._end_flow(key)

    @rule(key=st.sampled_from(KEYS), command=st.sampled_from(("/cancel", "/help")))
    def command(self, key: tuple[int, int], command: str) -> None:
        self._begin()
        self._run(self.h.text(key[0], key[1], command))
        if command == "/cancel" and key not in self.flows:
            self._advance(key)
        self._end_flow(key)

    @rule(
        key=st.sampled_from(KEYS),
        command=st.sampled_from(("add", "cancel", "help")),
        recipient=st.text(
            alphabet=string.ascii_letters + string.digits + "_", min_size=1, max_size=20
        ).filter(lambda recipient: recipient.lower() != "test_bot"),
    )
    def command_for_another_bot(self, key: tuple[int, int], command: str, recipient: str) -> None:
        self._begin()
        service_calls_before = len(self.services.calls)
        suffix = f" {URL_EUR}" if command == "add" else ""

        self._run(self.h.text(key[0], key[1], f"/{command}@{recipient}{suffix}"))

        assert len(self.services.calls) == service_calls_before
        assert self._step_calls() == []

    # -- rules: time, process, access -----------------------------------------

    @rule(data=st.data())
    def fire_timer(self, data: st.DataObject) -> None:
        if not self.h.timer.armed:
            return
        entry = data.draw(st.sampled_from(self.h.timer.armed))
        self._begin()
        self._run(self.h.timer.fire(entry))
        key = entry.snapshot.key
        flow = self.flows.get(key)
        if flow is not None and flow.token == entry.snapshot.token:
            event("timeout ended a flow")
            self._end_flow(key)
        else:
            event("stale timeout was a no-op")

    @precondition(lambda self: self.pending is None)
    @rule()
    def restart(self) -> None:
        self._begin()
        self._run(self.h.restart())
        self.flows.clear()
        self.generations.clear()

    @rule(user=st.sampled_from(USERS))
    def toggle_access(self, user: int) -> None:
        self._begin()
        for target in (self.active, self.services.active):
            if user in target:
                target.discard(user)
            else:
                target.add(user)

    @rule(
        user=st.sampled_from(USERS), default=st.sampled_from(("ask", "store_only", "other_stores"))
    )
    def set_scope_default(self, user: int, default: str) -> None:
        self._begin()
        self.defaults[user] = default
        self.services.scope_defaults[user] = default  # type: ignore[assignment]

    @rule(
        key=st.sampled_from(KEYS),
        kind=st.sampled_from(("channel_post", "edited_channel_post")),
        text=st.sampled_from(("30", URL_EUR, f"/add {URL_EUR}", "/cancel", "usd")),
    )
    def channel_post(self, key: tuple[int, int], kind: str, text: str) -> None:
        self._begin()
        services_before = len(self.services.calls)
        self._run(self.h.process(message_update(self.h.app.bot, key[0], key[1], text, kind=kind)))
        assert self._step_calls() == []
        assert len(self.services.calls) == services_before

    @rule(
        key=st.sampled_from(KEYS),
        product=st.sampled_from(sorted(OWNED)),
        verb=st.sampled_from(sorted(KIND_BY_VERB)),
    )
    def blocked_open_value(self, key: tuple[int, int], product: int, verb: str) -> None:
        """E13 on entry: the prompt cannot be delivered, so no flow opens."""
        self.h.request.fail_next_call_to(key[0])
        self._begin()
        self._run(self.h.press(key[0], key[1], f"p:{product}:{verb}"))
        self.h.request.clear_failures()
        if key[1] in self.active and OWNED[product] == key[1]:
            self._advance(key)
            self._end_flow(key)

    @rule(key=st.sampled_from(KEYS), url=st.sampled_from((URL_EUR, URL_NOCUR)))
    def blocked_add(self, key: tuple[int, int], url: str) -> None:
        """E13 on the add flow: writes committed, no prompt, no flow."""
        self.h.request.fail_next_call_to(key[0])
        self._begin()
        self._run(self.h.text(key[0], key[1], f"/add {url}"))
        self.h.request.clear_failures()
        self._advance(key)
        user = key[1]
        if user not in self.active:
            return
        self._end_flow(key)
        if url == URL_EUR:
            self._insert_and_branch(key, url, "EUR")

    @rule(key=st.sampled_from(KEYS), text=st.sampled_from(ALL_TEXTS))
    def blocked_answer(self, key: tuple[int, int], text: str) -> None:
        """E13 on an answer: the outcome is the one decided before the send."""
        self.h.request.fail_next_call_to(key[0])
        self._begin()
        self._run(self.h.text(key[0], key[1], text))
        self.h.request.clear_failures()
        flow = self.flows.get(key)
        if flow is not None and flow.state == "currency" and flow.typing:
            code = CURRENCY_TEXTS.get(text)
            if code is not None:
                self._end_flow(key)
                assert flow.url is not None
                self._insert_and_branch(key, flow.url, code)
                return
        if flow is not None and flow.state == "currency" and flow.typing is False:
            return
        if flow is not None and flow.state == "scope":
            return
        self._model_text(key, text)
        self.expect_fallback = None  # the fallback reply itself may be the blocked call

    # -- suspended service continuations: model D1-D4 ------------------------

    @precondition(lambda self: self.pending is None and bool(self.active))
    @rule(data=st.data(), phase=st.sampled_from(("prepare_add", "add_product")))
    def suspend_service(self, data: st.DataObject, phase: str) -> None:
        key = data.draw(st.sampled_from([k for k in KEYS if k[1] in self.active]))
        self._begin()
        barrier = ServiceBarrier(self.services, phase, after=True)
        url = URL_NOCUR if phase == "prepare_add" else URL_EUR

        async def start() -> asyncio.Task[None]:
            task = asyncio.create_task(self.h.text(key[0], key[1], f"/add {url}"))
            await barrier.wait()
            return task

        task = self.loop.run_until_complete(start())
        # Entry invalidates previous work even while no prompt is present.
        self._advance(key)
        self._end_flow(key)
        product = None
        if phase == "add_product":
            product = self.next_id
            self.next_id += 1
            self.writes.append(("insert", key[1], product, url, "EUR"))
        self.pending = (key, phase, self.generations[key], product, barrier, task)
        event(f"suspended {phase}")

    @precondition(lambda self: self.pending is not None)
    @rule()
    def resume_service(self) -> None:
        assert self.pending is not None
        key, phase, ticket, product, barrier, task = self.pending
        self.pending = None
        self._begin()
        stale = self.generations.get(key, 0) != ticket
        barrier.release.set()
        self._run(task)
        setattr(self.services, phase, barrier.original)
        if stale:
            event(f"resumed stale {phase}")
            calls = self._step_calls()
            assert not any(c.method == "editMessageText" or c.callback_data() for c in calls)
            if phase == "prepare_add":
                assert calls == []
            else:
                assert [c.method for c in calls] == ["sendMessage"]
                assert "reply_markup" not in calls[0].params
                assert calls[0].params["text"] == "Added. Kept: only on shop.example."
            # Detect an unpublished registry replacement as well.
            self.registry_matches_model()
            return
        event(f"resumed current {phase}")
        if phase == "prepare_add":
            self._open(key, MFlow("add", "currency", "", url=URL_NOCUR))
        else:
            default = self.defaults.get(key[1], "ask")
            if default == "ask":
                self._open(key, MFlow("add", "scope", "", product=product))
            elif default == "other_stores" and key[1] in self.active:
                self.writes.append(("scope", key[1], product, True, None))

    # -- invariants -------------------------------------------------------------

    @invariant()
    def registry_matches_model(self) -> None:
        registry = self.h.flow.registry
        assert registry.keys() == set(self.flows)
        for key, flow in self.flows.items():
            live = registry.get(key)
            assert live is not None
            assert live.token == flow.token

    @invariant()
    def writes_match_model(self) -> None:
        assert self.services.writes == self.writes

    @invariant()
    def text_consumed_once(self) -> None:
        if self.expect_fallback is None:
            return
        fallback = [
            c
            for c in self._step_calls()
            if c.method == "sendMessage" and c.params.get("text") == NO_OPEN_PROMPT
        ]
        assert len(fallback) == (1 if self.expect_fallback else 0)

    @invariant()
    def callbacks_answered_at_most_once(self) -> None:
        ids = [
            c.params["callback_query_id"]
            for c in self._step_calls()
            if c.method == "answerCallbackQuery"
        ]
        assert len(ids) == len(set(ids))

    @invariant()
    def no_call_after_forbidden(self) -> None:
        blocked: set[int] = set()
        for call in self._step_calls():
            if call.chat_id in blocked:
                raise AssertionError(f"call to blocked chat {call.chat_id}: {call.method}")
            if call.failed and call.chat_id is not None:
                blocked.add(call.chat_id)

    @invariant()
    def within_telegram_limits(self) -> None:
        assert self.h.request.violations == []


GuidedFlowMachine.TestCase.settings = settings(
    max_examples=int(os.environ.get("GUIDED_FLOW_MACHINE_EXAMPLES", "300")),
    stateful_step_count=50,
    deadline=None,
)
TestGuidedFlowMachine = GuidedFlowMachine.TestCase
