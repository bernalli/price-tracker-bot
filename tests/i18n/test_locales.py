"""Verifies price_tracker.i18n.locales: the supported set and Babel parsing."""

from __future__ import annotations

import pytest

from price_tracker.bot import callbacks
from price_tracker.i18n.locales import SUPPORTED_LOCALES, babel_locale


def test_supported_locales_matches_the_callback_registry_copy() -> None:
    assert SUPPORTED_LOCALES == callbacks.SUPPORTED_LOCALES


@pytest.mark.parametrize("code", [*SUPPORTED_LOCALES, "it_IT", "pt-BR", "zh-Hans"])
def test_babel_locale_accepts_every_supported_code(code: str) -> None:
    babel_locale(code)


@pytest.mark.parametrize(
    "bad",
    ["", "xx_YY", "en_", "../en", "en\x00", None, 42, "e" * 100],
)
def test_babel_locale_rejects_not_well_formed_input(bad: object) -> None:
    with pytest.raises(ValueError, match=r"."):
        babel_locale(bad)  # type: ignore[arg-type]
