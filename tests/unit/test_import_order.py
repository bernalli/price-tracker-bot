"""Every list presentation module imports on its own, whatever is imported first."""

from __future__ import annotations

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "price_tracker.bot.list_media",
        "price_tracker.bot.list_grid",
        "price_tracker.bot.telegram_outcome",
        "price_tracker.bot.handlers.callbacks._nav",
        "price_tracker.bot.handlers.product_list",
        "price_tracker.bot.handlers.product",
    ],
)
def test_module_imports_first_in_a_fresh_interpreter(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
