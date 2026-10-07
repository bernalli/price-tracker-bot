"""AST-enforced layer boundaries for i18n/, bot/ui/ and app/views.py.

There is no import-linter configuration in the repository yet, so the
``ui-pure``/``leaves`` boundary of these three packages is enforced here by
walking the AST of each file: ``i18n`` stays a leaf (stdlib and Babel only),
``bot/ui`` never reaches Telegram, storage, the scheduler or the notifier,
and ``app/views`` never reaches the bot package.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _REPO_ROOT / "src"
_PACKAGE_ROOT = _SRC_ROOT / "price_tracker"

# The gettext runtime still lives in price_tracker.bot.messages; once it moves
# into price_tracker.i18n this set becomes empty and the test below requires
# exactly that.
TRANSITIONAL_IMPORTS: Final = frozenset({"price_tracker.bot.messages"})

# The callback codec is plain data (stdlib and Babel only); screens encode their own buttons.
CODEC_MODULE: Final = "price_tracker.bot.callbacks"
CODEC_IMPORTERS: Final = frozenset(
    {"src/price_tracker/bot/ui/panels.py", "src/price_tracker/bot/ui/cards.py"}
)

# The command registry is plain data; only the help screen renders it.
COMMANDS_MODULE: Final = "price_tracker.bot.commands"
COMMANDS_IMPORTERS: Final = frozenset({"src/price_tracker/bot/ui/panels.py"})

_STDLIB: Final = frozenset(sys.stdlib_module_names)

# Modules that import at run time: any import of them, under any alias, is a
# way around the static scan and is rejected outright in these packages.
_DYNAMIC_IMPORT_MODULES: Final = frozenset({"importlib", "builtins"})


def _module_dotted_name(path: Path) -> tuple[str, bool]:
    """The dotted module name for ``path``, and whether it is a package ``__init__``."""
    rel = path.relative_to(_SRC_ROOT).with_suffix("")
    parts = list(rel.parts)
    is_init = parts[-1] == "__init__"
    if is_init:
        parts = parts[:-1]
    return ".".join(parts), is_init


def _resolved_imports(tree: ast.Module, module_dotted: str, is_init: bool) -> list[str]:
    """Every fully-qualified module name reached by an Import/ImportFrom node,
    at any depth in the file (so imports inside a function body count too)."""
    resolved: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            resolved.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            else:
                parts = module_dotted.split(".")
                if not is_init:
                    parts = parts[:-1]
                trim = node.level - 1
                if trim:
                    parts = parts[:-trim] if trim < len(parts) else []
                base = ".".join(parts)
                if node.module:
                    base = f"{base}.{node.module}" if base else node.module
            resolved.extend(f"{base}.{alias.name}" if base else alias.name for alias in node.names)
    return resolved


def _dynamic_import_usages(tree: ast.Module) -> list[str]:
    """Every run-time import path: a ``Name`` resolving to ``importlib`` or
    ``__import__``, an ``Attribute`` chain rooted in ``importlib``, or any
    ``import``/``from ... import`` of ``importlib`` or ``builtins`` under
    whatever alias."""
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in ("importlib", "__import__"):
            found.append(node.id)
        elif isinstance(node, ast.Attribute):
            base = node
            while isinstance(base, ast.Attribute):
                base = base.value  # type: ignore[assignment]
            if isinstance(base, ast.Name) and base.id == "importlib":
                found.append("importlib")
        elif isinstance(node, ast.Import):
            found.extend(
                alias.name
                for alias in node.names
                if alias.name.split(".")[0] in _DYNAMIC_IMPORT_MODULES
            )
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and node.module.split(".")[0] in _DYNAMIC_IMPORT_MODULES
        ):
            found.append(node.module)
    return found


def _check_dynamic_imports(tree: ast.Module, label: str) -> list[str]:
    usages = _dynamic_import_usages(tree)
    return [f"{label}: dynamic import via {usage!r}" for usage in usages]


def _check_i18n(imports: list[str], label: str) -> list[str]:
    violations = []
    for name in imports:
        top = name.split(".")[0]
        if top in _STDLIB:
            continue
        if name == "babel" or name.startswith("babel."):
            continue
        if name == "price_tracker.i18n" or name.startswith("price_tracker.i18n."):
            continue
        violations.append(f"{label}: import {name!r} is not stdlib, babel, or price_tracker.i18n")
    return violations


def _check_bot_ui(imports: list[str], label: str) -> list[str]:
    violations = []
    for name in imports:
        top = name.split(".")[0]
        if top == "telegram":
            violations.append(f"{label}: import {name!r} reaches telegram")
            continue
        if name == "price_tracker.db" or name.startswith("price_tracker.db."):
            violations.append(f"{label}: import {name!r} reaches price_tracker.db")
        elif name == "price_tracker.core" or (
            name.startswith("price_tracker.core.")
            and not (
                name == "price_tracker.core.textlimits"
                or name.startswith("price_tracker.core.textlimits.")
            )
        ):
            violations.append(f"{label}: import {name!r} reaches price_tracker.core")
        elif name == "price_tracker.notifier" or name.startswith("price_tracker.notifier."):
            violations.append(f"{label}: import {name!r} reaches price_tracker.notifier")
        elif name == "price_tracker.scrapers" or name.startswith("price_tracker.scrapers."):
            violations.append(f"{label}: import {name!r} reaches price_tracker.scrapers")
        elif name == "price_tracker.main" or name.startswith("price_tracker.main."):
            violations.append(f"{label}: import {name!r} reaches price_tracker.main")
        elif name == "price_tracker.app" or (
            name.startswith("price_tracker.app.")
            and not (
                name == "price_tracker.app.views" or name.startswith("price_tracker.app.views.")
            )
        ):
            violations.append(f"{label}: import {name!r} reaches price_tracker.app")
        elif name == "price_tracker.bot" or (
            name.startswith("price_tracker.bot.")
            and not (
                name == "price_tracker.bot.ui"
                or name.startswith("price_tracker.bot.ui.")
                or (
                    label in CODEC_IMPORTERS
                    and (name == CODEC_MODULE or name.startswith(f"{CODEC_MODULE}."))
                )
                or (label in COMMANDS_IMPORTERS and name.startswith(f"{COMMANDS_MODULE}."))
                or name in TRANSITIONAL_IMPORTS
                or any(name.startswith(f"{t}.") for t in TRANSITIONAL_IMPORTS)
            )
        ):
            violations.append(f"{label}: import {name!r} reaches price_tracker.bot")
    return violations


def _check_app_views(imports: list[str], label: str) -> list[str]:
    violations = []
    for name in imports:
        top = name.split(".")[0]
        if top == "telegram":
            violations.append(f"{label}: import {name!r} reaches telegram")
        if name == "price_tracker.bot" or name.startswith("price_tracker.bot."):
            violations.append(f"{label}: import {name!r} reaches price_tracker.bot")
    return violations


def _iter_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py")) if root.exists() else []


def _scan(root: Path, checker) -> list[str]:  # noqa: ANN001
    violations: list[str] = []
    for path in _iter_files(root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        module_dotted, is_init = _module_dotted_name(path)
        label = str(path.relative_to(_REPO_ROOT))
        imports = _resolved_imports(tree, module_dotted, is_init)
        violations.extend(checker(imports, label))
        violations.extend(_check_dynamic_imports(tree, label))
    return violations


def test_i18n_package_is_a_leaf() -> None:
    violations = _scan(_PACKAGE_ROOT / "i18n", _check_i18n)
    assert violations == []


def test_bot_ui_package_boundaries() -> None:
    violations = _scan(_PACKAGE_ROOT / "bot" / "ui", _check_bot_ui)
    assert violations == []


def test_codec_exception_is_scoped_to_the_screens_that_encode_their_buttons() -> None:
    for importer in CODEC_IMPORTERS:
        assert _check_bot_ui([CODEC_MODULE], importer) == []
    assert _check_bot_ui([CODEC_MODULE], "src/price_tracker/bot/ui/labels.py")


def test_registry_exception_is_scoped_to_the_help_screen() -> None:
    for importer in COMMANDS_IMPORTERS:
        assert _check_bot_ui([f"{COMMANDS_MODULE}.COMMANDS"], importer) == []
    assert _check_bot_ui([f"{COMMANDS_MODULE}.COMMANDS"], "src/price_tracker/bot/ui/cards.py")
    assert _check_bot_ui(
        ["price_tracker.bot.command_menus.menu_commands"], next(iter(COMMANDS_IMPORTERS))
    )


def test_app_views_boundaries() -> None:
    tree = ast.parse(
        (_PACKAGE_ROOT / "app" / "views.py").read_text(encoding="utf-8"),
        filename="app/views.py",
    )
    module_dotted, is_init = _module_dotted_name(_PACKAGE_ROOT / "app" / "views.py")
    imports = _resolved_imports(tree, module_dotted, is_init)
    violations = _check_app_views(imports, "src/price_tracker/app/views.py")
    violations.extend(_check_dynamic_imports(tree, "src/price_tracker/app/views.py"))
    assert violations == []


def test_transitional_imports_names_bot_messages_only() -> None:
    # Moving the gettext runtime into price_tracker.i18n empties this constant,
    # and this assertion is the one to update when that happens.
    assert frozenset({"price_tracker.bot.messages"}) == TRANSITIONAL_IMPORTS


# --- positive controls: the scan must actually be able to fail ---------------


def test_scan_flags_a_forbidden_telegram_import(tmp_path: Path) -> None:
    module = tmp_path / "leaky.py"
    module.write_text("import telegram\n", encoding="utf-8")
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    imports = _resolved_imports(tree, "leaky", is_init=False)
    violations = _check_bot_ui(imports, "leaky.py")
    assert violations, "the scan did not flag a forbidden telegram import"


def test_scan_flags_a_dynamic_telegram_import(tmp_path: Path) -> None:
    module = tmp_path / "leaky_dynamic.py"
    module.write_text('import importlib\nimportlib.import_module("telegram")\n', encoding="utf-8")
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    violations = _check_dynamic_imports(tree, "leaky_dynamic.py")
    assert violations, "the scan did not flag a dynamic import via importlib"


@pytest.mark.parametrize(
    "source",
    [
        'from importlib import import_module\nimport_module("telegram")\n',
        'import importlib as il\nil.import_module("telegram")\n',
        'import builtins\nbuiltins.__import__("telegram")\n',
    ],
)
def test_scan_flags_every_aliased_dynamic_import(source: str) -> None:
    tree = ast.parse(source)
    assert _check_dynamic_imports(tree, "leaky_alias.py"), source


def test_scan_flags_a_forbidden_non_babel_import_in_i18n(tmp_path: Path) -> None:
    module = tmp_path / "leaky_i18n.py"
    module.write_text("import requests\n", encoding="utf-8")
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    imports = _resolved_imports(tree, "leaky_i18n", is_init=False)
    violations = _check_i18n(imports, "leaky_i18n.py")
    assert violations, "the scan did not flag a non-stdlib, non-babel import"


# --- effect-free import: subprocess boundary (mirrors
# tests/unit/test_price_core_boundary.py) ------------------------------------


def test_ui_imports_are_effect_free() -> None:
    script = (
        "import sys\n"
        "import price_tracker.bot.ui.cards, price_tracker.i18n.format, "
        "price_tracker.app.views\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    loaded = set(result.stdout.splitlines())
    forbidden = {
        "telegram",
        "price_tracker.db",
        "price_tracker.core.scheduler",
        "price_tracker.notifier",
        "aiosqlite",
        "httpx",
    }
    leaked = loaded & forbidden
    assert not leaked, leaked
