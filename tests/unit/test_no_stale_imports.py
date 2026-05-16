"""Regression: no source file may import from legacy pre-refactor top-level names.

The Phase 1 F1 monolith split moved everything under ``price_tracker.*`` but
several call sites retained deferred imports of legacy module names
(``scrapers``, ``checker``, ``chart``). Those imports raised
``ModuleNotFoundError`` at runtime — the bot returned the generic
``"❌ Si è verificato un errore"`` to Telegram users on every command they
touched.

History of regressions sharing this exact class of bug:

* v0.1.3 — ``from scrapers import identify_site`` in ``/add`` / CSV import / ``/debug``.
* v0.1.6 — ``from checker import PriceChecker, format_alert`` in ``/check`` /
  ``/checkall`` / menu callback / per-product "Check now" button,
  plus ``from chart import render_price_history`` in ``_send_alert``.

This file walks every ``*.py`` under ``src/price_tracker`` and asserts no line
top-level imports a legacy bare module name. The legacy list is held as an
allow-list of *removed* modules so future drift onto any of them fails CI
instead of the user.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "price_tracker"

# Bare module names that used to live at top-level in the pre-Phase 1 layout.
# Every one of them has a price_tracker.* equivalent today; nothing in src/
# may import them again.
LEGACY_MODULES = (
    "scrapers",
    "checker",
    "chart",
    "database",
    "config",
    "notifier",
    "currency",
    "health",
    "utils",
)

_STALE_RE = re.compile(
    r"^\s*(from\s+("
    + "|".join(LEGACY_MODULES)
    + r")\b|import\s+("
    + "|".join(LEGACY_MODULES)
    + r")\b)"
)


def test_no_top_level_legacy_import_in_src() -> None:
    offenders: list[str] = []
    for py in SRC_ROOT.rglob("*.py"):
        for line_no, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if _STALE_RE.match(line):
                offenders.append(f"{py.relative_to(SRC_ROOT)}:{line_no}: {line.strip()}")
    assert not offenders, (
        "Stale top-level legacy module import (Phase 1 F1 regression). Use the "
        "price_tracker.* equivalent instead. Offending lines:\n  " + "\n  ".join(offenders)
    )
