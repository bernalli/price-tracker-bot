"""``/soglia`` speaks the one threshold grammar the guided flow and the CSV import use."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.app.inputs import (
    SCALAR_MAX_CHARS,
    Absolute,
    AnyDrop,
    Percentage,
    parse_threshold,
)
from price_tracker.bot.handlers.product import cmd_threshold
from price_tracker.bot.handlers.product_io import parse_csv_threshold

PRODUCT = {"id": 3, "user_id": 7, "name": "Kettle", "url": "https://shop.example/p/3"}

ACCEPTED = [
    ("20%", ("percentage", "20")),
    ("1%", ("percentage", "1")),
    ("99%", ("percentage", "99")),
    ("50", ("absolute", "50")),
    ("50,5", ("absolute", "50.5")),
    ("50.5", ("absolute", "50.5")),
    ("ogni", ("any_drop", "0")),
    ("ANY", ("any_drop", "0")),
    ("Sempre", ("any_drop", "0")),
    ("all", ("any_drop", "0")),
]
REJECTED = [
    "12.5%",
    "0%",
    "150%",
    "100%",
    "0",
    "-5",
    "1e3",
    "nan%",
    "Infinity",
    "inf",
    "1.23456",
    "1234567890",
    "€50",
    "x" * 65,
    "5\x00",
    "5\n5",
    "-",
    "no",
    "skip",
    "salta",
    "annulla",
    "٣٠%",
    "",
    "   ",
    "1e999",
    "1" * 10_000,
]


def _run(arg: str, language_code: str = "it") -> tuple[AsyncMock, AsyncMock]:
    """Run ``/soglia 3 <arg>`` and return the ``set_threshold`` and ``reply_text`` mocks."""
    db = AsyncMock()
    db.is_user_allowed.return_value = True
    db.is_user_admin.return_value = False
    db.get_product_for_user.return_value = PRODUCT
    context: Any = MagicMock()
    context.bot_data = {"db": db}
    context.args = ["3", arg]
    update = MagicMock()
    update.effective_user.id = 7
    update.effective_user.language_code = language_code
    update.message.reply_text = AsyncMock()

    async def call() -> None:
        await cmd_threshold(update, context)

    asyncio.run(call())
    return db.set_threshold, update.message.reply_text


@pytest.mark.parametrize(("text", "stored"), ACCEPTED)
def test_accepted_inputs_store_the_expected_threshold(text: str, stored: tuple[str, str]) -> None:
    set_threshold, _reply = _run(text)
    set_threshold.assert_awaited_once_with(3, *stored)


@pytest.mark.parametrize("text", REJECTED)
def test_rejected_inputs_write_nothing_and_say_so(text: str) -> None:
    set_threshold, reply = _run(text)
    set_threshold.assert_not_awaited()
    assert reply.await_args is not None
    assert reply.await_args.args[0].startswith("❌ Valore non valido")


def test_an_oversized_rejected_input_produces_a_bounded_error() -> None:
    set_threshold, reply = _run("1" * 10_000)
    set_threshold.assert_not_awaited()
    assert reply.await_args is not None
    assert reply.await_args.args[0] == (
        "❌ Valore non valido: " + "1" * (SCALAR_MAX_CHARS - 1) + "…"
    )


def test_the_rejection_is_translated() -> None:
    _set, reply = _run("150%", language_code="en")
    assert reply.await_args is not None
    assert reply.await_args.args[0] == "❌ Invalid value: 150%"


def _expected(text: str) -> tuple[str, str] | None:
    parsed = parse_threshold(text)
    if isinstance(parsed, Percentage):
        return ("percentage", str(parsed.value))
    if isinstance(parsed, Absolute):
        return ("absolute", str(parsed.amount))
    if isinstance(parsed, AnyDrop):
        return ("any_drop", "0")
    return None


@settings(max_examples=300, deadline=None)
@given(text=st.text(max_size=80))
def test_soglia_agrees_with_parse_threshold_on_any_text(text: str) -> None:
    set_threshold, _reply = _run(text)
    expected = _expected(text)
    if expected is None:
        set_threshold.assert_not_awaited()
    else:
        set_threshold.assert_awaited_once_with(3, *expected)


def test_csv_import_and_soglia_share_the_grammar() -> None:
    for text in ("20", "0", "12.5", "150", "nan", "50,5"):
        parsed = parse_threshold(f"{text}%")
        csv_value = parse_csv_threshold(f"percentage:{text}")
        assert (csv_value is not None) == isinstance(parsed, Percentage)


def test_exported_threshold_values_round_trip_through_the_import() -> None:
    """Whatever /soglia stores is accepted by the CSV import that reads it back."""
    for text, (kind, value) in ACCEPTED:
        cell = f"{kind}:{value}"
        assert parse_csv_threshold(cell) is not None, (text, cell)
    assert parse_csv_threshold("absolute:50.5") == ("absolute", Decimal("50.5"))
