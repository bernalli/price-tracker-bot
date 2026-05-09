import pytest

from price_tracker.core.url_utils import extract_etld_plus_one


class TestExtractEtldPlusOne:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.amazon.com/dp/B01", "amazon.com"),
            ("https://www.amazon.de/dp/B01", "amazon.de"),
            ("https://www.xteink.com/products/x", "xteink.com"),
            ("https://shop.example.co.uk/p/1", "example.co.uk"),
            ("https://store.google.com/product/pixel_8", "google.com"),
            ("https://www.aliexpress.com/item/1.html", "aliexpress.com"),
            ("https://www.aliexpress.it/item/1.html", "aliexpress.it"),
            ("http://localhost:8080/product/1", ""),  # no public suffix
        ],
    )
    def test_extracts_registrable_domain(self, url, expected):
        assert extract_etld_plus_one(url) == expected

    def test_strips_port_from_host(self):
        assert extract_etld_plus_one("https://www.amazon.com:8443/dp/B01") == "amazon.com"

    def test_returns_empty_for_invalid_url(self):
        assert extract_etld_plus_one("not-a-url") == ""
        assert extract_etld_plus_one("") == ""
