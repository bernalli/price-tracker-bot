import httpx

from price_tracker.core.exceptions import (
    LISTING_GONE_STATUSES,
    BlockEvent,
    CaptchaDetected,
    HTTPBlockStatus,
    ListingGone,
    ScrapeError,
    WAFBlocked,
)


class TestBlockEventHierarchy:
    def test_captcha_is_block_event(self):
        assert issubclass(CaptchaDetected, BlockEvent)

    def test_waf_is_block_event(self):
        assert issubclass(WAFBlocked, BlockEvent)

    def test_http_block_is_block_event(self):
        assert issubclass(HTTPBlockStatus, BlockEvent)

    def test_http_block_carries_status(self):
        exc = HTTPBlockStatus(status=429, url="https://x.com/p/1")
        assert exc.status == 429
        assert exc.url == "https://x.com/p/1"
        assert "429" in str(exc)

    def test_waf_carries_provider(self):
        exc = WAFBlocked(provider="cloudflare", url="https://x.com")
        assert exc.provider == "cloudflare"
        assert "cloudflare" in str(exc).lower()

    def test_captcha_carries_marker(self):
        exc = CaptchaDetected(marker="g-recaptcha", url="https://x.com")
        assert exc.marker == "g-recaptcha"


def test_listing_gone_domain_exception_contract() -> None:
    exc = ListingGone(status=404, url="https://shop.example/products/missing")

    assert isinstance(exc, ScrapeError)
    assert not isinstance(exc, BlockEvent)
    assert not isinstance(exc, httpx.HTTPError)
    assert exc.status == 404
    assert exc.url == "https://shop.example/products/missing"
    assert {404, 410} == LISTING_GONE_STATUSES
