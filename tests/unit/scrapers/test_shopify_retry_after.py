"""A Shopify 429 block carries the Retry-After of the response that raised it."""

from __future__ import annotations

import httpx
import pytest
import respx

from price_tracker.core.exceptions import HTTPBlockStatus
from price_tracker.core.scraper_base import detect_block_event
from price_tracker.scrapers.shopify import ShopifyScraper

_URL = "https://store.com/products/widget"


async def test_a_refused_page_raises_with_its_own_retry_after() -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{_URL}.json").respond(429, headers={"Retry-After": "60"})
        router.get(_URL).respond(429, headers={"Retry-After": "120"})
        async with httpx.AsyncClient() as client:
            with pytest.raises(HTTPBlockStatus) as caught:
                await ShopifyScraper().scrape(_URL, client)
    assert caught.value.status == 429
    assert caught.value.retry_after == "120"


async def test_an_unreachable_page_keeps_the_json_retry_after() -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{_URL}.json").respond(429, headers={"Retry-After": "60"})
        router.get(_URL).mock(side_effect=httpx.ConnectError("down"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(HTTPBlockStatus) as caught:
                await ShopifyScraper().scrape(_URL, client)
    assert caught.value.retry_after == "60"


async def test_a_block_without_retry_after_carries_none() -> None:
    with respx.mock(assert_all_called=True) as router:
        router.get(f"{_URL}.json").respond(429)
        router.get(_URL).respond(429)
        async with httpx.AsyncClient() as client:
            with pytest.raises(HTTPBlockStatus) as caught:
                await ShopifyScraper().scrape(_URL, client)
    assert caught.value.retry_after is None


def test_which_responses_count_as_blocks_is_unchanged() -> None:
    for status in (200, 404, 500, 503):
        detect_block_event(status_code=status, body="", url="u", retry_after="60")
    for status in (403, 429):
        with pytest.raises(HTTPBlockStatus) as caught:
            detect_block_event(status_code=status, body="", url="u", retry_after="60")
        assert caught.value.retry_after == "60"
