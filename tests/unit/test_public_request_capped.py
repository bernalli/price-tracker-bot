"""Bounded public responses keep redirect validation, closure and request deadlines."""

from __future__ import annotations

import asyncio
import socket
from typing import TYPE_CHECKING, Any

import httpcore
import httpx
import pytest
import respx
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core import http_client
from price_tracker.core.domain_spacing import DomainSpacing
from price_tracker.core.url_utils import UnsafeURLError
from tests.unit.test_public_connections import PUBLIC, Network

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

URL = "https://images.example/image.jpg"


class Chunks(httpx.AsyncByteStream):
    def __init__(self, parts: list[bytes], *, forbidden: bool = False, slow: bool = False) -> None:
        self.parts = parts
        self.forbidden = forbidden
        self.slow = slow
        self.closed = False
        self.read = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.read = True
        assert not self.forbidden, "a refused header must prevent body consumption"
        if self.slow:
            await asyncio.Event().wait()
        for part in self.parts:
            yield part

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> Network:
    net = Network()
    monkeypatch.setattr(socket, "getaddrinfo", net.resolve)

    async def connect(_backend: object, *args: Any, **kwargs: Any) -> httpcore.AsyncNetworkStream:
        return await net.connect(*args, **kwargs)

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect)
    return net


async def test_without_a_cap_the_call_is_unchanged() -> None:
    with respx.mock as router:
        route = router.get(URL).respond(200, content=b"abc")
        async with http_client.build_client() as client:
            response = await http_client.public_request(client, "GET", URL)
        assert response.content == b"abc"
        assert "https_only" not in route.calls[0].request.extensions
        assert "public_deadline_seconds" not in route.calls[0].request.extensions


async def test_https_only_without_a_cap_is_a_programming_error() -> None:
    async with http_client.build_client() as client:
        with pytest.raises(ValueError, match=r"."):
            await http_client.public_request(client, "GET", URL, https_only=True)


@pytest.mark.parametrize("cap", [0, -1, True, False, 1.5, "10", [], {}])
async def test_an_invalid_cap_is_a_programming_error(cap: Any) -> None:
    async with http_client.build_client() as client:
        with pytest.raises(ValueError, match=r"."):
            await http_client.public_request(client, "GET", URL, max_body_bytes=cap)


@pytest.mark.parametrize("timeout", [0, -1, True, False, float("nan"), float("inf"), "1", []])
async def test_an_invalid_timeout_is_refused_before_network(timeout: Any, network: Network) -> None:
    async with http_client.build_client() as client:
        with pytest.raises(ValueError, match=r"."):
            await http_client.public_request(client, "GET", URL, max_body_bytes=10, timeout=timeout)
    assert network.lookups == []
    assert network.targets == []


async def test_a_body_at_the_cap_is_returned() -> None:
    stream = Chunks([b"ab", b"c"])
    with respx.mock as router:
        route = router.get(URL).respond(200, stream=stream)
        async with http_client.build_client() as client:
            response = await http_client.public_request(
                client, "GET", URL, max_body_bytes=3, https_only=True
            )
        assert response.content == b"abc"
        assert response.url == URL
        assert route.calls[0].request.extensions["https_only"] is True
        assert route.calls[0].request.extensions["public_deadline_seconds"] == 30.0
    assert stream.closed


async def test_a_body_one_byte_over_the_cap_is_refused() -> None:
    stream = Chunks([b"ab", b"cd"])
    with respx.mock as router:
        router.get(URL).respond(200, stream=stream)
        async with http_client.build_client() as client:
            with pytest.raises(http_client.BodyLimitError, match="too large") as error:
                await http_client.public_request(client, "GET", URL, max_body_bytes=3)
            assert error.value.reason == "too large"
    assert stream.closed


@pytest.mark.parametrize(
    ("headers", "reason"),
    [
        ({"content-length": "4"}, "too large"),
        *[
            ({"content-length": value}, "bad length")
            for value in ("abc", "-1", "1e3", "", "+1", "1, 1")
        ],
        *[
            ({"content-encoding": value}, "encoded body")
            for value in ("gzip", "br", "deflate", "gzip, identity")
        ],
    ],
)
async def test_declared_headers_are_refused_without_reading(
    headers: dict[str, str], reason: str
) -> None:
    stream = Chunks([], forbidden=True)
    with respx.mock as router:
        router.get(URL).respond(200, headers=headers, stream=stream)
        async with http_client.build_client() as client:
            with pytest.raises(http_client.BodyLimitError, match=reason) as error:
                await http_client.public_request(client, "GET", URL, max_body_bytes=3)
            assert error.value.reason == reason
    assert not stream.read
    assert stream.closed


@pytest.mark.parametrize("encoding", ["", "identity", " Identity "])
async def test_identity_encoding_is_accepted(encoding: str) -> None:
    with respx.mock as router:
        router.get(URL).respond(
            200, headers={"content-encoding": encoding}, stream=Chunks([b"abc"])
        )
        async with http_client.build_client() as client:
            response = await http_client.public_request(client, "GET", URL, max_body_bytes=3)
        assert response.content == b"abc"


@settings(max_examples=100, deadline=None)
@given(parts=st.lists(st.binary(max_size=20), max_size=10), cap=st.integers(1, 100))
async def test_chunk_boundaries_do_not_change_the_limit(parts: list[bytes], cap: int) -> None:
    stream = Chunks(parts)
    with respx.mock as router:
        router.get(URL).respond(200, stream=stream)
        async with http_client.build_client() as client:
            if sum(map(len, parts)) <= cap:
                response = await http_client.public_request(client, "GET", URL, max_body_bytes=cap)
                assert response.content == b"".join(parts)
            else:
                with pytest.raises(http_client.BodyLimitError, match="too large"):
                    await http_client.public_request(client, "GET", URL, max_body_bytes=cap)
    assert stream.closed


@pytest.mark.parametrize("redirect", [False, True])
async def test_a_slow_body_hits_the_request_deadline(redirect: bool) -> None:
    stream = Chunks([], slow=True)
    with respx.mock as router:
        if redirect:
            router.get(URL).respond(302, headers={"location": "/final.jpg"})
            router.get("https://images.example/final.jpg").respond(200, stream=stream)
        else:
            router.get(URL).respond(200, stream=stream)
        async with http_client.build_client(timeout=10) as client:
            with pytest.raises(httpx.ReadTimeout):
                await asyncio.wait_for(
                    http_client.public_request(client, "GET", URL, max_body_bytes=10, timeout=0.05),
                    timeout=1,
                )
    assert stream.closed


async def test_the_request_budget_never_extends_the_client_deadline() -> None:
    with respx.mock as router:
        route = router.get(URL).respond(200, stream=Chunks([b"x"]))
        async with http_client.build_client(timeout=0.5) as client:
            await http_client.public_request(client, "GET", URL, max_body_bytes=1, timeout=2)
        assert route.calls[0].request.extensions["public_deadline_seconds"] == 0.5


@pytest.mark.parametrize(
    "target",
    ["https://127.0.0.1/x.jpg", "https://localhost/x.jpg", "https://169.254.169.254/x.jpg"],
)
async def test_a_redirect_to_a_private_address_is_refused(
    network: Network, target: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = network.resolve

    def resolve(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        network.answers = ["127.0.0.1"] if host == "localhost" else [PUBLIC]
        return original(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    network.responses = [
        f"HTTP/1.1 302 Found\r\nLocation: {target}\r\nContent-Length: 0\r\n\r\n".encode()
    ]
    async with http_client.build_client() as client:
        with pytest.raises(UnsafeURLError):
            await http_client.public_request(client, "GET", URL, max_body_bytes=10, https_only=True)
    assert network.targets == [(PUBLIC, 443)]
    assert all(s.closed for s in network.streams)


@pytest.mark.parametrize("initial", [False, True])
@pytest.mark.parametrize(
    "tail", ["http://plain.example/x", "http://plain.example/x?next=https://safe.example/"]
)
async def test_http_is_refused_before_dns_slots_and_connections(
    network: Network, monkeypatch: pytest.MonkeyPatch, initial: bool, tail: str
) -> None:
    network.responses = [
        f"HTTP/1.1 302 Found\r\nLocation: {tail}\r\nContent-Length: 0\r\n\r\n".encode()
    ]
    acquisitions = 0
    original = asyncio.Semaphore.acquire

    async def acquire(semaphore: asyncio.Semaphore) -> bool:
        nonlocal acquisitions
        acquisitions += 1
        return await original(semaphore)

    monkeypatch.setattr(asyncio.Semaphore, "acquire", acquire)
    async with http_client.build_client() as client:
        with pytest.raises(UnsafeURLError, match="https is required"):
            await http_client.public_request(
                client, "GET", tail if initial else URL, max_body_bytes=10, https_only=True
            )
    assert network.lookups == ([] if initial else ["images.example"])
    assert network.targets == ([] if initial else [(PUBLIC, 443)])
    assert acquisitions == (0 if initial else 1)
    assert all(s.closed for s in network.streams)


@pytest.mark.parametrize("target", ["/final.jpg", "https://cdn.example/final.jpg"])
async def test_the_redirect_chain_is_followed_and_reported(network: Network, target: str) -> None:
    network.responses = [
        f"HTTP/1.1 302 Found\r\nLocation: {target}\r\nContent-Length: 0\r\n\r\n".encode(),
        b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
    ]
    async with http_client.build_client() as client:
        response = await http_client.public_request(
            client, "GET", URL, max_body_bytes=2, https_only=True
        )
    assert response.content == b"ok"
    assert str(response.url) == str(httpx.URL(URL).join(target))
    assert [r.status_code for r in response.history] == [302]
    assert len(network.targets) == 2
    assert all(s.closed for s in network.streams)


async def test_a_cyclic_chain_is_bounded_and_closed(network: Network) -> None:
    network.responses = [b"HTTP/1.1 302 Found\r\nLocation: /image.jpg\r\nContent-Length: 0\r\n\r\n"]
    async with http_client.build_client() as client:
        with pytest.raises(httpx.TooManyRedirects):
            await http_client.public_request(client, "GET", URL, max_body_bytes=2, https_only=True)
    assert len(network.targets) == 6
    assert all(s.closed for s in network.streams)


@pytest.mark.parametrize("rebinding", [False, True])
async def test_mixed_dns_and_rebinding_are_refused(
    network: Network, monkeypatch: pytest.MonkeyPatch, rebinding: bool
) -> None:
    network.responses = [b"HTTP/1.1 302 Found\r\nLocation: /next.jpg\r\nContent-Length: 0\r\n\r\n"]
    original = network.resolve

    def resolve(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        network.answers = [PUBLIC] if rebinding and not network.lookups else [PUBLIC, "127.0.0.1"]
        return original(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    async with http_client.build_client() as client:
        with pytest.raises(UnsafeURLError):
            await http_client.public_request(client, "GET", URL, max_body_bytes=2, https_only=True)
    assert network.targets == ([(PUBLIC, 443)] if rebinding else [])
    assert all(s.closed for s in network.streams)


@pytest.mark.parametrize("cap", [None, 10])
@pytest.mark.parametrize("target", [None, "/next", "https://other.example/next"])
async def test_public_requests_do_not_perform_job_pacing(
    cap: int | None, target: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("HTTP must not admit, defer or wait for jobs")

    for name in ("record_start", "wait_for", "deferred_for", "is_deferred", "sleep", "defer"):
        monkeypatch.setattr(DomainSpacing, name, forbidden)
    with respx.mock as router:
        if target is None:
            router.get(URL).respond(200, stream=Chunks([b"ok"]))
        else:
            router.get(URL).respond(302, headers={"location": target})
            router.get(str(httpx.URL(URL).join(target))).respond(200, stream=Chunks([b"ok"]))
        async with http_client.build_client() as client:
            response = await http_client.public_request(client, "GET", URL, max_body_bytes=cap)
        assert response.content == b"ok"


async def test_huge_declared_lengths_are_refused_without_integer_conversion() -> None:
    stream = Chunks([], forbidden=True)
    with respx.mock as router:
        router.get(URL).respond(200, headers={"content-length": "9" * 5000}, stream=stream)
        async with http_client.build_client() as client:
            with pytest.raises(http_client.BodyLimitError, match="too large"):
                await http_client.public_request(client, "GET", URL, max_body_bytes=10)
    assert stream.closed
    assert not stream.read


@pytest.mark.parametrize("cap", [None, 10])
async def test_converging_chains_do_not_perform_job_pacing(
    cap: int | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("redirect convergence must not reserve or wait for jobs")

    for name in ("record_start", "wait_for", "deferred_for", "is_deferred", "sleep", "defer"):
        monkeypatch.setattr(DomainSpacing, name, forbidden)
    with respx.mock as router:
        router.get(URL).respond(302, headers={"location": "https://cdn.example/shared"})
        router.get("https://other.example/image").respond(
            302, headers={"location": "https://cdn.example/shared"}
        )
        router.get("https://cdn.example/shared").mock(
            side_effect=lambda request: httpx.Response(200, stream=Chunks([b"ok"]))
        )
        async with http_client.build_client() as client:
            responses = await asyncio.gather(
                http_client.public_request(client, "GET", URL, max_body_bytes=cap),
                http_client.public_request(
                    client, "GET", "https://other.example/image", max_body_bytes=cap
                ),
            )
        assert [r.content for r in responses] == [b"ok", b"ok"]


async def test_the_request_deadline_bounds_redirect_headers(network: Network) -> None:
    network.delay = 0.02
    network.responses = [b"HTTP/1.1 302 Found\r\nLocation: /next.jpg\r\nContent-Length: 0\r\n\r\n"]
    async with http_client.build_client(timeout=10) as client:
        with pytest.raises(httpx.ReadTimeout):
            await asyncio.wait_for(
                http_client.public_request(client, "GET", URL, max_body_bytes=10, timeout=0.05),
                timeout=1,
            )
    assert len(network.targets) < 6
    assert all(s.closed for s in network.streams)


async def test_a_plain_client_cannot_bypass_capped_public_validation(network: Network) -> None:
    async with httpx.AsyncClient(trust_env=False) as client:
        with pytest.raises(UnsafeURLError):
            await http_client.public_request(
                client, "GET", "https://127.0.0.1/image.jpg", max_body_bytes=10, https_only=True
            )
    assert network.targets == []
