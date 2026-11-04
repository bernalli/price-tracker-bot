"""The runtime image installs no browser.

Browser rendering is disabled because Chromium cannot use the validated-address
transport (``scrapers/playwright_fallback.py``), and no dependency installs
Playwright. The Dockerfile still tried to download Chromium and installed its
system libraries: dead weight and attack surface in every image.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
_INSTRUCTIONS = "\n".join(
    line for line in _DOCKERFILE.splitlines() if not line.lstrip().startswith("#")
)

# Libraries Chromium needs and nothing else in this image uses.
_CHROMIUM_ONLY = (
    "libnss3",
    "libnspr4",
    "libatk1.0-0",
    "libatk-bridge2.0-0",
    "libcups2",
    "libdrm2",
    "libxkbcommon0",
    "libxcomposite1",
    "libxdamage1",
    "libxrandr2",
    "libgbm1",
    "libasound2",
    "libatspi2.0-0",
    "fonts-unifont",
)


def test_no_playwright_install_step() -> None:
    assert "playwright" not in _INSTRUCTIONS.lower()
    assert "chromium" not in _INSTRUCTIONS.lower()


@pytest.mark.parametrize("package", _CHROMIUM_ONLY)
def test_no_chromium_only_system_library(package: str) -> None:
    assert re.search(rf"(?<![\w.-]){re.escape(package)}(?![\w.-])", _INSTRUCTIONS) is None


def test_runtime_still_runs_as_the_unprivileged_user() -> None:
    runtime = _INSTRUCTIONS.split("AS runtime", 1)[1]
    assert re.search(r"^USER botuser$", runtime, re.MULTILINE)
    assert "ca-certificates" in runtime


def test_no_dependency_installs_playwright() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requirements = [*project["dependencies"]]
    for extra in project.get("optional-dependencies", {}).values():
        requirements.extend(extra)
    assert not [r for r in requirements if r.lower().startswith("playwright")]
