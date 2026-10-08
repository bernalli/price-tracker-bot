"""Bridge from stored product records to the product card and its Telegram keyboard.

`bot.ui` renders a `Screen` without knowing Telegram or the database; this module
builds the `ProductView` from a repository record, encodes the legacy callbacks
the card buttons trigger, and turns the `Screen` rows into an inline keyboard.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Protocol, get_args

from babel.numbers import list_currencies
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from price_tracker.app.views import (
    PAGE_SIZE,
    HomeView,
    ListFilter,
    ListPage,
    ProductStatus,
    ProductView,
    ThresholdType,
)
from price_tracker.bot.callbacks import Action, encode
from price_tracker.bot.decorators import _config, _db, _get_conversion_rate
from price_tracker.bot.messages import _
from price_tracker.bot.ui.cards import CardActions
from price_tracker.core.url_utils import extract_etld_plus_one

if TYPE_CHECKING:
    from collections.abc import Sequence

    from telegram.ext import ContextTypes

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


def _out_of_stock(record: _Record) -> bool:
    """Sold out: an active product whose last check found it unavailable.

    A paused product is never sold out: nothing checks it, so its availability is stale.
    """
    return bool(record.get("is_active")) and not record.get("is_available", True)


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
        out_of_stock=_out_of_stock(record),
    )


def card_actions(view: ProductView, *, back: str) -> CardActions:
    """The registered callbacks the card buttons trigger, and ``back`` to the list."""
    pid = view.id
    toggle = Action("product.pause" if view.status == "active" else "product.reactivate", (pid,))
    return CardActions(
        check=encode(Action("product.check", (pid,))),
        history=encode(Action("product.chart", (pid, "all"))),
        toggle=encode(toggle),
        delete=encode(Action("product.remove", (pid,))),
        alert_rule=encode(Action("product.edit", (pid,))),
        interval=encode(Action("product.interval", (pid,))),
        back=back,
    )


async def default_interval(context: ContextTypes.DEFAULT_TYPE) -> int:
    """The global check interval in minutes: the saved setting, else the configured one."""
    saved = await _db(context).get_config("check_interval_minutes")
    return int(saved) if saved and saved.isdigit() else _config(context).check_interval_minutes


def empty_list_text() -> str:
    """What the list says when the user tracks nothing at all."""
    return _("📭 Non hai prodotti tracciati.\nIncollami un link per iniziare!")


_FILTERS = {
    "a": lambda record: bool(record.get("is_active")),
    "p": lambda record: not record.get("is_active"),
    "e": lambda record: int(record.get("consecutive_errors") or 0) > 0,
    "o": _out_of_stock,
}


def list_view(
    records: Sequence[_Record],
    list_filter: ListFilter,
    page: int,
    *,
    default_interval_minutes: int,
) -> ListPage:
    """The ``page`` of the products matching ``list_filter``, clamped to the last page."""
    matching = [record for record in records if _FILTERS[list_filter](record)]
    pages = max(1, math.ceil(len(matching) / PAGE_SIZE))
    page = min(page, pages)
    shown = matching[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    items = tuple(product_view(r, default_interval_minutes=default_interval_minutes) for r in shown)
    return ListPage(list_filter, page, pages, len(matching), items)


async def home_view(db: Any, user_id: int) -> HomeView:
    """The Home counts of ``user_id`` and whether they are an admin."""
    stats = await db.get_stats(user_id)
    active = stats["active_products"]
    return HomeView(
        active=active,
        paused=stats["total_products"] - active,
        is_admin=await db.is_user_admin(user_id),
    )


async def reply_screen(message: Any, screen: Screen) -> None:
    """Send ``screen`` as a new message."""
    await message.reply_text(
        screen.text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=screen.disable_link_preview,
        reply_markup=screen_markup(screen),
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
