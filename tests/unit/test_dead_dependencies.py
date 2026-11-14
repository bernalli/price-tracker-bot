"""No dependency, module or system library of the removed Scrapling, browserforge
and Playwright backends remains. `curl_cffi` is a dependency again, as the Chrome
store wire; its wheels link libcurl statically, so the image still installs no curl
library.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from price_tracker.core.registry import ScraperRegistry, discover_builtin_scrapers

_ROOT = Path(__file__).resolve().parents[2]
_THIS_FILE = Path(__file__).resolve()

_DEAD_DISTRIBUTIONS = frozenset({"scrapling", "browserforge", "playwright"})
_DEAD_LOCKED = frozenset(
    {"scrapling", "browserforge", "apify-fingerprint-datapoints", "playwright"}
)
_DEAD_MODULES = frozenset({"scrapling", "browserforge", "playwright"})


def _requirement_name(requirement: str) -> str:
    """Normalized distribution name of a PEP 508 requirement string."""
    name = re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0]
    return name.lower().replace("_", "-")


def test_project_declares_no_dead_fetch_dependency() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    requirements = list(project.get("dependencies", []))
    for group in project.get("optional-dependencies", {}).values():
        requirements.extend(group)
    names = {_requirement_name(requirement) for requirement in requirements}
    assert not names & _DEAD_DISTRIBUTIONS, names & _DEAD_DISTRIBUTIONS


def test_lock_holds_no_dead_fetch_package() -> None:
    lock = tomllib.loads((_ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = lock["package"]
    locked = {package["name"] for package in packages}
    assert not locked & _DEAD_LOCKED, locked & _DEAD_LOCKED
    (project,) = (package for package in packages if package["name"] == "price-tracker")
    declared = {dependency["name"] for dependency in project.get("dependencies", [])}
    assert not declared & _DEAD_LOCKED, declared & _DEAD_LOCKED


def test_source_tree_imports_no_dead_fetch_module() -> None:
    offenders: list[str] = []
    for top in ("src", "tests"):
        for path in sorted((_ROOT / top).rglob("*.py")):
            if path.resolve() == _THIS_FILE:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    modules = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    modules = [node.module]
                else:
                    continue
                for module in modules:
                    if module.split(".", 1)[0] in _DEAD_MODULES:
                        offenders.append(f"{path.relative_to(_ROOT)}:{node.lineno}: {module}")
    assert not offenders, offenders


def test_importing_every_module_loads_no_dead_fetch_module() -> None:
    script = (
        "import importlib, pkgutil, sys\n"
        "import price_tracker\n"
        "for info in pkgutil.walk_packages(price_tracker.__path__, 'price_tracker.'):\n"
        "    importlib.import_module(info.name)\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_ROOT,
        env={**os.environ, "PYTHONPATH": "src", "PYTHON_DOTENV_DISABLED": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = result.stdout.splitlines()
    leaked = [
        name
        for name in loaded
        if any(name == dead or name.startswith(f"{dead}.") for dead in _DEAD_MODULES)
    ]
    assert not leaked, leaked


def test_runtime_image_installs_no_curl_library() -> None:
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    instructions = "\n".join(
        line for line in dockerfile.splitlines() if not line.lstrip().startswith("#")
    )
    assert "libcurl" not in instructions


def test_registry_has_no_browser_fallback() -> None:
    registry = ScraperRegistry()
    discover_builtin_scrapers(registry)
    names = {scraper.name for scraper in registry}
    assert "playwright_fallback" not in names
    assert importlib.util.find_spec("price_tracker.scrapers.playwright_fallback") is None
