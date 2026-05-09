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


@pytest.mark.asyncio()
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


@pytest.mark.asyncio()
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


@pytest.mark.asyncio()
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


@pytest.mark.asyncio()
async def test_convert_to_eur_passes_through_eur():
    db = _StubDB()
    result = await convert_to_eur(db, Decimal("100"), "EUR")
    assert result == Decimal("100")


@pytest.mark.asyncio()
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


@pytest.mark.asyncio()
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


@pytest.mark.asyncio()
async def test_get_rates_corrupted_cache_falls_back_to_fetch():
    """A cache value that is not valid JSON triggers parse-error path then network fetch."""
    db = _StubDB({CACHE_KEY: "not-a-json-string"})
    with respx.mock(assert_all_called=False) as router:
        router.get(API_URL).respond(200, json={"rates": {"USD": 1.10}})
        async with httpx.AsyncClient() as client:
            result = await get_rates(db, client)
    assert result["USD"] == Decimal("1.10")


@pytest.mark.asyncio()
async def test_get_rates_empty_api_response_falls_back():
    """API returning rates={} → fresh is None → fallback table is used."""
    db = _StubDB()
    with respx.mock(assert_all_called=False) as router:
        router.get(API_URL).respond(200, json={"rates": {}})
        async with httpx.AsyncClient() as client:
            result = await get_rates(db, client)
    # Empty rates → returns None from _fetch_fresh_rates → fallback
    assert result["USD"] == Decimal("1.08")  # fallback rate


@pytest.mark.asyncio()
async def test_get_rates_creates_own_client_when_none_passed():
    """When no client is provided, get_rates should construct (and close) its own."""
    db = _StubDB()
    with respx.mock(assert_all_called=False) as router:
        router.get(API_URL).respond(200, json={"rates": {"USD": 1.05}})
        result = await get_rates(db, client=None)  # no client
    assert result["USD"] == Decimal("1.05")


@pytest.mark.asyncio()
async def test_convert_to_eur_passes_through_none_price():
    db = _StubDB()
    # type: ignore — testing defensive None handling at line 128
    result = await convert_to_eur(db, None, "USD")  # type: ignore[arg-type]
    assert result is None
