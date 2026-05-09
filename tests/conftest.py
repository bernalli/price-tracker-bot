"""Shared pytest fixtures for unit and integration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite
import httpx
import pytest
import pytest_asyncio
import respx

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture()
def fixtures_dir() -> Path:
    """Path to the tests/fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture()
def load_fixture(fixtures_dir: Path):
    """Helper to load a fixture file as text."""

    def _loader(rel_path: str) -> str:
        return (fixtures_dir / rel_path).read_text(encoding="utf-8")

    return _loader


@pytest_asyncio.fixture
async def memory_db() -> AsyncIterator[aiosqlite.Connection]:
    """In-memory SQLite connection for repository tests."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        yield conn
    finally:
        await conn.close()


@pytest_asyncio.fixture
async def http_client() -> AsyncIterator[httpx.AsyncClient]:
    """Plain async httpx client for tests that don't mock HTTP."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        yield client


@pytest.fixture()
def mock_router() -> respx.Router:
    """respx router for HTTP mocking. Use with `with router:` or `router.start()`."""
    return respx.mock(assert_all_called=False)


@pytest.fixture()
def event_loop_policy():
    """Pytest-asyncio event loop policy fixture (one per session)."""
    return asyncio.DefaultEventLoopPolicy()
