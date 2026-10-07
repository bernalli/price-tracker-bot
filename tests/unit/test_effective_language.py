"""Which language a user sees: the stored choice, else Telegram's tag, else the default."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from price_tracker.i18n.locales import (
    AVAILABLE_LANGUAGES,
    SUPPORTED_LOCALES,
    effective_language,
    endonym,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

ROOT = Path(__file__).resolve().parents[2] / "src/price_tracker"
TAG_RE = re.compile(r"[A-Za-z0-9_-]{1,35}")

ANY_VALUE = st.one_of(
    st.none(),
    st.just(""),
    st.just(" it"),
    st.just("IT"),
    st.just("it_IT"),
    st.just("de"),
    st.just("xx"),
    st.just("x" * 500),
    st.just("日本語"),
    st.just("\x00"),
    st.integers(),
    st.binary(),
    st.text(max_size=40),
)


@pytest.mark.parametrize(
    ("choice", "tag", "expected"),
    [
        ("it", "en", "it"),
        ("en", "it", "en"),
        (None, "en", "en"),
        (None, "pt-BR", "pt-BR"),
        ("de", "ja", "ja"),
        ("xx", None, None),
        ("IT", "", None),
        (None, None, None),
        (None, "", None),
    ],
)
def test_effective_language_table(choice: Any, tag: Any, expected: str | None) -> None:
    assert effective_language(choice, tag) == expected


@given(choice=ANY_VALUE, tag=ANY_VALUE)
def test_effective_language_is_total_and_closed(choice: Any, tag: Any) -> None:
    result = effective_language(choice, tag)
    assert result is None or result in AVAILABLE_LANGUAGES or result == tag


def test_available_languages_are_the_catalogue_directories() -> None:
    directories = {path.name.split("_")[0] for path in (ROOT / "locale").iterdir() if path.is_dir()}
    assert set(AVAILABLE_LANGUAGES) == directories
    assert set(AVAILABLE_LANGUAGES) <= set(SUPPORTED_LOCALES)


def test_endonym_is_the_language_name_in_itself() -> None:
    assert endonym("en") == "English"
    assert endonym("it") == "Italiano"
    with pytest.raises(ValueError, match="locale"):
        endonym("??")


def _with_repo(body: Callable[[Repository], Awaitable[None]]) -> None:
    async def run() -> None:
        async with aiosqlite.connect(":memory:") as conn:
            conn.row_factory = aiosqlite.Row
            await apply_migrations(conn, ROOT / "db/migrations")
            repo = Repository(conn)
            await repo.ensure_user(1)
            await body(repo)

    asyncio.run(run())


@pytest.mark.parametrize("code", ["de", "", "IT", "it_IT", 5])
def test_set_user_language_rejects_anything_but_a_catalogue_code(code: Any) -> None:
    async def body(repo: Repository) -> None:
        await repo.set_user_language(1, "en")
        with pytest.raises(ValueError, match="language"):
            await repo.set_user_language(1, code)
        user = await repo.get_user(1)
        assert user is not None
        assert user.language == "en"

    _with_repo(body)


def test_set_user_language_stores_clears_and_reports_a_missing_user() -> None:
    async def body(repo: Repository) -> None:
        assert await repo.set_user_language(1, "it") is True
        user = await repo.get_user(1)
        assert user is not None
        assert user.language == "it"
        assert await repo.set_user_language(1, None) is True
        user = await repo.get_user(1)
        assert user is not None
        assert user.language is None
        assert await repo.set_user_language(99, "it") is False

    _with_repo(body)


@pytest.mark.parametrize(
    "tag", ["", "x" * 36, "it IT", "it\n", "\x00", "italianoà", 5, None, b"it"]
)
def test_set_user_telegram_tag_refuses_malformed_input_and_keeps_the_row(tag: Any) -> None:
    async def body(repo: Repository) -> None:
        await repo.set_user_telegram_tag(1, "en")
        assert await repo.set_user_telegram_tag(1, tag) is False
        user = await repo.get_user(1)
        assert user is not None
        assert user.telegram_language_tag == "en"

    _with_repo(body)


@pytest.mark.parametrize("tag", ["x" * 35, "pt-BR", "zh_Hans"])
def test_set_user_telegram_tag_stores_a_valid_tag_verbatim(tag: str) -> None:
    async def body(repo: Repository) -> None:
        assert await repo.set_user_telegram_tag(1, tag) is True
        user = await repo.get_user(1)
        assert user is not None
        assert user.telegram_language_tag == tag
        assert await repo.set_user_telegram_tag(99, tag) is False

    _with_repo(body)


@settings(max_examples=150, deadline=None)
@given(tags=st.lists(ANY_VALUE, max_size=12))
def test_set_user_telegram_tag_writes_only_what_matches_the_grammar(tags: list[Any]) -> None:
    async def body(repo: Repository) -> None:
        for tag in tags:
            before = await repo.get_user(1)
            assert before is not None
            written = await repo.set_user_telegram_tag(1, tag)
            after = await repo.get_user(1)
            assert after is not None
            if isinstance(tag, str) and TAG_RE.fullmatch(tag):
                assert written is True
                assert after.telegram_language_tag == tag
            else:
                assert written is False
                assert after.telegram_language_tag == before.telegram_language_tag

    _with_repo(body)
