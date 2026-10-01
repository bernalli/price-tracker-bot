"""Private helpers shared by handler modules.

Split out of the original monolithic bot.py module, verbatim.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Final, cast

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

# Largest value an SQLite INTEGER holds; product and user ids never exceed it.
ID_MAX: Final = 9_223_372_036_854_775_807
_ID_RE: Final = re.compile(r"#?([0-9]{1,19})")


def _format_relative_time(iso_ts: str | None, *, now: datetime | None = None) -> str | None:
    """Render an ISO timestamp as a compact "Nmin/h/g fa" string, or None.

    Normalises naive timestamps (the DB stores ``YYYY-MM-DD HH:MM:SS`` without
    tzinfo) to UTC before subtracting, so the comparison never raises the
    naive-vs-aware ``TypeError`` that previously hid the "Ultimo check" line.
    """
    if not iso_ts:
        return None
    try:
        checked = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=UTC)
    reference = now or datetime.now(UTC)
    seconds = (reference - checked).total_seconds()
    if seconds < 3600:
        return f"{int(seconds / 60)}min fa"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h fa"
    return f"{int(seconds / 86400)}g fa"


def _escape_html(text: str) -> str:
    """HTML-escape for Telegram parse_mode=HTML messages."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _parse_threshold_input(text: str) -> tuple[str, str]:
    """Parse a user threshold string into (type, value).

    Returns (`any_drop`, `0`) for sentinel words, (`percentage`, `<n>`) for
    `<n>%`, or (`absolute`, `<n>`) for plain numerics.
    """
    text = text.strip().lstrip("-")
    if text.lower() in ("ogni", "any", "sempre", "all"):
        return ("any_drop", "0")
    if text.endswith("%"):
        value = text.rstrip("%").strip()
        try:
            Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"Valore non valido: {value}") from exc
        return ("percentage", value)
    value = text.replace(",", ".").replace("€", "").strip()
    try:
        Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"Valore non valido: {value}") from exc
    return ("absolute", value)


def _format_threshold(threshold_type: str, threshold_value: str) -> str:
    """Render a threshold tuple as a user-facing string."""
    if threshold_type == "any_drop":
        return "\U0001f514 Ogni ribasso"
    if threshold_type == "percentage":
        return f"-{threshold_value}%"
    return f"-€{Decimal(threshold_value):.2f}"


def _parse_id(text: str) -> int | None:
    """Parse a product or user id: `123` or `#123`, ASCII digits, 1..ID_MAX.

    Anything else (whitespace, signs, separators, non-ASCII digits, zero or an
    overflow) returns None.
    """
    match = _ID_RE.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        return None
    value = int(match.group(1))
    return value if 1 <= value <= ID_MAX else None


def _safe_dec(value: object) -> Decimal | None:
    """Best-effort Decimal conversion; returns None on failure."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


async def _get_user_product(
    ctx: ContextTypes.DEFAULT_TYPE, product_id: int, user_id: int
) -> dict[str, Any] | None:
    """Get a product an active user may see: their own, or any one for an admin.

    A deactivated user sees nothing, admin or not, whatever gate the caller ran.
    """
    from price_tracker.bot.decorators import _db

    db = _db(ctx)
    if not await db.is_user_allowed(user_id):
        return None
    is_admin = await db.is_user_admin(user_id)
    if is_admin:
        return cast("dict[str, Any] | None", await db.get_product(product_id))
    return cast("dict[str, Any] | None", await db.get_product_for_user(product_id, user_id))
