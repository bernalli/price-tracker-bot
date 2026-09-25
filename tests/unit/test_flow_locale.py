"""Language of the guided-flow texts, and when it is chosen.

The coordinator sets the language of the update synchronously, before any await,
and restores it when the update is done; a timeout uses the language of the user
who opened the prompt. Choosing the language before any await keeps the ticket
taken at entry meaningful: a ``/cancel`` processed while the entry waits on a
service invalidates it.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from babel.messages.pofile import read_po
from hypothesis import given
from hypothesis import strategies as st

from price_tracker.bot import flows
from price_tracker.bot.flows import GuidedFlow, Route, nothing_to_cancel
from price_tracker.bot.messages import _, reset_locale, set_locale
from tests.support.fake_telegram import FakeServices, callback_update, message_update
from tests.support.flow_harness import Harness, ServiceBarrier

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from babel.messages.catalog import Catalog
    from telegram import Update
    from telegram.ext import Application

    from price_tracker.bot.flows import AnyContext

USER = 10
PRIVATE = 100
LOCALE_DIR = Path(__file__).resolve().parents[2] / "src" / "price_tracker" / "locale"

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
}


def _catalog(locale: str) -> Catalog:
    with (LOCALE_DIR / locale / "LC_MESSAGES" / "messages.po").open("rb") as handle:
        return read_po(handle)


def _msgstr_it(msgid: str) -> str:
    """The Italian text as the catalog file states it (independent of gettext)."""
    message = _catalog("it_IT").get(msgid)
    assert message is not None, msgid
    assert isinstance(message.string, str)
    assert message.string, msgid
    assert message.string != msgid, msgid
    return message.string


# --- ordering of the language step (AST) ------------------------------------


def _function(func: Callable[..., Any]) -> ast.AsyncFunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    node = tree.body[0]
    assert isinstance(node, ast.AsyncFunctionDef)
    return node


def _pos(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


def _calls_to(tree: ast.AST, name: str) -> list[ast.Call]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called == name:
                found.append(node)
    return sorted(found, key=_pos)


def _first(tree: ast.AST, *types: type[ast.AST]) -> ast.AST | None:
    nodes = sorted((n for n in ast.walk(tree) if isinstance(n, types)), key=_pos)
    return nodes[0] if nodes else None


def _reset_in_finally(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.Try)
        and any(_calls_to(stmt, "reset_locale") for stmt in node.finalbody)
        for node in ast.walk(tree)
    )


def test_handle_update_sets_the_language_before_any_await() -> None:
    tree = _function(GuidedFlow.handle_update)
    first_await = _first(tree, ast.Await)
    first_async_block = _first(tree, ast.AsyncFor, ast.AsyncWith)
    set_calls = _calls_to(tree, "set_locale")
    try_node = _first(tree, ast.Try)

    assert isinstance(first_await, ast.Await)
    awaited = first_await.value
    assert isinstance(awaited, ast.Call)
    assert isinstance(awaited.func, ast.Attribute)
    assert awaited.func.attr == "_dispatch"
    assert first_async_block is None or _pos(first_async_block) > _pos(first_await)
    assert len(set_calls) == 1
    assert try_node is not None
    assert _pos(set_calls[0]) < _pos(try_node) < _pos(first_await)
    assert _reset_in_finally(tree)


def test_timeout_sets_the_saved_language_after_the_claim() -> None:
    tree = _function(GuidedFlow.on_timeout)
    claims = _calls_to(tree, "claim")
    set_calls = _calls_to(tree, "set_locale")
    first_await = _first(tree, ast.Await)

    assert claims
    assert set_calls
    assert first_await is not None
    assert _pos(claims[0]) < _pos(set_calls[0]) < _pos(first_await)
    assert _reset_in_finally(tree)


def test_nothing_to_cancel_sets_the_language_before_any_await() -> None:
    tree = _function(nothing_to_cancel)
    set_calls = _calls_to(tree, "set_locale")
    first_await = _first(tree, ast.Await)

    assert set_calls
    assert first_await is not None
    assert _pos(set_calls[0]) < _pos(first_await)
    assert _reset_in_finally(tree)


# --- behaviour ---------------------------------------------------------------


def _services() -> FakeServices:
    return FakeServices(active={USER}, products={1: (USER, "Kettle")})


@pytest.fixture
async def h() -> AsyncIterator[Harness]:
    harness = Harness(_services())
    await harness.start()
    yield harness
    await harness.stop()


async def _press(h: Harness, data: str, language_code: str | None) -> None:
    await h.process(callback_update(h.app.bot, PRIVATE, USER, data, language_code=language_code))


async def _text(h: Harness, text: str, language_code: str | None) -> None:
    await h.process(message_update(h.app.bot, PRIVATE, USER, text, language_code=language_code))


async def test_cancel_during_entry_admission_prevents_the_prompt() -> None:
    h = Harness(_services(), concurrent_updates=2)
    await h.start()
    barrier = ServiceBarrier(h.services, "is_active")
    task = asyncio.create_task(h.press(PRIVATE, USER, "p:1:th"))
    try:
        await barrier.wait()
        await h.text(PRIVATE, USER, "/cancel")
        barrier.release.set()
        await task
        assert not any(call.callback_data() for call in h.sent(PRIVATE))
        assert len(h.flow.registry) == 0
        assert len(h.request.calls_of("answerCallbackQuery")) == 1
    finally:
        barrier.release.set()
        await task
        await h.stop()


class _WaitsBeforeAdmission(GuidedFlow):
    """A coordinator with an await in front of the entry step."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def handle_update(
        self,
        update: Update,
        application: Application[Any, AnyContext, Any, Any, Any, Any],
        check_result: object,
        context: AnyContext,
    ) -> None:
        if isinstance(check_result, Route) and check_result.action is not None:
            self.entered.set()
            await self.release.wait()
        await super().handle_update(update, application, check_result, context)


async def test_an_await_before_admission_lets_a_cancelled_entry_open_its_prompt() -> None:
    """Positive control: the same sequence with a pre-step await does open the prompt."""
    h = Harness(_services(), concurrent_updates=2)
    h.app.remove_handler(h.flow, 0)
    slow = _WaitsBeforeAdmission(h.services, h.timer)
    slow.attach(h.app.bot)
    h.app.add_handler(slow, 0)
    h.flow = slow
    await h.start()
    task = asyncio.create_task(h.press(PRIVATE, USER, "p:1:th"))
    try:
        await asyncio.wait_for(slow.entered.wait(), 2)
        await h.text(PRIVATE, USER, "/cancel")
        slow.release.set()
        await task
        assert any(call.callback_data() for call in h.sent(PRIVATE))
        assert len(h.flow.registry) == 1
    finally:
        slow.release.set()
        await task
        await h.stop()


async def test_italian_user_reads_the_catalog_texts(h: Harness) -> None:
    await _press(h, "p:1:th", "it")
    prompt = h.last_prompt(PRIVATE)
    assert str(prompt.params["text"]) == "Kettle\n" + _msgstr_it(
        "Send the drop threshold: 20% or 5.50 (one dot or comma)."
    )
    assert "Annulla" in str(prompt.params["reply_markup"])

    await _text(h, "abc", "it")
    await _text(h, "20%", "it")
    await _press(h, "p:1:tg", "it")
    message_id = h.prompt_message_id(h.last_prompt(PRIVATE))
    await _text(h, "/cancel", "it")

    texts = h.texts_to(PRIVATE)
    assert _msgstr_it("Use digits with one dot or comma, e.g. 1299.99.") in texts
    assert _msgstr_it("Saved.") in texts
    assert h.edits_of(message_id) == [_msgstr_it("Cancelled - nothing changed.")]


async def test_english_user_reads_the_source_texts(h: Harness) -> None:
    await _press(h, "p:1:th", "en")
    assert str(h.last_prompt(PRIVATE).params["text"]) == (
        "Kettle\nSend the drop threshold: 20% or 5.50 (one dot or comma)."
    )
    await _text(h, "abc", "en")
    await _text(h, "20%", "en")
    await _press(h, "p:1:tg", "en")
    message_id = h.prompt_message_id(h.last_prompt(PRIVATE))
    await _text(h, "/cancel", "en")

    texts = h.texts_to(PRIVATE)
    assert "Use digits with one dot or comma, e.g. 1299.99." in texts
    assert "Saved." in texts
    assert h.edits_of(message_id) == ["Cancelled - nothing changed."]


async def test_timeout_uses_the_language_of_the_user_who_opened_the_prompt(h: Harness) -> None:
    await _press(h, "p:1:th", "it")
    message_id = h.prompt_message_id(h.last_prompt(PRIVATE))
    armed = h.timer.live()
    assert len(armed) == 1

    token = set_locale("en")
    try:
        await h.flow.on_timeout(armed[0].snapshot)
    finally:
        reset_locale(token)

    assert h.edits_of(message_id) == [_msgstr_it("Expired - nothing changed.")]


@pytest.mark.parametrize("locale", ["it_IT", "en"])
def test_every_flow_text_is_in_the_catalogs(locale: str) -> None:
    tree = ast.parse(Path(inspect.getsourcefile(flows) or "").read_text())
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

    await _press(h, "p:1:th", "it")
    await _text(h, "20%", "it")

    assert _("Saved.") == before


@given(st.none() | st.text())
def test_set_locale_accepts_any_language_code(language_code: str | None) -> None:
    token = set_locale(language_code)
    reset_locale(token)
