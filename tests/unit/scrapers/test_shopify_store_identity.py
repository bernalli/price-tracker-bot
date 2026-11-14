"""Every store request carries the store client's one Chrome identity, set by no scraper.

Scraper code sets no User-Agent, client hints or fake Referer: on a plain HTTPX
client its requests show HTTPX's own User-Agent, and on the store client the
Chrome wire supplies the whole identity.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import socket
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import respx

from price_tracker.core import scraper_base, store_wire
from price_tracker.core.exceptions import BlockEvent, HTTPBlockStatus
from price_tracker.core.http_client import build_store_client
from price_tracker.core.scraper_base import get_headers
from price_tracker.core.store_wire import STORE_ACCEPT_LANGUAGE, STORE_IMPERSONATE
from price_tracker.scrapers import amazon as amazon_module
from price_tracker.scrapers.amazon import AmazonScraper
from price_tracker.scrapers.shopify import ShopifyScraper
from tests.support.store_wire_fake import FakeCurlResponse, FakeSession, FakeWire
from tests.unit.test_public_connections import Network

if TYPE_CHECKING:
    from collections.abc import Callable

URL = "https://shop.example.com/products/widget"
JSON_URL = f"{URL}.json"
MOVED = "https://shop.example.com/products/widget-new"
OTHER_JSON = "https://shop.example.com/products/other.json"
PAGE = (
    '<html><head><script>var meta = {"product":{"id":1,"vendor":"Shop",'
    '"handle":"widget","variants":['
    '{"id":11,"price":19900,"price_currency":"EUR","name":"Widget","sku":"A1"}]},'
    '"page":{"pageType":"product"}};</script></head><body></body></html>'
)
JSON_PRICED = {
    "product": {
        "handle": "widget",
        "title": "Widget",
        "variants": [{"id": 1, "price": "29.99", "price_currency": "EUR"}],
    }
}
JSON_NO_CURRENCY = {
    "product": {"handle": "widget", "title": "Widget", "variants": [{"id": 1, "price": "29.99"}]}
}


def _json_priced(router: respx.MockRouter) -> None:
    router.get(JSON_URL).respond(200, json=JSON_PRICED)


def _json_then_enrichment(router: respx.MockRouter) -> None:
    router.get(JSON_URL).respond(200, json=JSON_NO_CURRENCY)
    router.get(URL).respond(200, text=PAGE)


def _json_404_then_page(router: respx.MockRouter) -> None:
    router.get(JSON_URL).respond(404)
    router.get(URL).respond(200, text=PAGE)


def _json_404_then_moved_page(router: respx.MockRouter) -> None:
    router.get(JSON_URL).respond(404)
    router.get(URL).respond(301, headers={"Location": MOVED})
    router.get(MOVED).respond(200, text=PAGE)


def _json_404_then_refused_page(router: respx.MockRouter) -> None:
    router.get(JSON_URL).respond(404)
    router.get(URL).respond(429)


def _json_redirect(router: respx.MockRouter) -> None:
    router.get(JSON_URL).respond(302, headers={"Location": OTHER_JSON})
    router.get(OTHER_JSON).respond(200, json=JSON_PRICED)


PATHS: dict[str, Callable[[respx.MockRouter], None]] = {
    "json-priced": _json_priced,
    "json-enrichment": _json_then_enrichment,
    "json-404-page": _json_404_then_page,
    "json-404-page-301": _json_404_then_moved_page,
    "json-404-page-429": _json_404_then_refused_page,
    "json-302": _json_redirect,
}


async def _requests(path: str) -> list[httpx.Request]:
    with respx.mock(assert_all_called=False) as router:
        PATHS[path](router)
        router.route().mock(side_effect=AssertionError("unexpected request"))
        async with httpx.AsyncClient() as client:
            with contextlib.suppress(BlockEvent):
                await ShopifyScraper().scrape(URL, client)
        return [call.request for call in router.calls]


@pytest.mark.parametrize("path", sorted(PATHS))
async def test_scraper_code_sets_no_user_agent(path: str) -> None:
    requests = await _requests(path)

    assert requests
    for request in requests:
        assert request.headers["user-agent"].startswith("python-httpx/")
        assert "price-tracker-bot" not in request.headers["user-agent"]


async def test_store_client_check_uses_one_chrome_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wire = FakeWire()
    wire.script = [
        FakeCurlResponse(
            headers=[("Content-Type", "application/json")],
            body=json.dumps(JSON_NO_CURRENCY).encode(),
        ),
        FakeCurlResponse(
            headers=[("Content-Type", "text/html; charset=utf-8")], body=PAGE.encode()
        ),
    ]

    def session_factory(**kwargs: Any) -> FakeSession:
        session = wire.open(kwargs["curl_options"])
        session.kwargs = kwargs
        return session

    monkeypatch.setattr(store_wire, "AsyncSession", session_factory)
    monkeypatch.setattr(socket, "getaddrinfo", Network().resolve)
    async with build_store_client() as client:
        info = await ShopifyScraper().scrape(URL, client)
        jar_empty = not client.cookies
    assert info.price is not None
    assert len(wire.sessions) == 2
    expected = [("Accept-Language", STORE_ACCEPT_LANGUAGE)]
    for session in wire.sessions:
        assert session.kwargs["impersonate"] == STORE_IMPERSONATE
        (request,) = session.requests
        headers = request["headers"]
        if jar_empty:
            assert headers == expected
        else:
            assert headers[: len(expected)] == expected
            assert [name for name, _ in headers[len(expected) :]] == ["Cookie"]


@pytest.mark.parametrize("status", [403, 429])
async def test_damaged_json_refusal_stops_after_one_request(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, status: int
) -> None:
    wire = FakeWire()
    wire.script = [
        FakeCurlResponse(
            status_code=status,
            headers=[("Content-Encoding", "gzip"), ("Retry-After", "60")],
            body=gzip.compress(b'{"errors":"local_rate_limited"}')[:-1],
        ),
        # Any fallback would consume this and fail the refusal assertion below.
        FakeCurlResponse(body=PAGE.encode()),
    ]
    monkeypatch.setattr(store_wire, "_open_session", wire.open)
    monkeypatch.setattr(socket, "getaddrinfo", Network().resolve)
    async with build_store_client() as client:
        with pytest.raises(HTTPBlockStatus) as exc:
            await ShopifyScraper().scrape(URL, client)
    assert exc.value.status == status
    assert exc.value.retry_after == "60"
    assert exc.value.url == JSON_URL
    (session,) = wire.sessions
    (request,) = session.requests
    assert request["url"] == JSON_URL
    refusals = [record for record in caplog.records if "store refusal " in record.getMessage()]
    assert len(refusals) == 1


async def test_amazon_requests_carry_no_fake_headers() -> None:
    page_url = "https://www.amazon.it/dp/X"
    with respx.mock(assert_all_called=False) as router:
        route = router.get(page_url).respond(200, text="<html></html>")
        async with httpx.AsyncClient() as client:
            await amazon_module._fetch_amazon_html(page_url, client)
    (call,) = route.calls
    assert "referer" not in call.request.headers
    assert call.request.headers["user-agent"].startswith("python-httpx/")

    short_url = "https://amzn.eu/d/abc"
    with respx.mock(assert_all_called=False) as router:
        head = router.head(short_url).respond(200)
        page = router.get(short_url).respond(200, text="<html></html>")
        async with httpx.AsyncClient() as client:
            await AmazonScraper().scrape(short_url, client)
    for request in [call.request for call in head.calls] + [c.request for c in page.calls]:
        assert "referer" not in request.headers
        assert request.headers["user-agent"].startswith("python-httpx/")
    assert head.calls


def test_identity_constants_are_gone() -> None:
    for name in ("USER_AGENTS", "DECLARED_USER_AGENT", "PROJECT_URL"):
        assert not hasattr(scraper_base, name), name
    assert not hasattr(amazon_module, "_fetch_with_fresh_client")
    assert get_headers() == {}
    extra = {"A": "b"}
    headers = get_headers(extra)
    assert headers == {"A": "b"}
    headers["A"] = "changed"
    assert extra == {"A": "b"}
