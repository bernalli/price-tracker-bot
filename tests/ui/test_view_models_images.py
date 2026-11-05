"""Canonical image data and filter counts at the view boundary."""

from dataclasses import replace
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.app import views
from price_tracker.app.views import ListPage
from price_tracker.bot.handlers._cards import list_view, product_view
from price_tracker.core.image_url import MAX_IMAGE_URL_LENGTH, normalize_image_url
from tests.support.list_variants import record, view


@pytest.mark.parametrize("url", [None, "https://cdn.example.com/photo.jpg"])
def test_the_view_accepts_canonical_image_urls(url: str | None) -> None:
    assert replace(view(1), image_url=url).image_url == url


@pytest.mark.parametrize(
    "url",
    [
        "http://cdn.example.com/x",
        "javascript:alert(1)",
        "https://example.com/" + "x" * 2029,
        123,
        True,
        [],
        {},
        "https://u:p@example.com/x",
        "https://127.0.0.1/x",
        "https://example.com:444/x",
        "https://bad_host/x",
        "https://example.com/x#fragment",
        "https://u:p@127.0.0.1/x",
        "https://EXAMPLE.com/x",
        "https://example.com:443/x",
    ],
)
def test_the_view_refuses_noncanonical_image_data(url: Any) -> None:
    with pytest.raises(ValueError, match="image_url:"):
        replace(view(1), image_url=url)


@settings(max_examples=200, deadline=None)
@given(st.text())
def test_view_image_validation_matches_the_shared_normalizer(url: str) -> None:
    if normalize_image_url(url, base_url="") == url:
        assert replace(view(1), image_url=url).image_url == url
    else:
        with pytest.raises(ValueError, match="image_url:"):
            replace(view(1), image_url=url)


def test_image_length_limits_agree() -> None:
    assert views.MAX_IMAGE_URL_LENGTH == MAX_IMAGE_URL_LENGTH == 2048


@pytest.mark.parametrize(
    "counts",
    [
        (1, 0, 0),
        (1, 0, 0, 0, 0),
        (-1, 0, 0, 0),
        (True, 0, 0, 0),
        (1.0, 0, 0, 0),
        ("1", 0, 0, 0),
        (2, 0, 0, 0),
        [1, 0, 0, 0],
        123,
    ],
)
def test_invalid_counts_are_refused(counts: Any) -> None:
    with pytest.raises(ValueError, match="counts:"):
        ListPage("a", 1, 1, 1, (view(1),), counts=counts)


def test_counts_are_optional_and_follow_filter_order() -> None:
    assert ListPage("a", 1, 1, 0, ()).counts is None
    filters: tuple[views.ListFilter, ...] = ("a", "p", "e", "o")
    assert filters == views.LIST_FILTER_ORDER
    for index, key in enumerate(filters):
        counts = (1, 2, 3, 4)
        assert ListPage(key, 1, 1, counts[index], (), counts=counts).counts == counts


@pytest.mark.parametrize(
    "url", ["http://example.com/x", 123, True, [], {}, "https://EXAMPLE.com/x"]
)
def test_bad_stored_image_data_is_dropped(url: Any) -> None:
    assert product_view(record(1, image_url=url), default_interval_minutes=60).image_url is None


def test_canonical_stored_image_data_is_preserved() -> None:
    url = "https://cdn.example.com/x.jpg"
    assert product_view(record(1, image_url=url), default_interval_minutes=60).image_url == url


@settings(max_examples=200, deadline=None)
@given(st.lists(st.tuples(st.booleans(), st.integers(0, 10), st.booleans()), max_size=35))
def test_filter_counts_and_six_item_slices_match_the_records(
    flags: list[tuple[bool, int, bool]],
) -> None:
    records = [
        record(i, is_active=a, consecutive_errors=e, is_available=o)
        for i, (a, e, o) in enumerate(flags, 1)
    ]
    wanted = (
        [i for i, (a, _, _) in enumerate(flags, 1) if a],
        [i for i, (a, _, _) in enumerate(flags, 1) if not a],
        [i for i, (_, e, _) in enumerate(flags, 1) if e > 0],
        [i for i, (a, _, o) in enumerate(flags, 1) if a and not o],
    )
    counts = tuple(len(ids) for ids in wanted)
    filters: tuple[views.ListFilter, ...] = ("a", "p", "e", "o")
    for key, ids in zip(filters, wanted, strict=True):
        for asked in (1, 2, 4, 100):
            page = list_view(records, key, asked, default_interval_minutes=60)
            shown = min(asked, max(1, (len(ids) + 5) // 6))
            assert page.counts == counts
            assert (page.total, page.page) == (len(ids), shown)
            assert [item.id for item in page.items] == ids[(shown - 1) * 6 : shown * 6]
