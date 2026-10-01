"""Disabled synchronous fetching cannot block the event loop or open sockets."""

from __future__ import annotations

import sys
import types

import pytest

from price_tracker.scrapers.amazon import _fetch_via_scrapling


async def test_scrapling_fallback_never_invokes_fetcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even an installed backend is never entered, synchronously or in a thread."""

    def forbidden(url: str, **kwargs: object) -> None:
        pytest.fail("disabled synchronous fetcher was invoked")

    module = types.ModuleType("scrapling")
    module.Fetcher = types.SimpleNamespace(get=forbidden)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "scrapling", module)
    assert await _fetch_via_scrapling("https://shop.example/product") is None
