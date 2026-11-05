"""HTTP clients whose connections are bound to validated public addresses."""

from __future__ import annotations

import asyncio
import ipaddress
import math
import socket
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

import httpcore
import httpx

from price_tracker.core.url_utils import UnsafeURLError, _is_blocked_ip


def _origin(url: httpx.URL) -> tuple[str, bytes, int]:
    default_port = 443 if url.scheme == "https" else 80
    return (url.scheme, url.raw_host, url.port or default_port)


async def _resolve_public(url: httpx.URL) -> tuple[str, ...]:
    if url.scheme not in {"http", "https"} or not url.raw_host:
        raise UnsafeURLError("a public HTTP(S) destination is required")
    host = url.raw_host.decode("ascii")
    if "%" in host:
        raise UnsafeURLError("scoped addresses are not public destinations")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if literal.version != 4:
            raise UnsafeURLError("IPv6 destinations are not allowed")
        addresses = [literal]
    else:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host,
                url.port or (443 if url.scheme == "https" else 80),
                family=socket.AF_INET,
                type=socket.SOCK_STREAM,
                proto=socket.IPPROTO_TCP,
            )
            addresses = [
                address
                for info in infos
                if info[0] == socket.AF_INET
                and (address := ipaddress.ip_address(info[4][0])).version == 4
            ]
        except (OSError, ValueError, UnicodeError) as exc:
            raise UnsafeURLError("destination resolution failed") from exc
    if not addresses or any(_is_blocked_ip(address) for address in addresses):
        raise UnsafeURLError("destination has a non-public or empty address set")
    return tuple(dict.fromkeys(str(address) for address in addresses))


class _PinnedBackend(httpcore.AnyIOBackend):
    def __init__(self, addresses: tuple[str, ...]) -> None:
        self._addresses = addresses

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        for index, address in enumerate(self._addresses):
            try:
                return await super().connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout):
                if index == len(self._addresses) - 1:
                    raise
        raise httpcore.ConnectError("no validated addresses available")


class _ConnectionTransport(httpx.AsyncHTTPTransport):
    def __init__(self, addresses: tuple[str, ...]) -> None:
        super().__init__(trust_env=False)
        # HTTPX exposes no network_backend argument. Keep its response adapter
        # and exception mapping, replacing only the unused connection pool.
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            network_backend=_PinnedBackend(addresses),
            max_keepalive_connections=0,
        )


class _OwnedStream(httpx.AsyncByteStream):
    def __init__(
        self,
        stream: httpx.AsyncByteStream,
        transport: httpx.AsyncBaseTransport,
        slots: asyncio.Semaphore,
        active: set[_OwnedStream],
    ) -> None:
        self._stream = stream
        self._transport = transport
        self._slots = slots
        self._active = active
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._stream:
            yield chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            try:
                await self._stream.aclose()
            finally:
                await self._transport.aclose()
        finally:
            self._active.discard(self)
            self._slots.release()


class _PublicTransport(httpx.AsyncBaseTransport):
    def __init__(self, max_connections: int) -> None:
        self._slots = asyncio.Semaphore(max_connections)
        self._active: set[_OwnedStream] = set()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.extensions.get("https_only") is True and request.url.scheme != "https":
            raise UnsafeURLError("https is required")
        addresses = await _resolve_public(request.url)
        # Neither an arbitrary Host nor a caller's SNI override may separate
        # the HTTP/TLS identity from the URL whose address was validated.
        request.headers["Host"] = httpx.Request(request.method, request.url).headers["Host"]
        request.extensions["sni_hostname"] = request.url.raw_host.decode("ascii")
        await self._slots.acquire()
        transport = _ConnectionTransport(addresses)
        try:
            response = await transport.handle_async_request(request)
        except BaseException:
            self._slots.release()
            await transport.aclose()
            raise
        assert isinstance(response.stream, httpx.AsyncByteStream)
        owned = _OwnedStream(response.stream, transport, self._slots, self._active)
        self._active.add(owned)
        response.stream = owned
        return response

    async def aclose(self) -> None:
        for stream in tuple(self._active):
            await stream.aclose()


class _DeadlineStream(httpx.AsyncByteStream):
    def __init__(
        self, stream: httpx.AsyncByteStream, deadline: float, request: httpx.Request
    ) -> None:
        self._stream = stream
        self._deadline = deadline
        self._request = request

    async def __aiter__(self) -> AsyncIterator[bytes]:
        iterator = self._stream.__aiter__()
        try:
            while True:
                try:
                    async with asyncio.timeout_at(self._deadline):
                        chunk = await anext(iterator)
                except StopAsyncIteration:
                    break
                yield chunk
        except TimeoutError as exc:
            raise httpx.ReadTimeout(
                "overall request deadline exceeded", request=self._request
            ) from exc
        finally:
            await self._stream.aclose()

    async def aclose(self) -> None:
        await self._stream.aclose()


class PublicAsyncClient(httpx.AsyncClient):
    """HTTPX client with manual redirects and one deadline per request chain."""

    def __init__(self, *, deadline: float = 30.0, max_connections: int = 10, **kwargs: Any) -> None:
        alternate_transports = {"app", "mounts", "proxy", "transport", "trust_env"}.intersection(
            kwargs
        )
        if alternate_transports:
            names = ", ".join(sorted(alternate_transports))
            raise TypeError(f"PublicAsyncClient does not allow alternate transports: {names}")
        self._deadline_seconds = deadline
        super().__init__(
            transport=_PublicTransport(max_connections),
            trust_env=False,
            follow_redirects=False,
            max_redirects=5,
            **kwargs,
        )

    async def send(
        self,
        request: httpx.Request,
        *,
        stream: bool = False,
        auth: Any = httpx.USE_CLIENT_DEFAULT,
        follow_redirects: Any = httpx.USE_CLIENT_DEFAULT,
    ) -> httpx.Response:
        """Send through validated connections; bound the complete redirect chain."""
        follow = follow_redirects is True
        duration = request.extensions.get("public_deadline_seconds", self._deadline_seconds)
        deadline = asyncio.get_running_loop().time() + duration
        history: list[httpx.Response] = []
        try:
            async with asyncio.timeout_at(deadline):
                while True:
                    response = await super().send(
                        request, stream=True, auth=auth, follow_redirects=False
                    )
                    try:
                        assert isinstance(response.stream, httpx.AsyncByteStream)
                        response.stream = _DeadlineStream(response.stream, deadline, request)
                        response.history = list(history)
                        if not follow or response.next_request is None:
                            if not stream:
                                await response.aread()
                            return response
                        await response.aclose()
                        if len(history) >= self.max_redirects:
                            raise httpx.TooManyRedirects(
                                "redirect hop limit exceeded", request=request
                            )
                        next_request = response.next_request
                        next_request.extensions["https_only"] = request.extensions.get(
                            "https_only", False
                        )
                        if _origin(request.url) != _origin(next_request.url):
                            next_request.headers.pop("Authorization", None)
                        history.append(response)
                        request = next_request
                        auth = None
                    except BaseException:
                        await response.aclose()
                        raise
        except TimeoutError as exc:
            raise httpx.ReadTimeout("overall request deadline exceeded", request=request) from exc


def build_client(
    *,
    timeout: float = 30.0,
    connect_timeout: float = 10.0,
    max_connections: int = 10,
    max_keepalive_connections: int = 5,
) -> httpx.AsyncClient:
    """Build a public-only client; close it after use. Connections are not reused."""
    return PublicAsyncClient(
        deadline=timeout,
        max_connections=max_connections,
        timeout=httpx.Timeout(timeout, connect=connect_timeout),
    )


@asynccontextmanager
async def public_client(client: httpx.AsyncClient) -> AsyncIterator[PublicAsyncClient]:
    """Use a protected client even when a caller supplies an ordinary HTTPX client."""
    if isinstance(client, PublicAsyncClient):
        yield client
    else:
        async with PublicAsyncClient(
            headers=client.headers,
            cookies=client.cookies,
            timeout=client.timeout,
            auth=client.auth,
            params=client.params,
        ) as protected:
            yield protected


class BodyLimitError(Exception):
    """A capped response was refused before its body was trusted."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


async def public_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float | None = None,
    max_body_bytes: int | None = None,
    https_only: bool = False,
) -> httpx.Response:
    """Fetch a product through validated connections and manually checked redirects."""
    if https_only and max_body_bytes is None:
        raise ValueError("https_only requires a body cap")
    if max_body_bytes is not None:
        if (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or max_body_bytes < 1
        ):
            raise ValueError("max_body_bytes must be a positive integer")
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be finite and positive")
    async with public_client(client) as protected:
        if max_body_bytes is not None:
            request = protected.build_request(
                method,
                url,
                headers=headers,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
            request.extensions["https_only"] = https_only
            request.extensions["public_deadline_seconds"] = (
                min(protected._deadline_seconds, float(timeout))
                if timeout is not None
                else protected._deadline_seconds
            )
            response = await protected.send(request, stream=True, follow_redirects=True)
            try:
                if response.headers.get("content-encoding", "").strip().lower() not in (
                    "",
                    "identity",
                ):
                    raise BodyLimitError("encoded body")
                length = response.headers.get("content-length")
                if length is not None:
                    if not length.isascii() or not length.isdigit():
                        raise BodyLimitError("bad length")
                    digits = length.lstrip("0") or "0"
                    cap = str(max_body_bytes)
                    if len(digits) > len(cap) or (len(digits) == len(cap) and digits > cap):
                        raise BodyLimitError("too large")
                buf = bytearray()
                async for chunk in response.aiter_raw():
                    if len(buf) + len(chunk) > max_body_bytes:
                        raise BodyLimitError("too large")
                    buf.extend(chunk)
                return httpx.Response(
                    response.status_code,
                    headers=response.headers,
                    content=bytes(buf),
                    request=response.request,
                    history=response.history,
                )
            finally:
                await response.aclose()
        if timeout is None:
            return await protected.request(method, url, headers=headers, follow_redirects=True)
        return await protected.request(
            method, url, headers=headers, timeout=timeout, follow_redirects=True
        )
