"""The closed command registry: it names exactly the commands that are registered."""

from __future__ import annotations

import ast
import re
import string
from pathlib import Path
from typing import Any

import pytest
from telegram.ext import CommandHandler

from price_tracker.bot.commands import ALIAS_COMMANDS, COMMANDS, GROUPS
from price_tracker.bot.handlers import register_handlers
from price_tracker.bot.messages import get_translation
from tests.support.fake_telegram import FakeRequest, make_application

HANDLERS_DIR = Path(__file__).resolve().parents[2] / "src/price_tracker/bot/handlers"
NAME_RE = re.compile(r"[a-z0-9_]{1,32}")


def _registered() -> dict[str, str]:
    """Every registered command name mapped to the name of its handler function."""
    app = make_application(FakeRequest(), with_job_queue=True)
    register_handlers(app)
    registered: dict[str, str] = {}
    for handlers in app.handlers.values():
        for handler in handlers:
            if isinstance(handler, CommandHandler):
                for command in handler.commands:
                    registered[command] = handler.callback.__name__
    return registered


def _admin_only_functions() -> set[str]:
    names: set[str] = set()
    for path in HANDLERS_DIR.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.AsyncFunctionDef) and any(
                isinstance(d, ast.Name) and d.id == "admin_only" for d in node.decorator_list
            ):
                names.add(node.name)
    return names


def _placeholders(text: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(text) if field}


def test_the_registry_and_the_registered_handlers_close_in_both_directions() -> None:
    registered = set(_registered())
    canonical = {spec.name for spec in COMMANDS}
    aliases = set(ALIAS_COMMANDS)
    assert canonical | aliases == registered
    assert canonical.isdisjoint(aliases)
    assert len(canonical) + len(aliases) == len(registered)
    assert "cancel" in canonical


def test_names_and_descriptions_are_valid_for_telegram() -> None:
    names = [spec.name for spec in COMMANDS]
    assert len(names) == len(set(names))
    assert len(names) <= 100
    assert all(NAME_RE.fullmatch(name) for name in [*names, *ALIAS_COMMANDS])
    for spec in COMMANDS:
        assert 1 <= len(spec.description) <= 256, spec.name
        assert not set("<>&") & set(spec.description), spec.name
        assert spec.group in GROUPS


def test_every_description_is_translated_into_italian_with_the_same_placeholders() -> None:
    get_translation.cache_clear()
    italian = get_translation("it_IT")
    for spec in COMMANDS:
        rendered = italian.gettext(spec.description)
        assert rendered != spec.description, spec.name
        assert 1 <= len(rendered) <= 256, spec.name
        assert not set("<>&") & set(rendered), spec.name
        assert _placeholders(rendered) == _placeholders(spec.description), spec.name


def test_the_admin_commands_are_exactly_the_admin_only_handlers() -> None:
    registered = _registered()
    admin_functions = _admin_only_functions()
    admin_names = {name for name, function in registered.items() if function in admin_functions}
    canonical_admin = {spec.name for spec in COMMANDS if spec.admin}
    assert canonical_admin == admin_names - set(ALIAS_COMMANDS)
    assert len(canonical_admin) == 7
    assert all(spec.group == "admin" for spec in COMMANDS if spec.admin)
    assert {spec.name for spec in COMMANDS if spec.group == "admin"} == canonical_admin


@pytest.mark.parametrize("group", ["track", "alerts", "data"])
def test_every_user_group_is_populated(group: str) -> None:
    assert any(spec.group == group for spec in COMMANDS)


def test_the_registry_is_immutable() -> None:
    spec: Any = COMMANDS[0]
    with pytest.raises(AttributeError):
        spec.name = "other"
