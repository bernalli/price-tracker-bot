"""Verifies the card catalog entries added to the production .po/.mo files."""

from __future__ import annotations

import string
import subprocess
from pathlib import Path

from babel.messages.pofile import read_po

from price_tracker.bot.messages import get_translation
from tests.support.card_msgids import CARD_IDENTICAL, CARD_PLURAL, CARD_SINGULAR

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PO_PATHS = {
    "it_IT": _REPO_ROOT / "src/price_tracker/locale/it_IT/LC_MESSAGES/messages.po",
    "en": _REPO_ROOT / "src/price_tracker/locale/en/LC_MESSAGES/messages.po",
}


def _placeholders(text: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


def test_it_mo_translates_every_card_msgid() -> None:
    get_translation.cache_clear()
    try:
        translation = get_translation("it_IT")
        for msgid in CARD_SINGULAR:
            rendered = translation.gettext(msgid)
            if msgid in CARD_IDENTICAL:
                assert rendered == msgid, msgid
            else:
                assert rendered != msgid, msgid
            assert _placeholders(rendered) == _placeholders(msgid), msgid

        singular, plural = CARD_PLURAL
        rendered_one = translation.ngettext(singular, plural, 1)
        rendered_many = translation.ngettext(singular, plural, 2)
        assert rendered_one != singular
        assert rendered_many != plural
        assert _placeholders(rendered_one) == _placeholders(singular)
        assert _placeholders(rendered_many) == _placeholders(plural)
    finally:
        get_translation.cache_clear()


def test_po_files_have_no_fuzzy_and_no_obsolete() -> None:
    for locale, path in _PO_PATHS.items():
        with path.open("rb") as handle:
            catalog = read_po(handle)
        assert not catalog.obsolete, locale
        assert [message.id for message in catalog if message.fuzzy] == [], locale
        ids = {message.id for message in catalog}
        for msgid in CARD_SINGULAR:
            assert msgid in ids, (locale, msgid)
        assert CARD_PLURAL in ids, locale
        if locale == "it_IT":
            assert len(list(catalog)) == 195


def test_po_old_msgids_unchanged_from_head() -> None:
    for locale, path in _PO_PATHS.items():
        rel_path = path.relative_to(_REPO_ROOT).as_posix()
        head_bytes = subprocess.run(
            ["git", "show", f"HEAD:{rel_path}"],
            cwd=_REPO_ROOT,
            capture_output=True,
            check=True,
        ).stdout
        old_catalog = read_po(head_bytes.split(b"\n"))
        with path.open("rb") as handle:
            new_catalog = read_po(handle)
        new_by_id = {message.id: message for message in new_catalog}
        for old_message in old_catalog:
            new_message = new_by_id.get(old_message.id)
            assert new_message is not None, (locale, old_message.id)
            assert new_message.string == old_message.string, (locale, old_message.id)
            assert not new_message.fuzzy, (locale, old_message.id)
