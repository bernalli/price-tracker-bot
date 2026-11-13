"""A Shopify check stops at the first refusal.

A refusal is a response that the block classifier recognises: HTTP 403, HTTP 429,
or a WAF or CAPTCHA body, whatever the status and whatever the final URL. On the
``.json`` endpoint and on the fallback page it ends the check with a block; on
the currency-only enrichment page it is swallowed and the price already read is
kept. No request follows a refusal in the same check.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx

from price_tracker.core.exceptions import BlockEvent, HTTPBlockStatus
from price_tracker.scrapers.shopify import ShopifyScraper

if TYPE_CHECKING:
    from collections.abc import Callable

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

# (id, status, Retry-After header or None, body)
REFUSALS: list[tuple[str, int, str | None, str]] = [
    ("403", 403, None, "forbidden"),
    ("429", 429, None, "too many"),
    ("429-60", 429, "60", "too many"),
    ("429-padded", 429, " 60 ", "too many"),
    ("429-text", 429, "abc", "too many"),
    ("429-negative", 429, "-1", "too many"),
    ("429-empty", 429, "", "too many"),
    ("429-huge", 429, "9" * 400, "too many"),
    ("429-far-date", 429, "Fri, 31 Dec 9999 23:59:59 GMT", "too many"),
    ("429-past-date", 429, "Wed, 21 Oct 2015 07:28:00 GMT", "too many"),
    ("200-waf", 200, None, WAF_BODY),
    ("200-captcha", 200, None, CAPTCHA_BODY),
    ("404-waf", 404, None, WAF_BODY),
]
REFUSAL_IDS = [refusal[0] for refusal in REFUSALS]


def _refusal(status: int, retry_after: str | None, body: str) -> httpx.Response:
    headers = {"Retry-After": retry_after} if retry_after is not None else {}
    return httpx.Response(status, headers=headers, text=body)


def _expected_header(retry_after: str | None) -> str | None:
    if retry_after is None:
        return None
    value = httpx.Headers({"Retry-After": retry_after}).get("Retry-After")
    return value if isinstance(value, str) else None


class Log:
    """The order of the requests one check made."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    def serve(self, factory: Callable[[], httpx.Response]) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            self.urls.append(str(request.url))
            return factory()

        return handler


@pytest.fixture(autouse=True)
def _no_retry_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tenacity.wait.wait_random_exponential.__call__", lambda self, retry_state: 0.0
    )


def _catch_all(router: respx.MockRouter) -> None:
    router.route().mock(side_effect=AssertionError("unexpected request"))


def _assert_block(exc: BlockEvent, status: int, retry_after: str | None, url: str) -> None:
    if status in (403, 429):
        assert isinstance(exc, HTTPBlockStatus)
        assert exc.status == status
        assert exc.retry_after == _expected_header(retry_after)
        assert exc.url == url


@pytest.mark.parametrize(("name", "status", "retry_after", "body"), REFUSALS, ids=REFUSAL_IDS)
async def test_a_refusal_on_the_json_endpoint_ends_the_check(
    name: str, status: int, retry_after: str | None, body: str
) -> None:
    log = Log()
    with respx.mock(assert_all_called=False) as router:
        router.get(JSON_URL).mock(
            side_effect=log.serve(lambda: _refusal(status, retry_after, body))
        )
        router.get(URL).mock(side_effect=log.serve(lambda: httpx.Response(200, text=PAGE)))
        _catch_all(router)
        async with httpx.AsyncClient() as client:
            with pytest.raises(BlockEvent) as exc_info:
                await ShopifyScraper().scrape(URL, client)
    _assert_block(exc_info.value, status, retry_after, JSON_URL)
    assert log.urls == [JSON_URL]


@pytest.mark.parametrize(("name", "status", "retry_after", "body"), REFUSALS, ids=REFUSAL_IDS)
async def test_a_refusal_on_the_fallback_page_ends_the_check(
    name: str, status: int, retry_after: str | None, body: str
) -> None:
    log = Log()
    with respx.mock(assert_all_called=False) as router:
        router.get(JSON_URL).mock(side_effect=log.serve(lambda: httpx.Response(404)))
        router.get(URL).mock(side_effect=log.serve(lambda: _refusal(status, retry_after, body)))
        _catch_all(router)
        async with httpx.AsyncClient() as client:
            with pytest.raises(BlockEvent) as exc_info:
                await ShopifyScraper().scrape(URL, client)
    _assert_block(exc_info.value, status, retry_after, URL)
    assert log.urls == [JSON_URL, URL]


@pytest.mark.parametrize(("name", "status", "retry_after", "body"), REFUSALS, ids=REFUSAL_IDS)
async def test_a_refusal_on_the_enrichment_page_keeps_the_price(
    name: str, status: int, retry_after: str | None, body: str
) -> None:
    log = Log()
    with respx.mock(assert_all_called=False) as router:
        router.get(JSON_URL).mock(
            side_effect=log.serve(lambda: httpx.Response(200, json=JSON_NO_CURRENCY))
        )
        router.get(URL).mock(side_effect=log.serve(lambda: _refusal(status, retry_after, body)))
        _catch_all(router)
        async with httpx.AsyncClient() as client:
            info = await ShopifyScraper().scrape(URL, client)
    assert info.price == Decimal("29.99")
    assert log.urls == [JSON_URL, URL]


async def test_a_refused_redirect_target_of_the_fallback_page_ends_the_check() -> None:
    moved = "https://shop.example.com/products/widget-new"
    log = Log()
    with respx.mock(assert_all_called=False) as router:
        router.get(JSON_URL).mock(side_effect=log.serve(lambda: httpx.Response(404)))
        router.get(URL).mock(
            side_effect=log.serve(lambda: httpx.Response(301, headers={"Location": moved}))
        )
        router.get(moved).mock(side_effect=log.serve(lambda: httpx.Response(429)))
        _catch_all(router)
        async with httpx.AsyncClient() as client:
            with pytest.raises(HTTPBlockStatus):
                await ShopifyScraper().scrape(URL, client)
    assert log.urls == [JSON_URL, URL, moved]


async def test_a_json_429_is_a_block_even_when_the_page_would_answer() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(JSON_URL).respond(429, text="too many")
        page = router.get(URL).respond(200, text=PAGE)
        _catch_all(router)
        async with httpx.AsyncClient() as client:
            with pytest.raises(HTTPBlockStatus) as exc_info:
                await ShopifyScraper().scrape(URL, client)
    assert exc_info.value.status == 429
    assert page.call_count == 0


async def test_a_json_server_error_still_reads_the_page() -> None:
    log = Log()
    with respx.mock(assert_all_called=False) as router:
        router.get(JSON_URL).mock(side_effect=log.serve(lambda: httpx.Response(500)))
        router.get(URL).mock(side_effect=log.serve(lambda: httpx.Response(200, text=PAGE)))
        _catch_all(router)
        async with httpx.AsyncClient() as client:
            info = await ShopifyScraper().scrape(URL, client)
    assert info.price == Decimal("199.00")
    assert log.urls == [JSON_URL, URL]


OTHER_JSON = "https://shop.example.com/products/other.json"


@pytest.mark.parametrize(("name", "status", "retry_after", "body"), REFUSALS, ids=REFUSAL_IDS)
async def test_a_refusal_after_a_redirect_precedes_the_identity_check(
    name: str, status: int, retry_after: str | None, body: str
) -> None:
    log = Log()
    with respx.mock(assert_all_called=False) as router:
        router.get(JSON_URL).mock(
            side_effect=log.serve(lambda: httpx.Response(302, headers={"Location": OTHER_JSON}))
        )
        router.get(OTHER_JSON).mock(
            side_effect=log.serve(lambda: _refusal(status, retry_after, body))
        )
        router.get(URL).mock(side_effect=log.serve(lambda: httpx.Response(200, text=PAGE)))
        _catch_all(router)
        async with httpx.AsyncClient() as client:
            with pytest.raises(BlockEvent) as exc_info:
                await ShopifyScraper().scrape(URL, client)
    _assert_block(exc_info.value, status, retry_after, OTHER_JSON)
    assert log.urls == [JSON_URL, OTHER_JSON]


async def test_an_ordinary_redirect_to_another_product_is_an_identity_error() -> None:
    log = Log()
    with respx.mock(assert_all_called=False) as router:
        router.get(JSON_URL).mock(
            side_effect=log.serve(lambda: httpx.Response(302, headers={"Location": OTHER_JSON}))
        )
        router.get(OTHER_JSON).mock(
            side_effect=log.serve(lambda: httpx.Response(200, json=JSON_PRICED))
        )
        router.get(URL).mock(side_effect=log.serve(lambda: httpx.Response(200, text=PAGE)))
        _catch_all(router)
        async with httpx.AsyncClient() as client:
            info = await ShopifyScraper().scrape(URL, client)
    assert info.price is None
    assert info.error == "Identità Shopify non corrispondente"
    assert log.urls == [JSON_URL, OTHER_JSON]
