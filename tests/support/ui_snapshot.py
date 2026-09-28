"""Text-format snapshots for a rendered price_tracker.bot.ui.screens.Screen.

File format (UTF-8, ``\\n``, trailing newline)::

    # ui snapshot v1
    # screen: card.<variant>
    # locale: <locale>
    # now: 2026-03-01T12:00:00Z
    text:
       | <line 1 of the HTML text, verbatim>
       | <line 2>
    keyboard:
       [<label>](cb:<callback>)  [<label>](cb:<callback>)
       [<label>](url:<url>)

Comparison is byte for byte. ``UI_SNAPSHOTS_UPDATE=1`` (re)writes the file
instead of comparing; that update is refused under ``CI`` (a real mismatch
must never be silently rebaselined in a CI run) before anything is written
to disk. A missing snapshot fails with the exact command to create it.
"""

from __future__ import annotations

import difflib
import os
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from price_tracker.bot.ui.screens import Screen

_HEADER = "# ui snapshot v1"


def render_snapshot(screen: Screen, *, screen_name: str, locale: str, now: str) -> str:
    """Render ``screen`` to the on-disk snapshot text format."""
    lines = [
        _HEADER,
        f"# screen: {screen_name}",
        f"# locale: {locale}",
        f"# now: {now}",
        "text:",
    ]
    lines.extend(f"   | {line}" for line in screen.text.split("\n"))
    lines.append("keyboard:")
    for row in screen.rows:
        buttons = "  ".join(
            f"[{btn.label}](cb:{btn.callback})"
            if btn.callback is not None
            else f"[{btn.label}](url:{btn.url})"
            for btn in row
        )
        lines.append(f"   {buttons}")
    return "\n".join(lines) + "\n"


def compare_or_update(path: Path, content: str) -> None:
    """Compare ``content`` against ``path``, or (re)write it under the update switch."""
    if os.environ.get("UI_SNAPSHOTS_UPDATE") == "1":
        if os.environ.get("CI"):
            raise RuntimeError(
                "UI_SNAPSHOTS_UPDATE=1 is refused under CI: a mismatch must be "
                "fixed in code or reviewed locally, never rebaselined in CI."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return

    if not path.exists():
        pytest.fail(
            f"missing snapshot {path}; create it with:\n"
            f"  UI_SNAPSHOTS_UPDATE=1 uv run pytest {path.as_posix()!r} "
            "(rerun the failing test id with that env var set)"
        )

    existing = path.read_text(encoding="utf-8")
    if existing != content:
        diff = "".join(
            difflib.unified_diff(
                existing.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=str(path),
                tofile="rendered",
            )
        )
        pytest.fail(f"snapshot mismatch for {path}:\n{diff}")
