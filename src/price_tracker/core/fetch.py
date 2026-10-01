"""Fetch pipeline with block and gone detection ahead of every status decision.

Block detection and gone detection run on every response, from the primary client
and from every fallback, before any status handling. Redirects are followed one hop
at a time, and every hop connects through the public-address transport. The body
is read in bounded steps and cut at
a byte cap counted on the decoded output, never on the wire. Network and status
errors leave this module as ``httpx.HTTPError``; a redirect toward a non-public
destination leaves it as ``UnsafeURLError``, for a caller that already folds both
into a scrape result.
"""

from __future__ import annotations

import asyncio
import codecs
import logging
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

import brotli  # type: ignore[import-untyped]
import httpx

if TYPE_CHECKING:
    from collections.abc import Iterator

from price_tracker.core.exceptions import BlockEvent, ListingGone
from price_tracker.core.http_client import public_client
from price_tracker.core.scraper_base import (
    detect_block_event,
    detect_listing_gone,
    get_headers,
)
from price_tracker.core.url_utils import UnsafeURLError, validate_public_url

logger = logging.getLogger(__name__)

DEFAULT_MAX_BYTES: Final = 5 * 1024 * 1024
DEFAULT_MAX_REDIRECTS: Final = 5
_DECODE_STEP: Final = 64 * 1024
PRIMARY_VIA: Final = "httpx"


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """A page delivered by the primary client or by a fallback, always 2xx."""

    url_requested: str
    url_final: str
    status: int
    text: str
    via: str
    redirects: tuple[str, ...]
    truncated: bool

    def __post_init__(self) -> None:
        if self.redirects:
            if self.url_final != self.redirects[-1]:
                raise ValueError(
                    f"url_final {self.url_final!r} does not match the last hop "
                    f"{self.redirects[-1]!r}"
                )
        elif self.url_final != self.url_requested:
            raise ValueError(
                f"url_final {self.url_final!r} differs from url_requested "
                f"{self.url_requested!r} but redirects is empty"
            )
        if not (200 <= self.status < 300):
            raise ValueError(f"status {self.status} is not 2xx")


@dataclass(frozen=True, slots=True)
class FallbackResponse:
    """What a :class:`FetchFallback` answers, before capping and detection."""

    status: int
    text: str
    url_final: str


class FetchFallback(Protocol):
    """A named fetch alternative, tried only after the primary has failed.

    A fallback that follows redirects itself must revalidate every hop that
    changes origin with ``validate_public_url`` (or must not follow such hops).
    """

    name: str

    async def __call__(self, url: str, *, headers: dict[str, str]) -> FallbackResponse | None: ...


class _StepDecoder(Protocol):
    """Decodes raw wire bytes into decoded chunks, each at most ``_DECODE_STEP``."""

    def feed(self, data: bytes) -> Iterator[bytes]: ...

    def complete(self) -> bool: ...


class _IdentityDecoder:
    """No content-encoding: the wire bytes are the decoded bytes."""

    def feed(self, data: bytes) -> Iterator[bytes]:
        yield data

    def complete(self) -> bool:
        return True


class _ZlibDecoder:
    """gzip decoding, chunked so no single call inflates more than the step."""

    def __init__(self, wbits: int, request: httpx.Request) -> None:
        self._dec = zlib.decompressobj(wbits)
        self._request = request

    def feed(self, data: bytes) -> Iterator[bytes]:
        try:
            yield self._dec.decompress(data, _DECODE_STEP)
            while self._dec.unconsumed_tail:
                yield self._dec.decompress(self._dec.unconsumed_tail, _DECODE_STEP)
        except zlib.error as exc:
            raise httpx.DecodingError(str(exc), request=self._request) from exc

    def complete(self) -> bool:
        return self._dec.eof


class _DeflateDecoder:
    """deflate decoding with the zlib/raw-deflate fallback (parity with httpx)."""

    def __init__(self, request: httpx.Request) -> None:
        self._dec = zlib.decompressobj()
        self._first_attempt = True
        self._request = request

    def _decompress(self, data: bytes) -> bytes:
        if self._first_attempt:
            self._first_attempt = False
            try:
                return self._dec.decompress(data, _DECODE_STEP)
            except zlib.error:
                self._dec = zlib.decompressobj(-zlib.MAX_WBITS)
        return self._dec.decompress(data, _DECODE_STEP)

    def feed(self, data: bytes) -> Iterator[bytes]:
        # Every corruption (both header forms failing, a bad later chunk, a bad
        # tail) leaves as a transport error, never as a raw zlib exception.
        try:
            yield self._decompress(data)
            while self._dec.unconsumed_tail:
                yield self._dec.decompress(self._dec.unconsumed_tail, _DECODE_STEP)
        except zlib.error as exc:
            raise httpx.DecodingError(str(exc), request=self._request) from exc

    def complete(self) -> bool:
        return self._dec.eof


class _BrotliDecoder:
    """br decoding, chunked via brotli's own output-buffer-limit growth cap."""

    def __init__(self, request: httpx.Request) -> None:
        self._dec = brotli.Decompressor()
        self._request = request

    def feed(self, data: bytes) -> Iterator[bytes]:
        try:
            yield self._dec.process(data, output_buffer_limit=_DECODE_STEP)
            # Drain what the output limit held back. An empty step means the
            # decoder needs more input or has finished: stop, never spin.
            while not self._dec.is_finished():
                piece = self._dec.process(b"", output_buffer_limit=_DECODE_STEP)
                if not piece:
                    return
                yield piece
        except brotli.error as exc:
            raise httpx.DecodingError(str(exc), request=self._request) from exc

    def complete(self) -> bool:
        return bool(self._dec.is_finished())


def _decoder_for(content_encoding: str, request: httpx.Request) -> _StepDecoder:
    value = (content_encoding or "identity").strip().lower()
    if value in ("", "identity"):
        return _IdentityDecoder()
    if value == "gzip":
        return _ZlibDecoder(zlib.MAX_WBITS | 16, request)
    if value == "deflate":
        return _DeflateDecoder(request)
    if value == "br":
        return _BrotliDecoder(request)
    raise httpx.DecodingError(f"unsupported content-encoding {value!r}", request=request)


def _cap(body: bytes, max_bytes: int) -> tuple[bytes, bool]:
    """Cut ``body`` to ``max_bytes`` and say whether it was longer."""
    return body[:max_bytes], len(body) > max_bytes


_NOT_DOCUMENT_ENCODINGS: Final = frozenset({"punycode", "idna", "undefined"})


def _decode(body: bytes, encoding: str | None) -> str:
    """Decode ``body`` with ``encoding``, or utf-8 if it is unknown. Never raises.

    ``LookupError`` covers unknown and non-text codecs; ``UnicodeError`` covers
    codecs that refuse ``errors="replace"`` (``idna``, ``undefined``). ``punycode``
    is checked ahead of decoding instead of relying on that fallback: it is a
    hostname transform, not a document encoding, and with ``errors="replace"`` it
    does not always raise — it can turn a WAF or CAPTCHA marker into garbage
    instead, which hides the block from every fingerprint that looks for it.
    """
    name = encoding or "utf-8"
    try:
        canonical = codecs.lookup(name).name
    except LookupError:
        return body.decode("utf-8", errors="replace")
    if canonical in _NOT_DOCUMENT_ENCODINGS:
        return body.decode("utf-8", errors="replace")
    try:
        return body.decode(name, errors="replace")
    except (LookupError, UnicodeError):
        return body.decode("utf-8", errors="replace")


def _cap_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """Apply the same byte cap used on the primary body to a fallback's text."""
    capped, truncated = _cap(text.encode("utf-8"), max_bytes)
    return _decode(capped, "utf-8"), truncated


async def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    """Read ``response`` on the wire, decompressing to at most ``max_bytes`` bytes.

    Reads raw (compressed) bytes via ``aiter_raw`` and decompresses them a step at a
    time, so a highly compressible body cannot inflate memory beyond the step size
    before the cap is seen. Stops as soon as the decoded output — or the wire itself,
    for a stream that consumes input without producing output — passes the cap.
    """
    buf = bytearray()
    wire_read = 0
    truncated = False
    try:
        # Inside the try: a rejected encoding must still release the connection.
        content_encoding = response.headers.get("content-encoding", "identity")
        decoder = _decoder_for(content_encoding, response.request)
        async for raw_chunk in response.aiter_raw():
            wire_read += len(raw_chunk)
            for piece in decoder.feed(raw_chunk):
                if piece:
                    buf.extend(piece)
                if len(buf) > max_bytes:
                    truncated = True
                    break
            if truncated or wire_read > max_bytes:
                truncated = True
                break
        if not truncated and wire_read and not decoder.complete():
            # A compressed body that stops before the end of its own stream is
            # damaged, not a short page: never hand it on as a complete one.
            raise httpx.DecodingError(
                "compressed body ended before the end of its stream", request=response.request
            )
    finally:
        await response.aclose()
    body, was_cut = _cap(bytes(buf), max_bytes)
    return body, truncated or was_cut


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    return (url.scheme, url.host, url.port)


async def _fetch_primary(
    url: str,
    client: httpx.AsyncClient,
    *,
    hdrs: dict[str, str],
    max_redirects: int,
    max_bytes: int,
) -> FetchedPage:
    current = url
    hops: list[str] = []
    request = client.build_request("GET", url, headers=hdrs)
    while True:
        try:
            response = await client.send(
                request,
                stream=True,
                follow_redirects=False,
                auth=httpx.USE_CLIENT_DEFAULT if not hops else None,
            )
        except httpx.InvalidURL as exc:
            # httpx builds next_request inside send(); a Location it cannot turn
            # into a URL surfaces as InvalidURL, which is not an HTTPError.
            raise httpx.RemoteProtocolError(
                f"unusable redirect target: {exc}", request=request
            ) from exc
        decode_error: httpx.DecodingError | None = None
        try:
            body, truncated = await _read_capped(response, max_bytes)
        except httpx.DecodingError as exc:
            # The status line still decides gone and hard blocks when the body
            # cannot be decoded; the decoding error surfaces only after that.
            decode_error, body, truncated = exc, b"", False
        text = _decode(body, response.encoding)

        # Gone is checked before block: the two status sets are disjoint,
        # so this only decides the one overlapping case — a 404/410 whose body also
        # carries a WAF/CAPTCHA marker, which is a removed listing, not a block.
        detect_listing_gone(status_code=response.status_code, url=current)

        if response.has_redirect_location and response.next_request is not None:
            if len(hops) == max_redirects:
                raise httpx.TooManyRedirects(
                    f"exceeded {max_redirects} redirects fetching {url}",
                    request=request,
                )
            next_request = response.next_request
            next_url = str(next_request.url)
            hops.append(next_url)
            current = next_url
            # httpx already built next_request with its own redirect policy
            # (Cookie dropped for the jar to reattach, Authorization dropped only
            # cross-origin, Host updated); rebuilding it from hdrs would resend the
            # caller's Cookie/Authorization to a host that never asked for them.
            request = next_request
            continue

        # Only a response that will not be followed reaches block detection and
        # status handling: the body of a followed redirect is never inspected.
        detect_block_event(status_code=response.status_code, body=text, url=current)
        if decode_error is not None:
            raise decode_error
        response.raise_for_status()
        return FetchedPage(
            url_requested=url,
            url_final=current,
            status=response.status_code,
            text=text,
            via=PRIMARY_VIA,
            redirects=tuple(hops),
            truncated=truncated,
        )


async def _fetch_page(
    url: str,
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str] | None = None,
    fallbacks: tuple[FetchFallback, ...] = (),
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> FetchedPage:
    """Fetch ``url``, trying every fallback in order if the primary cannot deliver.

    Raises ``BlockEvent`` or ``ListingGone`` subclasses on a block or a removed
    listing, ``httpx.HTTPError`` on a network or status error, and ``UnsafeURLError``
    (a ``ValueError``) if a redirect targets a non-public destination — that last one
    is never retried through the fallbacks.
    """
    if max_redirects < 0:
        raise ValueError(f"max_redirects must be >= 0, got {max_redirects}")
    if max_bytes <= 0:
        raise ValueError(f"max_bytes must be > 0, got {max_bytes}")
    try:
        httpx.URL(url)
    except httpx.InvalidURL as exc:
        raise ValueError(f"unusable url {url!r}: {exc}") from exc

    hdrs = get_headers(headers)

    try:
        return await _fetch_primary(
            url, client, hdrs=hdrs, max_redirects=max_redirects, max_bytes=max_bytes
        )
    except (BlockEvent, ListingGone, httpx.HTTPError) as exc:
        primary_failure: BlockEvent | ListingGone | httpx.HTTPError = exc

    deferred: BlockEvent | ListingGone | None = (
        primary_failure if not isinstance(primary_failure, httpx.HTTPError) else None
    )
    for fallback in fallbacks:
        result = await fallback(url, headers=hdrs)
        if result is None:
            continue
        text, truncated = _cap_text(result.text, max_bytes)
        try:
            detect_listing_gone(status_code=result.status, url=result.url_final)
            detect_block_event(status_code=result.status, body=text, url=result.url_final)
        except (BlockEvent, ListingGone) as exc:
            if deferred is None:
                deferred = exc
            logger.debug("fallback %s deferred a block/gone", fallback.name)
            continue
        if not (200 <= result.status < 300) or not text:
            continue
        try:
            final = httpx.URL(result.url_final)
        except httpx.InvalidURL as exc:
            raise UnsafeURLError(
                f"fallback {fallback.name} returned an unusable url_final {result.url_final!r}"
            ) from exc
        if _origin(final) != _origin(httpx.URL(url)):
            await asyncio.to_thread(validate_public_url, result.url_final)
        logger.debug("fallback %s delivered the page", fallback.name)
        return FetchedPage(
            url_requested=url,
            url_final=result.url_final,
            status=result.status,
            text=text,
            via=fallback.name,
            redirects=(result.url_final,) if result.url_final != url else (),
            truncated=truncated,
        )
    if deferred is not None:
        raise deferred
    raise primary_failure


async def fetch_page(
    url: str,
    client: httpx.AsyncClient,
    *,
    headers: dict[str, str] | None = None,
    fallbacks: tuple[FetchFallback, ...] = (),
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    max_bytes: int = DEFAULT_MAX_BYTES,
    deadline: float = 30.0,
) -> FetchedPage:
    """Fetch through validated connections with one deadline for all hops and bodies.

    Fallbacks are trusted local code, like installed scraper plugins, and must
    perform any network I/O through ``public_request`` or ``build_client``.
    """
    try:
        async with asyncio.timeout(deadline), public_client(client) as protected:
            return await _fetch_page(
                url,
                protected,
                headers=headers,
                fallbacks=fallbacks,
                max_redirects=max_redirects,
                max_bytes=max_bytes,
            )
    except TimeoutError as exc:
        raise httpx.ReadTimeout("overall fetch deadline exceeded") from exc
