"""i18n test suite (regression tests). Verifies bot/messages.py behavior."""

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

    asyncio.run(handler(update, context))
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

    asyncio.run(handler(update, context))
    assert captured["msg"] == "❌ ID non valido."


def test_compile_artifacts_present_smoke() -> None:
    """Smoke test against production catalog (skipped in dev if .mo not yet built)."""
    import price_tracker  # noqa: F401

    pkg_dir = pytest.importorskip("price_tracker").__path__[0]
    from pathlib import Path

    en_mo = Path(pkg_dir) / "locale" / "en" / "LC_MESSAGES" / "messages.mo"
    it_mo = Path(pkg_dir) / "locale" / "it_IT" / "LC_MESSAGES" / "messages.mo"
    if not en_mo.exists() or not it_mo.exists():
        pytest.skip("production catalog not yet compiled (item 21)")
    assert en_mo.is_file()
    assert it_mo.is_file()
