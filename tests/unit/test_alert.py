"""Tests for alert formatting and threshold trigger logic."""

from __future__ import annotations

import gettext
import html
from decimal import Decimal
from xml.etree import ElementTree

import pytest

from price_tracker.core.alert import (
    PriceAlert,
    crosses_threshold,
    format_alert,
    format_error_notification,
    format_operational_notice,
    format_warning_notice,
    operational_buttons,
)
from price_tracker.core.notices import NoticeGroup, OperationalEvent
from price_tracker.core.textlimits import (
    DOMAIN_BUDGET,
    ERROR_BUDGET,
    NAME_BUDGET,
    split_message,
    truncate_visible,
    visible_length,
)


def _operational_event(
    *,
    product_id: int = 1,
    reason: str = "listing_gone",
    name: str = "Widget",
    domain: str = "example.com",
    detail: str | None = "HTTP 404",
    last_error: str | None = "listing_gone: HTTP 404",
    last_price: Decimal | None = None,
    last_checked_at: str | None = None,
    event: str = "suspended",
) -> OperationalEvent:
    return OperationalEvent(
        event=event,  # type: ignore[arg-type]
        user_id=9,
        product_id=product_id,
        product_name=name,
        url=f"https://{domain}/p/{product_id}",
        group_key=domain,
        reason=reason,
        detail=detail,
        last_error=last_error,
        error_count=10,
        max_errors=10,
        last_price=last_price,
        currency="EUR",
        last_checked_at=last_checked_at,
    )


def _group(*events: OperationalEvent) -> NoticeGroup:
    return NoticeGroup(
        event=events[0].event,
        user_id=events[0].user_id,
        group_key=events[0].group_key,
        events=tuple(sorted(events, key=lambda item: item.product_id)),
    )


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide only the two translated strings required by this rendering test."""
    import price_tracker.bot.messages as messages

    class _FakeTranslations(gettext.NullTranslations):
        def gettext(self, message: str) -> str:
            return {"Listings removed on {domain}": "Prodotti rimossi da {domain}"}.get(
                message, message
            )

    monkeypatch.setattr(
        messages,
        "get_translation",
        lambda lang_code: _FakeTranslations() if lang_code == "it" else gettext.NullTranslations(),
    )


def test_crosses_threshold_percentage_drop():
    assert (
        crosses_threshold(
            old=Decimal("100"),
            new=Decimal("89"),
            threshold_type="percentage",
            threshold_value=Decimal("10"),
        )
        is True
    )


def test_crosses_threshold_percentage_no_trigger():
    assert (
        crosses_threshold(
            old=Decimal("100"),
            new=Decimal("95"),
            threshold_type="percentage",
            threshold_value=Decimal("10"),
        )
        is False
    )


def test_crosses_threshold_absolute_drop():
    assert (
        crosses_threshold(
            old=Decimal("100"),
            new=Decimal("89"),
            threshold_type="absolute",
            threshold_value=Decimal("10"),
        )
        is True
    )


def test_crosses_threshold_target_price():
    assert (
        crosses_threshold(
            old=Decimal("100"),
            new=Decimal("89"),
            threshold_type="target",
            threshold_value=Decimal("90"),
        )
        is True
    )


def test_crosses_threshold_target_price_above():
    assert (
        crosses_threshold(
            old=Decimal("100"),
            new=Decimal("91"),
            threshold_type="target",
            threshold_value=Decimal("90"),
        )
        is False
    )


def test_crosses_threshold_any_drop_triggers_on_any_decrease():
    """`any_drop` (sentinel, threshold_value 0) must fire on any price decrease."""
    assert (
        crosses_threshold(
            old=Decimal("100"),
            new=Decimal("99.99"),
            threshold_type="any_drop",
            threshold_value=Decimal("0"),
        )
        is True
    )


def test_crosses_threshold_any_drop_no_trigger_when_not_lower():
    """`any_drop` must NOT fire when the price is unchanged or higher."""
    assert (
        crosses_threshold(
            old=Decimal("100"),
            new=Decimal("100"),
            threshold_type="any_drop",
            threshold_value=Decimal("0"),
        )
        is False
    )


def test_crosses_threshold_no_drop():
    assert (
        crosses_threshold(
            old=Decimal("100"),
            new=Decimal("110"),
            threshold_type="percentage",
            threshold_value=Decimal("10"),
        )
        is False
    )


def test_format_alert_includes_drop_percentage():
    alert = PriceAlert(
        product_id=1,
        product_name="Test Widget",
        url="https://example.com/p/1",
        old_price=Decimal("100"),
        new_price=Decimal("80"),
        currency="EUR",
        threshold_type="percentage",
        threshold_value=Decimal("10"),
    )
    text = format_alert(alert)
    assert "Test Widget" in text
    assert "100" in text
    assert "80" in text
    assert "20" in text or "20%" in text
    assert "EUR" in text or "€" in text


def test_format_alert_escapes_html():
    alert = PriceAlert(
        product_id=1,
        product_name="<script>",
        url="https://example.com/",
        old_price=Decimal("100"),
        new_price=Decimal("80"),
        currency="EUR",
        threshold_type="percentage",
        threshold_value=Decimal("10"),
    )
    text = format_alert(alert)
    assert "<script>" not in text
    assert "&lt;script&gt;" in text


def test_format_error_notification_mentions_count():
    text = format_error_notification(
        product={"id": "1", "name": "Widget", "url": "https://x"},
        error_count=10,
        max_errors=10,
    )
    assert "Widget" in text
    assert "10" in text


def test_operational_notice_listing_gone_copy_and_buttons() -> None:
    group = _group(
        *[_operational_event(product_id=index, name=f"Widget {index}") for index in range(1, 6)]
    )

    text = format_operational_notice(group)
    buttons = operational_buttons(group)

    assert "Listings removed on example.com" in text
    assert "(5)" in text
    assert all(f"Widget {index}" in text for index in range(1, 6))
    assert "HTTP 404" in text
    assert "Delete" in buttons[0][0]["text"]
    assert buttons[0][0]["callback_data"] == "ops_del_1"


def test_operational_notice_parse_error_puts_reactivate_first() -> None:
    group = _group(_operational_event(reason="parse_error"))

    text = format_operational_notice(group)
    buttons = operational_buttons(group)

    assert "Price unreadable on example.com" in text
    assert "Reactivate" in buttons[0][0]["text"]


def test_operational_notice_unknown_reason_uses_default_copy() -> None:
    text = format_operational_notice(_group(_operational_event(reason="new_failure")))

    assert "Tracking suspended on example.com" in text
    assert "Checks kept failing." in text
    assert "check failed" in text


def test_operational_notice_last_good_read_and_missing() -> None:
    read = _operational_event(last_price=Decimal("19.95"), last_checked_at="2026-09-02 12:34:56")
    missing = _operational_event(product_id=2)
    text = format_operational_notice(_group(read, missing))

    assert "Last good read: 19.95 € on 2026-09-02 12:34 UTC" in text
    assert "No successful read yet" in text


def test_operational_notice_escapes_html_and_handles_missing_last_error() -> None:
    text = format_operational_notice(
        _group(_operational_event(name="<unsafe>", last_error=None, detail="<404>"))
    )

    assert "<unsafe>" not in text
    assert "&lt;unsafe&gt;" in text
    assert "<code>unknown</code>" in text


def test_operational_notice_keeps_user_values_inside_one_html_row() -> None:
    text = format_operational_notice(
        _group(
            _operational_event(
                name="Widget\nInjected",
                domain="shop\n.example",
                last_error="failure\nline",
            )
        )
    )

    assert "Widget Injected" in text
    assert "shop .example" in text
    assert "failure line" in text
    for line in text.splitlines():
        assert line.count("<b>") == line.count("</b>")
        assert line.count("<code>") == line.count("</code>")


def test_long_alert_with_newline_never_splits_inside_html_tag() -> None:
    alert = PriceAlert(
        product_id=1,
        product_name="a" * 3984 + "\n" + "z",
        url="https://example.com",
        old_price=Decimal("2"),
        new_price=Decimal("1"),
        currency="EUR",
        threshold_type="any_drop",
        threshold_value=Decimal("0"),
    )

    chunks = split_message(format_alert(alert))

    assert len(chunks) > 1
    assert all(visible_length(chunk) <= 4000 for chunk in chunks)
    for chunk in chunks:
        ElementTree.fromstring(f"<root>{chunk}</root>")


def test_operational_notice_budgets_cap_and_balanced_html() -> None:
    domain = "d" * 150
    group = _group(
        *[
            _operational_event(
                product_id=index,
                name="n" * 200,
                domain=domain,
                detail="x" * 500,
                last_error="e" * 300,
            )
            for index in range(1, 51)
        ]
    )

    text = format_operational_notice(group)

    assert text.count("• ") == 10
    assert "and 40 more" in text
    assert visible_length(text) <= 4000
    assert "n" * 61 not in text
    assert "e" * 121 not in text
    for line in text.splitlines():
        assert line.count("<b>") == line.count("</b>")
        assert line.count("<code>") == line.count("</code>")


def test_operational_notice_single_product_reads_fine() -> None:
    text = format_operational_notice(_group(_operational_event()))
    assert "(1)" in text
    assert "Widget" in text


def test_warning_notice_format_and_has_no_buttons() -> None:
    group = _group(_operational_event(event="warning", reason="http_error"))

    text = format_warning_notice(group)

    assert "10/10" in text
    assert "/errori" in text
    assert operational_buttons(group) == []


@pytest.mark.parametrize(
    ("reason", "title", "callbacks"),
    [
        ("listing_gone", "Listings removed on", ["ops_del_1", "ops_react_1"]),
        ("parse_error", "Price unreadable on", ["ops_react_1", "ops_del_1"]),
        ("price_none", "Price unreadable on", ["ops_react_1", "ops_del_1"]),
        ("no_scraper", "Price unreadable on", ["ops_react_1", "ops_del_1"]),
        ("condition_mismatch", "Price unreadable on", ["ops_react_1", "ops_del_1"]),
        ("implausible_read", "Price unreadable on", ["ops_react_1", "ops_del_1"]),
        ("http_error", "Site unreachable:", ["ops_react_1", "ops_del_1"]),
        ("unexpected", "Site unreachable:", ["ops_react_1", "ops_del_1"]),
        ("block", "Blocked by", ["ops_react_1", "ops_del_1"]),
        ("unknown_reason", "Tracking suspended on", ["ops_react_1", "ops_del_1"]),
    ],
)
def test_closed_reason_copy_and_exact_callback_contract(
    reason: str, title: str, callbacks: list[str]
) -> None:
    group = _group(_operational_event(reason=reason))

    assert title in format_operational_notice(group)
    assert [row[0]["callback_data"] for row in operational_buttons(group)] == callbacks


def test_renderer_rejects_empty_and_wrong_event_groups() -> None:
    suspended_empty = NoticeGroup("suspended", 9, "example.com", ())
    warning_empty = NoticeGroup("warning", 9, "example.com", ())

    with pytest.raises(ValueError, match="at least one"):
        format_operational_notice(suspended_empty)
    with pytest.raises(ValueError, match="at least one"):
        format_warning_notice(warning_empty)
    with pytest.raises(ValueError, match="suspended"):
        format_operational_notice(_group(_operational_event(event="warning")))
    with pytest.raises(ValueError, match="warning"):
        format_warning_notice(_group(_operational_event()))


def test_every_external_field_is_truncated_then_escaped() -> None:
    external = "&<>" * 100
    text = format_operational_notice(
        _group(
            _operational_event(
                name=external,
                domain=external,
                last_error=external,
            )
        )
    )

    assert html.escape(truncate_visible(external, NAME_BUDGET), quote=True) in text
    assert html.escape(truncate_visible(external, DOMAIN_BUDGET), quote=True) in text
    assert html.escape(truncate_visible(external, ERROR_BUDGET), quote=True) in text
    assert html.escape(external, quote=True) not in text
    ElementTree.fromstring(f"<root>{text}</root>")


def test_price_and_translated_why_have_exact_visible_budgets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import price_tracker.core.alert as alert_module

    monkeypatch.setattr(
        alert_module,
        "_",
        lambda text: "w" * 100 if text == "blocked" else text,
    )
    text = format_operational_notice(
        _group(
            _operational_event(
                reason="block",
                last_price=Decimal("9" * 100),
                last_checked_at="2026-09-02 12:34:56",
            )
        )
    )

    product_line = next(line for line in text.splitlines() if line.startswith("• "))
    assert product_line.endswith("w" * 39 + "…")
    price_line = next(line for line in text.splitlines() if line.startswith("Last good read:"))
    rendered_price = price_line.removeprefix("Last good read: ").rsplit(" on ", 1)[0].rstrip()
    assert visible_length(rendered_price) == 24
    assert rendered_price.endswith("…")


def test_operational_notice_it_locale(fake_catalog: None) -> None:
    from price_tracker.bot.messages import set_locale

    set_locale("it")
    text = format_operational_notice(_group(_operational_event()))
    assert "Prodotti rimossi da example.com" in text
