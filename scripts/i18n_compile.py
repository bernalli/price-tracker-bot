#!/usr/bin/env python3
"""Compile the .po catalogs to .mo without Babel's printf validator.

`pybabel compile` runs `babel.messages.checkers.python_format`, which treats a
string as a printf template whenever it contains a percent sign followed by a
conversion letter — including a *literal* percent in prose, as in
"-10% since tracking". It then rejects any translation whose word after the
percent starts with a different letter ("% desde", "% sotto"), reporting
"'s' and 'd' are not compatible" for text that is perfectly correct.

Every translatable string in this project is rendered with `str.format`
(`python-brace-format`); none uses printf interpolation, so that check has
nothing true to say here. Babel re-derives the `python-format` flag from the
msgid on every read, so it cannot be turned off in the .po files themselves —
hence compiling through the library API instead.

Usage: i18n_compile.py [locale_dir]   (default: src/price_tracker/locale)
"""

from __future__ import annotations

import sys
from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

DEFAULT_LOCALE_DIR = Path("src/price_tracker/locale")


def compile_catalog(po_path: Path) -> int:
    """Write `po_path`'s sibling .mo; returns the number of translated messages."""
    with po_path.open(encoding="utf-8") as fh:
        catalog = read_po(fh, locale=po_path.parent.parent.name)
    mo_path = po_path.with_suffix(".mo")
    with mo_path.open("wb") as fh:
        write_mo(fh, catalog, use_fuzzy=False)
    return len([m for m in catalog if m.id and m.string])


def main(argv: list[str]) -> int:
    locale_dir = Path(argv[0]) if argv else DEFAULT_LOCALE_DIR
    po_files = sorted(locale_dir.glob("*/LC_MESSAGES/messages.po"))
    if not po_files:
        print(f"no catalogs under {locale_dir}", file=sys.stderr)
        return 1
    for po_path in po_files:
        count = compile_catalog(po_path)
        print(f"compiled {po_path} -> {po_path.with_suffix('.mo')} ({count} messages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
