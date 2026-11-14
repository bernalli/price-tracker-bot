"""Damaged store bodies preserve refusals without changing successful responses."""

from __future__ import annotations

import gzip
import logging
import socket
import zlib

import brotli  # type: ignore[import-untyped]
import httpx
import pytest
import zstandard
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from price_tracker.core import store_wire
from price_tracker.core.http_client import build_store_client
from price_tracker.core.retry_policy import is_retryable_http_error
from tests.support.store_wire_fake import FakeCurlResponse, FakeWire
from tests.unit.test_public_connections import Network


@pytest.mark.parametrize("status", [200, 403, 429, 503])
@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "zstd"])
@pytest.mark.parametrize("damage", ["corrupt", "truncated"])
@settings(
    max_examples=24,
    deadline=None,
    # Clear the captured records for every generated example below.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    payload=st.binary(min_size=1, max_size=128),
    cut=st.integers(min_value=1, max_value=10000),
    retry_after=st.integers(min_value=0, max_value=86400),
)
async def test_damaged_body_preserves_only_non_success_responses(
    caplog: pytest.LogCaptureFixture,
    status: int,
    encoding: str,
    damage: str,
    payload: bytes,
    cut: int,
    retry_after: int,
) -> None:
    caplog.clear()
    plain = b"DO_NOT_LOG_BODY" + payload
    if encoding == "gzip":
        frame = gzip.compress(plain)
    elif encoding == "deflate":
        frame = zlib.compress(plain)
    elif encoding == "br":
        frame = brotli.compress(plain)
    else:
        frame = zstandard.ZstdCompressor().compress(plain)
    body = (
        frame[: 1 + cut % (len(frame) - 1)]
        if damage == "truncated"
        else b"\xff\xff\xff\xff" + payload
    )
    wire = FakeWire()
    wire.script = [
        FakeCurlResponse(
            status_code=status,
            headers=[
                ("Content-Encoding", encoding),
                ("Retry-After", str(retry_after)),
                ("X-Proof", "caf\xe9"),
                ("X-Proof", "DO_NOT_LOG_HEADER"),
            ],
            body=body,
        )
    ]
    with (
        pytest.MonkeyPatch.context() as patch,
        caplog.at_level(logging.WARNING, logger=store_wire.__name__),
    ):
        patch.setattr(socket, "getaddrinfo", Network().resolve)
        patch.setattr(store_wire, "_open_session", wire.open)
        async with build_store_client() as client:
            if status == 200:
                with pytest.raises(httpx.DecodingError) as exc:
                    await client.get("https://shop.example/p?DO_NOT_LOG_URL")
                assert is_retryable_http_error(exc.value) is False
            else:
                response = await client.get("https://shop.example/p?DO_NOT_LOG_URL")
                assert response.status_code == status
                assert response.content == b""
                assert response.headers.raw == [
                    (b"Retry-After", str(retry_after).encode("ascii")),
                    (b"X-Proof", b"caf\xe9"),
                    (b"X-Proof", b"DO_NOT_LOG_HEADER"),
                ]
    records = [record for record in caplog.records if record.name == store_wire.__name__]
    assert len(records) == (0 if status == 200 else 1)
    if records:
        assert str(status) in records[0].getMessage()
        assert "DO_NOT_LOG" not in records[0].getMessage()
        assert records[0].exc_info is None
    (session,) = wire.sessions
    assert len(session.requests) == 1
    assert session.closes == 1
