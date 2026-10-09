"""Semantic actions keep one recognizable icon across inline UI call sites."""

from __future__ import annotations

import ast
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = (
    ROOT / "src/price_tracker/bot/ui",
    ROOT / "src/price_tracker/bot/handlers",
    ROOT / "src/price_tracker/bot/keyboards.py",
)

EXPECTED = {
    "add": "➕",
    "admin": "👑",
    "back": "⬅️",
    "cancel": "❌",
    "check": "🔄",
    "data": "💾",
    "delete": "🗑",
    "digest": "📬",
    "digest_now": "📨",
    "errors": "⚠️",
    "export": "💾",
    "help": "ℹ️",
    "history": "📈",
    "home": "🏠",
    "import": "📥",
    "interval": "⏱",
    "language": "🗣",
    "mute": "🔕",
    "notifications": "🔔",
    "pause": "⏸",
    "prices": "💶",
    "products": "📦",
    "quiet_hours": "🌙",
    "resume": "▶️",
    "settings": "⚙️",
    "target": "🎯",
    "threshold": "📉",
    "throttle": "🚦",
    "timezone": "🌍",
}

ACTION_KEYS = {
    "add": "add",
    "admin": "admin",
    "check_all": "check",
    "data": "data",
    "data.export": "export",
    "data.import": "import",
    "errors": "errors",
    "help": "help",
    "history": "history",
    "home": "home",
    "list.remove_all": "delete",
    "list.remove_all_ok": "delete",
    "notifications": "notifications",
    "prices": "prices",
    "product.check": "check",
    "product.chart": "history",
    "product.interval": "interval",
    "product.pause": "pause",
    "product.prefs": "notifications",
    "product.reactivate": "resume",
    "product.remove": "delete",
    "product.remove_ok": "delete",
    "product.target": "target",
    "product.threshold": "threshold",
    "product.threshold_default": "threshold",
    "product.threshold_any": "notifications",
    "settings": "settings",
    "settings.digest_now": "digest_now",
}

SECTION_KEYS = {
    "dg": "digest",
    "lang": "language",
    "mu": "mute",
    "qh": "quiet_hours",
    "th": "throttle",
    "tz": "timezone",
}


def _files() -> list[Path]:
    files: list[Path] = []
    for source in SOURCES:
        files.extend(sorted(source.rglob("*.py")) if source.is_dir() else [source])
    return files


def _literal_label(call: ast.Call) -> str | None:
    if not call.args:
        return None
    value = call.args[0]
    if isinstance(value, ast.Call) and value.args:
        value = value.args[0]
    return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None


def _action(call: ast.Call) -> tuple[str, str | None] | None:
    for node in ast.walk(call):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        if not isinstance(node.func, ast.Name) or node.func.id != "Action":
            continue
        name = node.args[0]
        if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
            continue
        argument = None
        if len(node.args) > 1 and isinstance(node.args[1], ast.Tuple) and node.args[1].elts:
            first = node.args[1].elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                argument = first.value
        return name.value, argument
    return None


def _semantic_key(label: str, action: tuple[str, str | None]) -> str | None:
    icon = label.partition(" ")[0]
    if icon == "⬅️":
        return "back"
    if icon == "❌":
        return "cancel"
    name, argument = action
    if name == "settings.section" and argument is not None:
        return SECTION_KEYS[argument]
    if name == "list.page" and label.startswith("📦"):
        return "products"
    return ACTION_KEYS.get(name)


def test_same_action_key_uses_the_same_icon_everywhere() -> None:
    found: dict[str, set[str]] = defaultdict(set)
    for path in _files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = node.func.id if isinstance(node.func, ast.Name) else ""
            if name not in {"button", "InlineKeyboardButton"}:
                continue
            label = _literal_label(node)
            action = _action(node)
            if label is None or action is None or " " not in label:
                continue
            key = _semantic_key(label, action)
            if key is not None:
                found[key].add(label.partition(" ")[0])

    assert found
    for key, icons in found.items():
        assert icons == {EXPECTED[key]}, (key, icons)
