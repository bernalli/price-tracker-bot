"""Test fixtures for i18n suite. Build an isolated GNUTranslations catalog
in a tmp_path so tests don't depend on the production catalog state."""

from __future__ import annotations

import struct
from pathlib import Path  # noqa: TCH003

import pytest


def _make_mo(translations: dict[str, str]) -> bytes:
    """Build a minimal .mo binary in memory from a {msgid: msgstr} dict.

    Format reference: https://www.gnu.org/software/gettext/manual/html_node/MO-Files.html
    """
    keys = sorted(translations.keys())
    n = len(keys)
    # offsets: header(28) + 2*n*8 + concatenated strings
    header_size = 28
    table_size = n * 16
    offset = header_size + table_size
    key_offsets = []
    val_offsets = []
    blob = b""
    for k in keys:
        kb = k.encode()
        key_offsets.append((len(kb), offset))
        blob += kb + b"\x00"
        offset += len(kb) + 1
    for k in keys:
        vb = translations[k].encode()
        val_offsets.append((len(vb), offset))
        blob += vb + b"\x00"
        offset += len(vb) + 1
    out = struct.pack("Iiiiiii", 0x950412DE, 0, n, 28, 28 + n * 8, 0, 0)
    for length, off in key_offsets:
        out += struct.pack("ii", length, off)
    for length, off in val_offsets:
        out += struct.pack("ii", length, off)
    out += blob
    return out


@pytest.fixture
def fake_catalog(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create an isolated locale dir with en + it_IT catalogs, return its path."""
    locale_dir = tmp_path / "locale"
    en_dir = locale_dir / "en" / "LC_MESSAGES"
    it_dir = locale_dir / "it_IT" / "LC_MESSAGES"
    en_dir.mkdir(parents=True)
    it_dir.mkdir(parents=True)

    en_translations = {
        "❌ Invalid ID.": "",  # source language: empty msgstr means passthrough
        "❌ Product not found.": "",
        "1 product": "",
    }
    it_translations = {
        "❌ Invalid ID.": "❌ ID non valido.",
        "❌ Product not found.": "❌ Prodotto non trovato.",
        "1 product": "1 prodotto",
    }
    (en_dir / "messages.mo").write_bytes(_make_mo(en_translations))
    (it_dir / "messages.mo").write_bytes(_make_mo(it_translations))

    # Point the messages module at our fake locale dir for the duration of the test
    import price_tracker.bot.messages as msgs_mod

    monkeypatch.setattr(msgs_mod, "_LOCALE_DIR", locale_dir, raising=False)
    monkeypatch.setattr(msgs_mod, "_AVAILABLE", {"en", "it_IT"}, raising=False)
    msgs_mod.get_translation.cache_clear()
    return locale_dir
