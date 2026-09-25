"""No handler arms a product prompt through ``user_data["pending_action"]``.

``user_data`` is shared by every chat of a user, so a product prompt armed there
would capture an answer typed in another chat. The threshold, target and interval
prompts belong to the guided-flow coordinator; only the admin prompts still use
``pending_action``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "price_tracker"
PRODUCT_KINDS = {"threshold", "target", "refresh"}
ADMIN_KINDS = {"admin_adduser", "admin_nick", "admin_interval", "admin_debug"}


def _pending_action_kinds() -> list[tuple[str, int, str]]:
    """Every ``...["pending_action"] = ("<kind>", ...)`` assignment in the package."""
    found = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "pending_action"
                ):
                    continue
                value = node.value
                if (
                    isinstance(value, ast.Tuple)
                    and value.elts
                    and isinstance(value.elts[0], ast.Constant)
                    and isinstance(value.elts[0].value, str)
                ):
                    found.append((path.name, node.lineno, value.elts[0].value))
    return found


def test_no_product_prompt_is_armed_through_pending_action() -> None:
    product = [entry for entry in _pending_action_kinds() if entry[2] in PRODUCT_KINDS]

    assert product == []


def test_scan_sees_the_admin_prompts() -> None:
    """Positive control: the same scan finds the four admin assignments."""
    admin = [kind for _, _, kind in _pending_action_kinds() if kind in ADMIN_KINDS]

    assert sorted(admin) == sorted(ADMIN_KINDS)
