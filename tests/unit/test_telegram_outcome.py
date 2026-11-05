"""One request produces one classified outcome without retrying."""

from __future__ import annotations

import asyncio
import warnings
from datetime import timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from telegram import Message
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Conflict,
    EndPointNotFound,
    Forbidden,
    InvalidToken,
    NetworkError,
    RetryAfter,
    TelegramError,
    TimedOut,
)

from price_tracker.bot.telegram_outcome import Outcome, OutcomeKind, attempt
from tests.support.fake_telegram import FakeRequest, make_application


async def fail(error: BaseException) -> object:
    raise error


async def succeed(value: object) -> object:
    return value


@pytest.mark.parametrize(
    ("error", "kind", "seconds"),
    [
        (BadRequest("Message is not modified: unchanged"), OutcomeKind.APPLIED, None),
        (BadRequest("prefix MESSAGE IS NOT MODIFIED suffix"), OutcomeKind.APPLIED, None),
        (BadRequest("refused"), OutcomeKind.REJECTED, None),
        (Forbidden("refused"), OutcomeKind.REJECTED, None),
        (InvalidToken("refused"), OutcomeKind.REJECTED, None),
        (ChatMigrated(1), OutcomeKind.REJECTED, None),
        (Conflict("refused"), OutcomeKind.REJECTED, None),
        (EndPointNotFound("refused"), OutcomeKind.REJECTED, None),
        (RetryAfter(5), OutcomeKind.RETRY_AFTER, 5.0),
        (RetryAfter(timedelta(seconds=7.5)), OutcomeKind.RETRY_AFTER, 7.5),
        (RetryAfter(-3), OutcomeKind.RETRY_AFTER, 0.0),
        (TimedOut("lost"), OutcomeKind.UNCERTAIN, None),
        (NetworkError("lost"), OutcomeKind.UNCERTAIN, None),
        (TelegramError("lost"), OutcomeKind.UNCERTAIN, None),
    ],
    ids=lambda value: type(value).__name__,
)
async def test_error_table_and_local_warning_suppression(
    error: TelegramError, kind: OutcomeKind, seconds: float | None
) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        outcome = await attempt(fail(error))
    assert caught == []
    assert outcome == Outcome(kind, retry_after=seconds, reason=f"{type(error).__name__}: {error}")


@pytest.mark.parametrize("value", [True, None, 1, "response"])
async def test_success_without_a_message_has_no_invented_identifier(value: object) -> None:
    assert await attempt(succeed(value)) == Outcome(OutcomeKind.APPLIED)


async def test_success_retains_the_real_message() -> None:
    async with make_application(FakeRequest()) as app:
        message = await app.bot.send_message(1, "Original")
        assert isinstance(message, Message)
        result = await attempt(succeed(message))
        assert result.kind is OutcomeKind.APPLIED
        assert result.message is message
        assert result.reason == ""


@pytest.mark.parametrize("error", [asyncio.CancelledError(), ValueError("programming error")])
async def test_non_telegram_errors_and_cancellation_propagate(error: BaseException) -> None:
    with pytest.raises(type(error)):
        await attempt(fail(error))


@pytest.mark.parametrize(
    "fields",
    [
        {"kind": "applied"},
        {"message": True},
        {"reason": None},
        {"retry_after": 1.0},
        {"kind": OutcomeKind.RETRY_AFTER},
        {"kind": OutcomeKind.RETRY_AFTER, "retry_after": -1.0},
        {"kind": OutcomeKind.RETRY_AFTER, "retry_after": float("nan")},
        {"kind": OutcomeKind.RETRY_AFTER, "retry_after": float("inf")},
        {"kind": OutcomeKind.RETRY_AFTER, "retry_after": 1},
        {"kind": OutcomeKind.RETRY_AFTER, "retry_after": True},
    ],
)
def test_malformed_outcomes_are_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="outcome"):
        Outcome(**dict({"kind": OutcomeKind.APPLIED}, **fields))


@given(st.sampled_from(["bad", "network", "timeout", "forbidden", "other"]), st.text(max_size=1000))
@settings(max_examples=200, deadline=None)
def test_arbitrary_telegram_error_text_matches_the_classification_table(
    kind: str, reason: str
) -> None:
    async def scenario() -> None:
        error: TelegramError
        if kind == "bad":
            error = BadRequest(reason)
            expected = (
                OutcomeKind.APPLIED
                if "message is not modified" in str(error).lower()
                else OutcomeKind.REJECTED
            )
        elif kind == "forbidden":
            error = Forbidden(reason)
            expected = OutcomeKind.REJECTED
        else:
            error = {"network": NetworkError, "timeout": TimedOut, "other": TelegramError}[kind](
                reason
            )
            expected = OutcomeKind.UNCERTAIN
        outcome = await attempt(fail(error))
        assert outcome.kind is expected
        assert outcome.reason == f"{type(error).__name__}: {error}"

    asyncio.run(scenario())


@pytest.mark.parametrize("shape", ["missing", "extra", "duplicate"])
def test_constructor_refuses_missing_extra_and_duplicate_fields(shape: str) -> None:
    factory: Any = Outcome
    args: tuple[Any, ...] = () if shape == "missing" else (OutcomeKind.APPLIED,)
    kwargs: dict[str, Any] = {}
    if shape == "extra":
        kwargs["unexpected"] = True
    elif shape == "duplicate":
        kwargs["kind"] = OutcomeKind.REJECTED
    with pytest.raises(TypeError):
        factory(*args, **kwargs)


@given(
    st.sampled_from(["kind", "message", "reason", "retry_after"]),
    st.one_of(st.integers(), st.booleans(), st.binary(), st.lists(st.integers(), max_size=3)),
)
@settings(max_examples=200, deadline=None)
def test_malformed_field_types_never_form_an_outcome(field: str, value: Any) -> None:
    values = {"kind": OutcomeKind.APPLIED, field: value}
    with pytest.raises(ValueError, match="outcome"):
        Outcome(**values)
