"""Outbound connections use only the complete, validated DNS answer."""

from __future__ import annotations

import asyncio
import socket
import ssl
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable

import httpcore
import httpx
import pytest

from price_tracker.core.http_client import PublicAsyncClient, _PublicTransport, build_client
from price_tracker.core.url_utils import UnsafeURLError, validate_public_url

PUBLIC = "93.184.216.34"
BLOCKED = [
    "127.0.0.1",
    "10.0.0.1",
    "169.254.169.254",
    "169.254.1.1",
    "100.100.100.100",
    "::1",
    "fe80::1",
    "fc00::1",
    "::ffff:127.0.0.1",
    "::ffff:100.64.0.1",
    "::127.0.0.1",
    "64:ff9b::7f00:1",
    "0.0.0.0",
    "224.0.0.1",
]

PUBLIC_IPV6 = "2606:4700:4700::1111"


class Wire(httpcore.AsyncNetworkStream):
    def __init__(self, response: bytes, *, delay: float = 0) -> None:
        self.response = response
        self.writes = bytearray()
        self.sni: str | None = None
        self.tls_context: ssl.SSLContext | None = None
        self.closed = False
        self.delay = delay

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        await asyncio.sleep(self.delay)
        data, self.response = self.response[:max_bytes], self.response[max_bytes:]
        return data

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.writes.extend(buffer)

    async def aclose(self) -> None:
        self.closed = True

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.sni = server_hostname
        self.tls_context = ssl_context
        return self


class Network:
    def __init__(self) -> None:
        self.answers = [PUBLIC]
        self.lookups: list[str] = []
        self.lookup_families: list[int | None] = []
        self.targets: list[tuple[str, int]] = []
        self.responses = [b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"]
        self.streams: list[Wire] = []
        self.delay = 0.0

    def resolve(self, host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        self.lookups.append(host)
        self.lookup_families.append(kwargs.get("family", args[0] if args else None))
        return [
            (
                socket.AF_INET6 if ":" in ip else socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (ip, port),
            )
            for ip in self.answers
        ]

    async def connect(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.targets.append((host, port))
        wire = Wire(
            self.responses[min(len(self.streams), len(self.responses) - 1)], delay=self.delay
        )
        self.streams.append(wire)
        return wire


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> Network:
    net = Network()
    monkeypatch.setattr(socket, "getaddrinfo", net.resolve)

    async def connect(_backend: object, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        return await net.connect(*args, **kwargs)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    return net


@pytest.mark.parametrize("address", BLOCKED)
async def test_blocked_literal_never_connects(network: Network, address: str) -> None:
    host = f"[{address}]" if ":" in address else address
    async with build_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get(f"http://{host}/product")
    assert network.targets == []


async def test_public_ipv6_literal_never_connects(network: Network) -> None:
    async with build_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get(f"http://[{PUBLIC_IPV6}]/product")
    assert network.targets == []


async def test_resolution_requests_and_accepts_only_ipv4(network: Network) -> None:
    network.answers = [PUBLIC, PUBLIC_IPV6]
    async with build_client() as client:
        await client.get("https://shop.example/product")
    assert network.lookup_families == [socket.AF_INET]
    assert network.targets == [(PUBLIC, 443)]


async def test_hostname_without_ipv4_answer_never_connects(network: Network) -> None:
    network.answers = [PUBLIC_IPV6]
    async with build_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get("https://shop.example/product")
    assert network.targets == []


@pytest.mark.parametrize("answers", [["100.101.102.103"], [PUBLIC, "10.0.0.1"], []])
async def test_entire_dns_answer_must_be_public(network: Network, answers: list[str]) -> None:
    network.answers = answers
    async with build_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get("https://shop.example/product")
    assert network.targets == []


async def test_resolution_failure_is_closed(
    network: Network, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> list[Any]:
        raise socket.gaierror("no answer")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    async with build_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get("https://shop.example/product")
    assert network.targets == []


async def test_rebinding_between_admission_and_fetch(network: Network) -> None:
    validate_public_url("https://shop.example/product")
    network.answers = ["100.101.102.103"]
    async with build_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get("https://shop.example/product")
    assert network.targets == []


async def test_connects_validated_ip_with_original_host_and_tls(network: Network) -> None:
    async with build_client() as client:
        response = await client.get("https://shop.example:8443/product")
    assert response.text == "ok"
    assert str(response.url) == "https://shop.example:8443/product"
    assert network.lookups == ["shop.example"]
    assert network.targets == [(PUBLIC, 8443)]
    assert b"Host: shop.example:8443\r\n" in network.streams[0].writes
    assert network.streams[0].sni == "shop.example"
    assert network.streams[0].tls_context is not None
    assert network.streams[0].tls_context.check_hostname
    assert network.streams[0].tls_context.verify_mode == ssl.CERT_REQUIRED
    assert network.streams[0].closed


async def test_redirect_to_blocked_target_never_connects(network: Network) -> None:
    network.responses = [
        b"HTTP/1.1 302 Found\r\nLocation: http://100.100.100.100/\r\nContent-Length: 0\r\n\r\n"
    ]
    async with build_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get("https://shop.example/product", follow_redirects=True)
    assert network.targets == [(PUBLIC, 443)]


async def test_same_origin_redirect_resolves_again(
    network: Network, monkeypatch: pytest.MonkeyPatch
) -> None:
    network.responses = [b"HTTP/1.1 302 Found\r\nLocation: /next\r\nContent-Length: 0\r\n\r\n"]

    def resolve(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        network.answers = [PUBLIC] if not network.lookups else ["10.0.0.1"]
        return network.resolve(host, port)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    async with build_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get("https://shop.example/product", follow_redirects=True)
    assert len(network.lookups) == 2
    assert network.targets == [(PUBLIC, 443)]


async def test_redirect_hop_limit(network: Network) -> None:
    network.responses = [b"HTTP/1.1 302 Found\r\nLocation: /next\r\nContent-Length: 0\r\n\r\n"]
    async with build_client() as client:
        with pytest.raises(httpx.TooManyRedirects):
            await client.get("https://shop.example/product", follow_redirects=True)
    assert len(network.targets) == 6


async def test_overall_deadline_bounds_redirect_chain(network: Network) -> None:
    network.delay = 0.02
    network.responses = [b"HTTP/1.1 302 Found\r\nLocation: /next\r\nContent-Length: 0\r\n\r\n"]
    async with build_client(timeout=0.05) as client:
        with pytest.raises(httpx.TimeoutException):
            await client.get("https://shop.example/product", follow_redirects=True)
    assert len(network.targets) < 6


async def test_browser_cannot_start_even_if_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    from price_tracker.scrapers import playwright_fallback

    module = types.ModuleType("playwright.async_api")

    def forbidden() -> None:
        pytest.fail("browser startup permits uncontrolled subrequests")

    module.async_playwright = forbidden  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    monkeypatch.setattr(playwright_fallback, "available", lambda: True)
    result = await playwright_fallback.PlaywrightFallbackScraper().scrape("https://shop.example/")
    assert result.price is None
    assert result.error is not None
    assert "disabled" in result.error


@pytest.mark.parametrize("scheme", ["file", "ftp", "ws", "wss", "data"])
async def test_non_http_schemes_fail_closed(network: Network, scheme: str) -> None:
    async with build_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get(f"{scheme}://shop.example/product")
    assert network.targets == []


@pytest.mark.parametrize(
    ("module", "helper"),
    [
        ("aliexpress", "_fetch_aliexpress_html"),
        ("amazon", "_fetch_amazon_html"),
        ("apple_store", "_fetch_apple_html"),
        ("bestbuy", "_fetch_bestbuy_html"),
        ("ebay", "_fetch_ebay_html"),
        ("etsy", "_fetch_etsy_html"),
        ("generic", "_fetch_generic_html"),
        ("google_store", "_fetch_google_store_html"),
        ("mediamarkt", "_fetch_mediamarkt_html"),
        ("newegg", "_fetch_newegg_html"),
        ("otto", "_fetch_otto_html"),
        ("shopify", "_fetch_shopify_response"),
        ("shopify", "_fetch_shopify_json"),
        ("target", "_fetch_target_html"),
        ("walmart", "_fetch_walmart_html"),
        ("wayfair", "_fetch_wayfair_html"),
        ("zalando", "_fetch_zalando_html"),
    ],
)
async def test_scraper_cannot_bypass_guard_with_plain_client(
    network: Network,
    module: str,
    helper: str,
) -> None:
    import importlib

    fetch = getattr(importlib.import_module(f"price_tracker.scrapers.{module}"), helper)
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(UnsafeURLError):
            await fetch("http://100.100.100.100/product", client)
    assert network.targets == []


@pytest.mark.parametrize(
    ("module", "helper"),
    [
        ("amazon", "_fetch_via_curl_cffi"),
        ("amazon", "_fetch_via_scrapling"),
        ("generic", "_fetch_with_curl_cffi"),
        ("shopify", "_fetch_json_via_curl_cffi"),
    ],
)
async def test_unbound_backend_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
    module: str,
    helper: str,
) -> None:
    import importlib
    import sys
    import types

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("unbound backend was invoked")

    monkeypatch.setattr("curl_cffi.requests.AsyncSession", forbidden)
    fake = types.ModuleType("scrapling")
    fake.Fetcher = types.SimpleNamespace(get=forbidden)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scrapling", fake)
    target = importlib.import_module(f"price_tracker.scrapers.{module}")
    owner = target.ShopifyScraper if module == "shopify" else target
    assert await getattr(owner, helper)("https://shop.example/product") is None


async def test_fetch_page_cannot_bypass_guard_with_plain_client(network: Network) -> None:
    from price_tracker.core.fetch import fetch_page

    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(UnsafeURLError):
            await fetch_page("http://100.100.100.100/product", client)
    assert network.targets == []


async def test_cross_origin_redirect_drops_client_auth(network: Network) -> None:
    network.responses = [
        b"HTTP/1.1 302 Found\r\nLocation: https://other.example/product\r\n"
        b"Content-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
    ]
    async with build_client() as client:
        client.auth = ("example-user", "example-password")
        await client.get("https://shop.example/product", follow_redirects=True)
    assert b"Authorization:" in network.streams[0].writes
    assert b"Authorization:" not in network.streams[1].writes


async def test_scheme_change_redirect_drops_client_auth(network: Network) -> None:
    network.responses = [
        b"HTTP/1.1 302 Found\r\nLocation: https://shop.example/next\r\nContent-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
    ]
    async with build_client() as client:
        client.auth = ("example-user", "example-password")
        await client.get("http://shop.example/product", follow_redirects=True)
    assert b"Authorization:" in network.streams[0].writes
    assert b"Authorization:" not in network.streams[1].writes


@pytest.mark.parametrize(
    "module",
    [
        "aliexpress",
        "apple_store",
        "bestbuy",
        "etsy",
        "google_store",
        "mediamarkt",
        "newegg",
        "otto",
        "target",
        "walmart",
        "wayfair",
        "zalando",
    ],
)
async def test_scrape_returns_error_for_unsafe_destination(network: Network, module: str) -> None:
    import importlib

    from price_tracker.core.scraper_base import AbstractScraper

    namespace = importlib.import_module(f"price_tracker.scrapers.{module}")
    scraper_type = next(
        value
        for value in vars(namespace).values()
        if isinstance(value, type)
        and issubclass(value, AbstractScraper)
        and value is not AbstractScraper
    )
    async with build_client() as client:
        result = await scraper_type().scrape("http://100.100.100.100/product", client)
    assert result.price is None
    assert result.error
    assert network.targets == []


async def test_fetch_page_resolves_each_hop_only_once(network: Network) -> None:
    from price_tracker.core.fetch import fetch_page

    network.responses = [
        b"HTTP/1.1 302 Found\r\nLocation: https://other.example/product\r\n"
        b"Content-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
    ]
    async with build_client() as client:
        assert (await fetch_page("https://shop.example/product", client)).text == "ok"
    assert network.lookups == ["shop.example", "other.example"]


@pytest.mark.parametrize("stream", [False, True])
async def test_slow_body_is_bounded_and_closed(
    network: Network, monkeypatch: pytest.MonkeyPatch, stream: bool
) -> None:
    async def drip(self: Wire, max_bytes: int, timeout: float | None = None) -> bytes:
        await asyncio.sleep(0.02)
        if self.response:
            self.response = b""
            return b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nx"
        return b"x"

    monkeypatch.setattr(Wire, "read", drip)
    async with build_client(timeout=0.1) as client:

        async def consume() -> None:
            response = await client.send(
                client.build_request("GET", "https://shop.example/product"), stream=stream
            )
            await response.aread()

        with pytest.raises(httpx.TimeoutException):
            await consume()
    assert network.streams[0].closed


async def test_dns_is_included_in_deadline(
    network: Network, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def delayed(*args: Any, **kwargs: Any) -> list[Any]:
        await asyncio.sleep(10)
        return []

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", delayed)
    async with build_client(timeout=0.02) as client:
        with pytest.raises(httpx.TimeoutException):
            await client.get("https://shop.example/product")
    assert network.targets == []


@pytest.mark.parametrize(
    "host", ["shop.example.", "ß.example", "127.1", "2130706433", "local.localhost"]
)
async def test_actual_client_hostname_is_the_resolved_name(
    network: Network,
    host: str,
) -> None:
    network.answers = ["127.0.0.1"]
    async with build_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get(f"https://{host}/product")
    assert network.targets == []
    assert network.lookups == [httpx.URL(f"https://{host}/").raw_host.decode("ascii")]


async def test_caller_cannot_override_host_or_sni(network: Network) -> None:
    async with build_client() as client:
        await client.get(
            "https://shop.example/product",
            headers={"Host": "other.example"},
            extensions={"sni_hostname": "other.example"},
        )
    assert network.streams[0].sni == "shop.example"
    assert b"Host: shop.example\r\n" in network.streams[0].writes


async def test_proxy_environment_does_not_change_connection(
    network: Network,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:8080")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:8080")
    async with build_client() as client:
        await client.get("https://shop.example/product")
    assert network.targets == [(PUBLIC, 443)]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mounts": {"http://": httpx.MockTransport(lambda request: httpx.Response(200))}},
        {"proxy": "http://127.0.0.1:8080"},
        {"transport": httpx.MockTransport(lambda request: httpx.Response(200))},
        {"app": object()},
        {"trust_env": True},
    ],
    ids=["mounts", "proxy", "transport", "app", "trust-env"],
)
def test_alternate_transports_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(TypeError, match="does not allow alternate transports"):
        PublicAsyncClient(**kwargs)


def test_disabled_browser_does_not_shadow_generic_scraper() -> None:
    from price_tracker.core.registry import ScraperRegistry, discover_builtin_scrapers

    registry = ScraperRegistry()
    discover_builtin_scrapers(registry)
    scraper = registry.resolve("https://shop.example/item/1")
    assert scraper is not None
    assert scraper.name == "generic"


async def test_fetch_page_does_not_reapply_auth_on_redirect(network: Network) -> None:
    from price_tracker.core.fetch import fetch_page

    network.responses = [
        b"HTTP/1.1 302 Found\r\nLocation: https://other.example/product\r\n"
        b"Content-Length: 0\r\n\r\n",
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
    ]
    async with build_client() as client:
        client.auth = ("example-user", "example-password")
        await fetch_page("https://shop.example/product", client)
    assert b"Authorization:" not in network.streams[1].writes


async def test_connection_limit_includes_open_response_body(network: Network) -> None:
    async with build_client(timeout=0.1, max_connections=1) as client:
        response = await client.send(
            client.build_request("GET", "https://shop.example/one"), stream=True
        )
        try:
            with pytest.raises(httpx.TimeoutException):
                await client.get("https://shop.example/two")
            assert len(network.targets) == 1
        finally:
            await response.aclose()


@pytest.mark.parametrize("failure", [httpcore.ConnectError, httpcore.ConnectTimeout])
async def test_failed_address_uses_remaining_validated_answers(
    network: Network, monkeypatch: pytest.MonkeyPatch, failure: type[Exception]
) -> None:
    first = "8.8.8.8"
    last = "1.1.1.1"
    network.answers = [first, PUBLIC, first, last]
    attempts: list[str] = []

    async def connect(
        _backend: object, host: str, port: int, **kwargs: Any
    ) -> httpcore.AsyncNetworkStream:
        attempts.append(host)
        if host != last:
            raise failure("address unavailable")
        return await network.connect(host, port, **kwargs)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    async with build_client() as client:
        response = await client.get("https://shop.example:8443/product")
    assert response.text == "ok"
    assert attempts == [first, PUBLIC, last]
    assert network.lookups == ["shop.example"]
    assert network.targets == [(last, 8443)]
    assert b"Host: shop.example:8443\r\n" in network.streams[0].writes
    assert network.streams[0].sni == "shop.example"
    assert network.streams[0].closed


async def test_decoding_failure_immediately_closes_response(network: Network) -> None:
    network.responses = [
        b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 7\r\n\r\ninvalid",
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
    ]
    async with build_client(max_connections=1) as client:
        with pytest.raises(httpx.DecodingError):
            await client.get("https://shop.example/product")
        assert network.streams[0].closed
        assert isinstance(client._transport, _PublicTransport)
        assert not client._transport._active
        assert client._transport._slots._value == 1
        assert (await client.get("https://shop.example/next")).text == "ok"
