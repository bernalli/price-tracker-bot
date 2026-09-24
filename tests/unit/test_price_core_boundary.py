"""Boundary tests for the price-core port.

The port adds five modules under ``price_tracker.core`` without wiring them
into anything: no caller changes, nothing imports them yet. These tests
guard that boundary so a later change cannot cross it by accident, before
crossing it is a deliberate, reviewed decision.
"""

from __future__ import annotations

import inspect
import re
import subprocess
import sys
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "price_tracker"

CORE_MODULES = (
    "price_tracker.core.money",
    "price_tracker.core.pricegrammar",
    "price_tracker.core.anchoring",
    "price_tracker.core.identity",
    "price_tracker.core.structured_data",
)

FORBIDDEN_MODULES = (
    "price_tracker.bot",
    "price_tracker.db",
    "price_tracker.scrapers",
    "price_tracker.notifier",
    "telegram",
    "httpx",
    "aiosqlite",
)


def test_price_core_imports_are_effect_free() -> None:
    """Importing any of the five core modules pulls in no app, db, scraper,
    notifier or network-client module, and builds no on-disk TLD cache.

    tldextract's DiskCache stringifies a ``cache_dir=None`` constructor
    argument into the literal string ``"None"`` (``str(cache_dir) or ""``,
    see ``tldextract/cache.py``), so ``cache_dir is None`` is never true
    once the extractor is built. The invariant that matters — no disk
    cache is used — is ``enabled``, computed from the constructor argument
    *before* that stringification.
    """
    script = "\n".join(
        [
            "import sys",
            *(f"import {module}" for module in CORE_MODULES),
            "from price_tracker.core import identity",
            "extractor = identity._extractor",
            "assert extractor.suffix_list_urls == (), extractor.suffix_list_urls",
            "assert extractor._cache.enabled is False, extractor._cache.enabled",
            "print(chr(10).join(sorted(sys.modules)))",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    loaded = set(result.stdout.splitlines())
    for forbidden in FORBIDDEN_MODULES:
        assert forbidden not in loaded, (
            f"importing the price-core modules pulled in {forbidden!r}: "
            "the nucleus must stay effect-free and dependency-free"
        )


def test_price_core_has_no_callers_yet() -> None:
    """Nothing outside the five core modules references them yet.

    This is a deliberate tripwire, not a permanent invariant. The PR that
    wires the price-core nucleus into scraper_base/generic/the registry
    REPLACES this test with one that asserts an explicit, documented
    allow-list of callers — it does not add an exemption here.
    """
    core_dir = SRC_ROOT / "core"
    exempt = {
        core_dir / name
        for name in (
            "money.py",
            "pricegrammar.py",
            "anchoring.py",
            "identity.py",
            "structured_data.py",
        )
    }
    reference = re.compile(
        r"price_tracker\.core\.(money|pricegrammar|anchoring|identity|structured_data)"
    )
    offenders: list[str] = []
    for py in SRC_ROOT.rglob("*.py"):
        if py in exempt:
            continue
        text = py.read_text(encoding="utf-8")
        if reference.search(text):
            offenders.append(str(py.relative_to(SRC_ROOT)))
    assert not offenders, (
        "the price-core nucleus already has callers, but this PR is a pure "
        f"port with no wiring: {offenders}"
    )


def test_parse_price_untouched_by_the_port() -> None:
    """scraper_base.parse_price still exists, and scraper_base still does
    not reach into pricegrammar: the port adds a nucleus, it does not touch
    the caller that keeps working today."""
    from price_tracker.core import scraper_base

    assert callable(scraper_base.parse_price)
    source = inspect.getsource(scraper_base)
    assert "pricegrammar" not in source
