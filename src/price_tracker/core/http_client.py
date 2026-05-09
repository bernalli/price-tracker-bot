"""Shared httpx async client factory."""

from __future__ import annotations

import httpx


def build_client(
    *,
    timeout: float = 30.0,
    connect_timeout: float = 10.0,
    max_connections: int = 10,
    max_keepalive_connections: int = 5,
) -> httpx.AsyncClient:
    """Build a configured httpx.AsyncClient.

    Caller is responsible for `await client.aclose()` (or `async with`).
    """
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout, connect=connect_timeout),
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
        ),
    )
