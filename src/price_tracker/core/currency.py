"""Currency conversion via exchangerate.host with 24h disk cache.

Cache lives in `bot_config(key=CACHE_KEY, value=<json>)`. Falls back to a
built-in static rate table if the network is unreachable.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_random_exponential,
)

logger = logging.getLogger(__name__)

BASE = "EUR"
CACHE_KEY = "fx_rates_eur"
CACHE_TTL_HOURS = 24
API_URL = "https://api.exchangerate.host/latest?base=EUR"


class _ConfigStore(Protocol):
    """Subset of the Database interface required by the currency module."""

    async def get_config(self, key: str) -> str | None: ...
    async def set_config(self, key: str, value: str) -> None: ...


_FALLBACK_RATES: dict[str, Decimal] = {
    "USD": Decimal("1.08"),
    "GBP": Decimal("0.85"),
    "CHF": Decimal("0.95"),
    "SEK": Decimal("11.20"),
    "NOK": Decimal("11.60"),
    "DKK": Decimal("7.46"),
    "PLN": Decimal("4.30"),
    "JPY": Decimal("170.0"),
    "EUR": Decimal("1.0"),
}


@retry(
    stop=stop_after_attempt(2),
    wait=wait_random_exponential(multiplier=1, max=4),
    retry=retry_if_exception_type((httpx.TimeoutException, httpx.NetworkError)),
    reraise=True,
)
async def _fetch_fresh_rates(client: httpx.AsyncClient) -> dict[str, Any] | None:
    """Fetch fresh rates from API. Returns dict with rates+fetched_at, or None on parse."""
    try:
        r = await client.get(API_URL, timeout=15.0)
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning("FX rates fetch failed: %s", e)
        return None

    rates = data.get("rates") or {}
    if not rates:
        return None
    return {
        "rates": {k: str(v) for k, v in rates.items()},
        "fetched_at": datetime.now(UTC).isoformat(),
    }


async def get_rates(
    db: _ConfigStore,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Decimal]:
    """Return {CUR: Decimal(rate_to_EUR)} using a 24h-cached snapshot.

    Falls back to a built-in static table if the API is unreachable.
    """
    cached_raw = await db.get_config(CACHE_KEY)
    if cached_raw:
        try:
            cached = json.loads(cached_raw)
            fetched = datetime.fromisoformat(cached["fetched_at"])
            if datetime.now(UTC) - fetched < timedelta(hours=CACHE_TTL_HOURS):
                return {k: Decimal(str(v)) for k, v in cached["rates"].items()}
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning("Cache parse failed: %s", e)

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=15.0)
    else:
        assert client is not None
    try:
        fresh = await _fetch_fresh_rates(client)
    except httpx.HTTPError as e:
        logger.warning("FX rates fetch retried-out: %s", e)
        fresh = None
    finally:
        if own_client and client is not None:
            await client.aclose()

    if fresh:
        await db.set_config(CACHE_KEY, json.dumps(fresh))
        return {k: Decimal(str(v)) for k, v in fresh["rates"].items()}

    return dict(_FALLBACK_RATES)


async def convert_to_eur(
    db: _ConfigStore,
    price: Decimal,
    from_currency: str,
    client: httpx.AsyncClient | None = None,
) -> Decimal:
    """Convert `price` from `from_currency` to EUR.

    Returns the price unchanged if `from_currency` is EUR or unknown.
    """
    if price is None:
        return price
    cur = (from_currency or "EUR").upper()
    if cur == "EUR":
        return price
    rates = await get_rates(db, client)
    rate = rates.get(cur)
    if rate is None or rate == 0:
        logger.warning("No FX rate for %s; leaving price as-is", cur)
        return price
    return (price / rate).quantize(Decimal("0.01"))
