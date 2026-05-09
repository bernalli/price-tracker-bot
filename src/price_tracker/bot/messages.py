"""i18n stub — full gettext wiring comes later."""

from __future__ import annotations


def _(text: str) -> str:
    """No-op i18n marker. Returns the input string verbatim (gettext not wired yet)."""
    return text


def ngettext(singular: str, plural: str, n: int) -> str:
    """No-op plural-aware i18n marker. Returns singular for n==1, plural otherwise."""
    return singular if n == 1 else plural
