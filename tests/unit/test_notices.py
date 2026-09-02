"""Tests for operational-notice aggregation data."""

from __future__ import annotations

from decimal import Decimal

import pytest

from price_tracker.core import notices
from price_tracker.core.notices import (
    OPS_DELETE_CONFIRM_PREFIX,
    OPS_DELETE_PREFIX,
    OPS_REACTIVATE_PREFIX,
    NoticeCollector,
    NoticeGroup,
    OperationalEvent,
    group_key_for,
)


def _event(
    *,
    event: str = "suspended",
    user_id: int = 1,
    product_id: int = 1,
    url: str = "https://shop.example.com/products/1",
    reason: str | None = "parse_error",
) -> OperationalEvent:
    return OperationalEvent(
        event=event,  # type: ignore[arg-type]
        user_id=user_id,
        product_id=product_id,
        product_name=f"Product {product_id}",
        url=url,
        group_key=group_key_for(url),
        reason=reason,
        detail=None,
        last_error=None,
        error_count=5,
        max_errors=10,
        last_price=Decimal("12.50"),
        currency="EUR",
        last_checked_at=None,
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://a.example.com/p", "example.com"),
        ("https://shop.example/p", "shop.example"),
        ("", "unknown"),
        ("not a url", "unknown"),
        ("http://localhost/x", "localhost"),
        ("https://a.example.co.uk/p", "example.co.uk"),
        ("http://127.0.0.1/x", "127.0.0.1"),
        ("//fallback.example/path", "fallback.example"),
        ("http://[::1", "unknown"),
        (b"https://example.com", "unknown"),
        (None, "unknown"),
    ],
)
def test_group_key_for_never_raises(url: object, expected: str) -> None:
    assert group_key_for(url) == expected


def test_notice_constants_and_empty_anchor_contract() -> None:
    assert OPS_REACTIVATE_PREFIX == "ops_react_"
    assert OPS_DELETE_PREFIX == "ops_del_"
    assert OPS_DELETE_CONFIRM_PREFIX == "ops_delok_"

    empty = NoticeGroup("suspended", 1, "example.com", ())
    with pytest.raises(ValueError, match="at least one"):
        _ = empty.anchor_product_id


def test_collector_deduplicates_by_event_and_product_with_last_value_winning() -> None:
    collector = NoticeCollector()
    collector.add(_event(reason="parse_error"))
    collector.add(_event(reason="listing_gone"))
    collector.add(_event(event="warning", reason="http_error"))

    assert len(collector) == 2
    assert collector.groups()[0].events[0].reason == "listing_gone"


def test_groups_are_deterministic_and_anchor_is_the_lowest_product_id() -> None:
    collector = NoticeCollector()
    collector.add(_event(user_id=2, product_id=7, url="https://b.example.com/7"))
    collector.add(_event(user_id=1, product_id=9, url="https://z.example.org/9"))
    collector.add(_event(user_id=1, product_id=3, url="https://a.example.com/3"))
    collector.add(_event(user_id=1, product_id=1, url="https://b.example.com/1"))
    collector.add(_event(user_id=1, product_id=8, url="https://a.example.com/8"))

    groups = collector.groups()
    assert [(group.user_id, group.event, group.group_key) for group in groups] == [
        (1, "suspended", "example.com"),
        (1, "suspended", "example.org"),
        (2, "suspended", "example.com"),
    ]
    assert [event.product_id for event in groups[0].events] == [1, 3, 8]
    assert groups[0].anchor_product_id == 1


def test_primary_reason_uses_frequency_then_alphabetical_order() -> None:
    collector = NoticeCollector()
    collector.add(_event(product_id=1, reason="listing_gone"))
    collector.add(_event(product_id=2, reason="listing_gone"))
    collector.add(_event(product_id=3, reason="parse_error"))
    assert collector.groups()[0].primary_reason == "listing_gone"

    tie = NoticeCollector()
    tie.add(_event(product_id=1, reason="parse_error"))
    tie.add(_event(product_id=2, reason="listing_gone"))
    assert tie.groups()[0].primary_reason == "listing_gone"

    unknown = NoticeCollector()
    unknown.add(_event(reason="zzz"))
    assert unknown.groups()[0].primary_reason == "zzz"
    assert NoticeCollector().groups() == []


@pytest.mark.parametrize(
    ("reasons", "expected"),
    [
        ([None], "unknown"),
        ([None, "zzz"], "unknown"),
        ([None, "listing_gone"], "listing_gone"),
    ],
)
def test_primary_reason_normalizes_none_before_tie_breaking(
    reasons: list[str | None], expected: str
) -> None:
    collector = NoticeCollector()
    for product_id, reason in enumerate(reasons, 1):
        collector.add(_event(product_id=product_id, reason=reason))

    assert collector.groups()[0].primary_reason == expected


def test_group_key_for_stays_total_when_the_resolver_raises_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Grouping must be total: any resolver failure degrades to ``unknown``.

    A narrower ``except ValueError`` would let this escape and break the whole
    sweep's notice rendering, so the guard is asserted against a non-ValueError.
    """

    def _explode(url: str) -> str:
        raise RuntimeError("resolver is unavailable")

    monkeypatch.setattr(notices, "extract_etld_plus_one", _explode)

    assert group_key_for("https://shop.example/p") == "unknown"
