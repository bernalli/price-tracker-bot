"""Repository ↔ handler contract — defense against refactor drift.

Lesson learned in CONTINUITY 2026-05-14: after the monolithic ``bot.py`` was
split into ``bot/handlers/**`` modules, four successive hotfixes (v0.1.1
through v0.1.4) patched individual KeyErrors / AttributeErrors / TypeErrors
discovered when real users exercised the handlers. v0.1.4 added the
``hasattr`` contract; v0.1.5 adds **keyword-argument signature** validation
so calls like ``db.add_product(price=...)`` against a Repository that expects
``initial_price=...`` fail at CI time instead of in production.

Three contracts enforced:

1. Every ``db.<method>(...)`` call in ``src/price_tracker/bot/**/*.py`` resolves
   to an attribute on :class:`price_tracker.db.repository.Repository`.
2. Every ``kw=value`` argument used at the call sites must appear in
   ``inspect.signature(Repository.<method>).parameters`` (or the method must
   accept ``**kwargs``).
3. ``ProductRecord`` / ``UserRecord`` support the legacy dict-like API used by
   the (still-not-fully-migrated) handlers: ``record.get(key, default)``,
   ``record[key]``, and ``key in record``.
"""

from __future__ import annotations

import ast
import inspect
import re
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import pytest
import pytest_asyncio

from price_tracker.db.migrator import apply_migrations
from price_tracker.db.models import ProductRecord, UserRecord
from price_tracker.db.repository import Repository

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_DIR = REPO_ROOT / "src" / "price_tracker" / "bot"
MIGRATIONS_DIR = REPO_ROOT / "src" / "price_tracker" / "db" / "migrations"

# ``db.<method>(`` — captures method name only (arg count is brittle and not
# needed for the contract; existence + return-type compatibility is enough).
_DB_CALL_RE = re.compile(r"\bdb\.([a-z_][a-z0-9_]*)\s*\(", re.IGNORECASE)


def _collect_db_method_calls() -> dict[str, list[Path]]:
    """Return {method_name: [files calling it]} from all bot/**/*.py modules."""
    calls: dict[str, list[Path]] = {}
    for path in BOT_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in _DB_CALL_RE.finditer(text):
            calls.setdefault(match.group(1), []).append(path)
    return calls


def _collect_db_kwargs_calls() -> list[tuple[Path, int, str, tuple[str, ...]]]:
    """Return [(file, line, method, kwargs)] for every ``db.<method>(kw=...)``.

    Uses AST so it is robust to multi-line calls (the failing case in v0.1.4
    was a 9-line keyword argument list in ``product.py``).
    """
    found: list[tuple[Path, int, str, tuple[str, ...]]] = []
    for path in BOT_DIR.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "db"
            ):
                continue
            kwargs = tuple(kw.arg for kw in node.keywords if kw.arg is not None)
            if kwargs:
                found.append((path, node.lineno, func.attr, kwargs))
    return found


def test_every_db_call_resolves_on_repository() -> None:
    """Each ``db.<method>(...)`` call in handlers must exist on Repository.

    Fails if handler code references a Repository method that does not exist.
    This catches the regression class behind v0.1.1/0.1.2/0.1.3/0.1.4 hotfixes:
    refactor splits the monolith, new ``Repository`` API forgets a public
    method, ``AttributeError`` crashes the bot only when the matching user
    flow runs.
    """
    calls = _collect_db_method_calls()
    assert calls, "Sanity: expected at least one db.<method>() call in bot/"

    missing: list[tuple[str, list[Path]]] = []
    for method, files in sorted(calls.items()):
        if not hasattr(Repository, method):
            missing.append((method, files))

    if missing:
        detail = "\n".join(
            f"  - Repository.{m!s}  (called by {', '.join(p.name for p in files)})"
            for m, files in missing
        )
        pytest.fail(
            f"{len(missing)} db.<method>() handler calls have no matching "
            f"Repository attribute (naming drift after the module split). Either add "
            f"the method to Repository or update the handler:\n{detail}"
        )


def test_every_db_kwarg_exists_on_repository_signature() -> None:
    """Each ``kw=value`` at a ``db.<method>(...)`` site must be a real parameter.

    Catches the v0.1.5 class of regressions where the Repository renamed a
    keyword (``price`` → ``initial_price``) or dropped one (``target_price``)
    during the module refactor but the handlers still pass the old name.
    """
    drift: list[str] = []
    for path, lineno, method, kwargs in _collect_db_kwargs_calls():
        method_fn = getattr(Repository, method, None)
        if method_fn is None:
            # Covered by test_every_db_call_resolves_on_repository — skip here.
            continue
        try:
            params = inspect.signature(method_fn).parameters
        except (TypeError, ValueError):
            continue
        # Methods declaring **kwargs accept anything.
        if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
            continue
        unexpected = [k for k in kwargs if k not in params]
        if unexpected:
            drift.append(
                f"  - {path.name}:{lineno} db.{method}({', '.join(kwargs)}) "
                f"→ unknown kwarg(s) {unexpected} (signature accepts: "
                f"{sorted(k for k in params if k != 'self')})"
            )

    if drift:
        detail = "\n".join(drift)
        pytest.fail(
            f"{len(drift)} db.<method>(...) call sites pass keyword arguments "
            f"not present on the Repository signature (signature drift from "
            f"module refactor). Align caller or extend the method:\n{detail}"
        )


def test_product_record_supports_dict_api() -> None:
    """``ProductRecord`` must behave dict-like for legacy handler code."""
    record = ProductRecord(
        id=42,
        user_id=1,
        url="https://example.com/p/1",
        name="Widget",
        domain="example.com",
        initial_price=Decimal("9.99"),
        current_price=Decimal("8.00"),
        lowest_price=Decimal("8.00"),
        highest_price=Decimal("9.99"),
        target_price=None,
        threshold_type="percentage",
        threshold_value=Decimal("10"),
        is_active=True,
        is_available=True,
        consecutive_errors=0,
        currency="EUR",
        check_interval_minutes=None,
        last_checked_at=None,
        last_notified_at=None,
    )
    assert record.get("name") == "Widget"
    assert record.get("missing_key", "default") == "default"
    assert record["id"] == 42
    assert "current_price" in record
    assert "definitely_not_a_field" not in record


def test_user_record_supports_dict_api() -> None:
    """``UserRecord`` must behave dict-like for legacy handler code."""
    record = UserRecord(
        user_id=123,
        is_admin=True,
        is_active=True,
        display_name="Alice",
        username="alice42",
    )
    assert record.get("display_name") == "Alice"
    assert record.get("missing", 0) == 0
    assert record["user_id"] == 123
    assert "is_admin" in record


# ── Runtime smoke for the new wrapper methods ───────────────────────────────


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    try:
        yield Repository(conn)
    finally:
        await conn.close()


async def test_get_product_by_url_for_user_roundtrip(repo: Repository) -> None:
    """Path that crashes v0.1.3 in production: /add <url> dedup check."""
    pid = await repo.add_product(
        user_id=7,
        url="https://example.com/p/1",
        name="Widget",
        domain="example.com",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    found = await repo.get_product_by_url_for_user("https://example.com/p/1", 7)
    assert found is not None
    assert found["id"] == pid
    # Different user must not see the product.
    assert await repo.get_product_by_url_for_user("https://example.com/p/1", 99) is None
    # Different URL must not match.
    assert await repo.get_product_by_url_for_user("https://other.example/x", 7) is None


async def test_get_active_products_dict_shape(repo: Repository) -> None:
    """Handlers iterate the list and call ``.get('name')`` etc."""
    await repo.add_product(
        user_id=1,
        url="https://a.example/1",
        name="A",
        domain="a.example",
        initial_price=Decimal("1"),
        currency="EUR",
    )
    pid_b = await repo.add_product(
        user_id=1,
        url="https://a.example/2",
        name="B",
        domain="a.example",
        initial_price=Decimal("2"),
        currency="EUR",
    )
    await repo.deactivate_product(pid_b)

    rows = await repo.get_active_products(1)
    assert len(rows) == 1
    assert rows[0].get("name") == "A"
    assert rows[0]["url"] == "https://a.example/1"


async def test_is_user_admin_and_add_user(repo: Repository) -> None:
    await repo.add_user(1001, is_admin=True)
    await repo.add_user(1002, is_admin=False)
    assert await repo.is_user_admin(1001) is True
    assert await repo.is_user_admin(1002) is False
    assert await repo.is_user_admin(9999) is False  # absent → not admin


async def test_get_stats_user_and_global(repo: Repository) -> None:
    await repo.add_user(1, is_admin=False)
    await repo.add_product(
        user_id=1,
        url="https://x/1",
        name="A",
        domain="x",
        initial_price=Decimal("1"),
        currency="EUR",
    )
    pid_b = await repo.add_product(
        user_id=1,
        url="https://x/2",
        name="B",
        domain="x",
        initial_price=Decimal("2"),
        currency="EUR",
    )
    await repo.deactivate_product(pid_b)

    user_stats = await repo.get_stats(1)
    assert user_stats["active_products"] == 1
    assert user_stats["total_products"] == 2
    assert user_stats["total_checks"] >= 0

    global_stats = await repo.get_stats()
    assert global_stats["active_products"] == 1
    assert global_stats["total_products"] == 2


async def test_reset_initial_price_returns_bool(repo: Repository) -> None:
    pid = await repo.add_product(
        user_id=1,
        url="https://x/1",
        name="A",
        domain="x",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    await repo.update_price(pid, Decimal("75"))
    assert await repo.reset_initial_price(pid) is True
    record = await repo.get_product(pid)
    assert record is not None
    assert record.initial_price == Decimal("75")
    # Unknown id → False, no exception
    assert await repo.reset_initial_price(999_999) is False


async def test_set_product_interval_alias(repo: Repository) -> None:
    pid = await repo.add_product(
        user_id=1,
        url="https://x/1",
        name="A",
        domain="x",
        initial_price=Decimal("1"),
        currency="EUR",
    )
    await repo.set_product_interval(pid, 30)
    record = await repo.get_product(pid)
    assert record is not None
    assert record.check_interval_minutes == 30


async def test_get_all_users_dict_shape(repo: Repository) -> None:
    await repo.add_user(1, is_admin=True)
    await repo.add_user(2, is_admin=False)
    users = await repo.get_all_users()
    assert len(users) == 2
    by_id = {u["user_id"]: u for u in users}
    assert by_id[1].get("is_admin") is True
    assert by_id[2].get("is_admin") is False


async def test_cleanup_old_history_keyword_arg(repo: Repository) -> None:
    """Handler invokes ``cleanup_old_history(retention_days=30)`` as kwarg."""
    deleted = await repo.cleanup_old_history(retention_days=30)
    assert isinstance(deleted, int)
    assert deleted >= 0


async def test_set_product_preferences_keyword_args(repo: Repository) -> None:
    pid = await repo.add_product(
        user_id=1,
        url="https://x/1",
        name="A",
        domain="x",
        initial_price=Decimal("1"),
        currency="EUR",
    )
    await repo.set_product_preferences(pid, condition="new", seller="Acme")
    record = await repo.get_product(pid)
    assert record is not None
    assert record.preferred_condition == "new"
    assert record.preferred_seller == "Acme"


async def test_get_product_for_user_isolation(repo: Repository) -> None:
    pid = await repo.add_product(
        user_id=10,
        url="https://x/1",
        name="A",
        domain="x",
        initial_price=Decimal("1"),
        currency="EUR",
    )
    mine = await repo.get_product_for_user(pid, 10)
    assert mine is not None
    assert mine["id"] == pid
    # Other user cannot fetch it.
    assert await repo.get_product_for_user(pid, 99) is None
