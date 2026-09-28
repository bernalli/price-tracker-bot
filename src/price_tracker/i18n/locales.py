"""The set of locales this product ships translations for, and Babel parsing."""

from __future__ import annotations

from typing import Final

from babel.core import Locale, UnknownLocaleError

# Copy of `price_tracker.bot.callbacks.SUPPORTED_LOCALES`, which is normative
# (it feeds a `mypy --strict` registry) and stays untouched by this leaf
# package. `tests/i18n/test_locales.py` asserts the two tuples are equal, so
# the copies cannot silently drift.
SUPPORTED_LOCALES: Final = ("en", "it", "zh_Hans", "fr", "es", "de", "uk", "pt_BR", "ja")


def babel_locale(code: str) -> Locale:
    """Parse a catalogue code (``en``, ``it_IT``) or a Babel identifier (``zh_Hans``, ``pt_BR``).

    Raises ``ValueError`` (never Babel's own error type) for anything Babel cannot parse.
    """
    if not isinstance(code, str) or not code:
        raise ValueError(f"not a locale code: {code!r}")
    for sep in ("_", "-"):
        try:
            return Locale.parse(code, sep=sep)
        except (ValueError, TypeError, UnknownLocaleError):
            continue
    raise ValueError(f"not a locale code: {code!r}")
