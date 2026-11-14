"""One request per hop over a Chrome-fingerprinted connection to a validated address.

``_PublicTransport`` resolves and validates every hop first; this module only
connects to the addresses it was given, pinned with curl's ``RESOLVE`` option. It
resolves no name and follows no redirect: the client follows each redirect after
validating it again.
"""

from __future__ import annotations

import ipaddress
import re
from typing import TYPE_CHECKING, Final, Literal

import certifi
import httpx
from curl_cffi.const import CurlECode, CurlOpt
from curl_cffi.curl import CURL_WRITEFUNC_ERROR
from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import ConnectionError as CurlConnectionError
from curl_cffi.requests.exceptions import ConnectTimeout as CurlConnectTimeout
from curl_cffi.requests.exceptions import RequestException as CurlRequestException
from curl_cffi.requests.exceptions import Timeout as CurlTimeout

from price_tracker.core.url_utils import UnsafeURLError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from curl_cffi.requests import Response as CurlResponse

STORE_IMPERSONATE: Final = "chrome152"
STORE_ACCEPT_LANGUAGE: Final = "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7"
WIRE_MAX_BODY_BYTES: Final = 16 * 1024 * 1024
DECODED_MAX_BODY_BYTES: Final = 16 * 1024 * 1024
_ALLOWED_METHODS: Final = frozenset({"GET", "HEAD"})
_DROPPED_RESPONSE_HEADERS: Final = frozenset(
    {"content-encoding", "content-length", "transfer-encoding"}
)
_CURL_IPRESOLVE_V4: Final = 1
_LABEL: Final = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DNS_NAME_RE: Final = re.compile(rf"(?:{_LABEL}\.)+[a-z](?:[a-z0-9-]{{0,61}}[a-z0-9])?")


class BodyCeilingExceeded(httpx.TransportError):
    """A store response exceeded its wire or decoded body ceiling."""


def _open_session(curl_options: dict[CurlOpt, object]) -> AsyncSession[CurlResponse]:
    return AsyncSession(
        impersonate=STORE_IMPERSONATE,
        max_clients=1,
        trust_env=False,
        allow_redirects=False,
        discard_cookies=True,
        # A CA bundle path: curl_cffi's annotation says bool, its runtime takes a path.
        verify=certifi.where(),  # type: ignore[arg-type]
        curl_options=curl_options,
    )


def _is_canonical_ipv4(host: str, addresses: tuple[str, ...]) -> bool:
    try:
        literal = ipaddress.IPv4Address(host)
    except ValueError:
        return False
    return str(literal) == host and addresses == (host,)


def _positive_seconds(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return None


def _timeout(request: httpx.Request) -> float | tuple[float, float] | None:
    t = request.extensions.get("timeout") or {}
    connect, read = _positive_seconds(t.get("connect")), _positive_seconds(t.get("read"))
    if connect is not None and read is not None:
        return (connect, read)
    return connect if connect is not None else read


def _mapped_error(
    exc: CurlRequestException, *, exceeded: bool, request: httpx.Request
) -> httpx.TransportError | httpx.DecodingError:
    message = f"store wire: {type(exc).__name__}: {str(exc)[:200]}"
    if exceeded:
        return BodyCeilingExceeded(message, request=request)
    if exc.code == CurlECode.BAD_CONTENT_ENCODING:
        return httpx.DecodingError(message, request=request)
    if isinstance(exc, CurlConnectTimeout):
        return httpx.ConnectTimeout(message, request=request)
    if isinstance(exc, CurlTimeout):
        return httpx.ReadTimeout(message, request=request)
    if isinstance(exc, CurlConnectionError):
        return httpx.ConnectError(message, request=request)
    return httpx.ReadError(message, request=request)


class _DecodedBodyStream(httpx.AsyncByteStream):
    """Apply the fetch reader before exposing any decoded store body."""

    def __init__(self, raw_response: httpx.Response) -> None:
        self._raw_response = raw_response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        from price_tracker.core.fetch import _read_capped  # noqa: PLC0415

        body, truncated = await _read_capped(self._raw_response, DECODED_MAX_BODY_BYTES)
        if truncated:
            raise BodyCeilingExceeded(
                "store response exceeded the decoded body ceiling",
                request=self._raw_response.request,
            )
        yield body

    async def aclose(self) -> None:
        await self._raw_response.aclose()


class ChromeConnection(httpx.AsyncBaseTransport):
    """One Chrome-fingerprinted request to an address that the caller already validated."""

    def __init__(self, addresses: tuple[str, ...]) -> None:
        self._addresses = addresses

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Send one GET or HEAD, pinned to the validated addresses; never follow a redirect.

        Only ``_PublicTransport.handle_async_request`` calls this, after its
        ``https_only`` check and after ``_resolve_public`` produced the addresses.
        """
        addresses = self._addresses
        method = request.method.upper()
        if method not in _ALLOWED_METHODS:
            raise ValueError("the store wire sends only GET and HEAD")
        curl_method: Literal["GET", "HEAD"] = "GET" if method == "GET" else "HEAD"
        scheme = request.url.scheme
        if scheme not in {"http", "https"}:
            raise UnsafeURLError("the store wire accepts only HTTP(S) destinations")
        host = request.url.raw_host.decode("ascii")
        if not (
            _is_canonical_ipv4(host, addresses)
            or (len(host) <= 253 and _DNS_NAME_RE.fullmatch(host))
        ):
            raise UnsafeURLError(
                "the store wire accepts only plain DNS names and canonical IPv4 addresses"
            )
        default = 443 if scheme == "https" else 80
        port = request.url.port or default
        path = request.url.raw_path.decode("ascii")
        if any(not "!" <= char <= "~" for char in path):
            raise UnsafeURLError("the store wire accepts only printable ASCII paths")
        target = f"{scheme}://{host}{'' if port == default else f':{port}'}{path}"
        resolve_entry = f"{host}:{port}:{','.join(addresses)}"

        headers: list[tuple[str, str]] = [("Accept-Language", STORE_ACCEPT_LANGUAGE)]
        accept = request.headers.get_list("accept")
        if accept:
            headers.append(("Accept", ", ".join(accept)))
        cookie = request.headers.get_list("cookie")
        if cookie:
            headers.append(("Cookie", "; ".join(cookie)))

        buf = bytearray()
        exceeded = False

        def sink(chunk: bytes) -> int:
            nonlocal exceeded
            if len(buf) + len(chunk) > WIRE_MAX_BODY_BYTES:
                exceeded = True
                return CURL_WRITEFUNC_ERROR
            buf.extend(chunk)
            return len(chunk)

        session = _open_session(
            {
                CurlOpt.RESOLVE: [resolve_entry],
                CurlOpt.IPRESOLVE: _CURL_IPRESOLVE_V4,
                CurlOpt.PROTOCOLS_STR: "http,https",
                CurlOpt.REDIR_PROTOCOLS_STR: "http,https",
                CurlOpt.FOLLOWLOCATION: 0,
                CurlOpt.PROXY: "",
                CurlOpt.NOPROXY: "",
                CurlOpt.HTTP_CONTENT_DECODING: 0,
            }
        )
        try:
            response = await session.request(
                curl_method,
                target,
                headers=headers,
                content_callback=sink,
                timeout=_timeout(request),
            )
        except CurlRequestException as exc:
            raise _mapped_error(exc, exceeded=exceeded, request=request) from exc
        finally:
            await session.close()

        if response.primary_ip not in addresses:
            raise UnsafeURLError("the connection did not use a validated address")
        # curl_cffi types header values as optional; a parsed response line always has one.
        wire_headers = [
            (name, value) for name, value in response.headers.multi_items() if value is not None
        ]
        # The inner response keeps the wire's encoding headers for the fetch reader;
        # stream=, never content=, so HTTPX does not decode it on its own.
        raw_response = httpx.Response(
            response.status_code,
            headers=wire_headers,
            stream=httpx.ByteStream(bytes(buf)),
            request=request,
        )
        return httpx.Response(
            response.status_code,
            headers=[
                (name, value)
                for name, value in wire_headers
                if name.lower() not in _DROPPED_RESPONSE_HEADERS
            ],
            stream=_DecodedBodyStream(raw_response),
            request=request,
        )

    async def aclose(self) -> None:
        """Nothing stays open after a request: the session closes with it."""
