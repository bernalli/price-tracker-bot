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

# Telegram's restricted HTML subset (Bot API "HTML style"): only these tags
# are accepted, and every open tag must be closed with correct nesting.
_ALLOWED_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "code",
        "pre",
        "a",
        "span",
        "tg-spoiler",
        "blockquote",
    }
)
_MARKUP_TAG_RE = re.compile(r"<(/?)([a-zA-Z][a-zA-Z0-9-]*)(?:\s[^>]*)?>")
# Telegram rejects these tags when nested inside themselves.
_NON_NESTABLE_TAGS = frozenset({"blockquote", "pre", "code"})
# Only the four named entities Telegram documents, plus well-formed decimal
# and hexadecimal numeric references, count as valid entities.
_VALID_ENTITY_RE = re.compile(r"&(amp|lt|gt|quot|#[0-9]+|#[xX][0-9a-fA-F]+);")


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


def _is_valid_telegram_markup(fragment: str) -> bool:
    """Return whether ``fragment`` is well-formed Telegram HTML.

    Fail-closed grammar check: only tags in ``_ALLOWED_TAGS`` are accepted,
    every opening tag must be closed by the same tag with correct (stack)
    nesting inside the fragment, and every ``&``-led sequence must be one of
    the four named entities or a well-formed numeric entity. Anything else —
    an unknown tag, a tag left open or closed without a match, crossed
    nesting, or an entity-like sequence that is not well-formed — makes the
    fragment invalid, so callers degrade it to plain text instead of risking
    a Telegram-rejected message.
    """
    stack: list[str] = []
    pos = 0
    length = len(fragment)
    while pos < length:
        char = fragment[pos]
        if char == "<":
            tag_match = _MARKUP_TAG_RE.match(fragment, pos)
            if tag_match is None:
                return False
            is_closing = tag_match.group(1) == "/"
            tag_name = tag_match.group(2).lower()
            if tag_name not in _ALLOWED_TAGS:
                return False
            if is_closing:
                if not stack or stack[-1] != tag_name:
                    return False
                stack.pop()
            else:
                if tag_name in _NON_NESTABLE_TAGS and tag_name in stack:
                    return False
                stack.append(tag_name)
            pos = tag_match.end()
        elif char == ">":
            # Telegram requires a bare ">" to be escaped as "&gt;".
            return False
        elif char == "&":
            entity_match = _VALID_ENTITY_RE.match(fragment, pos)
            if entity_match is None:
                return False
            pos = entity_match.end()
        else:
            pos += 1
    return not stack


def split_message(text: str, *, limit: int = SAFE_LIMIT) -> list[str]:
    """Split balanced HTML rows into chunks no longer than ``limit`` visibly.

    Each input row must have balanced tags. A row too long, or one that does
    not pass the restricted Telegram markup grammar, is degraded to escaped
    plain text so it cannot leave an HTML entity open or reach Telegram with
    unsupported markup (fail-closed: plain text always renders).
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return []

    chunks: list[str] = []
    current: str | None = None
    for original_line in text.split("\n"):
        line = original_line
        if visible_length(line) > limit or not _is_valid_telegram_markup(line):
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
    # Two newlines plus at least one visible character for a block.
    if visible_length(header) + visible_length(footer) + 3 <= limit:
        return header, footer
    envelope_budget = limit - 3
    header_budget = max(0, envelope_budget // 2)
    footer_budget = max(0, envelope_budget - header_budget)
    return _degrade_line(header, header_budget), _degrade_line(footer, footer_budget)


def paginate(
    header: str, blocks: list[tuple[K, str]], footer: str, *, limit: int = SAFE_LIMIT
) -> list[tuple[str, list[K]]]:
    """Paginate indivisible blocks with a repeated header and footer under ``limit``.

    The header, the footer and each block must pass the restricted Telegram
    markup grammar; anything too long or failing that grammar is degraded to
    escaped plain text (fail-closed, same rule as ``split_message``).
    """
    if limit <= 2:
        raise ValueError("limit must leave room for a header, block, and footer")
    if not blocks:
        return []

    bounded_header, bounded_footer = _bounded_envelope(header, footer, limit)
    # Fail-closed on the envelope too: a header or footer whose markup is not
    # well-formed would break every page, not just one block.
    if not _is_valid_telegram_markup(bounded_header):
        bounded_header = _degrade_line(bounded_header, limit)
    if not _is_valid_telegram_markup(bounded_footer):
        bounded_footer = _degrade_line(bounded_footer, limit)
    room = limit - visible_length(bounded_header) - visible_length(bounded_footer) - 2
    if room <= 0:
        raise ValueError("header and footer leave no room for blocks")

    pages: list[tuple[str, list[K]]] = []
    page_blocks: list[str] = []
    page_keys: list[K] = []
    for key, original_block in blocks:
        block = original_block
        if visible_length(block) > room or not _is_valid_telegram_markup(block):
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
