"""Confine tests: the guided-flow coordinator lands on ``main`` unwired.

``price_tracker.app`` and the new ``price_tracker.bot.callbacks`` /
``price_tracker.bot.flows`` modules exist on disk but nothing imports or
registers them yet. These tests prove that boundary so a later PR that wires
the coordinator has to touch ``register_handlers`` (and this file) on
purpose, not by accident.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

from telegram.ext import Application, ApplicationBuilder, CallbackQueryHandler

from price_tracker.bot.handlers import register_handlers

# Not a real credential: ApplicationBuilder only validates the shape locally,
# no network call happens while building or registering handlers.
_FAKE_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "price_tracker"


def _build_app_with_handlers() -> Application[Any, Any, Any, Any, Any, Any]:
    app = ApplicationBuilder().token(_FAKE_TOKEN).build()
    register_handlers(app)
    return app


def test_register_handlers_does_not_wire_the_guided_flow_coordinator() -> None:
    """T-B1: register_handlers() registers none of the new coordinator's handlers."""
    app = _build_app_with_handlers()
    counts_by_group = {group: len(handlers) for group, handlers in app.handlers.items()}

    # Imported only now, after the Application is fully populated: proves the
    # count above did not depend on price_tracker.bot.flows being loadable.
    from price_tracker.bot.flows import GuidedFlow, is_unregistered

    for handlers in app.handlers.values():
        for handler in handlers:
            assert not isinstance(handler, GuidedFlow)
            if isinstance(handler, CallbackQueryHandler):
                assert handler.pattern != is_unregistered

    assert counts_by_group == {0: 52}


def test_bot_handlers_and_main_do_not_import_the_coordinator() -> None:
    """T-B2: neither entry point pulls in the coordinator's modules."""
    probe = (
        "import sys\n"
        "import price_tracker.bot.handlers\n"
        "import price_tracker.main\n"
        "loaded = {\n"
        "    'app.inputs': 'price_tracker.app.inputs' in sys.modules,\n"
        "    'bot.callbacks': 'price_tracker.bot.callbacks' in sys.modules,\n"
        "    'bot.flows': 'price_tracker.bot.flows' in sys.modules,\n"
        "}\n"
        "print(loaded)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    loaded = ast.literal_eval(result.stdout.strip())
    assert loaded == {"app.inputs": False, "bot.callbacks": False, "bot.flows": False}

    # Positive control: importing bot.flows really does load all three, so
    # the negative result above is not a broken probe.
    probe_positive = (
        "import sys\n"
        "import price_tracker.bot.flows\n"
        "loaded = {\n"
        "    'app.inputs': 'price_tracker.app.inputs' in sys.modules,\n"
        "    'bot.callbacks': 'price_tracker.bot.callbacks' in sys.modules,\n"
        "    'bot.flows': 'price_tracker.bot.flows' in sys.modules,\n"
        "}\n"
        "print(loaded)\n"
    )
    result_positive = subprocess.run(
        [sys.executable, "-c", probe_positive], capture_output=True, text=True, check=True
    )
    loaded_positive = ast.literal_eval(result_positive.stdout.strip())
    assert loaded_positive == {"app.inputs": True, "bot.callbacks": True, "bot.flows": True}


def _module_name(path: Path) -> str:
    rel = path.relative_to(_SRC_ROOT.parent)
    return ".".join(rel.with_suffix("").parts)


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
            if node.level:
                # Relative import: resolve against the file's own package.
                package_parts = list(path.relative_to(_SRC_ROOT.parent).parts[:-1])
                package_parts = package_parts[: len(package_parts) - (node.level - 1)]
                module = ".".join([*package_parts, module]) if package_parts else module
            names.add(module)
    return names


def test_only_flows_imports_app_inputs_or_bot_callbacks() -> None:
    """T-B3: static scan — app.inputs/bot.callbacks have exactly one importer."""
    importers_of_app_inputs = []
    importers_of_bot_callbacks = []
    importers_of_bot_flows = []

    for path in sorted(_SRC_ROOT.rglob("*.py")):
        module_name = _module_name(path)
        imported = _imported_names(path)
        if any(
            name == "price_tracker.app.inputs" or name.startswith("price_tracker.app.inputs.")
            for name in imported
        ):
            importers_of_app_inputs.append(module_name)
        if any(
            name == "price_tracker.bot.callbacks" or name.startswith("price_tracker.bot.callbacks.")
            for name in imported
        ):
            importers_of_bot_callbacks.append(module_name)
        if any(
            name == "price_tracker.bot.flows" or name.startswith("price_tracker.bot.flows.")
            for name in imported
        ):
            importers_of_bot_flows.append(module_name)

    assert importers_of_app_inputs == ["price_tracker.bot.flows"]
    assert importers_of_bot_callbacks == ["price_tracker.bot.flows"]
    assert importers_of_bot_flows == []


def test_app_inputs_never_imports_telegram_or_bot() -> None:
    """T-B4: app.inputs stays below the layer boundary (C7.2)."""
    probe = (
        "import sys\n"
        "import price_tracker.app.inputs\n"
        "loaded_telegram = any(m == 'telegram' or m.startswith('telegram.') for m in sys.modules)\n"
        "loaded_bot = any(\n"
        "    m == 'price_tracker.bot' or m.startswith('price_tracker.bot.') for m in sys.modules\n"
        ")\n"
        "print({'telegram': loaded_telegram, 'bot': loaded_bot})\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    loaded = ast.literal_eval(result.stdout.strip())
    assert loaded == {"telegram": False, "bot": False}

    # Positive control: bot.flows really does pull in both.
    probe_positive = (
        "import sys\n"
        "import price_tracker.bot.flows\n"
        "loaded_telegram = any(m == 'telegram' or m.startswith('telegram.') for m in sys.modules)\n"
        "loaded_bot = any(\n"
        "    m == 'price_tracker.bot' or m.startswith('price_tracker.bot.') for m in sys.modules\n"
        ")\n"
        "print({'telegram': loaded_telegram, 'bot': loaded_bot})\n"
    )
    result_positive = subprocess.run(
        [sys.executable, "-c", probe_positive], capture_output=True, text=True, check=True
    )
    loaded_positive = ast.literal_eval(result_positive.stdout.strip())
    assert loaded_positive == {"telegram": True, "bot": True}
