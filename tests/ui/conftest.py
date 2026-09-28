"""Shared fixtures and generators for the bot.ui / i18n rendering test suite."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Final

import pytest
from hypothesis import strategies as st

from price_tracker.bot import messages as msgs_mod
from price_tracker.i18n.locales import SUPPORTED_LOCALES
from tests.support.pseudo_locale import extract_messages, write_pseudo_catalog

if TYPE_CHECKING:
    from collections.abc import Iterator

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CARD_ROOT = _REPO_ROOT / "src/price_tracker/bot/ui"
# The two locales with a real, hand-translated catalog; the rest of
# SUPPORTED_LOCALES gets a pseudo-catalog.
_PSEUDO_LOCALES: Final = tuple(code for code in SUPPORTED_LOCALES if code not in ("en", "it"))

# Fixed code point ranges, stable between Unicode 14 and 15.1, so a
# hostile string generated on one CI matrix leg means the same thing on
# another: ASCII printable (includes < > & " '), CJK, hiragana/katakana,
# hangul, Arabic, Hebrew, three emoji blocks, skin-tone modifiers, ZWJ, VS16,
# combining marks, bidi controls and isolates, NBSP/NNBSP, \n \r \t, and the
# line/paragraph separators.
_HOSTILE_RANGES: Final = (
    (0x20, 0x7E),
    (0x4E00, 0x9FFF),
    (0x3040, 0x30FF),
    (0xAC00, 0xD7A3),
    (0x0621, 0x064A),
    (0x05D0, 0x05EA),
    (0x1F300, 0x1F5FF),
    (0x1F600, 0x1F64F),
    (0x1F900, 0x1F9FF),
    (0x1F3FB, 0x1F3FF),
    (0x200D, 0x200D),
    (0xFE0F, 0xFE0F),
    (0x0300, 0x036F),
    (0x202A, 0x202E),
    (0x2066, 0x2069),
    (0x00A0, 0x00A0),
    (0x202F, 0x202F),
    (0x000A, 0x000A),
    (0x000D, 0x000D),
    (0x0009, 0x0009),
    (0x2028, 0x2029),
)

hostile_text = st.text(
    alphabet=st.one_of(
        *(st.characters(min_codepoint=low, max_codepoint=high) for low, high in _HOSTILE_RANGES)
    ),
    min_size=0,
    max_size=300,
)


@pytest.fixture
def ui_locales(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """An isolated locale dir: real en/it_IT catalogs plus a pseudo-catalog
    for the seven remaining supported locales, built from the msgids that
    ``src/price_tracker/bot/ui/**`` actually uses."""
    locale_dir = tmp_path / "locale"
    shutil.copytree(_REPO_ROOT / "src/price_tracker/locale/en", locale_dir / "en")
    shutil.copytree(_REPO_ROOT / "src/price_tracker/locale/it_IT", locale_dir / "it_IT")

    messages = extract_messages(_CARD_ROOT)
    for code in _PSEUDO_LOCALES:
        mo_path = locale_dir / code / "LC_MESSAGES" / "messages.mo"
        write_pseudo_catalog(mo_path, code, messages)

    msgs_mod.get_translation.cache_clear()
    monkeypatch.setattr(msgs_mod, "_LOCALE_DIR", locale_dir, raising=False)
    monkeypatch.setattr(msgs_mod, "_AVAILABLE", {"en", "it_IT", *_PSEUDO_LOCALES}, raising=False)
    msgs_mod.get_translation.cache_clear()
    yield locale_dir
    msgs_mod.set_locale("en")
    msgs_mod.get_translation.cache_clear()
