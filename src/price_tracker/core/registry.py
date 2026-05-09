"""Plugin discovery and registry for scrapers."""

from __future__ import annotations

import importlib
import importlib.util
import logging
import pkgutil
from typing import TYPE_CHECKING

from price_tracker.core.scraper_base import AbstractScraper

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

logger = logging.getLogger(__name__)


class ScraperRegistry:
    """Holds registered scraper instances and resolves URLs to the right one."""

    def __init__(self) -> None:
        self._scrapers: list[AbstractScraper] = []
        self._names: set[str] = set()

    def register(self, scraper: AbstractScraper) -> None:
        """Register a scraper instance. Raises if name already registered."""
        if scraper.name in self._names:
            raise ValueError(f"Scraper '{scraper.name}' already registered")
        self._scrapers.append(scraper)
        self._names.add(scraper.name)
        # Re-sort by priority (higher = first)
        self._scrapers.sort(key=lambda s: s.priority, reverse=True)

    def list(self) -> list[AbstractScraper]:
        """Return scrapers in priority order (highest first)."""
        return list(self._scrapers)

    def resolve(self, url: str) -> AbstractScraper | None:
        """Return the first scraper whose `can_handle(url)` is True. None if none match."""
        for s in self._scrapers:
            if s.can_handle(url):
                return s
        return None

    def __iter__(self) -> Iterator[AbstractScraper]:
        return iter(self._scrapers)

    def __len__(self) -> int:
        return len(self._scrapers)


def discover_builtin_scrapers(registry: ScraperRegistry) -> None:
    """Scan `price_tracker.scrapers` package and register every Scraper subclass found."""
    import price_tracker.scrapers as scrapers_pkg

    for module_info in pkgutil.iter_modules(scrapers_pkg.__path__):
        module_name = f"price_tracker.scrapers.{module_info.name}"
        module = importlib.import_module(module_name)
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, AbstractScraper)
                and attr is not AbstractScraper
            ):
                try:
                    registry.register(attr())
                    logger.info("Registered built-in scraper: %s", attr.name)
                except ValueError:
                    # Already registered (subclass present in multiple modules)
                    pass


def discover_dropin_scrapers(registry: ScraperRegistry, plugin_dir: Path) -> None:
    """Load any *.py file in `plugin_dir` and register Scraper subclasses found."""
    if not plugin_dir.is_dir():
        return
    for file in plugin_dir.glob("*.py"):
        if file.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"plugins.{file.stem}", file)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, AbstractScraper)
                and attr is not AbstractScraper
            ):
                try:
                    registry.register(attr())
                    logger.info("Registered drop-in scraper: %s (from %s)", attr.name, file)
                except ValueError:
                    pass
