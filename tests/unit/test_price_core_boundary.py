"""Boundary tests for the price-core port.

The port adds eight modules under ``price_tracker.core`` without wiring them
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
    "price_tracker.core.currencies",
    "price_tracker.core.currency_symbols",
    "price_tracker.core._generated_currency_symbols",
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
    """Importing any of the eight core modules pulls in no app, db, scraper,
    notifier or network-client module, and builds no on-disk TLD cache.

    tldextract's DiskCache stringifies a ``cache_dir=None`` constructor
    argument into the literal string ``"None"`` (``str(cache_dir) or ""``,
    see ``tldextract/cache.py``), so ``cache_dir is None`` is never true
    once the extractor is built. The invariant that matters — no disk
    cache is used — is ``enabled``, computed from the constructor argument
    *before* that stringification.

    This couples to tldextract's private ``_cache``/``DiskCache.enabled``
    (no public API exposes it); a future tldextract release may rename or
    remove it, which will fail this test loudly (AttributeError), not silently.
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


def test_price_core_has_documented_callers_only() -> None:
    """Every file outside the eight core modules that references them is on this
    explicit allow-list.

    Replaces ``test_price_core_has_no_callers_yet``: the tripwire's own docstring
    said the PR that wires the nucleus into a caller replaces it with an
    allow-list, not an exemption. Wired by the Shopify headless (Next.js/Hydrogen)
    fallback: a JSON API 404 on a headless storefront falls back to the JSON-LD
    ``ProductGroup``/``hasVariant`` structure via ``identity``, ``pricegrammar``
    and ``structured_data.decode_json_strict`` instead of duplicating them.
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
            "currencies.py",
            "currency_symbols.py",
            "_generated_currency_symbols.py",
        )
    }
    allowed_callers = {
        SRC_ROOT / "scrapers" / "shopify.py",
        # scraper_base.parse_price/detect_currency delegate to the price grammar
        # and the currency engine.
        SRC_ROOT / "core" / "scraper_base.py",
    }
    module_names = (
        "money|pricegrammar|anchoring|identity|structured_data"
        "|currencies|currency_symbols|_generated_currency_symbols"
    )
    reference = re.compile(
        rf"price_tracker\.core\.({module_names})\b"
        rf"|from\s+price_tracker\.core\s+import\s*\(?\s*"
        rf"[A-Za-z0-9_,\s]*?\b(?:{module_names})\b"
    )
    offenders: list[str] = []
    for py in SRC_ROOT.rglob("*.py"):
        if py in exempt or py in allowed_callers:
            continue
        text = py.read_text(encoding="utf-8")
        if reference.search(text):
            offenders.append(str(py.relative_to(SRC_ROOT)))
    assert not offenders, (
        "the price-core nucleus has undocumented callers — add them to "
        f"allowed_callers above if intentional: {offenders}"
    )
    for path in allowed_callers:
        assert path.exists(), f"allow-listed caller no longer exists: {path}"


def test_parse_price_delegates_to_the_grammar() -> None:
    """scraper_base.parse_price/detect_currency delegate to the price core:
    the old inline parser table is gone, and the grammar is the one doing the work."""
    from price_tracker.core import scraper_base

    assert callable(scraper_base.parse_price)
    source = inspect.getsource(scraper_base)
    assert "pricegrammar" in source
    assert "_CURRENCY_SIGNS" not in source
