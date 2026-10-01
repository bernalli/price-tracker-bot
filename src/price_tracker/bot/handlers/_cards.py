"""Bridge from stored product records to the product card and its Telegram keyboard.

`bot.ui` renders a `Screen` without knowing Telegram or the database; this module
builds the `ProductView` from a repository record, encodes the legacy callbacks
the card buttons trigger, and turns the `Screen` rows into an inline keyboard.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Protocol, get_args

from babel.numbers import list_currencies
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from price_tracker.app.views import ProductStatus, ProductView, ThresholdType
from price_tracker.bot.decorators import _get_conversion_rate
from price_tracker.bot.ui.cards import CardActions
from price_tracker.core.url_utils import extract_etld_plus_one

if TYPE_CHECKING:
    from price_tracker.bot.ui.screens import Screen


class _Record(Protocol):
    """A stored product row read by key: a ``ProductRecord`` or a plain mapping."""

    def get(self, key: str, default: Any = None, /) -> Any: ...

    def __getitem__(self, key: str, /) -> Any: ...


REFERENCE_CURRENCY = "EUR"
_AMOUNT_CEILING = Decimal(10) ** 15


def _amount(value: object) -> Decimal | None:
    """A stored price as a finite, non-negative Decimal below the card's ceiling, else None."""
    if value is None:
        return None
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not amount.is_finite() or amount.is_signed() or amount >= _AMOUNT_CEILING:
        return None
    return amount


def _currency(value: object) -> str:
    code = str(value or "").strip().upper()
    return code if code in list_currencies() else REFERENCE_CURRENCY


def _status(record: _Record) -> ProductStatus:
    if record.get("is_active"):
        return "active"
    return "suspended" if record.get("suspension_kind") == "automatic" else "paused"


def _threshold_type(value: object) -> ThresholdType:
    allowed: tuple[ThresholdType, ...] = get_args(ThresholdType)
    for candidate in allowed:
        if value == candidate:
            return candidate
    return "percentage"


def _checked_at(value: object) -> datetime | None:
    """The database's naive UTC timestamp (or ISO-8601 text) as an aware datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def product_view(record: _Record, *, default_interval_minutes: int) -> ProductView:
    """Build the card's view of a stored product; out-of-range stored values are dropped."""
    url = str(record.get("url") or "")
    currency = _currency(record.get("currency"))
    current = _amount(record.get("current_price"))
    estimate = None
    if current is not None and currency != REFERENCE_CURRENCY:
        rate = _get_conversion_rate(currency)
        if rate is not None:
            estimate = _amount((current * rate).quantize(Decimal("0.01")))
    interval = record.get("check_interval_minutes")
    return ProductView(
        id=int(record["id"]),
        name=str(record.get("name") or ""),
        url=url,
        domain=str(record.get("domain") or extract_etld_plus_one(url) or ""),
        currency=currency,
        current=current,
        initial=_amount(record.get("initial_price")),
        lowest=_amount(record.get("lowest_price")),
        target=_amount(record.get("target_price")),
        threshold_type=_threshold_type(record.get("threshold_type")),
        threshold_value=_amount(record.get("threshold_value")) or Decimal(0),
        status=_status(record),
        consecutive_errors=max(0, int(record.get("consecutive_errors") or 0)),
        check_interval_minutes=interval if isinstance(interval, int) and interval >= 1 else None,
        default_interval_minutes=max(1, default_interval_minutes),
        last_checked_at=_checked_at(record.get("last_checked_at")),
        reference_estimate=estimate,
        reference_currency=REFERENCE_CURRENCY,
    )


def card_actions(view: ProductView) -> CardActions:
    """The legacy callbacks the card buttons trigger for this product."""
    pid = view.id
    toggle = f"pause_{pid}" if view.status == "active" else f"reactivate_{pid}"
    return CardActions(
        check=f"check_{pid}",
        history=f"chart_{pid}",
        toggle=toggle,
        delete=f"remove_{pid}",
        alert_rule=f"edit_{pid}",
        interval=f"setrefresh_{pid}",
        back="menu_prodotti",
    )


def screen_markup(screen: Screen) -> InlineKeyboardMarkup:
    """The screen's button rows as a Telegram inline keyboard."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(button.label, url=button.url)
                if button.url is not None
                else InlineKeyboardButton(button.label, callback_data=button.callback)
                for button in row
            ]
            for row in screen.rows
        ]
    )
