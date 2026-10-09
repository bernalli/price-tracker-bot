"""Shared connection base, constants, and row conversions for repository mixins."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from price_tracker.db.models import ProductRecord, UserRecord

if TYPE_CHECKING:
    import aiosqlite


class _RepositoryBase:
    """Store the SQLite connection shared by all repository responsibilities."""

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn


def _parse_ts(value: str | None) -> datetime | None:
    """Parse an ISO-format timestamp from DB into a timezone-aware datetime.

    SQLite CURRENT_TIMESTAMP writes naive strings ('2026-05-09 20:30:00').
    Fields written via .isoformat() from UTC-aware datetimes are already aware.
    Always attaches UTC when the parsed value is naive so callers receive a
    consistent type and comparisons never raise TypeError.
    """
    if value is None:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _dec(value: object) -> Decimal | None:
    """Safely convert a DB value to Decimal."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


def _dec_str(value: Decimal | None) -> str | None:
    """Convert Decimal to string for DB storage."""
    return str(value) if value is not None else None


_PRODUCT_COLS = (
    "id, user_id, url, name, domain, initial_price, current_price, "
    "lowest_price, highest_price, target_price, threshold_type, "
    "threshold_value, is_active, is_available, consecutive_errors, "
    "currency, check_interval_minutes, last_checked_at, last_notified_at, "
    "pending_alert_price, pending_alert_at, preferred_condition, preferred_seller, "
    "pending_read_price, pending_read_count, pending_read_streak, last_error, "
    "last_error_at, gone_streak, suspension_kind, suspension_reason"
)


def _row_to_product(row: tuple[Any, ...]) -> ProductRecord:
    # Default only on NULL — a stored 0 (e.g. 0% threshold / any_drop) is valid
    # and must not be coalesced away by a falsy `or` (#24).
    threshold_value = _dec(row[11])
    if threshold_value is None:
        threshold_value = Decimal("10")
    return ProductRecord(
        id=row[0],
        user_id=row[1],
        url=row[2],
        name=row[3],
        domain=row[4],
        initial_price=_dec(row[5]),
        current_price=_dec(row[6]),
        lowest_price=_dec(row[7]),
        highest_price=_dec(row[8]),
        target_price=_dec(row[9]),
        threshold_type=row[10],
        threshold_value=threshold_value,
        is_active=bool(row[12]),
        is_available=bool(row[13]),
        consecutive_errors=int(row[14]),
        currency=row[15],
        check_interval_minutes=row[16],
        last_checked_at=row[17],
        last_notified_at=row[18],
        pending_alert_price=_dec(row[19]),
        pending_alert_at=row[20],
        preferred_condition=row[21],
        preferred_seller=row[22],
        pending_read_price=_dec(row[23]),
        pending_read_count=int(row[24] or 0),
        pending_read_streak=int(row[25] or 0),
        last_error=row[26],
        last_error_at=row[27],
        gone_streak=int(row[28] or 0),
        suspension_kind=row[29],
        suspension_reason=row[30],
    )


_TELEGRAM_TAG_RE = re.compile(r"[A-Za-z0-9_-]{1,35}")
_USER_COLS = "user_id, is_admin, is_active, display_name, username, language, telegram_language_tag"


def _row_to_user(row: Any) -> UserRecord:
    return UserRecord(
        user_id=row[0],
        is_admin=_is_one(row[1]),
        is_active=_is_one(row[2]),
        display_name=row[3],
        username=row[4],
        language=row[5],
        telegram_language_tag=row[6],
    )


def _is_one(value: object) -> bool:
    """True only for the integer 1: the flag columns have no CHECK, so anything else is false."""
    return type(value) is int and value == 1
