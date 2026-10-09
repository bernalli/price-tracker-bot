"""Every callback button is registry data; old wires remain input-only."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from price_tracker.app.views import HomeView, ListPage, ProductView
from price_tracker.bot.callbacks import Action, InvalidCallback, decode, encode
from price_tracker.bot.handlers._cards import card_actions
from price_tracker.bot.keyboards import build_threshold_keyboard, menu_back_button
from price_tracker.bot.ui.cards import list_page, product_card
from price_tracker.bot.ui.panels import home_screen


def _assert_registry_data(values: list[str]) -> None:
    rejected = [value for value in values if isinstance(decode(value), InvalidCallback)]
    assert rejected == []


def _screen_callbacks(screen: object) -> list[str]:
    return [
        button.callback
        for row in screen.rows  # type: ignore[attr-defined]
        for button in row
        if button.callback is not None
    ]


def test_every_button_factory_emits_registry_callback_data() -> None:
    view = ProductView(
        id=42,
        name="Widget",
        url="https://example.com/widget",
        domain="example.com",
        currency="EUR",
        current=Decimal("10"),
        initial=Decimal("12"),
        lowest=Decimal("9"),
        target=None,
        threshold_type="percentage",
        threshold_value=Decimal("10"),
        status="active",
        consecutive_errors=0,
        check_interval_minutes=None,
        default_interval_minutes=60,
        last_checked_at=None,
        reference_estimate=None,
        reference_currency="EUR",
        out_of_stock=False,
    )
    actions = card_actions(view, back=encode(Action("list.page", ("a", 1))))
    card = product_card(view, actions, now=datetime(2026, 1, 1, tzinfo=UTC))
    page = list_page(ListPage("a", 1, 1, 2, (view, view)))
    home = home_screen(HomeView(active=1, paused=0, is_admin=True))
    telegram_buttons = [
        *[button for row in build_threshold_keyboard(42).inline_keyboard for button in row],
        *menu_back_button(),
    ]

    _assert_registry_data(
        [*_screen_callbacks(card), *_screen_callbacks(page), *_screen_callbacks(home)]
        + [
            str(button.callback_data)
            for button in telegram_buttons
            if button.callback_data is not None
        ]
    )


LEGACY_CASES = {
    "pause_42": Action("product.pause", (42,)),
    "remove_42": Action("product.remove", (42,)),
    "reactivate_42": Action("product.reactivate", (42,)),
    "confirm_delete_42": Action("product.remove_ok", (42,)),
    "cancel_delete": Action("delete.cancel"),
    "edit_42": Action("product.edit", (42,)),
    "chart_42": Action("product.chart", (42, "all")),
    "reset_42": Action("product.reset", (42,)),
    "track_threshold_42": Action("product.threshold", (42,)),
    "track_target_42": Action("product.target", (42,)),
    "track_any_42": Action("product.threshold_any", (42,)),
    "track_default_42": Action("product.threshold_default", (42,)),
    "pref_new_42": Action("product.offer_filter", (42, "n")),
    "pref_used_42": Action("product.offer_filter", (42, "u")),
    "pref_amazon_42": Action("product.offer_filter", (42, "s1")),
    "pref_anyseller_42": Action("product.offer_filter", (42, "s0")),
    "pref_default_42": Action("product.offer_filter", (42, "0")),
    "menu_main": Action("home"),
    "menu_prodotti": Action("products"),
    "menu_paused": Action("paused"),
    "menu_prezzi": Action("prices"),
    "menu_checkall": Action("check_all"),
    "menu_storia": Action("history"),
    "menu_notifiche": Action("notifications"),
    "menu_dati": Action("data"),
    "menu_esporta": Action("data.export"),
    "menu_importa_info": Action("data.import"),
    "menu_info": Action("stats"),
}


@pytest.mark.parametrize(("wire", "expected"), LEGACY_CASES.items())
def test_each_legacy_prefix_still_resolves_to_the_same_action(wire: str, expected: Action) -> None:
    from price_tracker.bot.handlers.callbacks._legacy import decode_legacy

    assert decode_legacy(wire) == expected
