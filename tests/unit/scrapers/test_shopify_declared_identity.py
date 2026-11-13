"""Every request of a Shopify check carries one declared, stable User-Agent.

The bot names itself, its version and its public repository; it sends no
browser navigation headers and never rotates its identity. Other scrapers keep
their own headers.
"""

from __future__ import annotations

import contextlib
import re
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

import price_tracker
from price_tracker.core.exceptions import BlockEvent
from price_tracker.core.scraper_base import DECLARED_USER_AGENT, USER_AGENTS
from price_tracker.scrapers import amazon as amazon_module
from price_tracker.scrapers.shopify import ShopifyScraper

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
BROWSER_HEADERS = (
    "sec-fetch-dest",
    "sec-fetch-mode",
    "sec-fetch-site",
    "sec-fetch-user",
    "upgrade-insecure-requests",
    "dnt",
    "cache-control",
)
PAGE_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
)


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


def test_the_declared_user_agent_names_the_bot_and_its_version() -> None:
    match = re.fullmatch(
        r"price-tracker-bot/(\d+\.\d+\.\d+) \(\+https://github\.com/bernalli/price-tracker-bot\)",
        DECLARED_USER_AGENT,
    )
    assert match is not None
    assert match.group(1) == price_tracker.__version__


@pytest.mark.parametrize("path", sorted(PATHS))
async def test_every_request_carries_the_declared_identity(path: str) -> None:
    requests = await _requests(path)

    assert requests
    for request in requests:
        assert request.headers["user-agent"] == DECLARED_USER_AGENT
        assert "Mozilla" not in request.headers["user-agent"]
        for name in BROWSER_HEADERS:
            assert name not in request.headers, (name, str(request.url))


async def test_the_identity_never_rotates(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object) -> object:
        raise AssertionError("the declared identity must not rotate")

    monkeypatch.setattr("price_tracker.core.scraper_base.random.choice", forbidden)
    agents: set[str] = set()
    for index in range(20):
        path = sorted(PATHS)[index % len(PATHS)]
        agents.update(request.headers["user-agent"] for request in await _requests(path))
    assert agents == {DECLARED_USER_AGENT}


async def test_the_header_sets_are_closed() -> None:
    requests = await _requests("json-enrichment")
    json_request, page_request = requests

    assert json_request.headers["accept"] == "application/json"
    assert json_request.headers["accept-language"] == "it-IT,it;q=0.9,en;q=0.8"
    assert page_request.headers["accept"] == PAGE_ACCEPT
    assert page_request.headers["accept-language"] == "en-US,en;q=0.9,it-IT;q=0.8,it;q=0.7"
    assert page_request.headers["accept-encoding"] == "gzip, deflate, br"


async def test_amazon_keeps_its_rotating_browser_identity() -> None:
    url = "https://www.amazon.it/dp/X"
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url).respond(200, text="<html></html>")
        async with httpx.AsyncClient() as client:
            await amazon_module._fetch_amazon_html(url, client)
    assert route.calls[0].request.headers["user-agent"] in USER_AGENTS
