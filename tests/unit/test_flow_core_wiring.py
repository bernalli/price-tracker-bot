"""Layout of the handlers once the guided-flow coordinator is wired.

Group 0 holds the coordinator alone, group 1 ``/cancel``, group 2 every legacy
handler in its historical order. Python-telegram-bot runs at most one handler
per group, so the legacy handlers must not share a group with the coordinator:
an update the coordinator closes and passes on still reaches its legacy handler.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    BaseHandler,
    CallbackQueryHandler,
    CommandHandler,
)

from price_tracker.bot.flow_services import RepositoryFlowServices
from price_tracker.bot.flows import GuidedFlow, JobQueueTimer, is_unregistered
from price_tracker.bot.handlers import _register_legacy, register_handlers
from tests.support.fake_telegram import FakeRequest, make_application, message_update

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Not a real credential: ApplicationBuilder only validates the shape locally,
# no network call happens while building or registering handlers.
_FAKE_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "price_tracker"

AnyApp = Application[Any, Any, Any, Any, Any, Any]


def _app() -> AnyApp:
    return ApplicationBuilder().token(_FAKE_TOKEN).build()


def _wired() -> AnyApp:
    app = _app()
    register_handlers(app)
    return app


def _signature(handler: BaseHandler[Any, Any, Any]) -> tuple[Any, ...]:
    commands = frozenset(handler.commands) if isinstance(handler, CommandHandler) else None
    return (type(handler), handler.callback, commands)


def test_groups_are_coordinator_cancel_and_legacy_in_order() -> None:
    app = _wired()
    reference = _app()
    _register_legacy(reference)
    legacy = reference.handlers[0]

    assert list(app.handlers) == [0, 1, 2]
    assert len(app.handlers[0]) == 1
    assert isinstance(app.handlers[0][0], GuidedFlow)
    assert len(app.handlers[1]) == 1
    cancel = app.handlers[1][0]
    assert isinstance(cancel, CommandHandler)
    assert cancel.commands == frozenset({"cancel"})
    assert len(legacy) == 52
    assert [_signature(h) for h in app.handlers[2]] == [_signature(h) for h in legacy]
    assert not any(
        isinstance(h, CallbackQueryHandler) and h.pattern is is_unregistered
        for handlers in app.handlers.values()
        for h in handlers
    )


def test_registered_coordinator_serves_the_three_value_prompts_only() -> None:
    flow = _wired().handlers[0][0]

    assert isinstance(flow, GuidedFlow)
    assert flow.config.add_entry is False
    assert isinstance(flow.timer, JobQueueTimer)
    assert isinstance(flow.services, RepositoryFlowServices)
    assert flow.config.timeout_seconds == 300
    assert flow.config.max_attempts == 3


@pytest.mark.filterwarnings("ignore:No `JobQueue` set up:UserWarning")
def test_registration_without_a_job_queue_fails_before_any_handler() -> None:
    app = ApplicationBuilder().token(_FAKE_TOKEN).job_queue(None).build()

    with pytest.raises(RuntimeError, match="job queue"):
        register_handlers(app)

    assert app.handlers == {}


def test_app_inputs_never_imports_telegram_or_bot() -> None:
    """app.inputs stays below the layer boundary."""
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
    assert ast.literal_eval(result.stdout.strip()) == {"telegram": False, "bot": False}

    # Positive control: bot.flows really does pull in both.
    positive = probe.replace("price_tracker.app.inputs", "price_tracker.bot.flows", 1)
    result = subprocess.run(
        [sys.executable, "-c", positive], capture_output=True, text=True, check=True
    )
    assert ast.literal_eval(result.stdout.strip()) == {"telegram": True, "bot": True}


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(_SRC_ROOT.parent).with_suffix("").parts)


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module = node.module
            if node.level:
                package = list(path.relative_to(_SRC_ROOT.parent).parts[:-1])
                package = package[: len(package) - (node.level - 1)]
                module = ".".join([*package, module]) if package else module
            names.add(module)
    return names


def _importers(target: str) -> list[str]:
    return [
        _module_name(path)
        for path in sorted(_SRC_ROOT.rglob("*.py"))
        if any(n == target or n.startswith(f"{target}.") for n in _imported_names(path))
    ]


def test_coordinator_modules_have_the_expected_importers() -> None:
    assert _importers("price_tracker.bot.flows") == [
        "price_tracker.bot.flow_services",
        "price_tracker.bot.handlers.__init__",
    ]
    assert _importers("price_tracker.bot.callbacks") == ["price_tracker.bot.flows"]
    assert _importers("price_tracker.app.inputs") == [
        "price_tracker.bot.flow_services",
        "price_tracker.bot.flows",
    ]
    assert "price_tracker.db.repository" not in _imported_names(
        _SRC_ROOT / "bot" / "flow_services.py"
    )
    # Positive control of the scan: a known importer of the repository is seen.
    assert "price_tracker.main" in _importers("price_tracker.db.repository")


@pytest.fixture
async def initialized() -> AsyncIterator[AnyApp]:
    """The wired application with a bot that knows its own username."""
    app = make_application(FakeRequest(), with_job_queue=True)
    register_handlers(app)
    await app.initialize()
    yield app
    await app.shutdown()


def _cancel_handler(app: AnyApp) -> CommandHandler[Any, Any]:
    handler = app.handlers[1][0]
    assert isinstance(handler, CommandHandler)
    return handler


@pytest.mark.parametrize("kind", ["channel_post", "edited_channel_post", "guest_message"])
async def test_cancel_handler_rejects_channel_and_guest_posts(
    initialized: AnyApp, kind: str
) -> None:
    update = message_update(initialized.bot, -1001, 10, "/cancel", kind=kind)

    assert not _cancel_handler(initialized).check_update(update)


async def test_cancel_handler_accepts_a_message(initialized: AnyApp) -> None:
    update = message_update(initialized.bot, 100, 10, "/cancel")

    assert _cancel_handler(initialized).check_update(update)
