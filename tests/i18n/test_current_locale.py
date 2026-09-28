"""Verifies bot/messages.py:current_locale() tracks the loaded catalogue code."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from price_tracker.bot import messages as msgs_mod
from price_tracker.bot.messages import current_locale, get_translation, reset_locale, set_locale

if TYPE_CHECKING:
    import pytest


def test_current_locale_follows_set_locale_it(fake_catalog) -> None:
    set_locale("it")
    assert current_locale() == "it_IT"


def test_current_locale_follows_set_locale_en(fake_catalog) -> None:
    set_locale("en")
    assert current_locale() == "en"


def test_current_locale_unsupported_falls_back_to_en(fake_catalog) -> None:
    # zh_CN has no catalogue on disk: get_translation() falls back to
    # NullTranslations(), which is not a _Catalog instance.
    set_locale("zh_CN")
    assert current_locale() == "en"


def test_current_locale_none_uses_default_locale(
    fake_catalog, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(msgs_mod, "_DEFAULT_LOCALE", "it_IT", raising=False)
    msgs_mod.get_translation.cache_clear()
    set_locale(None)
    assert current_locale() == "it_IT"


def test_reset_locale_restores_previous_current_locale(fake_catalog) -> None:
    set_locale("en")
    token = set_locale("it_IT")
    assert current_locale() == "it_IT"
    reset_locale(token)
    assert current_locale() == "en"


def test_current_locale_var_isolation_concurrent(fake_catalog) -> None:
    """Two concurrent asyncio tasks with different locales must not leak."""
    results: dict[str, str] = {}

    async def task(lang: str) -> None:
        set_locale(lang)
        await asyncio.sleep(0)  # yield to other task
        results[lang] = current_locale()

    async def runner() -> None:
        await asyncio.gather(
            task("it_IT"),
            task("en"),
        )

    asyncio.run(runner())
    assert results["it_IT"] == "it_IT"
    assert results["en"] == "en"


def test_current_locale_recalculates_after_cache_clear(fake_catalog) -> None:
    set_locale("it_IT")
    assert current_locale() == "it_IT"
    get_translation.cache_clear()
    set_locale("it_IT")
    assert current_locale() == "it_IT"


def test_get_translation_cache_is_stable_across_calls(fake_catalog) -> None:
    """Non-regression: the LRU cache returns the identical object, so a
    catalogue's `.code` attribute set once in `get_translation` is not
    recomputed on every `current_locale()` call."""
    assert get_translation("it_IT") is get_translation("it_IT")
