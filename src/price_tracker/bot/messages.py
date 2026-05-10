"""i18n runtime: gettext-backed translation with ContextVar dispatch.

Resolution chain for a requested lang_code:
  1. Normalized lang_code (e.g. "it-IT" -> "it_IT")
  2. 2-letter prefix matched against any available region (e.g. "it" -> "it_IT")
  3. _DEFAULT_LOCALE (env LOCALE)
  4. Hard fallback "en"

Per-update locale set via ContextVar so concurrent asyncio handlers stay
isolated. Translations are cached LRU(8) to keep file I/O off the hot path.
"""

from __future__ import annotations

import gettext
import os
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

_LOCALE_DIR: Path = Path(__file__).parent.parent / "locale"
_DEFAULT_LOCALE: str = os.getenv("LOCALE", "en")
_AVAILABLE: set[str] = {"en", "it_IT"}

_null_translations: gettext.NullTranslations = gettext.NullTranslations()

_translation_var: ContextVar[gettext.NullTranslations] = ContextVar(
    "_translation_var",
    default=_null_translations,
)


def _normalize(lang_code: str) -> str:
    """Normalize a BCP-47-ish code to gettext convention.

    Examples:
        "it"     -> "it"
        "it-IT"  -> "it_IT"
        "it_it"  -> "it_IT"
        "EN-us"  -> "en_US"
    """
    parts = lang_code.replace("-", "_").split("_")
    if len(parts) == 1:
        return parts[0].lower()
    return f"{parts[0].lower()}_{parts[1].upper()}"


def _resolve_to_available(cand: str) -> str | None:
    """Map a candidate code to an entry in _AVAILABLE, or None if no match.

    Two-letter candidates match the first available region with that prefix.
    """
    if cand in _AVAILABLE:
        return cand
    if "_" not in cand:
        for avail in _AVAILABLE:
            if avail.split("_")[0] == cand:
                return avail
    return None


@lru_cache(maxsize=8)
def get_translation(lang_code: str | None) -> gettext.NullTranslations:
    """Return GNUTranslations for the resolved lang_code, with fallback chain.

    Returns NullTranslations() (passthrough) only as a final resort.
    """
    candidates: list[str] = []
    if lang_code:
        normalized = _normalize(lang_code)
        candidates.append(normalized)
        if "_" in normalized:
            candidates.append(normalized.split("_")[0])
    candidates.append(_DEFAULT_LOCALE)
    candidates.append("en")

    seen: set[str] = set()
    for cand in candidates:
        if cand in seen:
            continue
        seen.add(cand)
        resolved = _resolve_to_available(cand)
        if resolved is None:
            continue
        try:
            return gettext.translation(
                "messages",
                localedir=_LOCALE_DIR,
                languages=[resolved],
            )
        except OSError:
            # FileNotFoundError (catalog absent) or corrupt .mo (Bad magic
            # number, truncated file, permission error) — fall through to
            # next candidate so the runtime never crashes on bad locale data.
            continue
    return gettext.NullTranslations()


def set_locale(lang_code: str | None) -> None:
    """Set the GNUTranslations instance for the current asyncio context."""
    _translation_var.set(get_translation(lang_code))


def _(text: str) -> str:
    """Translate `text` per current ContextVar locale."""
    return _translation_var.get().gettext(text)


def ngettext(singular: str, plural: str, n: int) -> str:
    """Plural-aware translation per current ContextVar locale."""
    return _translation_var.get().ngettext(singular, plural, n)
