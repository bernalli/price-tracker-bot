"""Tests for ScraperRegistry plugin discovery."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo

if TYPE_CHECKING:
    import httpx


class FakeAmazon(AbstractScraper):
    name = "amazon"
    priority = 100
    domain_patterns = [re.compile(r"amazon\.")]

    def can_handle(self, url: str) -> bool:
        return self.matches_domain(url)

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        return ProductInfo(name="amazon-result", price=Decimal("1"))


class FakeGeneric(AbstractScraper):
    name = "generic"
    priority = 0
    domain_patterns = []  # Generic accepts everything

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        return ProductInfo(name="generic-result", price=Decimal("2"))


def test_registry_orders_by_priority_descending():
    r = ScraperRegistry()
    r.register(FakeGeneric())
    r.register(FakeAmazon())
    ordered = r.list()
    assert ordered[0].name == "amazon"
    assert ordered[1].name == "generic"


def test_registry_resolve_returns_first_matching():
    r = ScraperRegistry()
    r.register(FakeAmazon())
    r.register(FakeGeneric())
    s = r.resolve("https://www.amazon.it/dp/B01")
    assert s.name == "amazon"


def test_registry_resolve_falls_back_to_generic():
    r = ScraperRegistry()
    r.register(FakeAmazon())
    r.register(FakeGeneric())
    s = r.resolve("https://random-site.example.com/product")
    assert s.name == "generic"


def test_registry_resolve_returns_none_when_no_match():
    r = ScraperRegistry()
    r.register(FakeAmazon())
    s = r.resolve("https://random-site.example.com/product")
    assert s is None


def test_registry_iterable():
    r = ScraperRegistry()
    r.register(FakeAmazon())
    r.register(FakeGeneric())
    names = [s.name for s in r]
    assert names == ["amazon", "generic"]


def test_registry_register_rejects_duplicates():
    r = ScraperRegistry()
    r.register(FakeAmazon())
    with pytest.raises(ValueError, match="already registered"):
        r.register(FakeAmazon())


def test_registry_len_and_empty_resolve():
    r = ScraperRegistry()
    assert len(r) == 0
    r.register(FakeAmazon())
    assert len(r) == 1


def test_discover_builtin_scrapers_loads_real_modules():
    """Smoke test for the discover-builtin path: scans price_tracker.scrapers package."""
    from price_tracker.core.registry import discover_builtin_scrapers

    r = ScraperRegistry()
    discover_builtin_scrapers(r)
    # Built-in scrapers: amazon, ebay, shopify, generic, playwright_fallback
    names = {s.name for s in r}
    # We don't pin the exact set (drop-in changes are possible) — just check non-empty
    assert len(names) >= 1


def test_discover_builtin_scrapers_skips_duplicates_silently():
    """If a scraper class is found twice (re-discovery), the ValueError branch swallows."""
    from price_tracker.core.registry import discover_builtin_scrapers

    r = ScraperRegistry()
    discover_builtin_scrapers(r)
    initial_count = len(r)
    # Second call should not raise — duplicate names hit the swallow branch (lines 71-73)
    discover_builtin_scrapers(r)
    assert len(r) == initial_count


def test_discover_dropin_scrapers_no_dir_returns_silently(tmp_path):
    """When plugin_dir doesn't exist, discover returns without error."""
    from price_tracker.core.registry import discover_dropin_scrapers

    r = ScraperRegistry()
    missing = tmp_path / "does-not-exist"
    discover_dropin_scrapers(r, missing)
    assert len(r) == 0


def test_discover_dropin_scrapers_loads_py_file(tmp_path):
    """Drop-in: a *.py file with a Scraper subclass gets registered."""
    from price_tracker.core.registry import discover_dropin_scrapers

    plugin_file = tmp_path / "myplugin.py"
    plugin_file.write_text(
        "import re\n"
        "from decimal import Decimal\n"
        "from price_tracker.core.scraper_base import AbstractScraper, ProductInfo\n"
        "\n"
        "class MyDropIn(AbstractScraper):\n"
        "    name = 'mydropin'\n"
        "    priority = 50\n"
        "    domain_patterns = [re.compile(r'mysite\\.')]\n"
        "    def can_handle(self, url):\n"
        "        return self.matches_domain(url)\n"
        "    async def scrape(self, url, client):\n"
        "        return ProductInfo(price=Decimal('9.99'))\n"
    )

    r = ScraperRegistry()
    discover_dropin_scrapers(r, tmp_path)
    names = {s.name for s in r}
    assert "mydropin" in names


def test_discover_dropin_scrapers_skips_underscore_files(tmp_path):
    """Files starting with _ (e.g. _helpers.py) are ignored."""
    from price_tracker.core.registry import discover_dropin_scrapers

    (tmp_path / "_helper.py").write_text(
        "from price_tracker.core.scraper_base import AbstractScraper\n"
        "class Hidden(AbstractScraper):\n"
        "    name = 'hidden'\n"
        "    priority = 1\n"
        "    domain_patterns = []\n"
        "    def can_handle(self, url):\n"
        "        return False\n"
        "    async def scrape(self, url, client):\n"
        "        from price_tracker.core.scraper_base import ProductInfo\n"
        "        return ProductInfo()\n"
    )
    r = ScraperRegistry()
    discover_dropin_scrapers(r, tmp_path)
    assert len(r) == 0


def test_discover_dropin_scrapers_swallows_duplicate(tmp_path):
    """Drop-in twice → ValueError branch swallows (lines 98-99)."""
    from price_tracker.core.registry import discover_dropin_scrapers

    plugin_file = tmp_path / "dup.py"
    plugin_file.write_text(
        "from decimal import Decimal\n"
        "from price_tracker.core.scraper_base import AbstractScraper, ProductInfo\n"
        "\n"
        "class DupOne(AbstractScraper):\n"
        "    name = 'dupone'\n"
        "    priority = 1\n"
        "    domain_patterns = []\n"
        "    def can_handle(self, url):\n"
        "        return False\n"
        "    async def scrape(self, url, client):\n"
        "        return ProductInfo(price=Decimal('1'))\n"
    )

    r = ScraperRegistry()
    discover_dropin_scrapers(r, tmp_path)
    assert len(r) == 1
    # Second call: same class definition reloaded → duplicate name → swallowed
    discover_dropin_scrapers(r, tmp_path)
    assert len(r) == 1
