"""Snapshot tests for the product card, in every supported locale."""

from __future__ import annotations

import string
from pathlib import Path

import pytest

from price_tracker.bot.callbacks import InvalidCallback, decode
from price_tracker.bot.messages import current_locale, set_locale
from price_tracker.bot.ui.cards import product_card
from price_tracker.bot.ui.width import display_width
from price_tracker.core.textlimits import SAFE_LIMIT, _is_valid_telegram_markup, visible_length
from tests.support.card_msgids import CARD_PLURAL, CARD_SINGULAR
from tests.support.card_variants import NOW, VARIANTS, actions_for
from tests.support.panel_msgids import PANEL_SINGULAR
from tests.support.pseudo_locale import extract_messages, write_pseudo_catalog
from tests.support.ui_snapshot import compare_or_update, render_snapshot

_SNAPSHOT_DIR = Path(__file__).parent / "snapshots" / "card"
_NOW_TEXT = "2026-03-01T12:00:00Z"
HALF_WIDTH = 17
ROW_WIDTH = 34

# The catalog code current_locale() reports once set_locale(<code>) resolves.
_EXPECTED_CATALOG = {
    "en": "en",
    "it": "it_IT",
    "zh_Hans": "zh_Hans",
    "fr": "fr",
    "es": "es",
    "de": "de",
    "uk": "uk",
    "pt_BR": "pt_BR",
    "ja": "ja",
}


def _snapshot_cases() -> list[tuple[str, str]]:
    return [(variant, locale) for variant in VARIANTS for locale in _EXPECTED_CATALOG]


def _snapshot_id(case: tuple[str, str]) -> str:
    variant, locale = case
    return f"{variant}.{locale}"


# --- mechanism: before any real snapshot file is involved --------------------


def test_missing_snapshot_fails_with_the_create_command(ui_locales: Path, tmp_path: Path) -> None:
    set_locale("en")
    view = VARIANTS["base"]
    screen = product_card(view, actions_for(view), now=NOW)
    content = render_snapshot(screen, screen_name="card.base", locale="en", now=_NOW_TEXT)
    missing_path = tmp_path / "does-not-exist.txt"
    with pytest.raises(pytest.fail.Exception, match=r"missing snapshot"):
        compare_or_update(missing_path, content)


def test_update_under_ci_is_refused_before_writing(
    ui_locales: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_locale("en")
    view = VARIANTS["base"]
    screen = product_card(view, actions_for(view), now=NOW)
    content = render_snapshot(screen, screen_name="card.base", locale="en", now=_NOW_TEXT)
    target = tmp_path / "under-ci.txt"
    monkeypatch.setenv("UI_SNAPSHOTS_UPDATE", "1")
    monkeypatch.setenv("CI", "1")
    with pytest.raises(RuntimeError, match=r"CI"):
        compare_or_update(target, content)
    assert not target.exists()


def test_mismatch_fails_with_a_unified_diff(tmp_path: Path) -> None:
    target = tmp_path / "existing.txt"
    target.write_text("old content\n", encoding="utf-8")
    with pytest.raises(pytest.fail.Exception, match=r"snapshot mismatch"):
        compare_or_update(target, "new content\n")


def test_no_orphan_snapshots() -> None:
    expected = {f"{variant}.{locale}.txt" for variant, locale in _snapshot_cases()}
    on_disk = (
        {path.name for path in _SNAPSHOT_DIR.glob("*.txt")} if _SNAPSHOT_DIR.exists() else set()
    )
    orphans = on_disk - expected
    assert not orphans, orphans


# --- the 63 snapshots ----------------------------------------------------


@pytest.mark.parametrize("case", _snapshot_cases(), ids=_snapshot_id)
def test_card_snapshot(case: tuple[str, str], ui_locales: Path) -> None:
    variant, locale = case
    set_locale(locale)
    assert current_locale() == _EXPECTED_CATALOG[locale]

    view = VARIANTS[variant]
    screen = product_card(view, actions_for(view), now=NOW)

    assert _is_valid_telegram_markup(screen.text)
    assert visible_length(screen.text) <= SAFE_LIMIT

    for row in screen.rows:
        assert len(row) in (1, 2), row
        if len(row) == 2:
            assert display_width(row[0].label) <= HALF_WIDTH, row
            assert display_width(row[1].label) <= HALF_WIDTH, row
        else:
            assert display_width(row[0].label) <= ROW_WIDTH, row
        for btn in row:
            if btn.callback is not None:
                assert not isinstance(decode(btn.callback), InvalidCallback), btn.callback

    content = render_snapshot(screen, screen_name=f"card.{variant}", locale=locale, now=_NOW_TEXT)
    path = _SNAPSHOT_DIR / f"{variant}.{locale}.txt"
    compare_or_update(path, content)


# --- pseudo_locale: its own properties ---------------------------------------


def _placeholders(text: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


def test_pseudo_catalog_contains_exactly_the_29_card_msgids() -> None:
    root = Path(__file__).resolve().parents[2] / "src/price_tracker/bot/ui"
    messages = extract_messages(root)
    singular_ids = {m.id for m in messages if isinstance(m.id, str)}
    plural_ids = {m.id for m in messages if not isinstance(m.id, str)}
    # "Product #{product_id}" is the pre-existing empty-name fallback msgid
    # (already in the catalog, shared with notifier/digest.py): it is a real
    # _() call site under bot/ui/, so extraction finds it, but it is not one
    # of the 29 msgids the card adds.
    singular_ids -= {"Product #{product_id}", *PANEL_SINGULAR}
    assert singular_ids == set(CARD_SINGULAR)
    assert plural_ids == {CARD_PLURAL}


def test_pseudo_catalog_msgstr_keeps_the_same_placeholders(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2] / "src/price_tracker/bot/ui"
    messages = extract_messages(root)
    mo_path = tmp_path / "zh_Hans" / "LC_MESSAGES" / "messages.mo"
    write_pseudo_catalog(mo_path, "zh_Hans", messages)

    import gettext

    translation = gettext.translation(
        "messages", localedir=tmp_path, languages=["zh_Hans"], class_=gettext.GNUTranslations
    )
    for msgid in CARD_SINGULAR:
        rendered = translation.gettext(msgid)
        assert _placeholders(rendered) == _placeholders(msgid), msgid
    singular, plural = CARD_PLURAL
    for n in (1, 2, 5, 12, 21):
        rendered = translation.ngettext(singular, plural, n)
        assert _placeholders(rendered) <= (_placeholders(singular) | _placeholders(plural))


@pytest.mark.parametrize("locale", ["zh_Hans", "fr", "es", "de", "uk", "pt_BR", "ja"])
def test_pseudo_ngettext_is_bracketed_for_every_plural_form(locale: str, tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2] / "src/price_tracker/bot/ui"
    messages = extract_messages(root)
    mo_path = tmp_path / locale / "LC_MESSAGES" / "messages.mo"
    write_pseudo_catalog(mo_path, locale, messages)

    import gettext

    translation = gettext.translation(
        "messages", localedir=tmp_path, languages=[locale], class_=gettext.GNUTranslations
    )
    singular, plural = CARD_PLURAL
    for n in (1, 2, 5, 12, 21):
        rendered = translation.ngettext(singular, plural, n)
        assert rendered.startswith("["), (locale, n, rendered)
        assert rendered.endswith("]"), (locale, n, rendered)
        assert rendered not in (singular, plural), (locale, n, rendered)
