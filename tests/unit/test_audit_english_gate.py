"""The English-only audit is a gate: when it cannot search, it must not pass."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "audit_english.sh"
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not installed")


def _run(tmp_path: Path, fake_rg: str | None) -> subprocess.CompletedProcess[str]:
    """Run the audit with a PATH holding only bash's needs and an optional fake ``rg``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if fake_rg is not None:
        tool = bin_dir / "rg"
        tool.write_text(f"#!/bin/sh\n{fake_rg}\n", encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    assert BASH is not None
    return subprocess.run(  # noqa: S603 - fixed argv, test-owned script
        [BASH, str(SCRIPT)],
        cwd=REPO,
        env={**os.environ, "PATH": str(bin_dir)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_no_match_passes(tmp_path: Path) -> None:
    result = _run(tmp_path, "exit 1")
    assert result.returncode == 0
    assert "OK" in result.stdout


def test_a_match_fails(tmp_path: Path) -> None:
    result = _run(tmp_path, "echo 'src/x.py:1:prezzo'; exit 0")
    assert result.returncode == 1
    assert "prezzo" in result.stdout


def test_a_missing_search_tool_is_not_a_pass(tmp_path: Path) -> None:
    result = _run(tmp_path, None)
    assert result.returncode == 2
    assert "OK" not in result.stdout


def test_a_failing_search_tool_is_not_a_pass(tmp_path: Path) -> None:
    result = _run(tmp_path, "echo 'regex engine unavailable' >&2; exit 2")
    assert result.returncode == 2
    assert "OK" not in result.stdout
