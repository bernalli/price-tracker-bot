"""Private helpers shared by handler modules.

Ported verbatim from monolithic bot.py [Task 17].
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, cast

from price_tracker.bot.messages import _

if TYPE_CHECKING:
    from telegram.ext import ContextTypes


def _format_relative_time(iso_ts: str | None, *, now: datetime | None = None) -> str | None:
    """Render an ISO timestamp as a compact "N min/h/d ago" string, or None.

    Normalises naive timestamps (the DB stores ``YYYY-MM-DD HH:MM:SS`` without
    tzinfo) to UTC before subtracting, so the comparison never raises the
    naive-vs-aware ``TypeError`` that previously hid the "Last check" line.
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
        return _("{n}min ago").format(n=int(seconds / 60))
    if seconds < 86400:
        return _("{n}h ago").format(n=int(seconds / 3600))
    return _("{n}d ago").format(n=int(seconds / 86400))


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
            raise ValueError(_("Invalid value: {value}").format(value=value)) from exc
        return ("percentage", value)
    value = text.replace(",", ".").replace("€", "").strip()
    try:
        Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(_("Invalid value: {value}").format(value=value)) from exc
    return ("absolute", value)


def _format_minutes(minutes: int) -> str:
    """Render an interval in minutes as a localized "N hours" / "N minutes"."""
    if minutes >= 60:
        hours = minutes / 60
        if hours == int(hours):
            return _("{n:.0f} hours").format(n=hours)
        return _("{n:.1f} hours").format(n=hours)
    return _("{n} minutes").format(n=minutes)


def _format_threshold(threshold_type: str, threshold_value: str) -> str:
    """Render a threshold tuple as a user-facing string."""
    if threshold_type == "any_drop":
        return _("\U0001f514 Every drop")
    if threshold_type == "percentage":
        return f"-{threshold_value}%"
    return f"-€{Decimal(threshold_value):.2f}"


def _parse_id(text: str) -> int | None:
    """Parse a product id from a string (accepts `#123` and `123`)."""
    try:
        return int(text.strip().replace("#", ""))
    except (ValueError, AttributeError):
        return None


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
    """Get a product ensuring it belongs to the user (admin sees all)."""
    from price_tracker.bot.decorators import _db

    db = _db(ctx)
    is_admin = await db.is_user_admin(user_id)
    if is_admin:
        return cast("dict[str, Any] | None", await db.get_product(product_id))
    return cast("dict[str, Any] | None", await db.get_product_for_user(product_id, user_id))


async def _get_product_name(db: Any, product_id: int) -> str:
    """Get product name by ID (truncated to 60 chars)."""
    product = await db.get_product(product_id)
    if product:
        return (product.get("name") or _("Unknown"))[:60]
    return _("Unknown")
