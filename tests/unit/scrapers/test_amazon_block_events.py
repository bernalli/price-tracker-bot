"""Amazon must raise BlockEvent on 403/429/CAPTCHA so quarantine engages (bug #7).

Amazon swallowed hard blocks into ProductInfo(error=...), so the scheduler
recorded a generic error and never quarantined the domain (the 12 detect_block_event
scrapers do quarantine). These tests pin the block-signalling contract.
"""

from __future__ import annotations

import logging

import httpx
import pytest
import respx

from price_tracker.core.exceptions import CaptchaDetected, HTTPBlockStatus
from price_tracker.scrapers.amazon import AmazonScraper


@pytest.mark.parametrize("status", [403, 429])
async def test_amazon_raises_block_on_hard_status(status: int) -> None:
    scraper = AmazonScraper()
    url = f"https://www.amazon.it/dp/BLOCK{status}"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(status, text="blocked")
        async with httpx.AsyncClient() as client:
            with pytest.raises(HTTPBlockStatus) as exc:
                await scraper.scrape(url, client)
    assert exc.value.status == status


async def test_amazon_raises_captcha_on_validate_captcha_form() -> None:
    scraper = AmazonScraper()
    url = "https://www.amazon.it/dp/CAPTCHA1"
    captcha_html = (
        "<html><body><form action='/errors/validateCaptcha' method='get'>"
        "<input name='amzn'/></form></body></html>"
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=captcha_html)
        async with httpx.AsyncClient() as client:
            with pytest.raises(CaptchaDetected):
                await scraper.scrape(url, client)


async def test_amazon_captcha_page_logs_one_refusal_line(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING)
    scraper = AmazonScraper()
    url = "https://www.amazon.it/dp/CAPTCHA2"
    captcha_html = (
        "<html><body><form action='/errors/validateCaptcha' method='get'>"
        "<input name='amzn'/></form></body></html>"
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=captcha_html)
        async with httpx.AsyncClient() as client:
            with pytest.raises(CaptchaDetected):
                await scraper.scrape(url, client)
    lines = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("store refusal ")
    ]
    assert len(lines) == 1
    assert "reason=CAPTCHA/amazon-validateCaptcha" in lines[0].split(" ")
    assert "marker=captcha_amazon" in lines[0].split(" ")
