"""The renderer's output type: plain data, no Telegram types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Button:
    """One inline keyboard button: exactly one of ``callback`` or ``url``."""

    label: str
    callback: str | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("label must not be empty")
        if (self.callback is None) == (self.url is None):
            raise ValueError("exactly one of callback or url is required")


@dataclass(frozen=True, slots=True)
class Screen:
    """A rendered screen: HTML text plus an inline keyboard laid out in rows."""

    text: str
    rows: tuple[tuple[Button, ...], ...] = ()
    disable_link_preview: bool = True
