"""The Chrome store wire against a real loopback server, with the real curl_cffi."""

from __future__ import annotations

import gzip
import threading
import time
import zlib
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import brotli  # type: ignore[import-untyped]
import httpx
import pytest
from curl_cffi.const import CurlOpt
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core import http_client, store_wire
from price_tracker.core.http_client import PublicAsyncClient, build_store_client
from price_tracker.core.retry_policy import is_retryable_http_error
from price_tracker.core.store_wire import STORE_ACCEPT_LANGUAGE, BodyCeilingExceeded
from price_tracker.core.url_utils import UnsafeURLError

WIRE_HOST = "wire.test"
PLAIN = b"Store page sample for the Chrome wire. " * 8 + bytes(range(32, 127)) * 2
# A zstd frame of PLAIN (content size declared, no checksum), kept as bytes so the
# test needs no zstd encoder.
ZSTD_PLAIN = (
    b"(\xb5/\xfd`\xf6\x00\x9d\x04\x00d\x08Store page sample for the Chrome wire.  "
    b"!\"#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[\\]^_`"
    b"abcdefghijklmnopqrstuvwxyz{|}~\x02\x00\x1f\x8ba\x8awP\xaaz\x02"
)
SAMPLES: dict[str, bytes] = {
    "gzip": gzip.compress(PLAIN),
    "deflate": zlib.compress(PLAIN),
    "br": brotli.compress(PLAIN),
    "zstd": ZSTD_PLAIN,
}
Route = Callable[[BaseHTTPRequestHandler], None]


class LoopbackServer:
    """A threaded HTTP server on 127.0.0.1 whose routes the tests replace."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.seen: list[tuple[str, list[tuple[str, str]]]] = []
        self.closed = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _handle(self) -> None:
                path = self.path.split("?", 1)[0]
                server.seen.append((path, list(self.headers.items())))
                self.close_connection = True
                server.routes[path](self)

            def do_GET(self) -> None:  # noqa: N802 — http.server dispatch name
                self._handle()

            def do_HEAD(self) -> None:  # noqa: N802 — http.server dispatch name
                self._handle()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def url(self, path: str) -> str:
        return f"http://{WIRE_HOST}:{self.port}{path}"

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _send(
    handler: BaseHTTPRequestHandler,
    status: int,
    headers: list[tuple[str, str]],
    body: bytes = b"",
) -> None:
    handler.send_response(status)
    for name, value in headers:
        handler.send_header(name, value)
    handler.send_header("Connection", "close")
    handler.end_headers()
    if body and handler.command != "HEAD":
        handler.wfile.write(body)


@pytest.fixture(scope="module")
def server() -> Iterator[LoopbackServer]:
    loopback = LoopbackServer()
    loopback.start()
    try:
        yield loopback
    finally:
        loopback.stop()


@pytest.fixture(scope="module")
def wire_host() -> Iterator[None]:
    original = http_client._resolve_public

    async def resolve(url: httpx.URL) -> tuple[str, ...]:
        if url.raw_host == WIRE_HOST.encode("ascii"):
            return ("127.0.0.1",)
        return await original(url)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(http_client, "_resolve_public", resolve)
        yield


@pytest.fixture
def routes(server: LoopbackServer, wire_host: None) -> LoopbackServer:
    server.routes.clear()
    server.seen.clear()
    server.closed.clear()
    return server


class _Sessions:
    """Wrap the real session factory: record options and wire bytes, check closing."""

    def __init__(self) -> None:
        self.opened: list[Any] = []
        self.options: list[dict[Any, object]] = []
        self.wire_bytes = bytearray()
        self._real = store_wire._open_session

    def __call__(self, curl_options: dict[Any, object]) -> Any:
        session = self._real(curl_options)
        self.opened.append(session)
        self.options.append(curl_options)
        real_request = session.request
        wire_bytes = self.wire_bytes

        async def request(
            method: Any, url: str, *, content_callback: Callable[[bytes], object], **kwargs: Any
        ) -> Any:
            def record(chunk: bytes) -> object:
                wire_bytes.extend(chunk)
                return content_callback(chunk)

            return await real_request(method, url, content_callback=record, **kwargs)

        session.request = request  # type: ignore[method-assign, assignment]
        return session

    def all_closed(self) -> bool:
        return all(session._closed for session in self.opened)


def _header(headers: list[tuple[str, str]], name: str) -> str | None:
    values = [value for key, value in headers if key.lower() == name]
    return ", ".join(values) if values else None


async def test_real_request_carries_one_chrome_identity(
    routes: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = gzip.compress(b"hello chrome", mtime=0)
    routes.routes["/ok"] = lambda h: _send(
        h,
        200,
        [
            ("Content-Type", "text/plain"),
            ("Content-Encoding", "gzip"),
            ("Content-Length", str(len(payload))),
        ],
        payload,
    )
    sessions = _Sessions()
    monkeypatch.setattr(store_wire, "_open_session", sessions)
    async with build_store_client() as client:
        response = await client.get(routes.url("/ok"), headers={"User-Agent": "evil"})
    assert response.text == "hello chrome"
    assert "content-encoding" not in response.headers
    # curl handed over the compressed wire bytes; the Python reader decoded them.
    assert bytes(sessions.wire_bytes) == payload
    ((options),) = sessions.options
    assert options[CurlOpt.HTTP_CONTENT_DECODING] == 0
    assert sessions.all_closed()
    ((_, seen),) = routes.seen
    assert _header(seen, "accept-encoding") == "gzip, deflate, br, zstd"
    assert "Chrome/152." in (_header(seen, "user-agent") or "")
    assert '"Google Chrome";v="152"' in (_header(seen, "sec-ch-ua") or "")
    assert _header(seen, "accept-language") == STORE_ACCEPT_LANGUAGE
    rendered = repr(seen)
    assert "python-httpx" not in rendered
    assert "evil" not in rendered


async def test_real_cookies_round_trip_through_the_client_jar(routes: LoopbackServer) -> None:
    routes.routes["/set"] = lambda h: _send(
        h,
        200,
        [("Set-Cookie", "a=1; Path=/"), ("Set-Cookie", "b=2; Path=/"), ("Content-Length", "2")],
        b"ok",
    )
    routes.routes["/echo"] = lambda h: _send(h, 200, [("Content-Length", "2")], b"ok")
    async with build_store_client() as client:
        await client.get(routes.url("/set"))
        await client.get(routes.url("/echo"))
    (_, first), (_, second) = routes.seen
    assert _header(first, "cookie") is None
    cookie = _header(second, "cookie") or ""
    assert sorted(cookie.split("; ")) == ["a=1", "b=2"]


async def test_real_redirect_to_a_private_address_is_refused_after_one_request(
    routes: LoopbackServer,
) -> None:
    routes.routes["/go"] = lambda h: _send(
        h, 302, [("Location", "http://10.0.0.1/x"), ("Content-Length", "0")]
    )
    async with build_store_client() as client:
        with pytest.raises(UnsafeURLError):
            await client.get(routes.url("/go"), follow_redirects=True)
    assert len(routes.seen) == 1


_BOMB: list[bytes] = []


def _bomb_pieces() -> list[bytes]:
    """64 MiB of zeros, gzip-compressed once per module, in pieces."""
    if not _BOMB:
        compressor = zlib.compressobj(9, zlib.DEFLATED, 31)
        zeros = bytes(1024 * 1024)
        for _ in range(64):
            _BOMB.append(compressor.compress(zeros))
        _BOMB.append(compressor.flush())
    return _BOMB


async def test_real_compressed_bomb_is_refused_at_the_ceiling(
    routes: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    pieces = _bomb_pieces()

    def bomb(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Content-Length", str(sum(len(piece) for piece in pieces)))
        handler.send_header("Connection", "close")
        handler.end_headers()
        try:
            for piece in pieces:
                handler.wfile.write(piece)
        except OSError:
            return

    routes.routes["/bomb"] = bomb
    # The bomb fits the wire ceiling: only the decoded ceiling can stop it.
    assert sum(len(piece) for piece in pieces) < store_wire.WIRE_MAX_BODY_BYTES
    monkeypatch.setattr(store_wire, "DECODED_MAX_BODY_BYTES", 1024 * 1024)
    sessions = _Sessions()
    monkeypatch.setattr(store_wire, "_open_session", sessions)
    async with build_store_client() as client:
        with pytest.raises(BodyCeilingExceeded) as exc:
            await client.get(routes.url("/bomb"))
        assert client._transport._slots._value == 10  # type: ignore[attr-defined]
    assert is_retryable_http_error(exc.value) is False
    assert "decoded body ceiling" in str(exc.value)
    assert sessions.all_closed()


async def test_real_deadline_closes_the_connection(
    routes: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hang(handler: BaseHTTPRequestHandler) -> None:
        connection = handler.connection
        connection.settimeout(2.0)
        try:
            data = connection.recv(1)
        except (TimeoutError, OSError):
            return
        if data == b"":
            routes.closed.set()

    routes.routes["/hang"] = hang
    sessions = _Sessions()
    monkeypatch.setattr(store_wire, "_open_session", sessions)
    client = PublicAsyncClient(wire="chrome", deadline=0.5)
    started = time.monotonic()
    try:
        with pytest.raises(httpx.ReadTimeout):
            await client.get(routes.url("/hang"))
        assert time.monotonic() - started < 2.0
        remaining = max(0.0, 2.0 - (time.monotonic() - started))
        assert routes.closed.wait(remaining)
        assert client._transport._slots._value == 10  # type: ignore[attr-defined]
    finally:
        await client.aclose()
    assert sessions.all_closed()


@pytest.mark.parametrize("encoding", list(SAMPLES))
@settings(max_examples=8, deadline=None)
@given(cut=st.floats(min_value=0.0, max_value=1.0, exclude_max=True))
async def test_real_incomplete_encoding_never_becomes_a_page(
    server: LoopbackServer, wire_host: None, encoding: str, cut: float
) -> None:
    sample = SAMPLES[encoding]
    length = 1 + int(cut * (len(sample) - 1))  # 1 .. len(sample) - 1
    prefix = sample[:length]
    server.routes["/partial"] = lambda h: _send(
        h, 200, [("Content-Encoding", encoding), ("Content-Length", str(len(prefix)))], prefix
    )
    server.routes["/full"] = lambda h: _send(
        h, 200, [("Content-Encoding", encoding), ("Content-Length", str(len(sample)))], sample
    )
    sessions = _Sessions()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(store_wire, "_open_session", sessions)
        async with build_store_client() as client:
            with pytest.raises(httpx.HTTPError):
                await client.get(f"http://{WIRE_HOST}:{server.port}/partial")
            assert client._transport._slots._value == 10  # type: ignore[attr-defined]
            response = await client.get(f"http://{WIRE_HOST}:{server.port}/full")
            assert response.content == PLAIN
    assert sessions.all_closed()
