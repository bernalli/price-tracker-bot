"""Pure helpers for Telegram's post-entity-parsing message limit."""

from __future__ import annotations

import html
import re
from typing import TypeVar

TELEGRAM_MESSAGE_LIMIT = 4096
SAFE_LIMIT = 4000
NAME_BUDGET = 60
DOMAIN_BUDGET = 60
ERROR_BUDGET = 120
WHY_BUDGET = 40

_TAG_RE = re.compile(r"<[^>]+>")
_ELLIPSIS = "…"
K = TypeVar("K")


def visible_length(html_text: str) -> int:
    """Return Telegram-visible length after stripping tags and unescaping entities."""
    return len(html.unescape(_TAG_RE.sub("", html_text)))


def truncate_visible(text: str, budget: int) -> str:
    """Truncate plain text to ``budget`` characters, including a final ellipsis."""
    if budget < 0:
        raise ValueError("budget must not be negative")
    if len(text) <= budget:
        return text
    if budget == 0:
        return ""
    if budget == 1:
        return _ELLIPSIS
    return f"{text[: budget - 1]}{_ELLIPSIS}"


def _degrade_line(line: str, limit: int) -> str:
    """Turn an oversized HTML line into bounded, escaped plain text."""
    plain = html.unescape(_TAG_RE.sub("", line))
    return html.escape(truncate_visible(plain, limit), quote=True)


def split_message(text: str, *, limit: int = SAFE_LIMIT) -> list[str]:
    """Split balanced HTML rows into chunks no longer than ``limit`` visibly.

    Each input row must have balanced tags. A row too long by itself is
    degraded to escaped plain text so it cannot leave an HTML entity open.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return []

    chunks: list[str] = []
    current: str | None = None
    for original_line in text.split("\n"):
        line = original_line
        if visible_length(line) > limit:
            line = _degrade_line(line, limit)
        candidate = line if current is None else f"{current}\n{line}"
        if current is not None and visible_length(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current is not None:
        chunks.append(current)
    return chunks


def _bounded_envelope(header: str, footer: str, limit: int) -> tuple[str, str]:
    """Reduce an overlong page envelope while retaining valid escaped text."""
    if visible_length(header) + visible_length(footer) + 2 <= limit:
        return header, footer
    header_budget = max(0, (limit - 2) // 2)
    footer_budget = max(0, limit - 2 - header_budget)
    return _degrade_line(header, header_budget), _degrade_line(footer, footer_budget)


def paginate(
    header: str, blocks: list[tuple[K, str]], footer: str, *, limit: int = SAFE_LIMIT
) -> list[tuple[str, list[K]]]:
    """Paginate indivisible blocks with a repeated header and footer under ``limit``."""
    if limit <= 2:
        raise ValueError("limit must leave room for a header, block, and footer")
    if not blocks:
        return []

    bounded_header, bounded_footer = _bounded_envelope(header, footer, limit)
    room = limit - visible_length(bounded_header) - visible_length(bounded_footer) - 2
    if room <= 0:
        raise ValueError("header and footer leave no room for blocks")

    pages: list[tuple[str, list[K]]] = []
    page_blocks: list[str] = []
    page_keys: list[K] = []
    for key, original_block in blocks:
        block = original_block
        if visible_length(block) > room:
            block = _degrade_line(block, room)
        candidate_blocks = [*page_blocks, block]
        candidate = "\n".join((bounded_header, *candidate_blocks, bounded_footer))
        if page_blocks and visible_length(candidate) > limit:
            pages.append(("\n".join((bounded_header, *page_blocks, bounded_footer)), page_keys))
            page_blocks = [block]
            page_keys = [key]
        else:
            page_blocks = candidate_blocks
            page_keys.append(key)
    pages.append(("\n".join((bounded_header, *page_blocks, bounded_footer)), page_keys))
    return pages
