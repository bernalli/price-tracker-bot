"""Verifies the card catalog entries added to the production .po/.mo files."""

from __future__ import annotations

import json
import string
from pathlib import Path

from babel.messages.pofile import read_po

from price_tracker.bot.messages import get_translation
from tests.support.card_msgids import CARD_IDENTICAL, CARD_PLURAL, CARD_SINGULAR

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PO_PATHS = {
    "it_IT": _REPO_ROOT / "src/price_tracker/locale/it_IT/LC_MESSAGES/messages.po",
    "en": _REPO_ROOT / "src/price_tracker/locale/en/LC_MESSAGES/messages.po",
}
# Every entry the two catalogs held before the card entries were appended, as
# {id, string} pairs (plural ids and strings are lists). A translation that
# changes on purpose is changed here in the same commit, in the open.
_BASELINE_PATH = Path(__file__).parent / "fixtures" / "catalog_baseline.json"


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
            assert len(list(catalog)) == 493


def _as_key(value: str | tuple[str, ...] | list[str] | None) -> str | tuple[str, ...] | None:
    return tuple(value) if isinstance(value, (tuple, list)) else value


def test_pre_existing_entries_match_the_pinned_baseline() -> None:
    baseline = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    for locale, path in _PO_PATHS.items():
        with path.open("rb") as handle:
            catalog = read_po(handle)
        by_id = {_as_key(message.id): message for message in catalog if message.id}
        expected_entries = baseline[locale]
        assert len(expected_entries) > 0, locale
        for entry in expected_entries:
            key = _as_key(entry["id"])
            message = by_id.get(key)
            assert message is not None, (locale, key)
            assert _as_key(message.string) == _as_key(entry["string"]), (locale, key)
            assert not message.fuzzy, (locale, key)


def test_it_catalog_has_no_empty_translation() -> None:
    with _PO_PATHS["it_IT"].open("rb") as handle:
        catalog = read_po(handle)
    empty: list[object] = []
    for message in catalog:
        if not message.id:
            continue
        source = message.id if isinstance(message.id, str) else message.id[0]
        strings = message.string if isinstance(message.string, tuple) else (message.string,)
        if not all(strings):
            empty.append(message.id)
            continue
        expected = _placeholders(source)
        for text in strings:
            assert _placeholders(str(text)) == expected, message.id
    assert empty == []
