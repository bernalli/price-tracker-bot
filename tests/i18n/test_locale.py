"""i18n test suite (TDD fail-first). Verifies bot/messages.py behavior."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from price_tracker.bot import messages as msgs_mod
from price_tracker.bot.messages import (
    _,
    get_translation,
    ngettext,
    set_locale,
)
from price_tracker.core.alert import format_operational_notice, format_warning_notice
from price_tracker.core.notices import NoticeGroup, OperationalEvent


def test_normalize_lang_code_two_letter() -> None:
    assert msgs_mod._normalize("it") == "it"
    assert msgs_mod._normalize("EN") == "en"


def test_normalize_lang_code_dash_or_underscore() -> None:
    assert msgs_mod._normalize("it-IT") == "it_IT"
    assert msgs_mod._normalize("it_it") == "it_IT"
    assert msgs_mod._normalize("EN-us") == "en_US"


def test_resolve_two_letter_to_region(fake_catalog) -> None:
    # 'it' (no region) should resolve to 'it_IT' (only available it_*)
    t = get_translation("it")
    assert t.gettext("❌ Invalid ID.") == "❌ ID non valido."


def test_get_translation_caches_lru(fake_catalog) -> None:
    info_before = get_translation.cache_info()
    get_translation("it_IT")
    get_translation("it_IT")
    info_after = get_translation.cache_info()
    assert info_after.hits >= info_before.hits + 1


def test_translation_known_key_it(fake_catalog) -> None:
    set_locale("it_IT")
    assert _("❌ Invalid ID.") == "❌ ID non valido."


def test_translation_known_key_en(fake_catalog) -> None:
    set_locale("en")
    # source language: empty msgstr falls back to msgid
    assert _("❌ Invalid ID.") == "❌ Invalid ID."


def test_ngettext_singular_en(fake_catalog) -> None:
    set_locale("en")
    assert ngettext("1 product", "{n} products", 1) == "1 product"


def test_ngettext_plural_en(fake_catalog) -> None:
    set_locale("en")
    assert ngettext("1 product", "{n} products", 5) == "{n} products"


def test_missing_key_passthrough(fake_catalog) -> None:
    set_locale("it_IT")
    assert _("Some untranslated string") == "Some untranslated string"


def test_locale_unsupported_falls_back_to_en(fake_catalog) -> None:
    # zh_CN not in _AVAILABLE -> falls back to en (passthrough)
    set_locale("zh_CN")
    assert _("❌ Invalid ID.") == "❌ Invalid ID."


def test_locale_env_fallback(fake_catalog, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(msgs_mod, "_DEFAULT_LOCALE", "it_IT", raising=False)
    msgs_mod.get_translation.cache_clear()
    set_locale(None)
    assert _("❌ Invalid ID.") == "❌ ID non valido."


def test_locale_var_isolation_concurrent(fake_catalog) -> None:
    """Two concurrent asyncio tasks with different locales must not leak."""
    results: dict[str, str] = {}

    async def task(lang: str, key: str) -> None:
        set_locale(lang)
        await asyncio.sleep(0)  # yield to other task
        results[lang] = _(key)

    async def runner() -> None:
        await asyncio.gather(
            task("it_IT", "❌ Invalid ID."),
            task("en", "❌ Invalid ID."),
        )

    asyncio.run(runner())
    assert results["it_IT"] == "❌ ID non valido."
    assert results["en"] == "❌ Invalid ID."


def test_with_locale_decorator_sets_var(fake_catalog) -> None:
    from price_tracker.bot.decorators import with_locale

    update = MagicMock()
    update.effective_user.language_code = "it"
    context = MagicMock()
    captured: dict[str, str] = {}

    @with_locale
    async def handler(upd, ctx) -> None:  # noqa: ARG001
        captured["msg"] = _("❌ Invalid ID.")

    asyncio.run(handler(update, context))  # type: ignore[arg-type]  # with_locale returns Awaitable
    assert captured["msg"] == "❌ ID non valido."


def test_with_locale_decorator_no_user(fake_catalog, monkeypatch: pytest.MonkeyPatch) -> None:
    from price_tracker.bot.decorators import with_locale

    monkeypatch.setattr(msgs_mod, "_DEFAULT_LOCALE", "it_IT", raising=False)
    msgs_mod.get_translation.cache_clear()
    update = MagicMock()
    update.effective_user = None
    context = MagicMock()
    captured: dict[str, str] = {}

    @with_locale
    async def handler(upd, ctx) -> None:  # noqa: ARG001
        captured["msg"] = _("❌ Invalid ID.")

    asyncio.run(handler(update, context))  # type: ignore[arg-type]  # with_locale returns Awaitable
    assert captured["msg"] == "❌ ID non valido."


def test_compile_artifacts_present_smoke() -> None:
    """Smoke test against production catalog (skipped in dev if .mo not yet built)."""
    import price_tracker  # noqa: F401

    pkg_dir = pytest.importorskip("price_tracker").__path__[0]
    from pathlib import Path

    en_mo = Path(pkg_dir) / "locale" / "en" / "LC_MESSAGES" / "messages.mo"
    it_mo = Path(pkg_dir) / "locale" / "it_IT" / "LC_MESSAGES" / "messages.mo"
    if not en_mo.exists() or not it_mo.exists():
        pytest.skip("production catalog not yet compiled (Task T21)")
    assert en_mo.is_file()
    assert it_mo.is_file()


@pytest.fixture
def _reset_locale_after():
    """Restore the locale ContextVar to English once the test is done.

    `set_locale` writes to a module-level ContextVar with no per-test scoping;
    left on `it_IT`, it silently leaks into whichever test runs next in this
    process (regardless of file) and can flip *their* `_()` calls to Italian
    once the production catalog actually has a translation for the msgid they
    render -- exactly the failure mode this file's own render-time tests
    exist to catch, just aimed at unrelated tests instead of this one.
    """
    yield
    set_locale("en")
    get_translation.cache_clear()


def test_production_catalog_has_operational_notice_strings() -> None:
    """The BUILT production .mo (not a test fixture) carries the operational-notice
    msgids from the plan's §5.3 table, with the exact it_IT translations."""
    get_translation.cache_clear()
    translation = get_translation("it_IT")
    assert translation.gettext("Listings removed on {domain}") == "Prodotti rimossi da {domain}"
    assert translation.gettext("Blocked by {domain}") == "Bloccato da {domain}"
    assert translation.gettext("Tracking suspended on {domain}") == "Tracking sospeso su {domain}"
    assert translation.gettext("⚠️ Operational notices") == "⚠️ Avvisi operativi"
    get_translation.cache_clear()


def _operational_event(
    *,
    reason: str = "listing_gone",
    domain: str = "example.com",
    detail: str | None = "HTTP 404",
) -> OperationalEvent:
    return OperationalEvent(
        event="suspended",
        user_id=1,
        product_id=1,
        product_name="Widget",
        url=f"https://{domain}/p/1",
        group_key=domain,
        reason=reason,
        detail=detail,
        last_error="listing_gone: HTTP 404",
        error_count=10,
        max_errors=10,
        last_price=None,
        currency="EUR",
        last_checked_at=None,
    )


@pytest.mark.usefixtures("_reset_locale_after")
def test_operational_notice_title_is_translated_at_render_time() -> None:
    """Reproduces the real production path: `_REASON_COPY` holds plain-string
    literals (not `_()` calls), so `format_operational_notice` must resolve the
    translation of `title` at RENDER time via `_(title)`, using the ContextVar
    locale current when the function runs -- not whatever locale was active at
    *import* time of `core/alert.py`. If this regresses to an import-time bind
    (e.g. the copy tuples get pre-translated at module load), this assertion
    fails because the title stays in English regardless of `set_locale`.
    """
    get_translation.cache_clear()
    group = NoticeGroup(
        event="suspended",
        user_id=1,
        group_key="example.com",
        events=(_operational_event(),),
    )

    set_locale("en")
    english_text = format_operational_notice(group)
    assert "Listings removed on example.com" in english_text

    set_locale("it_IT")
    italian_text = format_operational_notice(group)
    assert "Prodotti rimossi da example.com" in italian_text
    assert "Listings removed" not in italian_text

    # Re-render in English on the same process, after having rendered Italian:
    # proves resolution tracks the ContextVar per call, not a cached bind.
    set_locale("en")
    english_again = format_operational_notice(group)
    assert "Listings removed on example.com" in english_again
    get_translation.cache_clear()


@pytest.mark.usefixtures("_reset_locale_after")
def test_operational_warning_headline_is_translated_at_render_time() -> None:
    """Same render-time proof for the pre-suspension warning copy."""
    get_translation.cache_clear()
    event = OperationalEvent(
        event="warning",
        user_id=1,
        product_id=1,
        product_name="Widget",
        url="https://example.com/p/1",
        group_key="example.com",
        reason="listing_gone",
        detail="HTTP 404",
        last_error=None,
        error_count=2,
        max_errors=3,
        last_price=None,
        currency="EUR",
        last_checked_at=None,
    )
    group = NoticeGroup(event="warning", user_id=1, group_key="example.com", events=(event,))

    set_locale("it_IT")
    text = format_warning_notice(group)
    assert "Controlli in errore su example.com" in text
    assert "Checks failing" not in text
    get_translation.cache_clear()
