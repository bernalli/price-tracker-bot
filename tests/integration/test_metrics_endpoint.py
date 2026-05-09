"""Integration tests for MetricsServer: /metrics endpoint over aiohttp."""

from __future__ import annotations

import socket

import pytest
from aiohttp import ClientSession
from prometheus_client import CollectorRegistry

from price_tracker.observability.metrics import MetricsRegistry, MetricsServer


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_prometheus_text() -> None:
    port = _free_port()
    reg = CollectorRegistry()
    metrics = MetricsRegistry(registry=reg)
    metrics.price_check_total.labels(scraper="amazon", domain="amazon.com", status="success").inc()
    server = MetricsServer(host="127.0.0.1", port=port, metrics=metrics)
    await server.start()
    try:
        url = f"http://127.0.0.1:{port}/metrics"
        async with ClientSession() as session, session.get(url) as resp:
            assert resp.status == 200
            ctype = resp.headers["Content-Type"]
            assert ctype.startswith("text/plain")
            body = await resp.text()
            assert "price_tracker_price_check_total" in body
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_metrics_endpoint_binds_localhost_only() -> None:
    port = _free_port()
    reg = CollectorRegistry()
    metrics = MetricsRegistry(registry=reg)
    server = MetricsServer(host="127.0.0.1", port=port, metrics=metrics)
    await server.start()
    try:
        # 127.0.0.1 reachable
        url = f"http://127.0.0.1:{port}/metrics"
        async with ClientSession() as session, session.get(url) as resp:
            assert resp.status == 200
        # Connection from a non-loopback interface alias must fail.
        # We assert by checking the server's actual bind sockets.
        # runner.sites is a set in aiohttp 3.13, so use list() to access first element.
        first_site = list(server._runner.sites)[0]  # type: ignore[union-attr]
        assert all(sock.getsockname()[0] == "127.0.0.1" for sock in first_site._server.sockets)
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_metrics_endpoint_404_other_paths() -> None:
    port = _free_port()
    reg = CollectorRegistry()
    server = MetricsServer(host="127.0.0.1", port=port, metrics=MetricsRegistry(registry=reg))
    await server.start()
    try:
        async with ClientSession() as session, session.get(f"http://127.0.0.1:{port}/") as resp:
            assert resp.status == 404
    finally:
        await server.stop()
