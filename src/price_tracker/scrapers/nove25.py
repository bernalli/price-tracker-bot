"""Nove25 domain routing using the shared generic extraction pipeline."""

from __future__ import annotations

import re
from typing import ClassVar

from price_tracker.scrapers import generic


class Nove25Scraper(generic.GenericScraper):
    """Use the validated generic pipeline for Nove25 product pages."""

    name: ClassVar[str] = "nove25"
    priority: ClassVar[int] = 85
    domain_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"(?:^|\.)nove25\.(?:net|com)$", re.IGNORECASE),
    ]

    def can_handle(self, url: str) -> bool:
        return self.matches_domain(url)
