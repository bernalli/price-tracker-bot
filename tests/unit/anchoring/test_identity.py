"""Requested identity and echo comparison (I3)."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.identity import (
    IdentityCheck,
    RequestedIdentity,
    check_echoes,
    normalize_url,
)

A = "https://shop.example.com/p/widget-a"
B = "https://shop.example.com/p/widget-b"
REQUESTED = RequestedIdentity.from_url(A)


def test_redirect_to_other_product_is_mismatch() -> None:
    """``/p/A -> /p/B`` with a page fully consistent with B: the echoes never replace A."""
    check = check_echoes(REQUESTED, final_url=B, canonical=B, og_url=B)
    assert check.error_code == "identity_mismatch"
    assert A in check.error
    assert B in check.error
    assert check_echoes(REQUESTED, final_url=A, canonical=A, og_url=A).ok  # control


def test_canonical_to_other_product_without_redirect_is_mismatch() -> None:
    check = check_echoes(REQUESTED, canonical=B)
    assert check.error_code == "identity_mismatch"
    assert "canonical" in check.error
    assert check_echoes(REQUESTED, og_url=B).error_code == "identity_mismatch"


def test_slug_redirect_accepted_only_through_canonical() -> None:
    moved = "https://shop.example.com/it/p/widget-a-new-slug"
    assert check_echoes(REQUESTED, final_url=moved, canonical=A).ok
    assert check_echoes(REQUESTED, final_url=moved).error_code == "identity_mismatch"
    assert check_echoes(REQUESTED, final_url=moved, canonical=moved).error_code == (
        "identity_mismatch"
    )


def test_redirect_off_the_registrable_domain_is_mismatch_even_with_canonical() -> None:
    elsewhere = "https://other.example.net/p/widget-a"
    check = check_echoes(REQUESTED, final_url=elsewhere, canonical=A)
    assert check.error_code == "identity_mismatch"
    assert elsewhere in check.error
    same_site = "https://www.shop.example.com/p/widget-a"  # other host, same registrable domain
    assert check_echoes(REQUESTED, final_url=same_site, canonical=A).ok


def test_relative_canonical_is_resolved_against_the_final_url() -> None:
    assert check_echoes(REQUESTED, final_url=A, canonical="/p/widget-a").ok
    assert check_echoes(REQUESTED, final_url=A, canonical="widget-b").error_code == (
        "identity_mismatch"
    )


def test_declared_echo_absent_is_identity_missing() -> None:
    check = check_echoes(REQUESTED, final_url=A, required=frozenset({"canonical"}))
    assert check == IdentityCheck("identity_missing", "required echo canonical is absent")
    assert check_echoes(REQUESTED, final_url=A, canonical=A, required=frozenset({"canonical"})).ok


@pytest.mark.parametrize("bad", ["", "   ", "javascript:alert(1)", "http://[::1", "ftp://x/y"])
def test_unreadable_echo_is_mismatch(bad: str) -> None:
    assert check_echoes(REQUESTED, canonical=bad).error_code == "identity_mismatch"
    assert check_echoes(REQUESTED, og_url=bad).error_code == "identity_mismatch"
    assert check_echoes(REQUESTED, final_url=bad).error_code == "identity_mismatch"


def test_normalisation() -> None:
    assert normalize_url("HTTPS://Shop.Example.COM:443/p/widget-a/?utm_source=x&b=2&a=1#r") == (
        "https://shop.example.com/p/widget-a?a=1&b=2"
    )
    assert normalize_url("http://shop.example.com:80/") == "http://shop.example.com"
    assert normalize_url("https://shop.example.com:8443/p") == "https://shop.example.com:8443/p"
    assert normalize_url("https://shop.example.com/p?gclid=1&fbclid=2&ref=3&tag=4") == (
        "https://shop.example.com/p?ref=3&tag=4"
    )
    for bad in (None, 3, "", "/p/a", "mailto:x@example.com", "https://user:pw@example.com/p"):
        assert normalize_url(bad) is None
    assert normalize_url("https://example.com:99999/p") is None
    assert normalize_url("https://example.com/p\nq") is None
    assert normalize_url("https://example.com/" + "a" * 5000) is None


def test_requested_identity_rejects_unusable_urls() -> None:
    for bad in ("/p/a", "https://user@example.com/p", "https://localhost/p"):
        with pytest.raises(ValueError, match="requested URL"):
            RequestedIdentity.from_url(bad)


segments = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-", min_size=1, max_size=8)
params = st.lists(st.tuples(segments, segments), max_size=4, unique_by=lambda p: p[0])


@settings(max_examples=400, deadline=None)
@given(st.lists(segments, min_size=1, max_size=4), params, st.randoms(use_true_random=False))
def test_normalisation_is_idempotent_and_distinct_keys_order_free(
    path: list[str], query: list[tuple[str, str]], rnd: object
) -> None:
    import random

    assert isinstance(rnd, random.Random)
    base = "https://shop.example.com/" + "/".join(path)
    shuffled = list(query)
    rnd.shuffle(shuffled)
    tracking = [("utm_campaign", "x"), ("gclid", "y")]
    url_a = base + ("?" + "&".join(f"{k}={v}" for k, v in query) if query else "")
    both = [*shuffled, *tracking]
    rnd.shuffle(both)
    url_b = base.upper().replace("/" + "/".join(path).upper(), "/" + "/".join(path))
    url_b = url_b + "/?" + "&".join(f"{k}={v}" for k, v in both) + "#frag"
    first = normalize_url(url_a)
    assert first is not None
    assert normalize_url(first) == first
    assert normalize_url(url_b) == first


_f7_a = "https://shop.example.com/p/A"
_f7_requested = RequestedIdentity.from_url(_f7_a)


@settings(max_examples=80, database=None)
@given(
    st.sampled_from([chr(i) for i in range(33)] + [chr(127)]),
    st.integers(min_value=0, max_value=len(_f7_a)),
)
def test_echo_rejects_raw_controls(char, index):
    bad = _f7_a[:index] + char + _f7_a[index:]
    assert _f7_requested.resolve(bad) is None
    assert check_echoes(_f7_requested, canonical=bad).error_code == "identity_mismatch"
    assert check_echoes(_f7_requested, canonical=_f7_a).ok


@pytest.mark.parametrize("auth", ["", ":", "u", "u:p"])
def test_echo_rejects_all_userinfo(auth):
    bad = "https://" + auth + "@shop.example.com/p/A"
    assert normalize_url(bad) is None
    assert not check_echoes(_f7_requested, canonical=bad).ok


@pytest.mark.parametrize(
    ("requested_query", "candidate_query", "reason"),
    [
        ("ref=A", "ref=B", "foreign_only"),
        ("tag=A", "tag=B", "foreign_only"),
        ("id=A&id=B", "id=B&id=A", "foreign_only"),
        ("a=1&b=2", "b=2&a=1", "owned"),
        ("a=1", "a=1&utm_source=feed", "owned"),
    ],
    ids=["ref", "tag", "duplicate_order", "distinct_order", "utm"],
)
def test_query_identity_ownership(requested_query, candidate_query, reason):
    import json
    from decimal import Decimal

    from price_tracker.core.anchoring import AnchorState, Money
    from price_tracker.core.structured_data import anchor_structured_page

    base = "https://shop.example.com/product?"
    requested = RequestedIdentity.from_url(base + requested_query)
    document = {
        "@type": "Product",
        "url": base + candidate_query,
        "offers": {"price": "9.99", "priceCurrency": "EUR"},
    }
    result = anchor_structured_page([json.dumps(document)], requested)
    assert result.reason == reason
    if reason == "owned":
        assert result.state is AnchorState.FOUND
        assert result.price == Money(Decimal("9.99"), "EUR")
    else:
        assert result.state is AnchorState.AMBIGUOUS
        assert result.price is None


@settings(max_examples=400, deadline=None)
@given(st.lists(segments, min_size=2, max_size=6, unique=True))
def test_normalisation_preserves_duplicate_key_order(values):
    base = "https://shop.example.com/product?"
    pairs = "&".join("id=" + value for value in values)
    expected = base + "a=1&" + pairs + "&z=2"
    assert normalize_url(base + "z=2&" + pairs + "&a=1") == expected
    assert normalize_url(expected) == expected
    reversed_pairs = "&".join("id=" + value for value in reversed(values))
    assert normalize_url(base + reversed_pairs) == base + reversed_pairs
    assert normalize_url(base + reversed_pairs) != base + pairs
