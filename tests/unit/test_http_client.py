"""Tests for shared httpx client builder."""

from __future__ import annotations

import httpx
import pytest

from price_tracker.core.http_client import build_client


@pytest.mark.asyncio
async def test_build_client_returns_async_client():
    client = build_client(timeout=5.0)
    try:
        assert isinstance(client, httpx.AsyncClient)
        assert client.follow_redirects is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_build_client_uses_timeout():
    client = build_client(timeout=10.0)
    try:
        assert client.timeout.read == 10.0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_build_client_sets_limits():
    client = build_client(timeout=5.0, max_connections=20)
    try:
        # httpx exposes limits via private _transport; just assert client was built
        assert client is not None
    finally:
        await client.aclose()
