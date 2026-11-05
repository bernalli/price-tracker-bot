"""Classify a single Telegram request without retrying uncertain delivery."""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from datetime import timedelta
from enum import Enum
from typing import TYPE_CHECKING, Final

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
from telegram.warnings import PTBDeprecationWarning

if TYPE_CHECKING:
    from collections.abc import Awaitable

NOT_MODIFIED: Final = "message is not modified"


class OutcomeKind(Enum):
    """What a Bot API request did, as far as the bot can know."""

    APPLIED = "applied"
    REJECTED = "rejected"
    RETRY_AFTER = "retry_after"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class Outcome:
    """The classified result of one Bot API request."""

    kind: OutcomeKind
    message: Message | None = None
    retry_after: float | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        valid_delay = (
            (
                isinstance(self.retry_after, float)
                and math.isfinite(self.retry_after)
                and self.retry_after >= 0
            )
            if self.kind is OutcomeKind.RETRY_AFTER
            else self.retry_after is None
        )
        if (
            not isinstance(self.kind, OutcomeKind)
            or not (self.message is None or isinstance(self.message, Message))
            or not isinstance(self.reason, str)
            or not valid_delay
        ):
            raise ValueError("invalid outcome")


def _retry_seconds(exc: RetryAfter) -> float:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", PTBDeprecationWarning)
        value = exc.retry_after
    seconds = value.total_seconds() if isinstance(value, timedelta) else float(value)
    return max(0.0, seconds)


async def attempt(request: Awaitable[object]) -> Outcome:
    """Await one Bot API request and classify its result; Telegram errors never escape."""
    try:
        result = await request
    except RetryAfter as exc:
        return Outcome(
            OutcomeKind.RETRY_AFTER,
            retry_after=_retry_seconds(exc),
            reason=f"{type(exc).__name__}: {exc}",
        )
    except BadRequest as exc:
        kind = OutcomeKind.APPLIED if NOT_MODIFIED in str(exc).lower() else OutcomeKind.REJECTED
        return Outcome(kind, reason=f"{type(exc).__name__}: {exc}")
    except (Forbidden, InvalidToken, ChatMigrated, Conflict, EndPointNotFound) as exc:
        return Outcome(OutcomeKind.REJECTED, reason=f"{type(exc).__name__}: {exc}")
    except (TimedOut, NetworkError) as exc:
        return Outcome(OutcomeKind.UNCERTAIN, reason=f"{type(exc).__name__}: {exc}")
    except TelegramError as exc:
        return Outcome(OutcomeKind.UNCERTAIN, reason=f"{type(exc).__name__}: {exc}")
    return Outcome(OutcomeKind.APPLIED, message=result if isinstance(result, Message) else None)
