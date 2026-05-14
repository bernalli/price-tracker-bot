"""Regression: no source file may import from the top-level ``scrapers`` name.

The Phase 1 F1 monolith split moved everything under ``price_tracker.*``, but
some handler modules (``product``, ``product_io``) retained deferred imports of
``from scrapers import identify_site`` from the pre-refactor layout. Those
imports raised ``ModuleNotFoundError`` at runtime — the bot returned the
generic ``"❌ Si è verificato un errore"`` to Telegram users on every
``/add`` and CSV import. There was no test exercising those code paths
before this regression test landed.

This file walks every ``*.py`` under ``src/price_tracker`` and asserts no
line matches ``^\\s*(from scrapers|import scrapers)``.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "price_tracker"

_STALE_RE = re.compile(r"^\s*(from\s+scrapers\b|import\s+scrapers\b)")


def test_no_top_level_scrapers_import_in_src() -> None:
    offenders: list[str] = []
    for py in SRC_ROOT.rglob("*.py"):
        for line_no, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if _STALE_RE.match(line):
                offenders.append(f"{py.relative_to(SRC_ROOT)}:{line_no}: {line.strip()}")
    assert not offenders, (
        "Stale top-level 'scrapers' import (Phase 1 F1 regression). Use "
        "'from price_tracker.scrapers import ...' or 'from "
        "price_tracker.core.url_utils import extract_etld_plus_one' instead:\n  "
        + "\n  ".join(offenders)
    )
