"""Offline contracts for the managed Nove25 scraper."""

from __future__ import annotations

import json
import socket
from decimal import Decimal
from typing import TYPE_CHECKING

import httpcore
import pytest

from price_tracker.core.exceptions import BlockEvent
from price_tracker.core.http_client import build_client
from price_tracker.core.registry import (
    ScraperRegistry,
    discover_builtin_scrapers,
    discover_dropin_scrapers,
)

if TYPE_CHECKING:
    from price_tracker.core.scraper_base import AbstractScraper


class _Wire(httpcore.AsyncNetworkStream):
    def __init__(self, payload: bytes):
        self.payload = payload

    async def read(self, max_bytes, timeout=None):
        data, self.payload = self.payload[:max_bytes], self.payload[max_bytes:]
        return data

    async def write(self, buffer, timeout=None):
        pass

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        return self


@pytest.fixture
def wire(monkeypatch):
    responses: list[bytes] = []
    connections: list[tuple[str, int]] = []

    def resolve(host, port, *args, **kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("93.184.216.34", port))
        ]

    async def connect(_backend, host, port, **kwargs):
        connections.append((host, port))
        return _Wire(responses.pop(0))

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    return responses, connections


def _response(status: str, body: str = "", extra: str = "") -> bytes:
    data = body.encode()
    return f"HTTP/1.1 {status}\r\nContent-Length: {len(data)}\r\n{extra}\r\n".encode() + data


def _registry() -> ScraperRegistry:
    registry = ScraperRegistry()
    discover_builtin_scrapers(registry)
    return registry


def _resolve(url: str) -> AbstractScraper:
    scraper = _registry().resolve(url)
    assert scraper is not None
    return scraper


def test_nove25_domain_resolution_and_dropin_collision(tmp_path):
    # The lower-priority generic fallback must not win and a drop-in with the
    # same name must not replace the built-in, even though it is imported.
    (tmp_path / "collision.py").write_text("""\
from pathlib import Path
Path(__file__).with_suffix(".loaded").touch()
class Dropin(AbstractScraper):
    name = "nove25"
    priority = 99
    def can_handle(self, url): return True
    async def scrape(self, url, client): raise AssertionError("drop-in was selected")
""")
    registry = _registry()
    discover_dropin_scrapers(registry, tmp_path)
    assert (tmp_path / "collision.loaded").is_file()
    for url in (
        "https://nove25.net/item",
        "https://shop.nove25.com/item",
        "https://nove25.net/products/item",
    ):
        selected = registry.resolve(url)
        assert selected is not None
        assert type(selected).__module__ == "price_tracker.scrapers.nove25"
    selected = registry.resolve("https://nove25.net.attacker.example/item")
    assert selected is not None
    assert selected.name == "generic"
    selected = registry.resolve("https://notnove25.net/item")
    assert selected is not None
    assert selected.name == "generic"


async def test_selected_product_out_of_stock_and_currency(wire):
    responses, _ = wire
    url = "https://nove25.net/item"
    product = {
        "@type": "Product",
        "url": url,
        "name": "Ring",
        "offers": {
            "price": "20.00",
            "priceCurrency": "EUR",
            "availability": "https://schema.org/OutOfStock",
        },
    }
    responses.append(
        _response(
            "200 OK", '<script type="application/ld+json">' + json.dumps(product) + "</script>"
        )
    )
    async with build_client() as client:
        info = await _resolve(url).scrape(url, client)
    assert info.price == Decimal("20.00")
    assert info.currency == "EUR"
    assert info.available is False


async def test_other_product_jsonld_is_rejected(wire):
    responses, _ = wire
    url = "https://nove25.net/item"
    product = {
        "@type": "Product",
        "url": "https://nove25.net/other",
        "name": "Other",
        "offers": {"price": "12.00", "priceCurrency": "USD"},
    }
    responses.append(
        _response(
            "200 OK", '<script type="application/ld+json">' + json.dumps(product) + "</script>"
        )
    )
    async with build_client() as client:
        info = await _resolve(url).scrape(url, client)
    assert info.price is None
    assert info.error


async def test_403_is_a_block_event(wire):
    responses, _ = wire
    url = "https://nove25.net/item"
    responses.append(_response("403 Forbidden", "blocked"))
    async with build_client() as client:
        with pytest.raises(BlockEvent):
            await _resolve(url).scrape(url, client)


async def test_redirect_to_loopback_never_connects(wire):
    responses, connections = wire
    url = "https://nove25.net/item"
    responses.append(_response("302 Found", extra="Location: http://127.0.0.1/private\r\n"))
    async with build_client() as client:
        info = await _resolve(url).scrape(url, client)
    assert info.price is None
    assert info.error
    assert len(connections) == 1


async def test_missing_currency_is_not_assumed_to_be_eur(wire):
    responses, _ = wire
    url = "https://nove25.net/item"
    responses.append(_response("200 OK", '<meta property="product:price:amount" content="20.00">'))
    async with build_client() as client:
        info = await _resolve(url).scrape(url, client)
    assert info.price == Decimal("20.00")
    assert info.currency is None


async def test_fetch_failure_logs_neither_url_query_nor_exception_text(monkeypatch, caplog):
    import logging

    import httpx

    from price_tracker.scrapers import generic

    async def fail(url, client):
        raise httpx.ConnectError("SYNTHETIC_EXCEPTION_MARKER")

    monkeypatch.setattr(generic, "_fetch_generic_html", fail)
    url = "https://nove25.net/item?private=SYNTHETIC_QUERY_MARKER"
    with caplog.at_level(logging.DEBUG, logger=generic.__name__):
        async with httpx.AsyncClient() as client:
            info = await _resolve(url).scrape(url, client)
    assert info.price is None
    assert info.error
    assert "SYNTHETIC_QUERY_MARKER" not in caplog.text
    assert "SYNTHETIC_EXCEPTION_MARKER" not in caplog.text
    assert "nove25.net" in caplog.text
    assert "ConnectError" in caplog.text


async def test_regex_fallback_logs_hostname_without_query(monkeypatch, caplog):
    import logging
    from pathlib import Path

    import httpx

    from price_tracker.scrapers import generic

    html = (
        Path(__file__).resolve().parents[2] / "fixtures/generic/sample_heuristic.html"
    ).read_text()

    async def fetch(url, client):
        return html

    monkeypatch.setattr(generic, "_fetch_generic_html", fetch)
    url = "https://nove25.net/item?private=SYNTHETIC_QUERY_MARKER"
    with caplog.at_level(logging.WARNING, logger=generic.__name__):
        async with httpx.AsyncClient() as client:
            info = await _resolve(url).scrape(url, client)
    assert info.price is not None
    assert "heuristic_fallback" in caplog.text
    assert "nove25.net" in caplog.text
    assert "SYNTHETIC_QUERY_MARKER" not in caplog.text
