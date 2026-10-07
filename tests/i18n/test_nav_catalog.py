"""Every message the navigation screens use is in both catalogs, with its placeholders."""

from __future__ import annotations

import re
import string
from pathlib import Path

from babel.messages.extract import extract_from_file
from babel.messages.pofile import read_po

from price_tracker.bot.messages import get_translation
from tests.support.card_msgids import CARD_IDENTICAL
from tests.support.panel_msgids import PANEL_IDENTICAL, PANEL_SINGULAR
from tests.support.pseudo_locale import extract_messages

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UI_ROOT = _REPO_ROOT / "src/price_tracker/bot/ui"
_NAV = _REPO_ROOT / "src/price_tracker/bot/handlers/callbacks/_nav.py"
_PO_PATHS = {
    "it_IT": _REPO_ROOT / "src/price_tracker/locale/it_IT/LC_MESSAGES/messages.po",
    "en": _REPO_ROOT / "src/price_tracker/locale/en/LC_MESSAGES/messages.po",
}
_ENGLISH_AUDIT = re.compile(
    r"[àèéìòù]|\b(prezzo|errore|comando|impostazion|aggiungere|elenca|notifica|riprova|sono)\b"
)
_NEW_FILES = (_UI_ROOT / "panels.py", _NAV)


def _placeholders(text: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


def _msgids() -> set[str]:
    """Every singular msgid under ``bot/ui`` and in ``_nav.py``."""
    ids = [m.id for m in extract_messages(_UI_ROOT)]
    ids += [
        message
        for _line, message, _comments, _context in extract_from_file(
            "python", str(_NAV), keywords={"_": None}
        )
    ]
    return {msgid for msgid in ids if isinstance(msgid, str)}


def test_the_pinned_panel_list_is_what_the_source_uses() -> None:
    from tests.support.card_msgids import CARD_SINGULAR

    used = _msgids() - {"Product #{product_id}"}
    assert used == set(CARD_SINGULAR) | set(PANEL_SINGULAR)


def test_every_msgid_is_in_en_and_translated_in_it_with_the_same_placeholders() -> None:
    catalogs = {}
    for locale, path in _PO_PATHS.items():
        with path.open("rb") as handle:
            catalogs[locale] = {message.id: message for message in read_po(handle)}
    get_translation.cache_clear()
    try:
        italian = get_translation("it_IT")
        for msgid in sorted(_msgids()):
            assert msgid in catalogs["en"], msgid
            assert msgid in catalogs["it_IT"], msgid
            rendered = italian.gettext(msgid)
            assert _placeholders(rendered) == _placeholders(msgid), msgid
            if msgid in CARD_IDENTICAL | PANEL_IDENTICAL:
                assert rendered == msgid, msgid
            else:
                assert rendered != msgid, msgid
    finally:
        get_translation.cache_clear()


def test_the_new_files_hold_no_italian() -> None:
    for path in _NEW_FILES:
        assert _ENGLISH_AUDIT.search(path.read_text(encoding="utf-8")) is None, path
