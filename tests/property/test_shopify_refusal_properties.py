"""Properties of the Shopify first-refusal rule and of the Retry-After parser.

Whatever the ``.json`` endpoint and the product page answer, a check makes no
request after the first refusal, and ends with a ``ProductInfo``, a
``BlockEvent`` or a ``ListingGone``. The Retry-After parser never raises.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core.domain_spacing import (
    MAX_DEFERRAL_SECONDS,
    DomainSpacing,
    parse_retry_after,
)
from price_tracker.core.exceptions import BlockEvent, HTTPBlockStatus, ListingGone
from price_tracker.core.scraper_base import ProductInfo
from price_tracker.scrapers.shopify import ShopifyScraper

URL = "https://shop.example.com/products/widget"
JSON_URL = f"{URL}.json"
WAF_BODY = "<html><title>Just a moment...</title></html>"
CAPTCHA_BODY = '<html><body><form id="captcha-form"></form></body></html>'
JSON_PRICED = {
    "product": {
        "handle": "widget",
        "title": "Widget",
        "variants": [{"id": 1, "price": "29.99", "price_currency": "EUR"}],
    }
}
JSON_NO_CURRENCY = {
    "product": {
        "handle": "widget",
        "title": "Widget",
        "variants": [{"id": 1, "price": "29.99"}],
    }
}
PAGE = (
    '<html><head><script>var meta = {"product":{"id":1,"vendor":"Shop",'
    '"handle":"widget","variants":['
    '{"id":11,"price":19900,"price_currency":"EUR","name":"Widget","sku":"A1"}]},'
    '"page":{"pageType":"product"}};</script></head><body></body></html>'
)
OUTCOMES = (
    "403",
    "429",
    "404",
    "500",
    "priced",
    "priced_no_currency",
    "waf",
    "captcha",
    "timeout",
)
REFUSALS = frozenset({"403", "429", "waf", "captcha"})

_HEADER = st.one_of(
    st.none(),
    st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E) | st.just("\t"),
        max_size=40,
    ),
    st.integers(min_value=0).map(str),
)


def _response(outcome: str, kind: str, retry_after: str | None) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    if outcome in ("403", "429", "404", "500"):
        return httpx.Response(int(outcome), headers=headers, text="no")
    if outcome == "waf":
        return httpx.Response(200, headers=headers, text=WAF_BODY)
    if outcome == "captcha":
        return httpx.Response(200, headers=headers, text=CAPTCHA_BODY)
    if kind == "json":
        payload = JSON_PRICED if outcome == "priced" else JSON_NO_CURRENCY
        return httpx.Response(200, headers=headers, json=payload)
    return httpx.Response(200, headers=headers, text=PAGE)


def _check(
    json_script: list[str], page_script: list[str], retry_after: str | None
) -> tuple[list[str], object]:
    served: list[str] = []
    scripts = {"json": list(json_script), "page": list(page_script)}

    def serve(kind: str) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            script = scripts[kind]
            outcome = script.pop(0) if len(script) > 1 else script[0]
            served.append(outcome)
            if outcome == "timeout":
                raise httpx.ConnectTimeout("timed out", request=request)
            return _response(outcome, kind, retry_after)

        return handler

    async def scenario() -> object:
        with respx.mock(assert_all_called=False) as router:
            router.get(JSON_URL).mock(side_effect=serve("json"))
            router.get(URL).mock(side_effect=serve("page"))
            router.route().mock(side_effect=AssertionError("unexpected request"))
            async with httpx.AsyncClient() as client:
                try:
                    return await ShopifyScraper().scrape(URL, client)
                except (BlockEvent, ListingGone) as exc:
                    return exc

    return served, asyncio.run(scenario())


def _assert_stops_at_first_refusal(served: list[str], result: object) -> None:
    assert isinstance(result, ProductInfo | BlockEvent | ListingGone)
    first = next((i for i, outcome in enumerate(served) if outcome in REFUSALS), None)
    if first is not None:
        assert first == len(served) - 1, served


_SCRIPT = st.lists(st.sampled_from(OUTCOMES), min_size=1, max_size=4)


@given(json_script=_SCRIPT, page_script=_SCRIPT, retry_after=_HEADER)
@settings(max_examples=150, deadline=None)
def test_no_request_follows_the_first_refusal(
    json_script: list[str], page_script: list[str], retry_after: str | None
) -> None:
    patch = pytest.MonkeyPatch()
    patch.setattr("tenacity.wait.wait_random_exponential.__call__", lambda self, state: 0.0)
    try:
        served, result = _check(json_script, page_script, retry_after)
    finally:
        patch.undo()
    _assert_stops_at_first_refusal(served, result)


@pytest.mark.parametrize("retry_after", ["9" * 400, "9" * 10000], ids=["400-nines", "10k-nines"])
def test_huge_retry_after_headers_are_carried_verbatim(
    retry_after: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("tenacity.wait.wait_random_exponential.__call__", lambda self, s: 0.0)
    served, result = _check(["429"], ["priced"], retry_after)
    assert served == ["429"]
    assert isinstance(result, HTTPBlockStatus)
    assert result.retry_after == retry_after


def test_duplicate_retry_after_headers_do_not_raise() -> None:
    async def scenario() -> object:
        with respx.mock(assert_all_called=False) as router:
            router.get(JSON_URL).respond(
                429, headers=[("Retry-After", "60"), ("Retry-After", "120")], text="no"
            )
            router.route().mock(side_effect=AssertionError("unexpected request"))
            async with httpx.AsyncClient() as client:
                try:
                    return await ShopifyScraper().scrape(URL, client)
                except BlockEvent as exc:
                    return exc

    result = asyncio.run(scenario())
    assert isinstance(result, HTTPBlockStatus)
    assert isinstance(result.retry_after, str)
    spacing = DomainSpacing(clock=lambda: 0.0)
    spacing.defer("example.com", result.retry_after)
    assert 0.0 <= spacing.deferred_for("example.com") <= MAX_DEFERRAL_SECONDS


# --- the Retry-After parser ----------------------------------------------------

_ANY = st.one_of(
    st.none(),
    st.text(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.binary(max_size=10),
    st.lists(st.integers(), max_size=3),
    st.dictionaries(st.text(max_size=3), st.integers(), max_size=2),
)
_NOW = st.datetimes(
    min_value=datetime(1971, 1, 1),
    max_value=datetime(9000, 1, 1),
    timezones=st.just(UTC),
)


@given(value=_ANY, now=_NOW)
@settings(max_examples=200, deadline=None)
def test_the_parser_never_raises(value: object, now: datetime) -> None:
    result = parse_retry_after(value, now=now)  # type: ignore[arg-type]
    assert result is None or (isinstance(result, float) and result >= 0)
    if result is not None and math.isinf(result):
        assert isinstance(value, str)
        assert value.strip().isdigit()


@given(
    values=st.lists(st.one_of(st.none(), st.text(max_size=30), _HEADER), min_size=1, max_size=6),
    advances=st.lists(st.floats(0, 10_000), min_size=6, max_size=6),
)
@settings(max_examples=200, deadline=None)
def test_a_deferral_is_finite_bounded_and_never_shortened(
    values: list[str | None], advances: list[float]
) -> None:
    clock = [0.0]
    spacing = DomainSpacing(
        clock=lambda: clock[0], wall_clock=lambda: datetime(2026, 11, 13, tzinfo=UTC)
    )
    for value, advance in zip(values, advances, strict=False):
        before = spacing.deferred_for("example.com")
        spacing.defer("example.com", value)
        after = spacing.deferred_for("example.com")
        assert math.isfinite(after)
        # The clock is a float: allow its rounding, nothing more.
        assert 0.0 <= after <= MAX_DEFERRAL_SECONDS + 1e-6
        assert after >= before - 1e-6
        clock[0] += advance
