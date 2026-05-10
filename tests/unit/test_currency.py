"""Tests for FX rate fetcher and conversion."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
import respx

from price_tracker.core.currency import (
    API_URL,
    CACHE_KEY,
    CACHE_TTL_HOURS,
    convert_to_eur,
    get_rates,
)


class _StubDB:
    """Minimal stand-in for the Database object: get_config / set_config."""

    def __init__(self, store: dict[str, str] | None = None) -> None:
        self.store = store or {}

    async def get_config(self, key: str) -> str | None:
        return self.store.get(key)

    async def set_config(self, key: str, value: str) -> None:
        self.store[key] = value


@pytest.mark.asyncio
async def test_get_rates_uses_cache_if_fresh():
    rates = {"USD": "1.08", "GBP": "0.85"}
    cached = json.dumps(
        {
            "rates": rates,
            "fetched_at": datetime.now(UTC).isoformat(),
        }
    )
    db = _StubDB({CACHE_KEY: cached})

    result = await get_rates(db)
    assert result["USD"] == Decimal("1.08")
    assert result["GBP"] == Decimal("0.85")


@pytest.mark.asyncio
async def test_get_rates_fetches_when_cache_expired():
    expired = (datetime.now(UTC) - timedelta(hours=CACHE_TTL_HOURS + 1)).isoformat()
    db = _StubDB({CACHE_KEY: json.dumps({"rates": {"USD": "1.0"}, "fetched_at": expired})})

    with respx.mock(assert_all_called=True) as router:
        router.get(API_URL).respond(200, json={"rates": {"USD": 1.10, "GBP": 0.86}})
        async with httpx.AsyncClient() as client:
            result = await get_rates(db, client)

    assert result["USD"] == Decimal("1.10")
    # Cache should have been updated
    assert CACHE_KEY in db.store


@pytest.mark.asyncio
async def test_get_rates_falls_back_when_api_unreachable():
    db = _StubDB()
    with respx.mock(assert_all_called=False) as router:
        router.get(API_URL).mock(side_effect=httpx.ConnectError("offline"))
        async with httpx.AsyncClient() as client:
            result = await get_rates(db, client)

    # Built-in fallback rates must contain at least USD/GBP/CHF
    assert "USD" in result
    assert "GBP" in result
    assert "CHF" in result


@pytest.mark.asyncio
async def test_convert_to_eur_passes_through_eur():
    db = _StubDB()
    result = await convert_to_eur(db, Decimal("100"), "EUR")
    assert result == Decimal("100")


@pytest.mark.asyncio
async def test_convert_to_eur_converts_usd():
    rates = {"USD": "1.08"}
    cached = json.dumps(
        {
            "rates": rates,
            "fetched_at": datetime.now(UTC).isoformat(),
        }
    )
    db = _StubDB({CACHE_KEY: cached})
    result = await convert_to_eur(db, Decimal("108"), "USD")
    # 108 USD / 1.08 (USD per EUR) = 100 EUR
    assert result == Decimal("100.00")


@pytest.mark.asyncio
async def test_convert_to_eur_unknown_currency_returns_unchanged():
    db = _StubDB(
        {
            CACHE_KEY: json.dumps(
                {
                    "rates": {"USD": "1.08"},
                    "fetched_at": datetime.now(UTC).isoformat(),
                }
            )
        }
    )
    result = await convert_to_eur(db, Decimal("100"), "ZZZ")
    assert result == Decimal("100")


@pytest.mark.asyncio
async def test_get_rates_corrupted_cache_falls_back_to_fetch():
    """A cache value that is not valid JSON triggers parse-error path then network fetch."""
    db = _StubDB({CACHE_KEY: "not-a-json-string"})
    with respx.mock(assert_all_called=False) as router:
        router.get(API_URL).respond(200, json={"rates": {"USD": 1.10}})
        async with httpx.AsyncClient() as client:
            result = await get_rates(db, client)
    assert result["USD"] == Decimal("1.10")


@pytest.mark.asyncio
async def test_get_rates_empty_api_response_falls_back():
    """API returning rates={} → fresh is None → fallback table is used."""
    db = _StubDB()
    with respx.mock(assert_all_called=False) as router:
        router.get(API_URL).respond(200, json={"rates": {}})
        async with httpx.AsyncClient() as client:
            result = await get_rates(db, client)
    # Empty rates → returns None from _fetch_fresh_rates → fallback
    assert result["USD"] == Decimal("1.08")  # fallback rate


@pytest.mark.asyncio
async def test_get_rates_creates_own_client_when_none_passed():
    """When no client is provided, get_rates should construct (and close) its own."""
    db = _StubDB()
    with respx.mock(assert_all_called=False) as router:
        router.get(API_URL).respond(200, json={"rates": {"USD": 1.05}})
        result = await get_rates(db, client=None)  # no client
    assert result["USD"] == Decimal("1.05")


@pytest.mark.asyncio
async def test_convert_to_eur_passes_through_none_price():
    db = _StubDB()
    # NOTE: testing defensive None handling at line 128
    result = await convert_to_eur(db, None, "USD")  # type: ignore[arg-type]
    assert result is None


def _count_currency_label(reg: object, label: str) -> float:
    """Sum samples for currency_lookups_total{result=<label>} on a registry."""
    from prometheus_client import CollectorRegistry  # noqa: PLC0415

    assert isinstance(reg, CollectorRegistry)
    return sum(
        sample.value
        for metric in reg.collect()
        if metric.name == "price_tracker_currency_lookups"
        for sample in metric.samples
        if sample.name == "price_tracker_currency_lookups_total"
        and sample.labels.get("result") == label
    )


@pytest.mark.asyncio
async def test_get_rates_network_error_emits_error_only_not_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock invariant: a network failure increments `error` exactly once and
    does NOT also increment `fallback` (the labels must be mutually exclusive)."""
    from prometheus_client import CollectorRegistry  # noqa: PLC0415

    from price_tracker.core import currency as currency_mod  # noqa: PLC0415
    from price_tracker.observability.metrics import MetricsRegistry  # noqa: PLC0415

    reg = CollectorRegistry()
    metrics = MetricsRegistry(registry=reg)
    db = _StubDB()

    async def _raise(_client: httpx.AsyncClient) -> dict[str, object] | None:
        raise httpx.NetworkError("simulated offline")

    monkeypatch.setattr(currency_mod, "_fetch_fresh_rates", _raise)

    async with httpx.AsyncClient() as client:
        result = await currency_mod.get_rates(db, client, metrics=metrics)

    # Static fallback table should still be returned
    assert "USD" in result
    # Mutual exclusion invariant
    assert _count_currency_label(reg, "error") == 1.0
    assert _count_currency_label(reg, "fallback") == 0.0
    # And `miss` was emitted once (cache empty path)
    assert _count_currency_label(reg, "miss") == 1.0


@pytest.mark.asyncio
async def test_get_rates_parse_failure_emits_fallback_only_not_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lock invariant: when `_fetch_fresh_rates` returns None for non-network
    reasons (e.g. parse failure / empty payload), `fallback` is incremented
    and `error` is NOT (mutually exclusive)."""
    from prometheus_client import CollectorRegistry  # noqa: PLC0415

    from price_tracker.core import currency as currency_mod  # noqa: PLC0415
    from price_tracker.observability.metrics import MetricsRegistry  # noqa: PLC0415

    reg = CollectorRegistry()
    metrics = MetricsRegistry(registry=reg)
    db = _StubDB()

    async def _none(_client: httpx.AsyncClient) -> dict[str, object] | None:
        return None  # parse failure / empty rates

    monkeypatch.setattr(currency_mod, "_fetch_fresh_rates", _none)

    async with httpx.AsyncClient() as client:
        result = await currency_mod.get_rates(db, client, metrics=metrics)

    assert "USD" in result
    assert _count_currency_label(reg, "fallback") == 1.0
    assert _count_currency_label(reg, "error") == 0.0
    assert _count_currency_label(reg, "miss") == 1.0
