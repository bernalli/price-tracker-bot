"""Tests for ScraperRegistry plugin discovery."""

from __future__ import annotations

import re
from decimal import Decimal

import httpx
import pytest

from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo


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
