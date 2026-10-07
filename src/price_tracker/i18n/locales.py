"""The set of locales this product ships translations for, and Babel parsing."""

from __future__ import annotations

from typing import Final

from babel.core import Locale, UnknownLocaleError

# Copy of `price_tracker.bot.callbacks.SUPPORTED_LOCALES`, which is normative
# (it feeds a `mypy --strict` registry) and stays untouched by this leaf
# package. `tests/i18n/test_locales.py` asserts the two tuples are equal, so
# the copies cannot silently drift.
SUPPORTED_LOCALES: Final = ("en", "it", "zh_Hans", "fr", "es", "de", "uk", "pt_BR", "ja")


# Languages with a translation catalogue; `tests/unit/test_effective_language.py`
# ties this tuple to the folders under `locale/`.
AVAILABLE_LANGUAGES: Final = ("en", "it")


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


def endonym(code: str) -> str:
    """The language's own name, capitalised (``English``, ``Italiano``).

    Raises ``ValueError`` when ``code`` is not a parseable locale.
    """
    locale = babel_locale(code)
    name = locale.get_display_name(locale) or code
    return name[:1].upper() + name[1:]


def effective_language(choice: str | None, tag: str | None) -> str | None:
    """The stored choice when it names a catalogue, else the Telegram tag, else ``None``.

    Total: any input type yields a result, never an exception.
    """
    if isinstance(choice, str) and choice in AVAILABLE_LANGUAGES:
        return choice
    if isinstance(tag, str) and tag:
        return tag
    return None
