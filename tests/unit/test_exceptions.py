from price_tracker.core.exceptions import (
    BlockEvent,
    CaptchaDetected,
    HTTPBlockStatus,
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
