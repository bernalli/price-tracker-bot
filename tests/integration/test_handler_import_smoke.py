"""Handler import smoke — defense against legacy module ``ModuleNotFoundError``.

The monolith split sprinkled deferred imports of legacy bare module
names inside handler functions. Those imports stayed dormant until a Telegram
user hit the command, at which point the bot crashed with
``ModuleNotFoundError`` and the user received the generic
``"❌ Si è verificato un errore"``.

``test_no_stale_imports.py`` catches the **textual** form of those imports;
this file catches the **resolvable** form by literally importing every handler
module and asserting it loads. The pair closes the class:

* Static regex catches ``from <legacy> import X`` that grep can see.
* Import resolution catches the same drift on any other path we might have
  forgotten to enumerate (renames, package moves, deleted symbols).

A failure here means the bot is shippable but every command would fail; we
want CI red, not Telegram red.
"""

from __future__ import annotations

import importlib
import pkgutil

import price_tracker.bot.handlers as handlers_pkg
import price_tracker.bot.handlers.callbacks as callbacks_pkg


def _iter_modules(pkg: object) -> list[str]:
    """Walk every (sub)module path of ``pkg`` so we can ``import_module`` each one."""
    paths = pkg.__path__  # type: ignore[attr-defined]
    name = pkg.__name__  # type: ignore[attr-defined]
    return [m.name for m in pkgutil.walk_packages(paths, prefix=f"{name}.")]


def test_every_handler_module_imports_clean() -> None:
    """Import every handler + callback module top-down; assert no error."""
    modules = _iter_modules(handlers_pkg) + _iter_modules(callbacks_pkg)
    assert modules, "No handler modules discovered — packaging is broken"
    for mod_name in modules:
        importlib.import_module(mod_name)


def test_every_handler_function_imports_run() -> None:
    """Force every deferred ``from X import Y`` inside handlers to resolve.

    Some handlers defer heavy imports (matplotlib, scheduler, alert formatter)
    behind ``# noqa: PLC0415`` lines that only run when the handler is called
    by Telegram. We can't *call* the handlers here (they need a Telegram
    Update), but we can scan for ``from <name> import`` lines inside function
    bodies and try to resolve ``<name>`` as a module.

    This catches the v0.1.6 class of bug — ``from checker import ...`` /
    ``from chart import ...`` — without needing a Telegram replay test.
    """
    import ast
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    bot_dir = repo_root / "src" / "price_tracker" / "bot"

    failures: list[str] = []
    for path in bot_dir.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # Skip top-level imports (covered by import_module above) — focus
            # on deferred function-body imports which are the typical drift site.
            if isinstance(node, ast.ImportFrom) and node.module:
                module_name = node.module
                try:
                    importlib.import_module(module_name)
                except (ModuleNotFoundError, ImportError) as e:
                    failures.append(
                        f"{path.relative_to(repo_root)}:{node.lineno}: "
                        f"'from {module_name} import ...' → {e}"
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    try:
                        importlib.import_module(alias.name)
                    except (ModuleNotFoundError, ImportError) as e:
                        failures.append(
                            f"{path.relative_to(repo_root)}:{node.lineno}: "
                            f"'import {alias.name}' → {e}"
                        )
    assert not failures, (
        "Deferred import inside a handler cannot be resolved — the bot will "
        "crash with ModuleNotFoundError when the command is invoked. Fix:\n  "
        + "\n  ".join(failures)
    )
