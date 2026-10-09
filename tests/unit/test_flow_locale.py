"""Language of the guided-flow texts, and when it is read.

The coordinator answers in the user's stored language. ``handle_update`` only
sets the Telegram language as a fallback; the stored one is read by the injected
resolver *after* the route's synchronous registry step (ticket, claim or
replace), never before it. A ``/cancel`` that runs while an opening waits on that
read therefore still invalidates the opening, and a ``/cancel`` that already
claimed its flow is not undone by an update that arrives during its own read.
A timeout reuses the language stored in the flow, without any read.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import textwrap
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from babel.messages.pofile import read_po
from hypothesis import given
from hypothesis import strategies as st

from price_tracker.bot import flow_rendering, flows
from price_tracker.bot.flows import FlowRegistry, FlowSnapshot, GuidedFlow, Route
from price_tracker.bot.messages import _, reset_locale, set_locale
from tests.support.fake_telegram import FakeServices, callback_update, message_update
from tests.support.flow_harness import Harness, ServiceBarrier

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from babel.messages.catalog import Catalog
    from telegram import Update

    from price_tracker.bot.flows import ActiveFlow, AnyContext, FlowKey, _Transport

USER = 10
PRIVATE = 100
LOCALE_DIR = Path(__file__).resolve().parents[2] / "src" / "price_tracker" / "locale"
BARRIER_CALLS = frozenset({"generation", "advance", "claim", "replace"})

ITALIAN: dict[str, str] = {
    "This button has expired.": "Questo pulsante è scaduto.",
    "Not authorised.": "Non autorizzato.",
    "Product not found.": "Prodotto non trovato.",
    "Replaced by a newer prompt.": "Sostituita da una richiesta più recente.",
    "Nothing to cancel.": "Niente da annullare.",
    "Too many invalid answers - cancelled, nothing changed.": (
        "Troppe risposte non valide - annullato, nessuna modifica."
    ),
    "Saved.": "Salvato.",
    "Cancelled - nothing changed.": "Annullato - nessuna modifica.",
    "Expired - nothing changed.": "Scaduto - nessuna modifica.",
    "Cancel": "Annulla",
    "Send the drop threshold: 20% or 5.50 (one dot or comma).": (
        "Invia la soglia di ribasso: 20% oppure 5,50 (un punto o una virgola)."
    ),
    "Send the target price, e.g. 49.90 (0 clears it).": (
        "Invia il prezzo obiettivo, per esempio 49,90 (0 lo rimuove)."
    ),
    "Send the check interval in minutes (5-10080, 0 resets).": (
        "Invia l'intervallo di controllo in minuti (5-10080, 0 ripristina quello globale)."
    ),
    "Please send a value.": "Invia un valore.",
    "That is too long.": "Troppo lungo.",
    "That contains invisible characters.": "Contiene caratteri invisibili.",
    "Use digits with one dot or comma, e.g. 1299.99.": (
        "Usa cifre con un punto o una virgola, per esempio 1299,99."
    ),
    "That value is out of range.": "Valore fuori intervallo.",
    "That is not a link.": "Non è un link.",
    "Send the link of the product page to analyse.": (
        "Invia il link della pagina prodotto da analizzare."
    ),
}


def _catalog(locale: str) -> Catalog:
    with (LOCALE_DIR / locale / "LC_MESSAGES" / "messages.po").open("rb") as handle:
        return read_po(handle)


def _it(msgid: str) -> str:
    """The Italian text as the catalog file states it (independent of gettext)."""
    message = _catalog("it_IT").get(msgid)
    assert message is not None, msgid
    assert isinstance(message.string, str)
    assert message.string, msgid
    assert message.string != msgid, msgid
    return message.string


# --- ordering of the language read (AST) --------------------------------------


def _function(func: Callable[..., Any]) -> ast.AsyncFunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    node = tree.body[0]
    assert isinstance(node, ast.AsyncFunctionDef)
    return node


def _pos(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _called(node: ast.Call) -> str | None:
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _calls_to(tree: ast.AST, *names: str) -> list[ast.Call]:
    found = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _called(n) in names]
    return sorted(found, key=_pos)


def _awaits(tree: ast.AST) -> list[ast.Await]:
    return sorted((n for n in ast.walk(tree) if isinstance(n, ast.Await)), key=_pos)


def _reset_in_finally(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Try)
        and any(_calls_to(stmt, "reset_locale") for stmt in node.finalbody)
        for node in ast.walk(tree)
    )


def _read_follows_the_barrier(tree: ast.AsyncFunctionDef) -> bool:
    """The first await is the language read, and a registry step comes before it."""
    awaits = _awaits(tree)
    barrier = _calls_to(tree, *BARRIER_CALLS)
    if not awaits or not barrier:
        return False
    first = awaits[0].value
    return (
        isinstance(first, ast.Call)
        and _called(first) == "_localise"
        and _pos(barrier[0]) < _pos(awaits[0])
    )


def _no_read_before_the_barrier(tree: ast.AsyncFunctionDef) -> bool:
    reads = _calls_to(tree, "_localise")
    barrier = _calls_to(tree, *BARRIER_CALLS)
    return bool(reads) and bool(barrier) and _pos(barrier[0]) < _pos(reads[0])


@pytest.mark.parametrize(
    "method",
    [
        GuidedFlow._on_entry_callback,
        GuidedFlow._on_global_entry,
        GuidedFlow._on_add_entry,
        GuidedFlow._end_by_user,
    ],
    ids=lambda m: m.__name__,
)
def test_the_read_is_the_first_await_after_the_ticket_or_claim(
    method: Callable[..., Any],
) -> None:
    assert _read_follows_the_barrier(_function(method))


@pytest.mark.parametrize(
    "method",
    [
        GuidedFlow._close_by_button,
        GuidedFlow._on_currency_chosen,
        GuidedFlow._on_scope_chosen,
        GuidedFlow._on_answer,
        GuidedFlow._reject,
    ],
    ids=lambda m: m.__name__,
)
def test_no_read_precedes_the_claim_or_replace(method: Callable[..., Any]) -> None:
    assert _no_read_before_the_barrier(_function(method))


class _ReadsBeforeTheTicket(GuidedFlow):
    """Negative control: the stored language is read before the ticket is taken."""

    async def _on_entry_callback(
        self, update: Update, route: Route, context: AnyContext, transport: _Transport
    ) -> bool:
        await self._localise(route)
        return await super()._on_entry_callback(update, route, context, transport)


def test_the_order_check_rejects_a_read_before_the_ticket() -> None:
    assert not _read_follows_the_barrier(_function(_ReadsBeforeTheTicket._on_entry_callback))


def test_handle_update_sets_only_the_telegram_fallback_before_any_await() -> None:
    tree = _function(GuidedFlow.handle_update)
    first_await = _awaits(tree)[0]
    set_calls = _calls_to(tree, "set_locale")

    assert isinstance(first_await.value, ast.Call)
    assert _called(first_await.value) == "_dispatch"
    assert len(set_calls) == 1
    assert _pos(set_calls[0]) < _pos(first_await)
    assert not _calls_to(tree, "_localise", "_locale_resolver")
    assert _reset_in_finally(tree)


def test_timeout_reuses_the_stored_language_without_a_read() -> None:
    tree = _function(GuidedFlow.on_timeout)
    claims = _calls_to(tree, "claim")
    set_calls = _calls_to(tree, "set_locale")

    assert claims
    assert set_calls
    assert _pos(claims[0]) < _pos(set_calls[0]) < _pos(_awaits(tree)[0])
    assert not _calls_to(tree, "_localise", "_locale_resolver")
    assert _reset_in_finally(tree)


# --- behaviour ---------------------------------------------------------------


def _services(language: str | None = "it") -> FakeServices:
    services = FakeServices(active={USER}, products={1: (USER, "Kettle")})
    if language is not None:
        services.languages[USER] = language
    return services


@pytest.fixture
async def h() -> AsyncIterator[Harness]:
    harness = Harness(_services())
    await harness.start()
    yield harness
    await harness.stop()


async def _press(h: Harness, data: str, language_code: str | None = "en") -> None:
    await h.process(callback_update(h.app.bot, PRIVATE, USER, data, language_code=language_code))


async def _text(h: Harness, text: str, language_code: str | None = "en") -> None:
    await h.process(message_update(h.app.bot, PRIVATE, USER, text, language_code=language_code))


def _prompt_text(h: Harness) -> str:
    return str(h.last_prompt(PRIVATE).params["text"])


THRESHOLD_PROMPT = "Send the drop threshold: 20% or 5.50 (one dot or comma)."


async def test_the_stored_language_wins_over_the_telegram_one_in_every_flow_text(
    h: Harness,
) -> None:
    await _press(h, "p:1:th")
    assert _prompt_text(h) == "Kettle\n" + _it(THRESHOLD_PROMPT)
    assert "Annulla" in str(h.last_prompt(PRIVATE).params["reply_markup"])

    await _text(h, "abc")
    await _text(h, "20%")
    await _press(h, "p:1:tg")
    message_id = h.prompt_message_id(h.last_prompt(PRIVATE))
    await _text(h, "/cancel")

    texts = h.texts_to(PRIVATE)
    assert _it("Use digits with one dot or comma, e.g. 1299.99.") in texts
    assert _it("Saved.") in texts
    assert h.edits_of(message_id) == [_it("Cancelled - nothing changed.")]


async def test_cancel_outside_a_flow_answers_in_the_stored_language(h: Harness) -> None:
    await _text(h, "/cancel")
    assert h.texts_to(PRIVATE) == [_it("Nothing to cancel.")]


async def test_the_timeout_closes_in_the_language_read_when_the_prompt_opened(
    h: Harness,
) -> None:
    await _press(h, "p:1:th")
    message_id = h.prompt_message_id(h.last_prompt(PRIVATE))
    armed = h.timer.live()
    assert len(armed) == 1
    h.services.languages[USER] = "en"
    reads = len(h.services.language_reads)

    token = set_locale("en")
    try:
        await h.flow.on_timeout(armed[0].snapshot)
    finally:
        reset_locale(token)

    assert h.edits_of(message_id) == [_it("Expired - nothing changed.")]
    assert len(h.services.language_reads) == reads


async def test_without_a_stored_language_the_telegram_one_is_used() -> None:
    h = Harness(_services(language=None))
    await h.start()
    try:
        await _press(h, "p:1:th", "it")
        assert _prompt_text(h) == "Kettle\n" + _it(THRESHOLD_PROMPT)
        await _press(h, "p:1:tg", "en")
        assert _prompt_text(h) == "Kettle\nSend the target price, e.g. 49.90 (0 clears it)."
    finally:
        await h.stop()


async def test_a_failed_read_falls_back_to_the_telegram_language_and_the_flow_goes_on(
    h: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(user_id: int) -> None:
        raise RuntimeError("database is locked")

    h.services.get_user = broken  # type: ignore[method-assign]
    with caplog.at_level(logging.WARNING):
        await _press(h, "p:1:th", "it")
        await _text(h, "20%", "en")

    assert _prompt_text(h) == "Kettle\n" + _it(THRESHOLD_PROMPT)
    assert "Saved." in h.texts_to(PRIVATE)
    assert h.services.writes
    assert "could not read the language" in caplog.text


# --- concurrency: /cancel against the language read (both orders) -------------


async def test_b1_a_cancel_completed_first_leaves_the_next_opening_alone(h: Harness) -> None:
    await _text(h, "/cancel")
    await _press(h, "p:1:th")

    assert h.texts_to(PRIVATE)[0] == _it("Nothing to cancel.")
    assert len(h.flow.registry) == 1
    assert _prompt_text(h) == "Kettle\n" + _it(THRESHOLD_PROMPT)
    assert h.request.calls_of("editMessageText") == []


async def test_b1_a_cancel_waiting_on_its_read_has_already_closed_its_flow() -> None:
    h = Harness(_services(), concurrent_updates=2)
    await h.start()
    await _press(h, "p:1:th")
    first_prompt = h.prompt_message_id(h.last_prompt(PRIVATE))
    barrier = ServiceBarrier(h.services, "get_user")
    task = asyncio.create_task(_text(h, "/cancel"))
    try:
        await barrier.wait()
        assert len(h.flow.registry) == 0
        await _press(h, "p:1:tg")
        assert len(h.flow.registry) == 1
        barrier.release.set()
        await task
        flow = h.flow.registry.get((PRIVATE, USER))
        assert flow is not None
        assert flow.kind is flows.FlowKind.TARGET
        assert h.edits_of(first_prompt) == [_it("Cancelled - nothing changed.")]
        assert _it("Nothing to cancel.") not in h.texts_to(PRIVATE)
    finally:
        barrier.release.set()
        await task
        await h.stop()


async def test_b2_a_cancel_during_the_openings_read_prevents_the_prompt() -> None:
    h = Harness(_services(), concurrent_updates=2)
    await h.start()
    barrier = ServiceBarrier(h.services, "get_user")
    task = asyncio.create_task(_press(h, "p:1:th"))
    try:
        await barrier.wait()
        await _text(h, "/cancel")
        barrier.release.set()
        await task
        assert not any(call.callback_data() for call in h.sent(PRIVATE))
        assert len(h.flow.registry) == 0
        assert len(h.request.calls_of("answerCallbackQuery")) == 1
        assert not any(c[0] == "is_active" for c in h.services.calls)
    finally:
        barrier.release.set()
        await task
        await h.stop()


async def test_cancel_during_an_answers_language_read_prevents_the_write() -> None:
    h = Harness(_services(), concurrent_updates=2)
    await h.start()
    await _press(h, "p:1:th")
    barrier = ServiceBarrier(h.services, "get_user")
    task = asyncio.create_task(_text(h, "20%"))
    try:
        await barrier.wait()
        await _text(h, "/cancel")
        barrier.release.set()
        await task

        assert h.services.writes == []
        assert _it("Saved.") not in h.texts_to(PRIVATE)
    finally:
        barrier.release.set()
        await task
        await h.stop()


async def test_cancel_during_terminal_reject_language_read_suppresses_stale_reply() -> None:
    h = Harness(_services(), concurrent_updates=2)
    await h.start()
    await _press(h, "p:1:th")
    await _text(h, "abc")
    await _text(h, "abc")
    barrier = ServiceBarrier(h.services, "get_user")
    task = asyncio.create_task(_text(h, "abc"))
    try:
        await barrier.wait()
        await _text(h, "/cancel")
        barrier.release.set()
        await task

        assert _it("Too many invalid answers - cancelled, nothing changed.") not in h.texts_to(
            PRIVATE
        )
    finally:
        barrier.release.set()
        await task
        await h.stop()


async def test_b2_negative_control_a_read_before_the_ticket_opens_a_cancelled_prompt() -> None:
    """The same sequence against a coordinator that reads first does open the prompt."""
    h = Harness(_services(), concurrent_updates=2)
    h.app.remove_handler(h.flow, 0)
    early = _ReadsBeforeTheTicket(h.services, h.timer, locale_resolver=h.flow._locale_resolver)
    early.attach(h.app.bot)
    h.app.add_handler(early, 0)
    h.flow = early
    await h.start()
    barrier = ServiceBarrier(h.services, "get_user")
    task = asyncio.create_task(_press(h, "p:1:th"))
    try:
        await barrier.wait()
        await _text(h, "/cancel")
        barrier.release.set()
        await task
        assert any(call.callback_data() for call in h.sent(PRIVATE))
        assert len(h.flow.registry) == 1
    finally:
        barrier.release.set()
        await task
        await h.stop()


class _RecordingRegistry(FlowRegistry):
    """Writes every ticket, advance, claim and replace into a shared log."""

    def __init__(self, log: list[str]) -> None:
        super().__init__()
        self.log = log

    def generation(self, key: FlowKey) -> int:
        self.log.append("generation")
        return super().generation(key)

    def advance(self, key: FlowKey) -> int:
        self.log.append("advance")
        return super().advance(key)

    def claim(self, snapshot: FlowSnapshot) -> ActiveFlow | None:
        self.log.append("claim")
        return super().claim(snapshot)

    def replace(self, snapshot: FlowSnapshot, flow: ActiveFlow) -> bool:
        self.log.append("replace")
        return super().replace(snapshot, flow)


def _recorded(log: list[str], *, coordinator: type[GuidedFlow] = GuidedFlow) -> Harness:
    services = _services()
    services.prepare_by_url["https://shop.example/p"] = flows.PrepareResult(
        flows.PrepareStatus.READY,
        flows.PreparedProduct("https://shop.example/p", "Fan", "shop.example", Decimal(10), None),
    )
    h = Harness(services, registry=_RecordingRegistry(log))
    if coordinator is not GuidedFlow:
        h.app.remove_handler(h.flow, 0)
        other = coordinator(
            h.services, h.timer, registry=h.flow.registry, locale_resolver=h.flow._locale_resolver
        )
        other.attach(h.app.bot)
        h.app.add_handler(other, 0)
        h.flow = other
    original = services.get_user

    async def get_user(user_id: int) -> Any:
        log.append("language")
        return await original(user_id)

    services.get_user = get_user  # type: ignore[method-assign]
    return h


def _token(h: Harness) -> str:
    flow = h.flow.registry.get((PRIVATE, USER))
    assert flow is not None
    return flow.token


async def test_b3_no_update_reads_the_language_before_its_registry_step() -> None:
    log: list[str] = []
    h = _recorded(log)
    await h.start()
    steps: list[tuple[str, Callable[[], Any]]] = [
        ("entry", lambda: _press(h, "p:1:th")),
        ("invalid answer", lambda: _text(h, "abc")),
        ("valid answer", lambda: _text(h, "20%")),
        ("entry again", lambda: _press(h, "p:1:iv")),
        ("cancel with a flow", lambda: _text(h, "/cancel")),
        ("cancel without a flow", lambda: _text(h, "/cancel")),
        ("add", lambda: _text(h, "https://shop.example/p")),
        ("type a code", lambda: _press(h, f"p:{_token(h)}:cur:type")),
        ("code", lambda: _text(h, "USD")),
        ("other stores", lambda: _press(h, f"p:{_token(h)}:sc")),
        ("level", lambda: _press(h, f"p:{_token(h)}:sc:world")),
        ("entry for a command", lambda: _press(h, "p:1:th")),
        ("another command", lambda: _text(h, "/help")),
        ("entry for a button", lambda: _press(h, "p:1:tg")),
        ("cancel button", lambda: _press(h, f"p:{_token(h)}:x")),
    ]
    try:
        for name, step in steps:
            log.clear()
            await step()
            assert "language" in log, name
            assert log[0] in BARRIER_CALLS, (name, log)
    finally:
        await h.stop()


async def test_b3_negative_control_the_log_sees_a_read_before_the_ticket() -> None:
    log: list[str] = []
    h = _recorded(log, coordinator=_ReadsBeforeTheTicket)
    await h.start()
    try:
        await _press(h, "p:1:th")
        assert log[0] == "language"
    finally:
        await h.stop()


# --- catalogs ---------------------------------------------------------------


@pytest.mark.parametrize("locale", ["it_IT", "en"])
def test_every_flow_text_is_in_the_catalogs(locale: str) -> None:
    tree = ast.parse(Path(inspect.getsourcefile(flow_rendering) or "").read_text())
    marked = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "N_"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    ids = {message.id for message in _catalog(locale) if message.id}

    assert set(ITALIAN) <= marked
    assert marked - ids == set()


def test_reachable_texts_have_the_italian_translation() -> None:
    catalog = _catalog("it_IT")
    for msgid, expected in ITALIAN.items():
        message = catalog.get(msgid)
        assert message is not None, msgid
        assert not message.fuzzy, msgid
        assert message.string == expected, msgid


async def test_handle_update_restores_the_callers_language(h: Harness) -> None:
    before = _("Saved.")
    assert before == "Saved."

    await _press(h, "p:1:th")
    await _text(h, "20%")

    assert _("Saved.") == before


@given(st.none() | st.text())
def test_set_locale_accepts_any_language_code(language_code: str | None) -> None:
    token = set_locale(language_code)
    reset_locale(token)
