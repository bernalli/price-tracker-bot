"""Tests for the fetch pipeline: block/gone detection order, revalidated redirects,
the body cap, and the fallback protocol.

Every negative test sits next to its positive control on the same code path.
Domains are examples (``f.example``, ``b.example``); nothing here talks to the
network — every request goes through respx.
"""

from __future__ import annotations

import gzip
import inspect
import re
import subprocess
import sys
import tracemalloc
import zlib
from pathlib import Path
from typing import TYPE_CHECKING

import brotli  # type: ignore[import-untyped]
import httpx
import pytest
import pytest_asyncio
import respx
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.core import fetch
from price_tracker.core.exceptions import (
    BlockEvent,
    CaptchaDetected,
    HTTPBlockStatus,
    ListingGone,
    WAFBlocked,
)
from price_tracker.core.fetch import (
    FallbackResponse,
    FetchedPage,
    _cap,
    _decode,
    fetch_page,
)
from price_tracker.core.identity import RequestedIdentity, check_echoes
from price_tracker.core.scraper_base import USER_AGENTS
from price_tracker.core.url_utils import UnsafeURLError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "price_tracker"


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(follow_redirects=True) as c:
        yield c


class FakeFallback:
    """A `FetchFallback` that returns queued results, or raises them, in order."""

    def __init__(self, name: str, *results: FallbackResponse | None | BaseException) -> None:
        self.name = name
        self._results: list[FallbackResponse | None | BaseException] = list(results)
        self.calls = 0
        self.seen_headers: dict[str, str] | None = None

    async def __call__(self, url: str, *, headers: dict[str, str]) -> FallbackResponse | None:
        self.calls += 1
        self.seen_headers = headers
        if not self._results:
            return None
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ValidateSpy:
    """Records every URL passed to it and lets the target through."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, url: str) -> None:
        self.calls.append(url)


# ── 5.1 Block prima dello stato — percorso primario (I1) ─────────────────────


@pytest.mark.parametrize("status", [403, 429])
async def test_hard_status_is_block_before_status_error(client, status):
    url = "https://f.example/p/A"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(status, text="blocked"))
        with pytest.raises(HTTPBlockStatus) as exc_info:
            await fetch_page(url, client)
    assert exc_info.value.status == status
    assert exc_info.value.url == url


@pytest.mark.parametrize(
    ("provider", "block_body", "positive_body"),
    [
        (
            "cloudflare",
            "Just a moment...",
            '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
            "<title>Widget</title>",
        ),
        (
            "akamai",
            "Access Denied",
            '<script src="/akam/13/1a2b3c"></script><title>Widget</title>',
        ),
        (
            "imperva",
            "Incapsula incident ID",
            '<script src="/_sec/cp_challenge/loader.js"></script><title>Widget</title>',
        ),
    ],
)
async def test_waf_body_with_200_is_block(client, provider, block_body, positive_body):
    url = "https://f.example/p/A"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(200, text=block_body))
        with pytest.raises(WAFBlocked) as exc_info:
            await fetch_page(url, client)
    assert exc_info.value.provider == provider

    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(200, text=positive_body))
        page = await fetch_page(url, client)
    assert page.status == 200


async def test_captcha_challenge_is_block_and_bootstrap_script_is_not(client):
    url = "https://f.example/p/A"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(
            return_value=httpx.Response(200, text='<form id="captcha-form"></form>')
        )
        with pytest.raises(CaptchaDetected) as exc_info:
            await fetch_page(url, client)
    assert exc_info.value.marker == "captcha-form"

    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(
            return_value=httpx.Response(
                200,
                text='<script id="captcha-bootstrap"></script><title>Widget</title>',
            )
        )
        page = await fetch_page(url, client)
    assert page.status == 200


async def test_hard_status_wins_over_waf_body(client):
    url = "https://f.example/p/A"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(403, text="Just a moment..."))
        with pytest.raises(HTTPBlockStatus):
            await fetch_page(url, client)


async def test_block_on_intermediate_hop_is_raised_with_hop_url(client):
    url_a = "https://f.example/p/A"
    url_b = "https://f.example/p/B"
    with respx.mock(assert_all_called=False) as router:
        router.get(url_a).mock(return_value=httpx.Response(302, headers={"Location": url_b}))
        router.get(url_b).mock(return_value=httpx.Response(403, text="blocked"))
        with pytest.raises(HTTPBlockStatus) as exc_info:
            await fetch_page(url_a, client)
    assert exc_info.value.url == url_b


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_server_errors_are_status_errors_not_blocks(client, status):
    url = "https://f.example/p/A"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(status, text="oops"))
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_page(url, client)


async def test_redirect_without_location_is_status_error(client):
    url = "https://f.example/p/A"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(302))
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_page(url, client)


@pytest.mark.parametrize("status", [400, 401, 418])
async def test_other_client_errors_are_status_errors(client, status):
    url = "https://f.example/p/A"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(status, text="nope"))
        with pytest.raises(httpx.HTTPStatusError):
            await fetch_page(url, client)


# ── 5.2 Gone non è blocco (I6) ────────────────────────────────────────────────


@pytest.mark.parametrize("status", [404, 410])
async def test_gone_status_is_listing_gone(client, status):
    url = "https://f.example/p/A"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(status, text="gone"))
        with pytest.raises(ListingGone) as exc_info:
            await fetch_page(url, client)
    assert exc_info.value.status == status
    assert not isinstance(exc_info.value, BlockEvent)


async def test_gone_on_last_hop_carries_hop_url(client):
    url_a = "https://f.example/p/A"
    url_b = "https://f.example/p/B"
    with respx.mock(assert_all_called=False) as router:
        router.get(url_a).mock(return_value=httpx.Response(302, headers={"Location": url_b}))
        router.get(url_b).mock(return_value=httpx.Response(410, text="gone"))
        with pytest.raises(ListingGone) as exc_info:
            await fetch_page(url_a, client)
    assert exc_info.value.url == url_b
    assert exc_info.value.status == 410


async def test_gone_is_terminal_without_fallbacks(client):
    url = "https://f.example/p/A"
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url).mock(return_value=httpx.Response(404, text="gone"))
        with pytest.raises(ListingGone):
            await fetch_page(url, client)
    assert route.calls.call_count == 1


@pytest.mark.parametrize("status", [404, 410])
async def test_gone_status_wins_over_waf_or_captcha_body(client, status):
    url = "https://f.example/p/A"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(status, text="Access Denied"))
        with pytest.raises(ListingGone) as exc_info:
            await fetch_page(url, client)
    assert not isinstance(exc_info.value, BlockEvent)

    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(
            return_value=httpx.Response(
                status, text='<div class="g-recaptcha" data-sitekey="x"></div>'
            )
        )
        with pytest.raises(ListingGone) as exc_info:
            await fetch_page(url, client)
    assert not isinstance(exc_info.value, BlockEvent)


# ── 5.3 Redirect seguiti a mano e rivalidati (D3) ─────────────────────────────


async def test_redirect_is_followed_and_recorded(client):
    url_a = "https://f.example/p/A"
    url_b = "https://f.example/p/B"
    with respx.mock(assert_all_called=False) as router:
        router.get(url_a).mock(return_value=httpx.Response(302, headers={"Location": url_b}))
        router.get(url_b).mock(return_value=httpx.Response(200, text="B"))
        page = await fetch_page(url_a, client)
    assert page.url_requested == url_a
    assert page.url_final == url_b
    assert page.redirects == (url_b,)
    assert page.status == 200
    assert page.text == "B"
    assert page.via == "httpx"


async def test_relative_location_is_resolved_against_current_hop(client):
    url = "https://f.example/rel"
    target = "https://f.example/p/target"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(301, headers={"Location": "/p/target"}))
        router.get(target).mock(return_value=httpx.Response(200, text="ok"))
        page = await fetch_page(url, client)
    assert page.url_final == target


async def test_chain_of_exactly_max_redirects_succeeds(client):
    base = "https://f.example/p/{}"
    with respx.mock(assert_all_called=False) as router:
        current = base.format("0")
        for i in range(1, 6):
            nxt = base.format(str(i))
            router.get(current).mock(return_value=httpx.Response(302, headers={"Location": nxt}))
            current = nxt
        router.get(current).mock(return_value=httpx.Response(200, text="final"))
        page = await fetch_page(base.format("0"), client, max_redirects=5)
    assert len(page.redirects) == 5
    assert list(page.redirects) == [base.format(str(i)) for i in range(1, 6)]
    assert page.text == "final"


async def test_chain_beyond_max_redirects_stops_before_requesting_the_next_target(client):
    base = "https://f.example/p/{}"
    with respx.mock(assert_all_called=False) as router:
        current = base.format("0")
        for i in range(1, 7):
            nxt = base.format(str(i))
            router.get(current).mock(return_value=httpx.Response(302, headers={"Location": nxt}))
            current = nxt
        sixth_target_route = router.get(base.format("6")).mock(
            return_value=httpx.Response(200, text="never")
        )
        with pytest.raises(httpx.TooManyRedirects):
            await fetch_page(base.format("0"), client, max_redirects=5)
    assert sixth_target_route.called is False


@pytest.mark.parametrize("addr", ["169.254.169.254", "127.0.0.1", "[::1]"])
async def test_hop_to_non_public_address_is_refused_and_never_requested(client, addr):
    url_a = "https://f.example/p/A"
    private_target = f"http://{addr}/latest"
    with respx.mock(assert_all_called=False) as router:
        router.get(url_a).mock(
            return_value=httpx.Response(302, headers={"Location": private_target})
        )
        private_route = router.get(private_target).mock(
            side_effect=AssertionError("must never be requested")
        )
        with pytest.raises(UnsafeURLError):
            await fetch_page(url_a, client)
    assert private_route.called is False


@pytest.mark.parametrize(
    ("location", "reason"),
    [
        ("ftp://a.example/x", "different host"),
        ("ftp://f.example/x", "same host, different scheme"),
    ],
)
async def test_hop_to_non_http_scheme_is_refused_and_never_requested(client, location, reason):
    url_a = "https://f.example/p/A"
    with respx.mock(assert_all_called=False) as router:
        router.get(url_a).mock(return_value=httpx.Response(302, headers={"Location": location}))
        target_route = router.get(location).mock(
            side_effect=AssertionError(f"must never be requested ({reason})")
        )
        with pytest.raises(UnsafeURLError):
            await fetch_page(url_a, client)
    assert target_route.calls.call_count == 0


async def test_same_host_hop_skips_revalidation_and_cross_host_hop_triggers_it(client, monkeypatch):
    url_a = "https://f.example/p/A"
    url_b = "https://f.example/p/B"
    spy = ValidateSpy()
    monkeypatch.setattr(fetch, "validate_public_url", spy)
    with respx.mock(assert_all_called=False) as router:
        router.get(url_a).mock(return_value=httpx.Response(302, headers={"Location": url_b}))
        router.get(url_b).mock(return_value=httpx.Response(200, text="B"))
        await fetch_page(url_a, client)
    assert spy.calls == []

    cross_b = "https://b.example/p/B"
    spy2 = ValidateSpy()
    monkeypatch.setattr(fetch, "validate_public_url", spy2)
    with respx.mock(assert_all_called=False) as router:
        router.get(url_a).mock(return_value=httpx.Response(302, headers={"Location": cross_b}))
        router.get(cross_b).mock(return_value=httpx.Response(200, text="B"))
        await fetch_page(url_a, client)
    assert spy2.calls == [cross_b]


async def test_redirect_loop_terminates(client):
    url = "https://f.example/loop"
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url).mock(return_value=httpx.Response(302, headers={"Location": url}))
        with pytest.raises(httpx.TooManyRedirects):
            await fetch_page(url, client, max_redirects=5)
    assert route.calls.call_count == 6


async def test_redirect_chain_reaches_check_echoes(client):
    # identity.registrable_domain needs a real public-suffix TLD (offline PSL
    # snapshot has no bare ".example"); example.com is the IANA-reserved one.
    url_a = "https://f.example.com/p/A"
    url_b = "https://f.example.com/p/B"
    with respx.mock(assert_all_called=False) as router:
        router.get(url_a).mock(return_value=httpx.Response(302, headers={"Location": url_b}))
        router.get(url_b).mock(return_value=httpx.Response(200, text="B"))
        page = await fetch_page(url_a, client)
    requested = RequestedIdentity.from_url(url_a)
    check = check_echoes(requested, final_url=page.url_final)
    assert check.error_code == "identity_mismatch"
    assert url_a.rstrip("/") in check.error or url_a in check.error
    assert url_b.rstrip("/") in check.error or url_b in check.error

    # positive control: no redirect at all
    with respx.mock(assert_all_called=False) as router:
        router.get(url_a).mock(return_value=httpx.Response(200, text="A"))
        page_no_redirect = await fetch_page(url_a, client)
    assert check_echoes(requested, final_url=page_no_redirect.url_final).ok is True

    # positive control: redirected, but canonical points back at the requested URL
    with respx.mock(assert_all_called=False) as router:
        router.get(url_a).mock(return_value=httpx.Response(302, headers={"Location": url_b}))
        router.get(url_b).mock(return_value=httpx.Response(200, text="B"))
        page_canonical = await fetch_page(url_a, client)
    assert check_echoes(requested, final_url=page_canonical.url_final, canonical=url_a).ok is True


# ── 5.4 Limite del corpo (D4) ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("size", "expected_truncated", "expected_len"),
    [(1023, False, 1023), (1024, False, 1024), (1025, True, 1024)],
)
async def test_body_cap_boundary(client, size, expected_truncated, expected_len):
    url = "https://f.example/p/cap"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(200, text="a" * size))
        page = await fetch_page(url, client, max_bytes=1024)
    assert page.truncated is expected_truncated
    assert len(page.text) == expected_len


async def test_cap_counts_decoded_bytes_not_wire_bytes(client):
    url = "https://f.example/p/gz"
    decoded = b"y" * (300 * 1024)
    compressed = gzip.compress(decoded)
    max_bytes = 100 * 1024
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(
            return_value=httpx.Response(
                200, content=compressed, headers={"content-encoding": "gzip"}
            )
        )
        page = await fetch_page(url, client, max_bytes=max_bytes)
    assert page.truncated is True
    assert len(page.text.encode()) == max_bytes


async def test_reading_stops_at_the_cap(client):
    url = "https://f.example/p/stream"
    counter = {"calls": 0}

    async def gen() -> AsyncIterator[bytes]:
        for _ in range(100):
            counter["calls"] += 1
            yield b"a" * 1024

    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(200, content=gen()))
        page = await fetch_page(url, client, max_bytes=4096)
    assert counter["calls"] <= 5
    assert page.truncated is True


@pytest.mark.parametrize("encoding", ["deflate", "br"])
async def test_compressed_bomb_never_inflates_beyond_the_step(client, encoding):
    n = 8 * 1024 * 1024
    raw_zeros = b"\x00" * n
    if encoding == "deflate":
        compressed = zlib.compress(raw_zeros, 9)
    else:
        compressed = brotli.compress(raw_zeros, quality=5)
    url = f"https://f.example/p/bomb-{encoding}"

    # content must be an async generator, not raw bytes: httpx.Response(content=bytes)
    # builds a ByteStream and eagerly self-decodes it at construction time (see
    # httpx._models.Response.__init__), which would decompress the whole bomb before
    # fetch_page ever runs and defeat the very thing this test measures.
    async def body() -> AsyncIterator[bytes]:
        yield compressed

    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(
            return_value=httpx.Response(200, content=body(), headers={"content-encoding": encoding})
        )
        tracemalloc.start()
        tracemalloc.reset_peak()
        try:
            page = await fetch_page(url, client, max_bytes=64 * 1024)
        finally:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
    assert page.truncated is True
    assert len(page.text) == 64 * 1024
    assert peak < 1024 * 1024


async def test_wire_cap_stops_a_stream_that_produces_no_output(client):
    url = "https://f.example/p/emptyblocks"
    chunk = b"\x00\x00\x00\xff\xff" * 200  # 200 raw-deflate stored-empty blocks, 1000 B
    counter = {"calls": 0}

    async def gen() -> AsyncIterator[bytes]:
        for _ in range(200):
            counter["calls"] += 1
            yield chunk

    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(
            return_value=httpx.Response(200, content=gen(), headers={"content-encoding": "deflate"})
        )
        page = await fetch_page(url, client, max_bytes=4096)
    assert page.truncated is True
    assert page.text == ""
    assert counter["calls"] <= 6


@pytest.mark.parametrize("encoding", ["zstd", "gzip, br"])
async def test_unknown_or_multiple_content_encoding_is_a_decoding_error(client, encoding):
    url = "https://f.example/p/badenc"

    # An async generator, not raw bytes: httpx.Response(content=bytes) builds a
    # ByteStream and eagerly self-decodes it at construction time, which would
    # raise deep inside httpx's own decoder before fetch_page ever sees the body.
    async def body() -> AsyncIterator[bytes]:
        yield b"data"

    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(
            return_value=httpx.Response(200, content=body(), headers={"content-encoding": encoding})
        )
        with pytest.raises(httpx.DecodingError):
            await fetch_page(url, client)

    url_identity = "https://f.example/p/goodenc-identity"
    with respx.mock(assert_all_called=False) as router:
        router.get(url_identity).mock(
            return_value=httpx.Response(200, text="ok", headers={"content-encoding": "identity"})
        )
        page = await fetch_page(url_identity, client)
    assert page.text == "ok"

    url_absent = "https://f.example/p/goodenc-absent"
    with respx.mock(assert_all_called=False) as router:
        router.get(url_absent).mock(return_value=httpx.Response(200, text="ok2"))
        page = await fetch_page(url_absent, client)
    assert page.text == "ok2"


async def test_deflate_corruption_after_the_header_is_settled_is_a_decoding_error(client):
    """Coverage completeness, not a numbered §5 item: once the zlib/raw-deflate
    choice succeeds on the first wire chunk, a later corrupted chunk must not leak
    a raw ``zlib.error`` — it is folded into ``httpx.DecodingError`` like every
    other malformed body, so callers that only catch ``httpx.HTTPError``/``ValueError``
    (P4) still see it."""
    payload = b"x" * 5000
    compressed = zlib.compress(payload, 9)
    split = len(compressed) // 2
    first, second = compressed[:split], compressed[split:]
    corrupted_second = bytes(b ^ 0xFF for b in second)
    url = "https://f.example/p/deflate-corrupt"

    async def gen() -> AsyncIterator[bytes]:
        yield first
        yield corrupted_second

    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(
            return_value=httpx.Response(200, content=gen(), headers={"content-encoding": "deflate"})
        )
        with pytest.raises(httpx.DecodingError):
            await fetch_page(url, client)


async def test_brotli_decoding_spans_more_than_one_process_call(client):
    """Coverage completeness, not a numbered §5 item: a decoded body larger than
    one brotli ``output_buffer_limit`` chunk must keep decoding, at the step size,
    across the ``can_accept_more_data`` loop — not just the first call."""
    n = 8 * 1024 * 1024
    compressed = brotli.compress(b"\x00" * n, quality=5)
    max_bytes = 256 * 1024  # a few times the ~96 KiB a single brotli call yields
    url = "https://f.example/p/brotli-multistep"

    async def body() -> AsyncIterator[bytes]:
        yield compressed

    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(
            return_value=httpx.Response(200, content=body(), headers={"content-encoding": "br"})
        )
        page = await fetch_page(url, client, max_bytes=max_bytes)
    assert page.truncated is True
    assert len(page.text) == max_bytes


async def test_cut_inside_a_multibyte_char_does_not_raise(client):
    url = "https://f.example/p/multibyte"
    body_text = "é" * 1000
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(200, text=body_text))
        page = await fetch_page(url, client, max_bytes=999)
    assert page.truncated is True
    assert page.text[-1] in ("é", "�")


@pytest.mark.parametrize("charset", ["iso-8859-1", "klingon", None])
async def test_charset_header_is_honoured_and_unknown_charset_falls_back(client, charset):
    url = "https://f.example/p/charset"
    body = "café".encode("latin-1") if charset == "iso-8859-1" else "café".encode()
    headers = {}
    if charset is not None:
        headers["content-type"] = f"text/html; charset={charset}"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(200, content=body, headers=headers))
        page = await fetch_page(url, client)
    assert page.text == "café"


async def test_block_detection_runs_on_truncated_body(client):
    max_bytes = 1024
    url_hard = "https://f.example/p/hardblock"
    with respx.mock(assert_all_called=False) as router:
        router.get(url_hard).mock(
            return_value=httpx.Response(403, text="blocked" + "x" * (3 * max_bytes))
        )
        with pytest.raises(HTTPBlockStatus):
            await fetch_page(url_hard, client, max_bytes=max_bytes)

    url_waf = "https://f.example/p/wafblock"
    with respx.mock(assert_all_called=False) as router:
        router.get(url_waf).mock(
            return_value=httpx.Response(200, text="Just a moment..." + "x" * (3 * max_bytes))
        )
        with pytest.raises(WAFBlocked):
            await fetch_page(url_waf, client, max_bytes=max_bytes)


@given(body=st.binary(max_size=4096), n=st.integers(1, 4096))
@settings(deadline=None)
def test_cap_is_pure_and_total(body, n):
    assert _cap(body, n) == (body[:n], len(body) > n)


# ── 5.5 Fallback (D5, D6) ──────────────────────────────────────────────────────


async def test_fallback_page_after_primary_block(client):
    url = "https://f.example/p/A"
    fb = FakeFallback("A", FallbackResponse(status=200, text="from A", url_final=url))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(403, text="blocked"))
        page = await fetch_page(url, client, fallbacks=(fb,))
    assert page.via == "A"
    assert page.text == "from A"
    assert page.redirects == ()


async def test_all_paths_blocked_raises_the_first_block(client):
    # A and B both answer 429 (never 403, and each on its own distinct url_final)
    # so that "the first block wins" is actually distinguishable from "the last
    # block wins": if it weren't, a mutation that raised B's block instead of the
    # primary's would still pass on a same-status, same-url coincidence.
    url = "https://f.example/p/A"
    fb_a = FakeFallback(
        "A", FallbackResponse(status=429, text="blocked A", url_final="https://f.example/p/A-fbA")
    )
    fb_b = FakeFallback(
        "B", FallbackResponse(status=429, text="blocked B", url_final="https://f.example/p/A-fbB")
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(403, text="blocked primary"))
        with pytest.raises(HTTPBlockStatus) as exc_info:
            await fetch_page(url, client, fallbacks=(fb_a, fb_b))
    assert exc_info.value.status == 403
    assert exc_info.value.url == url
    assert fb_a.calls == 1
    assert fb_b.calls == 1


async def test_fallbacks_run_in_order_and_none_is_skipped(client):
    url = "https://f.example/p/A"
    fb_a = FakeFallback("A", None)
    fb_b = FakeFallback("B", FallbackResponse(status=200, text="from B", url_final=url))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(403, text="blocked"))
        page = await fetch_page(url, client, fallbacks=(fb_a, fb_b))
    assert page.via == "B"
    assert fb_a.calls == 1
    assert fb_b.calls == 1


async def test_fallbacks_never_run_after_primary_success(client):
    url = "https://f.example/p/A"
    fb_a = FakeFallback("A", FallbackResponse(status=200, text="from A", url_final=url))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(200, text="primary ok"))
        page = await fetch_page(url, client, fallbacks=(fb_a,))
    assert page.via == "httpx"
    assert fb_a.calls == 0


async def test_transport_error_then_fallback_page_or_reraise(client):
    url = "https://f.example/p/A"
    fb_a = FakeFallback("A", FallbackResponse(status=200, text="from A", url_final=url))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(side_effect=httpx.ConnectError("boom"))
        page = await fetch_page(url, client, fallbacks=(fb_a,))
    assert page.via == "A"

    fb_none = FakeFallback("A", None)
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(side_effect=httpx.ConnectError("boom"))
        with pytest.raises(httpx.ConnectError):
            await fetch_page(url, client, fallbacks=(fb_none,))


async def test_gone_on_primary_then_fallback_page_is_kept_or_gone_reraised(client):
    url = "https://f.example/p/A"
    fb_page = FakeFallback("A", FallbackResponse(status=200, text="from A", url_final=url))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(404, text="gone"))
        page = await fetch_page(url, client, fallbacks=(fb_page,))
    assert page.via == "A"

    fb_none = FakeFallback("A", None)
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(404, text="gone"))
        with pytest.raises(ListingGone):
            await fetch_page(url, client, fallbacks=(fb_none,))


async def test_waf_body_from_fallback_is_detected_not_returned(client):
    url = "https://f.example/p/A"
    fb_waf = FakeFallback("A", FallbackResponse(status=200, text="Just a moment...", url_final=url))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(503, text="server busy"))
        with pytest.raises(WAFBlocked):
            await fetch_page(url, client, fallbacks=(fb_waf,))

    benign = (
        '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>'
        '<script src="/akam/13/1a2b3c"></script>'
        '<script src="/_sec/cp_challenge/loader.js"></script><title>Widget</title>'
    )
    fb_benign = FakeFallback("A", FallbackResponse(status=200, text=benign, url_final=url))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(503, text="server busy"))
        page = await fetch_page(url, client, fallbacks=(fb_benign,))
    assert page.via == "A"


async def test_gone_from_fallback_is_deferred_then_raised(client):
    url = "https://f.example/p/A"
    fb_a_url_final = "https://f.example/p/A-final"
    fb_a = FakeFallback("A", FallbackResponse(status=404, text="gone", url_final=fb_a_url_final))
    fb_b_none = FakeFallback("B", None)
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(503, text="busy"))
        with pytest.raises(ListingGone) as exc_info:
            await fetch_page(url, client, fallbacks=(fb_a, fb_b_none))
    assert exc_info.value.url == fb_a_url_final

    fb_a2 = FakeFallback("A", FallbackResponse(status=404, text="gone", url_final=fb_a_url_final))
    fb_b_page = FakeFallback("B", FallbackResponse(status=200, text="from B", url_final=url))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(503, text="busy"))
        page = await fetch_page(url, client, fallbacks=(fb_a2, fb_b_page))
    assert page.via == "B"


async def test_fallback_final_url_on_non_public_host_is_refused(client, monkeypatch):
    url = "https://f.example/p/A"
    fb = FakeFallback(
        "A",
        FallbackResponse(status=200, text="ok", url_final="http://169.254.169.254/x"),
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(403, text="blocked"))
        with pytest.raises(UnsafeURLError):
            await fetch_page(url, client, fallbacks=(fb,))

    same_host_final = "https://f.example/p/A-2"
    fb_same = FakeFallback("A", FallbackResponse(status=200, text="ok", url_final=same_host_final))
    spy_same = ValidateSpy()
    monkeypatch.setattr(fetch, "validate_public_url", spy_same)
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(403, text="blocked"))
        await fetch_page(url, client, fallbacks=(fb_same,))
    assert spy_same.calls == []

    other_public_final = "https://b.example/p/B"
    fb_other = FakeFallback(
        "A", FallbackResponse(status=200, text="ok", url_final=other_public_final)
    )
    spy_other = ValidateSpy()
    monkeypatch.setattr(fetch, "validate_public_url", spy_other)
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(403, text="blocked"))
        await fetch_page(url, client, fallbacks=(fb_other,))
    assert spy_other.calls == [other_public_final]


async def test_fallback_exception_propagates(client):
    url = "https://f.example/p/A"
    fb = FakeFallback("A", RuntimeError("boom"))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(403, text="blocked"))
        with pytest.raises(RuntimeError, match="boom"):
            await fetch_page(url, client, fallbacks=(fb,))


async def test_fallback_non_2xx_or_empty_is_skipped(client):
    url = "https://f.example/p/A"
    fb_a = FakeFallback("A", FallbackResponse(status=500, text="x", url_final=url))
    fb_b = FakeFallback("B", FallbackResponse(status=200, text="", url_final=url))
    fb_c = FakeFallback("C", FallbackResponse(status=200, text="ok", url_final=url))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(403, text="blocked"))
        page = await fetch_page(url, client, fallbacks=(fb_a, fb_b, fb_c))
    assert page.via == "C"


async def test_fallback_text_is_capped(client):
    url = "https://f.example/p/A"
    max_bytes = 1024
    fb = FakeFallback("A", FallbackResponse(status=200, text="z" * (max_bytes + 1), url_final=url))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(403, text="blocked"))
        page = await fetch_page(url, client, fallbacks=(fb,), max_bytes=max_bytes)
    assert page.truncated is True
    assert len(page.text.encode()) == max_bytes


async def test_fallback_receives_the_same_headers_as_the_primary(client):
    url = "https://f.example/p/A"
    fb = FakeFallback("A", FallbackResponse(status=200, text="ok", url_final=url))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(403, text="blocked"))
        await fetch_page(url, client, fallbacks=(fb,), headers={"Accept-Language": "de-DE"})
    assert fb.seen_headers is not None
    assert fb.seen_headers["User-Agent"] in USER_AGENTS
    assert fb.seen_headers["Accept-Language"] == "de-DE"


async def test_unsafe_hop_on_primary_does_not_try_fallbacks(client):
    url = "https://f.example/p/A"
    fb = FakeFallback("A", FallbackResponse(status=200, text="ok", url_final=url))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(
            return_value=httpx.Response(302, headers={"Location": "http://127.0.0.1/"})
        )
        with pytest.raises(UnsafeURLError):
            await fetch_page(url, client, fallbacks=(fb,))
    assert fb.calls == 0


async def test_fallback_redirects_field(client):
    url = "https://f.example/p/A"
    fb_same = FakeFallback("A", FallbackResponse(status=200, text="ok", url_final=url))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(403, text="blocked"))
        page = await fetch_page(url, client, fallbacks=(fb_same,))
    assert page.redirects == ()

    other_url = "https://f.example/p/A-final"
    fb_other = FakeFallback("A", FallbackResponse(status=200, text="ok", url_final=other_url))
    with respx.mock(assert_all_called=False) as router:
        router.get(url).mock(return_value=httpx.Response(403, text="blocked"))
        page2 = await fetch_page(url, client, fallbacks=(fb_other,))
    assert page2.redirects == (other_url,)


# ── 5.6 Intestazioni ───────────────────────────────────────────────────────────


async def test_default_headers_rotate_user_agent_and_extra_headers_merge(client):
    url_a = "https://f.example/p/A"
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url_a).mock(return_value=httpx.Response(200, text="ok"))
        await fetch_page(url_a, client)
    ua = route.calls.last.request.headers["user-agent"]
    assert ua in USER_AGENTS

    with respx.mock(assert_all_called=False) as router:
        route = router.get(url_a).mock(return_value=httpx.Response(200, text="ok"))
        await fetch_page(url_a, client, headers={"Accept-Language": "de-DE"})
    req_headers = route.calls.last.request.headers
    assert req_headers["accept-language"] == "de-DE"
    assert req_headers["user-agent"] in USER_AGENTS

    url_b = "https://f.example/p/B"
    url_c = "https://f.example/p/C"
    with respx.mock(assert_all_called=False) as router:
        router.get(url_a).mock(return_value=httpx.Response(302, headers={"Location": url_b}))
        router.get(url_b).mock(return_value=httpx.Response(302, headers={"Location": url_c}))
        route_c = router.get(url_c).mock(return_value=httpx.Response(200, text="ok"))
        await fetch_page(url_a, client, headers={"Accept-Language": "de-DE"})
        all_calls = [c.request for c in router.calls]
        uas = {r.headers["user-agent"] for r in all_calls}
        als = {r.headers["accept-language"] for r in all_calls}
        assert len(uas) == 1
        assert als == {"de-DE"}
        assert route_c.called

    cross_b = "https://b.example/p/B"
    with respx.mock(assert_all_called=False) as router:
        router.get(url_a).mock(return_value=httpx.Response(302, headers={"Location": cross_b}))
        route_cross = router.get(cross_b).mock(return_value=httpx.Response(200, text="ok"))
        await fetch_page(
            url_a,
            client,
            headers={"Authorization": "Bearer x", "Cookie": "a=b"},
        )
    cross_headers = route_cross.calls.last.request.headers
    assert "authorization" not in cross_headers
    assert "cookie" not in cross_headers

    same_host_target = "https://f.example/p/D"
    with respx.mock(assert_all_called=False) as router:
        router.get(url_a).mock(
            return_value=httpx.Response(302, headers={"Location": same_host_target})
        )
        route_same = router.get(same_host_target).mock(return_value=httpx.Response(200, text="ok"))
        await fetch_page(
            url_a,
            client,
            headers={"Authorization": "Bearer x", "Cookie": "a=b"},
        )
    same_headers = route_same.calls.last.request.headers
    assert same_headers["authorization"] == "Bearer x"
    assert "cookie" not in same_headers


# ── 5.7 Confini e costruzione ───────────────────────────────────────────────────


def test_fetch_has_no_callers_yet():
    """the PR that wires the first adapter replaces this tripwire with an
    allow-list of callers, as `test_price_core_has_documented_callers_only` did
    for the price core."""
    fetch_file = SRC_ROOT / "core" / "fetch.py"
    pattern = re.compile(
        r"price_tracker\.core\.fetch\b"
        r"|from\s+price_tracker\.core\s+import\s+[^\n]*\bfetch\b"
        r"|from\s+\.\s*import\s+[^\n]*\bfetch\b"
        r"|from\s+\.fetch\s+import"
    )
    offenders: list[str] = []
    for py in SRC_ROOT.rglob("*.py"):
        if py == fetch_file:
            continue
        text = py.read_text(encoding="utf-8")
        if pattern.search(text):
            offenders.append(str(py.relative_to(SRC_ROOT)))
    assert not offenders, f"fetch.py already has undocumented callers: {offenders}"


def test_fetch_import_is_layer_clean():
    script = "\n".join(
        [
            "import sys",
            "import price_tracker.core.fetch",
            "print(chr(10).join(sorted(sys.modules)))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    loaded = set(result.stdout.splitlines())
    forbidden = (
        "price_tracker.bot",
        "price_tracker.app",
        "price_tracker.notifier",
        "price_tracker.charts",
        "price_tracker.db",
        "price_tracker.scrapers",
        "telegram",
        "bs4",
        "price_tracker.core.money",
        "price_tracker.core.pricegrammar",
        "price_tracker.core.anchoring",
        "price_tracker.core.identity",
        "price_tracker.core.structured_data",
    )
    for name in forbidden:
        assert name not in loaded, f"importing fetch pulled in {name!r}"
    # D9: fetch.py names none of the five core-price modules, even in comments or
    # docstrings. Matched as a qualified reference (as the boundary tripwire for the
    # nucleus itself does), not as a bare word: "identity" also names the ordinary
    # Content-Encoding value, unrelated to price_tracker.core.identity.
    source = inspect.getsource(fetch)
    core_module_ref = re.compile(
        r"price_tracker\.core\.(money|pricegrammar|anchoring|identity|structured_data)"
    )
    assert core_module_ref.search(source) is None


def test_fetched_page_rejects_inconsistent_construction():
    with pytest.raises(ValueError, match="does not match the last hop"):
        FetchedPage(
            url_requested="https://f.example/p/A",
            url_final="https://f.example/p/A",
            status=200,
            text="x",
            via="httpx",
            redirects=("https://f.example/p/B",),
            truncated=False,
        )
    with pytest.raises(ValueError, match="differs from url_requested"):
        FetchedPage(
            url_requested="https://f.example/p/A",
            url_final="https://f.example/p/B",
            status=200,
            text="x",
            via="httpx",
            redirects=(),
            truncated=False,
        )
    with pytest.raises(ValueError, match="is not 2xx"):
        FetchedPage(
            url_requested="https://f.example/p/A",
            url_final="https://f.example/p/A",
            status=404,
            text="x",
            via="httpx",
            redirects=(),
            truncated=False,
        )


async def test_fetch_page_rejects_bad_limits(client):
    url = "https://f.example/p/A"
    with respx.mock(assert_all_called=False) as router:
        route = router.get(url).mock(return_value=httpx.Response(200, text="ok"))
        with pytest.raises(ValueError, match="max_redirects"):
            await fetch_page(url, client, max_redirects=-1)
        assert route.calls.call_count == 0
        with pytest.raises(ValueError, match="max_bytes"):
            await fetch_page(url, client, max_bytes=0)
        assert route.calls.call_count == 0
        with pytest.raises(ValueError, match="unusable url"):
            await fetch_page("http://host:abc/", client)
        assert route.calls.call_count == 0


@given(
    body=st.binary(max_size=2048),
    encoding=st.sampled_from(["utf-8", "iso-8859-1", "klingon", "", None]),
)
@settings(deadline=None)
def test_decode_never_raises(body, encoding):
    result = _decode(body, encoding)
    assert isinstance(result, str)
