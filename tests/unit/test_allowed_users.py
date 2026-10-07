"""``ALLOWED_USERS`` uses the user-id grammar and fails before any database access."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.app.inputs import InputError, parse_user_id
from price_tracker.config import Config
from price_tracker.db.repository import Repository


def _load(raw: str) -> Config:
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "ALLOWED_USERS": raw}):
        return Config.from_env()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,2,2, 3 ,", (1, 2, 3)),
        ("123, 456,789", (123, 456, 789)),
        ("  ,  ,", ()),
        ("", ()),
        ("9223372036854775807", (9223372036854775807,)),
        ("7,5,7,5", (7, 5)),
    ],
)
def test_valid_lists_are_parsed_deduplicated_in_order(raw: str, expected: tuple[int, ...]) -> None:
    assert _load(raw).admin_users == expected


@pytest.mark.parametrize(
    "raw", ["-1", "0", "٣", "9223372036854775808", "1_0", "a", "1,+2", "1.5", "1e3", "0x10", "1 2"]
)
def test_malformed_entries_fail_the_load_without_echoing_the_value(raw: str) -> None:
    with pytest.raises(ValueError, match="ALLOWED_USERS") as caught:
        _load(raw)
    bad = [t.strip() for t in raw.split(",") if isinstance(parse_user_id(t.strip()), InputError)]
    assert bad
    assert all(token not in str(caught.value) for token in bad if len(token) > 1)


def test_the_message_names_the_position() -> None:
    with pytest.raises(ValueError, match=r"entry 3"):
        _load("1,2,abc")


def test_a_bad_list_never_reaches_the_repository() -> None:
    with (
        patch.object(Repository, "__init__", side_effect=AssertionError("repository used")),
        pytest.raises(ValueError, match="ALLOWED_USERS"),
    ):
        _load("1,-5")


_TOKEN = st.text(
    alphabet=st.characters(blacklist_categories=["Cs"], blacklist_characters="\x00,"), max_size=24
)


@settings(max_examples=300, deadline=None)
@given(tokens=st.lists(_TOKEN, max_size=8))
def test_load_agrees_with_the_user_id_grammar(tokens: list[str]) -> None:
    raw = ",".join(tokens)
    present = [t for t in tokens if t.strip()]
    parsed = [parse_user_id(t) for t in present]
    if any(isinstance(p, InputError) for p in parsed):
        with pytest.raises(ValueError, match="ALLOWED_USERS"):
            _load(raw)
    else:
        assert _load(raw).admin_users == tuple(
            dict.fromkeys(p for p in parsed if isinstance(p, int))
        )
