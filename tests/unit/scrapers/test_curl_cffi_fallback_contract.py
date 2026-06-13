"""Contract: curl_cffi fallback helpers must swallow a bare CurlError.

curl_cffi raises ``RequestsError`` (an ``OSError`` subclass) for ordinary
network failures, but the lower async multi-handle can raise a *bare*
``CurlError`` which is NOT an ``OSError``. The helpers previously caught only
``(httpx.HTTPError | ValueError | OSError | AttributeError)``, so a bare
``CurlError`` escaped the fallback, propagated out of ``scrape()`` and could
abort the scheduler tick — a violation of the "scrape() never raises" contract
(bugs #6 / #36).
"""

from __future__ import annotations

import pytest

from price_tracker.scrapers.amazon import _fetch_via_curl_cffi
from price_tracker.scrapers.generic import _fetch_with_curl_cffi
from price_tracker.scrapers.shopify import ShopifyScraper


class _RaisingSession:
    """Fake curl_cffi AsyncSession whose .get() raises a bare CurlError."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    async def __aenter__(self) -> _RaisingSession:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    async def get(self, *args: object, **kwargs: object) -> object:
        from curl_cffi import CurlError

        raise CurlError("simulated async multi-handle failure")


@pytest.fixture
def _patch_raising_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("curl_cffi.requests.AsyncSession", _RaisingSession)


@pytest.mark.usefixtures("_patch_raising_session")
async def test_amazon_curl_cffi_swallows_bare_curlerror() -> None:
    assert await _fetch_via_curl_cffi("https://www.amazon.it/dp/X") is None


@pytest.mark.usefixtures("_patch_raising_session")
async def test_generic_curl_cffi_swallows_bare_curlerror() -> None:
    assert await _fetch_with_curl_cffi("https://shop.example/p/1") is None


@pytest.mark.usefixtures("_patch_raising_session")
async def test_shopify_curl_cffi_swallows_bare_curlerror() -> None:
    assert await ShopifyScraper._fetch_json_via_curl_cffi("https://shop.example/p.js") is None
