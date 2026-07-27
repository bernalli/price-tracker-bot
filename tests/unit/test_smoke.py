"""Smoke test — verifies the package imports and reports a coherent version."""

from __future__ import annotations

import tomllib
from pathlib import Path

from price_tracker import __version__

PYPROJECT = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_version_is_set():
    assert __version__


def test_version_matches_pyproject():
    """The two declared versions must not drift apart.

    They silently did: the package reported ``0.1.0.dev0`` while the project had
    already shipped ``0.1.15``, because the old assertion pinned the literal
    placeholder instead of checking the two sources agreed.
    """
    declared = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]
    assert __version__ == declared
