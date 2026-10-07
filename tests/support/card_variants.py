"""The seven product-card variants shared by test_cards.py and test_snapshots.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from price_tracker.app.views import ProductView
from price_tracker.bot.callbacks import Action, encode
from price_tracker.bot.ui.cards import CardActions

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)

_DOMAIN = "shop.example.com"


def _url(product_id: int) -> str:
    return f"https://{_DOMAIN}/item/{product_id}"


def actions_for(view: ProductView) -> CardActions:
    """Real registry-encoded callback data for ``view``, built the way a caller would."""
    toggle_action = (
        Action("product.pause", (view.id,))
        if view.status == "active"
        else Action("product.reactivate", (view.id,))
    )
    return CardActions(
        check=encode(Action("product.check", (view.id,))),
        history=encode(Action("product.chart", (view.id, "30d"))),
        toggle=encode(toggle_action),
        delete=encode(Action("product.remove", (view.id,))),
        alert_rule=encode(Action("product.threshold", (view.id,))),
        interval=encode(Action("product.interval", (view.id,))),
        back=encode(Action("list.page", ("a", 1))),
    )


def _base() -> ProductView:
    return ProductView(
        id=42,
        name="Wireless Headphones X200 Pro Edition with Active Noise Cancelling and Case",
        url=_url(42),
        domain=_DOMAIN,
        currency="EUR",
        current=Decimal("1299.99"),
        initial=Decimal("1499.99"),
        lowest=Decimal("1249.00"),
        target=Decimal("1199.00"),
        threshold_type="percentage",
        threshold_value=Decimal("10"),
        status="active",
        consecutive_errors=0,
        check_interval_minutes=None,
        default_interval_minutes=360,
        last_checked_at=NOW - timedelta(hours=6),
        reference_estimate=None,
        reference_currency="EUR",
    )


def _paused() -> ProductView:
    return ProductView(
        id=43,
        name="Coffee Grinder G5",
        url=_url(43),
        domain=_DOMAIN,
        currency="EUR",
        current=Decimal("89.90"),
        initial=Decimal("88.05"),
        lowest=None,
        target=None,
        threshold_type="any_drop",
        threshold_value=Decimal("0"),
        status="paused",
        consecutive_errors=0,
        check_interval_minutes=None,
        default_interval_minutes=360,
        last_checked_at=NOW - timedelta(hours=3),
        reference_estimate=None,
        reference_currency="EUR",
    )


def _errors() -> ProductView:
    return ProductView(
        id=51,
        name="Running Shoes — size 44",
        url=_url(51),
        domain=_DOMAIN,
        currency="EUR",
        current=None,
        initial=Decimal("34.50"),
        lowest=Decimal("34.50"),
        target=None,
        threshold_type="absolute",
        threshold_value=Decimal("5.00"),
        status="active",
        consecutive_errors=3,
        check_interval_minutes=None,
        default_interval_minutes=360,
        last_checked_at=NOW - timedelta(days=2),
        reference_estimate=None,
        reference_currency="EUR",
    )


def _jpy_cjk() -> ProductView:
    return ProductView(
        id=47,
        name="電動歯ブラシ プレミアム",
        url=_url(47),
        domain=_DOMAIN,
        currency="JPY",
        current=Decimal("12800"),
        initial=Decimal("13500"),
        lowest=Decimal("12800"),
        target=None,
        threshold_type="percentage",
        threshold_value=Decimal("5"),
        status="active",
        consecutive_errors=0,
        check_interval_minutes=30,
        default_interval_minutes=360,
        last_checked_at=NOW - timedelta(minutes=12),
        reference_estimate=None,
        reference_currency="JPY",
    )


def _estimate() -> ProductView:
    return ProductView(
        id=60,
        name="Desk Lamp",
        url=_url(60),
        domain=_DOMAIN,
        currency="USD",
        current=Decimal("1299.99"),
        initial=Decimal("1299.99"),
        lowest=Decimal("1199.5"),
        target=Decimal("999.99"),
        threshold_type="target",
        threshold_value=Decimal("999.99"),
        status="active",
        consecutive_errors=0,
        check_interval_minutes=None,
        default_interval_minutes=1440,
        last_checked_at=NOW - timedelta(minutes=90),
        reference_estimate=Decimal("1180.00"),
        reference_currency="EUR",
    )


def _hostile() -> ProductView:
    name = '<b>&"Deal"</b> 👨\u200d👩\u200d👧 \u202eRTL\u202c line\nbreak <script>x</script> ' + (
        "e\u0301" * 40
    )
    return ProductView(
        id=9_223_372_036_854_775_807,
        name=name,
        url="",
        domain=_DOMAIN,
        currency="EUR",
        current=Decimal("0.5"),
        initial=Decimal("0.5"),
        lowest=None,
        target=None,
        threshold_type="percentage",
        threshold_value=Decimal("10"),
        status="suspended",
        consecutive_errors=12,
        check_interval_minutes=10080,
        default_interval_minutes=360,
        last_checked_at=None,
        reference_estimate=None,
        reference_currency="EUR",
    )


def _just_now() -> ProductView:
    return ProductView(
        id=7,
        name="Kettle",
        url=_url(7),
        domain=_DOMAIN,
        currency="EUR",
        current=Decimal("64.00"),
        initial=Decimal("80.00"),
        lowest=Decimal("64.00"),
        target=Decimal("60.00"),
        threshold_type="percentage",
        threshold_value=Decimal("10"),
        status="active",
        consecutive_errors=0,
        check_interval_minutes=None,
        default_interval_minutes=360,
        last_checked_at=NOW - timedelta(seconds=30),
        reference_estimate=None,
        reference_currency="EUR",
    )


VARIANTS: dict[str, ProductView] = {
    "base": _base(),
    "paused": _paused(),
    "errors": _errors(),
    "jpy_cjk": _jpy_cjk(),
    "estimate": _estimate(),
    "hostile": _hostile(),
    "just_now": _just_now(),
}
