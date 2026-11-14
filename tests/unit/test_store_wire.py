"""The Chrome store wire: pinned to the validated answer, closed headers, closed error map."""

from __future__ import annotations

import gzip
import ipaddress
import socket
import zlib
from typing import Any

import certifi
import httpx
import pytest
from curl_cffi.const import CurlECode, CurlOpt
from curl_cffi.requests import exceptions as curl_exceptions
from hypothesis import example, given, settings
from hypothesis import strategies as st

from price_tracker.core import fetch, store_wire
from price_tracker.core.exceptions import HTTPBlockStatus, ListingGone
from price_tracker.core.http_client import (
    PublicAsyncClient,
    build_client,
    build_store_client,
    public_request,
)
from price_tracker.core.retry_policy import is_retryable_http_error
from price_tracker.core.store_wire import (
    STORE_ACCEPT_LANGUAGE,
    STORE_IMPERSONATE,
    BodyCeilingExceeded,
)
from price_tracker.core.url_utils import UnsafeURLError, _is_blocked_ip
from tests.support import store_wire_fake
from tests.support.store_wire_fake import HANG, FakeCurlResponse, FakeWire
from tests.unit.test_public_connections import BLOCKED, PUBLIC, PUBLIC_IPV6, Network

fake_wire = store_wire_fake.fake_wire

PUBLIC_2 = "93.184.216.35"
OK = FakeCurlResponse(status_code=200, headers=[("Content-Type", "text/plain")], body=b"ok")


class HostNetwork(Network):
    """The shared DNS fake, with per-host answers and an optional resolver failure."""

    def __init__(self) -> None:
        super().__init__()
        self.by_host: dict[str, list[str]] = {}
        self.error: OSError | None = None

    def resolve(self, host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        if self.error is not None:
            self.lookups.append(host)
            raise self.error
        saved = self.answers
        self.answers = self.by_host.get(host, saved)
        try:
            return super().resolve(host, port, *args, **kwargs)
        finally:
            self.answers = saved


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> HostNetwork:
    net = HostNetwork()
    monkeypatch.setattr(socket, "getaddrinfo", net.resolve)
    return net


def _literal_url(address: str) -> str:
    host = f"[{address}]" if ":" in address else address
    return f"https://{host}/p"


def _sent_cookies(request: dict[str, Any]) -> dict[str, str]:
    values = [value for name, value in request["headers"] if name == "Cookie"]
    if not values:
        return {}
    (value,) = values
    return dict(pair.split("=", 1) for pair in value.split("; "))


# ── Address checks happen before any session opens ───────────────


@pytest.mark.parametrize("address", BLOCKED)
async def test_blocked_literal_opens_no_session(
    fake_wire: FakeWire, dns: HostNetwork, address: str
) -> None:
    async with build_store_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get(_literal_url(address))
    assert fake_wire.sessions == []


async def test_public_ipv6_literal_opens_no_session(fake_wire: FakeWire, dns: HostNetwork) -> None:
    async with build_store_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get(_literal_url(PUBLIC_IPV6))
    assert fake_wire.sessions == []


@pytest.mark.parametrize("answers", [[PUBLIC, BLOCKED[1]], [BLOCKED[0]], []])
async def test_dns_answer_with_any_blocked_address_opens_no_session(
    fake_wire: FakeWire, dns: HostNetwork, answers: list[str]
) -> None:
    dns.answers = answers
    async with build_store_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get("https://shop.example/p")
    assert fake_wire.sessions == []


async def test_resolution_failure_opens_no_session(fake_wire: FakeWire, dns: HostNetwork) -> None:
    dns.error = socket.gaierror(socket.EAI_NONAME, "no such host")
    async with build_store_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get("https://shop.example/p")
    assert fake_wire.sessions == []


@pytest.mark.parametrize(
    ("url", "host", "port", "target"),
    [
        ("https://shop.example/p", "shop.example", 443, "https://shop.example/p"),
        ("http://shop.example/p", "shop.example", 80, "http://shop.example/p"),
        ("https://shop.example:8443/p", "shop.example", 8443, "https://shop.example:8443/p"),
        ("https://SHOP.Example/p", "shop.example", 443, "https://shop.example/p"),
        (
            "https://bücher.example/p",
            "xn--bcher-kva.example",
            443,
            "https://xn--bcher-kva.example/p",
        ),
    ],
)
async def test_session_is_pinned_to_the_validated_answer(
    fake_wire: FakeWire, dns: HostNetwork, url: str, host: str, port: int, target: str
) -> None:
    dns.answers = [PUBLIC, PUBLIC_2, PUBLIC]
    fake_wire.script = [OK]
    async with build_store_client() as client:
        response = await client.get(url)
    assert response.text == "ok"
    (session,) = fake_wire.sessions
    assert session.kwargs["curl_options"][CurlOpt.HTTP_CONTENT_DECODING] == 0
    assert session.resolve_entry == f"{host}:{port}:{PUBLIC},{PUBLIC_2}"
    (request,) = session.requests
    assert request["url"] == target
    assert httpx.URL(request["url"]).raw_host == host.encode("ascii")
    assert session.closes == 1


async def test_session_options_are_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    options: dict[CurlOpt, object] = {CurlOpt.RESOLVE: ["shop.example:443:93.184.216.34"]}

    # A real session ignores CA bundles named by the environment: verify stays certifi's.
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", "/nonexistent/requests-ca.pem")
    monkeypatch.setenv("CURL_CA_BUNDLE", "/nonexistent/curl-ca.pem")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ssl-cert.pem")
    real = store_wire._open_session(options)
    try:
        verify: object = real.verify
        assert verify == certifi.where()
    finally:
        await real.close()

    recorded: list[dict[str, Any]] = []

    def factory(**kwargs: Any) -> object:
        recorded.append(kwargs)
        return object()

    monkeypatch.setattr(store_wire, "AsyncSession", factory)
    store_wire._open_session(options)
    (kwargs,) = recorded
    assert kwargs == {
        "impersonate": STORE_IMPERSONATE,
        "max_clients": 1,
        "trust_env": False,
        "allow_redirects": False,
        "discard_cookies": True,
        "verify": certifi.where(),
        "curl_options": options,
    }
    assert not {"proxy", "proxies", "cookies", "headers"} & kwargs.keys()


@pytest.mark.parametrize(
    "host",
    [
        "127.1",
        "0177.0.0.1",
        "0x7f.0.0.1",
        "2130706433",
        "0x7f000001",
        "1.2.3",
        "shop.example.",
        "under_score.example",
        "-bad.example",
        "localhost",
    ],
)
async def test_numeric_or_malformed_host_opens_no_session(
    fake_wire: FakeWire, dns: HostNetwork, host: str
) -> None:
    dns.answers = [PUBLIC]
    # HTTPX itself refuses a dotted quad with a leading zero; every other form
    # reaches the wire's host rule.
    expected = httpx.InvalidURL if host == "0177.0.0.1" else UnsafeURLError
    async with build_store_client() as client:
        with pytest.raises(expected):
            await client.get(f"https://{host}/p")
        assert client._transport._slots._value == 10  # type: ignore[attr-defined]
    assert fake_wire.sessions == []


async def test_userinfo_and_fragment_never_reach_the_wire(
    fake_wire: FakeWire, dns: HostNetwork
) -> None:
    fake_wire.script = [OK]
    async with build_store_client() as client:
        await client.get("https://user:pw@shop.example/p?q=1#f")
    (session,) = fake_wire.sessions
    assert session.requests[0]["url"] == "https://shop.example/p?q=1"


# ── Redirects: every hop is validated and pinned again ───────────


@pytest.mark.parametrize(
    "location",
    [
        "http://127.0.0.1/x",
        "http://10.0.0.1/x",
        "//169.254.169.254/latest",
        "http://[::1]/x",
        "HTTP://127.0.0.1/x",
        "http://user@127.0.0.1/x",
        "http://0x7f000001/x",
        "http://127.1/x",
        "http://shop.example@10.0.0.1/x",
        "ftp://shop.example/x",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "http://evil.example/x",
    ],
)
async def test_hostile_redirect_never_opens_a_session_to_a_non_public_address(
    fake_wire: FakeWire, dns: HostNetwork, location: str
) -> None:
    dns.by_host["evil.example"] = [BLOCKED[1]]
    fake_wire.script = [FakeCurlResponse(status_code=302, headers=[("Location", location)])]
    fake_wire.default = OK
    response: httpx.Response | None
    async with build_store_client() as client:
        try:
            response = await client.get("https://shop.example/p", follow_redirects=True)
        except Exception:
            response = None
    if response is not None:
        assert response.status_code == 302
    assert len(fake_wire.sessions) == 1
    for session in fake_wire.sessions:
        for address in session.pinned_addresses:
            assert not _is_blocked_ip(ipaddress.ip_address(address))


async def test_redirect_chain_is_bounded_and_every_hop_is_pinned(
    fake_wire: FakeWire, dns: HostNetwork
) -> None:
    fake_wire.default = FakeCurlResponse(status_code=302, headers=[("Location", "/next")])
    async with build_store_client() as client:
        with pytest.raises(httpx.TooManyRedirects):
            await client.get("https://shop.example/product", follow_redirects=True)
    assert len(fake_wire.sessions) == 6
    assert all(session.pinned_addresses == [PUBLIC] for session in fake_wire.sessions)
    assert all(session.closes == 1 for session in fake_wire.sessions)


@pytest.mark.parametrize("second", [PUBLIC_2, BLOCKED[1]])
async def test_rebinding_between_hops_is_checked_again(
    fake_wire: FakeWire, dns: HostNetwork, second: str
) -> None:
    fake_wire.default = OK
    async with build_store_client() as client:
        await client.get("https://shop.example/a")
        dns.answers = [second]
        if second == BLOCKED[1]:
            with pytest.raises(UnsafeURLError):
                await client.get("https://shop.example/b")
            assert len(fake_wire.sessions) == 1
        else:
            await client.get("https://shop.example/b")
            assert fake_wire.sessions[1].pinned_addresses == [second]
    assert fake_wire.sessions[0].pinned_addresses == [PUBLIC]


# ── Request headers: a closed list ───────────────────────────────


async def test_only_accept_and_cookie_are_forwarded(fake_wire: FakeWire, dns: HostNetwork) -> None:
    fake_wire.default = OK
    async with build_store_client() as client:
        client.cookies.set("a", "1")
        client.cookies.set("b", "2")
        await client.get(
            "https://shop.example/p",
            headers={
                "User-Agent": "evil",
                "Host": "10.0.0.1",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "Referer": "https://www.google.com/",
                "Sec-Fetch-Site": "cross-site",
                "Authorization": "Bearer t",
                "X-Forwarded-For": "10.0.0.1",
                "Accept-Language": "de-DE",
                "Accept": "image/webp",
            },
        )
        client.cookies.clear()
        await client.get("https://shop.example/p")
    first, second = (session.requests[0]["headers"] for session in fake_wire.sessions)
    assert first == [
        ("Accept-Language", STORE_ACCEPT_LANGUAGE),
        ("Accept", "image/webp"),
        ("Cookie", "a=1; b=2"),
    ]
    assert second == [("Accept-Language", STORE_ACCEPT_LANGUAGE)]

    async with build_store_client() as client:
        assert "accept" not in client.build_request("GET", "https://shop.example/p").headers
        request = client.build_request(
            "GET", "https://shop.example/p", headers={"Accept": "image/webp"}
        )
        assert request.headers.get_list("accept") == ["image/webp"]


async def test_duplicate_accept_and_cookie_are_joined(
    fake_wire: FakeWire, dns: HostNetwork
) -> None:
    fake_wire.default = OK
    async with build_store_client() as client:
        await client.get(
            "https://shop.example/p",
            headers=[("cookie", "x=1"), ("cookie", "y=2"), ("accept", "a"), ("accept", "b")],
        )
    assert fake_wire.sessions[0].requests[0]["headers"] == [
        ("Accept-Language", STORE_ACCEPT_LANGUAGE),
        ("Accept", "a, b"),
        ("Cookie", "x=1; y=2"),
    ]


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
async def test_methods_other_than_get_and_head_open_no_session(
    fake_wire: FakeWire, dns: HostNetwork, method: str
) -> None:
    async with build_store_client() as client:
        with pytest.raises(ValueError, match="only GET and HEAD"):
            await client.request(method, "https://shop.example/p")
        assert client._transport._slots._value == 10  # type: ignore[attr-defined]
    assert fake_wire.sessions == []


async def test_head_is_allowed(fake_wire: FakeWire, dns: HostNetwork) -> None:
    # A HEAD answer declares the encoding and length of a body it does not send:
    # no completeness check may read the empty body as truncated gzip.
    fake_wire.script = [
        FakeCurlResponse(headers=[("Content-Encoding", "gzip"), ("Content-Length", "1234")])
    ]
    async with build_store_client() as client:
        response = await client.head("https://shop.example/p")
    assert fake_wire.sessions[0].requests[0]["method"] == "HEAD"
    assert response.status_code == 200
    assert response.content == b""


# ── Responses ────────────────────────────────────────────────────


GZIP_PLAIN = gzip.compress(b"plain", mtime=0)


async def test_wire_encoding_headers_are_dropped_and_duplicates_kept(
    fake_wire: FakeWire, dns: HostNetwork, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_wire.script = [
        FakeCurlResponse(
            headers=[
                ("Content-Encoding", "gzip"),
                ("content-length", str(len(GZIP_PLAIN))),
                ("Transfer-Encoding", "chunked"),
                ("Set-Cookie", "a=1"),
                ("Set-Cookie", "b=2"),
                ("Server", "x"),
                ("Server", "y"),
            ],
            body=GZIP_PLAIN,
        )
    ]
    seen: list[httpx.Headers] = []
    real_read = fetch._read_capped

    async def spy(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
        seen.append(httpx.Headers(response.headers))
        return await real_read(response, max_bytes)

    monkeypatch.setattr(fetch, "_read_capped", spy)
    async with build_store_client() as client:
        response = await client.get("https://shop.example/p")
    # The reader saw the wire representation; the caller sees only the decoded body.
    (inner,) = seen
    assert inner.get_list("content-encoding") == ["gzip"]
    assert inner["content-length"] == str(len(GZIP_PLAIN))
    names = {name.lower() for name in response.headers}
    assert not names & {"content-encoding", "content-length", "transfer-encoding"}
    assert response.content == b"plain"
    assert len(response.headers.get_list("set-cookie")) == 2
    assert response.headers.get_list("server") == ["x", "y"]


@pytest.mark.parametrize(
    ("encoding_headers", "body"),
    [
        ([], b"plain"),
        ([("Content-Encoding", "identity")], b"plain"),
        ([("Content-Encoding", "gzip")], GZIP_PLAIN),
        ([("X-Extra", "1"), ("Content-Encoding", "gzip"), ("Server", "z")], GZIP_PLAIN),
    ],
)
async def test_accepted_wire_encodings_deliver_the_same_body(
    fake_wire: FakeWire,
    dns: HostNetwork,
    encoding_headers: list[tuple[str, str]],
    body: bytes,
) -> None:
    fake_wire.script = [FakeCurlResponse(headers=list(reversed(encoding_headers)), body=body)]
    async with build_store_client() as client:
        response = await client.get("https://shop.example/p")
        assert client._transport._slots._value == 10  # type: ignore[attr-defined]
    assert response.content == b"plain"
    assert fake_wire.sessions[0].closes == 1


@pytest.mark.parametrize(
    "encoding_headers",
    [
        [("Content-Encoding", "compress")],
        [("Content-Encoding", "gzip,")],
        [("Content-Encoding", "gzip, br")],
        [("Content-Encoding", "gzip"), ("Content-Encoding", "gzip")],
    ],
    ids=["unknown", "empty-element", "multiple", "duplicate"],
)
@pytest.mark.parametrize("status", [200, 403, 429, 503])
async def test_rejected_wire_encodings_are_never_silently_dropped(
    fake_wire: FakeWire, dns: HostNetwork, encoding_headers: list[tuple[str, str]], status: int
) -> None:
    fake_wire.script = [
        FakeCurlResponse(
            status_code=status,
            headers=[*encoding_headers, ("Retry-After", "60")],
            body=GZIP_PLAIN,
        )
    ]
    async with build_store_client() as client:
        if status == 200:
            with pytest.raises(httpx.DecodingError) as exc:
                await client.get("https://shop.example/p")
            assert is_retryable_http_error(exc.value) is False
        else:
            response = await client.get("https://shop.example/p")
            assert response.status_code == status
            assert response.headers["retry-after"] == "60"
            assert response.content == b""
        assert client._transport._slots._value == 10  # type: ignore[attr-defined]
    assert fake_wire.sessions[0].closes == 1


@pytest.mark.parametrize(
    ("status", "expected"),
    [(200, httpx.DecodingError), (403, HTTPBlockStatus), (404, ListingGone)],
)
async def test_fetch_page_keeps_status_precedence_over_a_damaged_body(
    fake_wire: FakeWire, dns: HostNetwork, status: int, expected: type[Exception]
) -> None:
    truncated = gzip.compress(b"<html>" + b"price 9.99 " * 500 + b"</html>")[:40]
    fake_wire.script = [
        FakeCurlResponse(status_code=status, headers=[("Content-Encoding", "gzip")], body=truncated)
    ]
    async with build_store_client() as client:
        with pytest.raises(expected):
            await fetch.fetch_page("https://shop.example/p", client)
        assert client._transport._slots._value == 10  # type: ignore[attr-defined]
    (session,) = fake_wire.sessions
    assert session.closes == 1


_JAR_CASES = {
    "returns_only_to_its_domain": [
        ("https://shop.example/p", ["a=1; Path=/", "b=2"], {}),
        ("https://shop.example/p", [], {"a": "1", "b": "2"}),
        ("https://other.example/p", [], {}),
    ],
    "foreign_domain_rejected": [
        ("https://shop.example/p", ["c=3; Domain=other.example"], {}),
        ("https://other.example/p", [], {}),
        ("https://shop.example/p", [], {}),
    ],
    "path_respected": [
        ("https://shop.example/p", ["p=1; Path=/account"], {}),
        ("https://shop.example/p", [], {}),
        ("https://shop.example/account/x", [], {"p": "1"}),
    ],
    "secure_not_sent_over_http": [
        ("https://shop.example/p", ["s=1; Secure"], {}),
        ("http://shop.example/p", [], {}),
        ("https://shop.example/p", [], {"s": "1"}),
    ],
    "max_age_zero_deletes": [
        ("https://shop.example/p", ["d=1"], {}),
        ("https://shop.example/p", ["d=1; Max-Age=0"], {"d": "1"}),
        ("https://shop.example/p", [], {}),
    ],
    "same_name_domain_path_replaces": [
        ("https://shop.example/p", ["v=1"], {}),
        ("https://shop.example/p", ["v=2"], {"v": "1"}),
        ("https://shop.example/p", [], {"v": "2"}),
    ],
}


@pytest.mark.parametrize("steps", list(_JAR_CASES.values()), ids=list(_JAR_CASES))
async def test_set_cookie_lands_in_the_jar_and_returns_only_to_its_domain(
    fake_wire: FakeWire,
    dns: HostNetwork,
    steps: list[tuple[str, list[str], dict[str, str]]],
) -> None:
    fake_wire.script = [
        FakeCurlResponse(headers=[("Set-Cookie", value) for value in set_cookies], body=b"ok")
        for _, set_cookies, _ in steps
    ]
    async with build_store_client() as client:
        for url, _, _ in steps:
            await client.get(url)
    for session, (url, _, expected) in zip(fake_wire.sessions, steps, strict=True):
        assert _sent_cookies(session.requests[0]) == expected, url


async def test_cookies_stay_with_their_domain_across_redirects(
    fake_wire: FakeWire, dns: HostNetwork
) -> None:
    fake_wire.script = [
        FakeCurlResponse(
            status_code=302,
            headers=[("Location", "https://other.example/q"), ("Set-Cookie", "t=1")],
        ),
        FakeCurlResponse(status_code=302, headers=[("Location", "https://shop.example/r")]),
        OK,
    ]
    async with build_store_client() as client:
        response = await client.get("https://shop.example/p", follow_redirects=True)
    assert response.status_code == 200
    shop, other, back = (session.requests[0] for session in fake_wire.sessions)
    assert _sent_cookies(shop) == {}
    assert _sent_cookies(other) == {}
    assert _sent_cookies(back) == {"t": "1"}


# ── Body ceiling inside the wire ─────────────────────────────────


@pytest.mark.parametrize("axis", ["wire", "decoded"])
@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("status", [200, 403, 429, 503])
@settings(max_examples=16, deadline=None)
@example(sizes=[])
@example(sizes=[1] * 1100)
@given(sizes=st.lists(st.integers(min_value=1, max_value=1100), max_size=12))
async def test_body_ceiling(axis: str, delta: int, status: int, sizes: list[int]) -> None:
    limit = 1024
    plain = b"a" * (limit + delta)
    if axis == "wire":
        # Identity body: the content callback is the only ceiling in play.
        wire_limit, decoded_limit = limit, 1 << 20
        headers: list[tuple[str, str]] = []
        body = plain
    else:
        # A compressible gzip body far below both ceilings on the wire: only the
        # decoded ceiling can refuse it (the fetch reader also cuts on wire bytes).
        wire_limit, decoded_limit = 1 << 20, limit
        headers = [("Content-Encoding", "gzip")]
        body = gzip.compress(plain, mtime=0)
        assert len(body) < limit
    pieces: list[int] = []
    for size in sizes:
        if sum(pieces) + size > len(body):
            break
        pieces.append(size)
    wire = FakeWire()
    wire.script = [
        FakeCurlResponse(status_code=status, headers=headers, body=body, chunk_sizes=tuple(pieces))
    ]
    net = HostNetwork()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket, "getaddrinfo", net.resolve)
        patch.setattr(store_wire, "_open_session", wire.open)
        patch.setattr(store_wire, "WIRE_MAX_BODY_BYTES", wire_limit)
        patch.setattr(store_wire, "DECODED_MAX_BODY_BYTES", decoded_limit)
        async with build_store_client() as client:
            if len(plain) > limit:
                with pytest.raises(BodyCeilingExceeded) as exc:
                    await client.get("https://shop.example/p")
                assert is_retryable_http_error(exc.value) is False
            else:
                response = await client.get("https://shop.example/p")
                assert response.content == plain
            assert client._transport._slots._value == 10  # type: ignore[attr-defined]
    (session,) = wire.sessions
    assert session.closes == 1


async def test_https_only_refuses_http_before_any_session(
    fake_wire: FakeWire, dns: HostNetwork
) -> None:
    async with build_store_client() as client:
        with pytest.raises(UnsafeURLError):
            await public_request(
                client, "GET", "http://shop.example/i.jpg", max_body_bytes=10, https_only=True
            )
    assert fake_wire.sessions == []
    assert dns.lookups == []


# ── Closed error map ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (
            curl_exceptions.RequestException(
                "bad encoding",
                code=CurlECode.BAD_CONTENT_ENCODING,
            ),
            httpx.DecodingError,
        ),
        (curl_exceptions.ConnectTimeout("connect timeout"), httpx.ConnectTimeout),
        (curl_exceptions.Timeout("timeout"), httpx.ReadTimeout),
        (curl_exceptions.ReadTimeout("read timeout"), httpx.ReadTimeout),
        (curl_exceptions.ConnectionError("refused"), httpx.ConnectError),
        (curl_exceptions.DNSError("dns"), httpx.ConnectError),
        (curl_exceptions.SSLError("tls"), httpx.ConnectError),
        (curl_exceptions.CertificateVerifyError("cert"), httpx.ConnectError),
        (curl_exceptions.IncompleteRead("short"), httpx.ReadError),
        (curl_exceptions.RequestException("other"), httpx.ReadError),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, BaseException) else None,
)
async def test_curl_errors_map_to_httpx_errors(
    fake_wire: FakeWire,
    dns: HostNetwork,
    raised: curl_exceptions.RequestException,
    expected: type[httpx.HTTPError],
) -> None:
    fake_wire.script = [raised]
    async with build_store_client() as client:
        with pytest.raises(expected) as exc:
            await client.get("https://shop.example/p")
        assert client._transport._slots._value == 10  # type: ignore[attr-defined]
    assert type(exc.value) is expected
    assert exc.value.__cause__ is raised
    assert exc.value.request.url.host == "shop.example"
    assert str(exc.value).startswith(f"store wire: {type(raised).__name__}: ")
    if expected is httpx.DecodingError:
        assert is_retryable_http_error(exc.value) is False
    (session,) = fake_wire.sessions
    assert session.closes == 1


async def test_connection_outside_the_validated_answer_is_refused(
    fake_wire: FakeWire, dns: HostNetwork
) -> None:
    fake_wire.script = [FakeCurlResponse(body=b"secret page", primary_ip="10.0.0.1")]
    async with build_store_client() as client:
        with pytest.raises(UnsafeURLError, match="validated address"):
            await client.get("https://shop.example/p")
        assert client._transport._slots._value == 10  # type: ignore[attr-defined]


async def test_deadline_cancels_and_closes_the_session(
    fake_wire: FakeWire, dns: HostNetwork
) -> None:
    fake_wire.script = [HANG]
    async with PublicAsyncClient(wire="chrome", deadline=0.05) as client:
        with pytest.raises(httpx.ReadTimeout):
            await client.get("https://shop.example/p")
        assert client._transport._slots._value == 10  # type: ignore[attr-defined]
    (session,) = fake_wire.sessions
    assert session.closes == 1


async def test_unknown_wire_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown wire"):
        PublicAsyncClient(wire="curl")  # type: ignore[arg-type]


async def test_build_store_client_uses_the_chrome_wire() -> None:
    async with build_store_client() as store, build_client() as plain:
        assert store._transport._wire == "chrome"  # type: ignore[attr-defined]
        assert plain._transport._wire == "httpx"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("encoding", "tail_kind"),
    [("gzip", "garbage"), ("deflate", "garbage"), ("gzip", "truncated")],
)
@settings(max_examples=32, deadline=None)
@given(extra=st.binary(max_size=64), cut=st.integers(min_value=0, max_value=1000))
async def test_compressed_tail_is_never_accepted(
    encoding: str, tail_kind: str, extra: bytes, cut: int
) -> None:
    compress = gzip.compress if encoding == "gzip" else zlib.compress
    tail = b"\x00" + extra
    if tail_kind == "truncated":
        frame = gzip.compress(extra)
        tail = frame[: 1 + cut % (len(frame) - 1)]
    wire = FakeWire()
    wire.script = [
        FakeCurlResponse(
            headers=[("Content-Encoding", encoding)],
            body=compress(b"first") + tail,
        )
    ]
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket, "getaddrinfo", HostNetwork().resolve)
        patch.setattr(store_wire, "_open_session", wire.open)
        async with build_store_client() as client:
            with pytest.raises(httpx.DecodingError) as exc:
                await client.get("https://shop.example/p")
    assert is_retryable_http_error(exc.value) is False


async def test_complete_gzip_members_are_all_delivered(
    fake_wire: FakeWire, dns: HostNetwork
) -> None:
    fake_wire.script = [
        FakeCurlResponse(
            headers=[("Content-Encoding", "gzip")],
            body=gzip.compress(b"first") + gzip.compress(b"second"),
        )
    ]
    async with build_store_client() as client:
        response = await client.get("https://shop.example/p")
    assert response.content == b"firstsecond"


@pytest.mark.parametrize("value", [b"caf\xe9", b"caf\xc3\xa9"])
async def test_response_header_bytes_survive_unchanged(
    fake_wire: FakeWire, dns: HostNetwork, value: bytes
) -> None:
    fake_wire.script = [
        FakeCurlResponse(
            headers=[("X-Name", value.decode("latin-1")), ("X-Name", "second")],
            body=b"ok",
        )
    ]
    async with build_store_client() as client:
        response = await client.get("https://shop.example/p")
    assert response.content == b"ok"
    assert response.headers.raw == [(b"X-Name", value), (b"X-Name", b"second")]
