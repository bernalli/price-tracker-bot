"""The list pages rendered by test_list_page.py, and the stored records they are built from."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from price_tracker.app.views import ListPage, ProductStatus, ProductView
from price_tracker.bot.ui.cards import list_page

if TYPE_CHECKING:
    from collections.abc import Callable

    from price_tracker.bot.ui.screens import Screen

HOSTILE = "<b>&\"'‮‍\n</b> " + "x" * 80


def view(
    product_id: int,
    name: str = "Kettle",
    *,
    status: ProductStatus = "active",
    errors: int = 0,
    current: str | None = "19.99",
    currency: str = "EUR",
) -> ProductView:
    """A product as the list shows it."""
    return ProductView(
        id=product_id,
        name=name,
        url=f"https://shop.example.com/item/{product_id}",
        domain="shop.example.com",
        currency=currency,
        current=None if current is None else Decimal(current),
        initial=None,
        lowest=None,
        target=None,
        threshold_type="any_drop",
        threshold_value=Decimal(0),
        status=status,
        consecutive_errors=errors,
        check_interval_minutes=None,
        default_interval_minutes=360,
        last_checked_at=None,
        reference_estimate=None,
        reference_currency="EUR",
    )


def record(product_id: int, **fields: Any) -> dict[str, Any]:
    """A stored product row, as the repository returns it, with ``fields`` overriding."""
    row: dict[str, Any] = {
        "id": product_id,
        "name": f"Item {product_id}",
        "url": f"https://shop.example.com/item/{product_id}",
        "domain": "shop.example.com",
        "currency": "EUR",
        "current_price": "10.00",
        "is_active": 1,
        "suspension_kind": None,
        "consecutive_errors": 0,
    }
    row.update(fields)
    return row


MID = ListPage(
    filter="a",
    page=2,
    pages=3,
    total=12,
    items=(
        view(6, "Kettle"),
        view(7, "Fan", errors=2),
        view(8, "Lamp", current=None),
        view(9, "Heater", currency="CHF", current="1299.50"),
        view(10, "Wireless Headphones X200 Pro Edition with Active Noise Cancelling and Case"),
    ),
)

HOSTILE_PAGE = ListPage(
    filter="p",
    page=1,
    pages=1,
    total=3,
    items=(
        view(1, HOSTILE, status="paused"),
        view(2, HOSTILE, status="suspended", errors=1),
        view(3, "⁧" + HOSTILE),
    ),
)

SCREENS: dict[str, Callable[[], Screen]] = {
    "mid": lambda: list_page(MID),
    "hostile": lambda: list_page(HOSTILE_PAGE),
}
