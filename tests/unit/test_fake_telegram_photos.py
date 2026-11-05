"""Exercise photo state and uncertain delivery through a real Telegram application."""

from __future__ import annotations

import asyncio
import hashlib
from collections import defaultdict, deque
from io import BytesIO
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, LinkPreviewOptions
from telegram.error import BadRequest, RetryAfter, TimedOut

from tests.support.fake_telegram import (
    FakeRequest,
    LoseResponse,
    Reject,
    TimeoutBeforeApply,
    make_application,
    overlapping,
)


def photo(size=(32, 24), fmt="JPEG") -> bytes:
    output = BytesIO()
    Image.new("RGB", size, "white").save(output, format=fmt)
    return output.getvalue()


async def test_send_photo_returns_a_photo_message_and_records_it():
    request = FakeRequest()
    async with make_application(request) as app:
        data = photo()
        message = await app.bot.send_photo(1, data, caption="Sample")
        assert message.photo[0].width == 32
        remote = request.remote_of(1, message.message_id)
        assert remote is not None
        assert remote.kind == "photo"
        assert remote.caption == "Sample"
        assert remote.photo_size == (32, 24)
        assert remote.photo_sha256 == hashlib.sha256(data).hexdigest()


async def test_edit_media_keeps_the_message_id_and_makes_it_a_photo():
    request = FakeRequest()
    async with make_application(request) as app:
        message = await app.bot.send_message(1, "Original")
        edited = await app.bot.edit_message_media(
            InputMediaPhoto(photo(), caption="Changed"), 1, message.message_id
        )
        assert edited.message_id == message.message_id
        remote = request.remote_of(1, message.message_id)
        assert remote is not None
        assert remote.kind == "photo"
        remote = request.remote_of(1, message.message_id)
        assert remote is not None
        assert remote.caption == "Changed"


async def test_edit_text_on_a_photo_message_is_refused():
    request = FakeRequest()
    async with make_application(request) as app:
        message = await app.bot.send_photo(1, photo())
        with pytest.raises(BadRequest, match="no text"):
            await app.bot.edit_message_text("Changed", 1, message.message_id)
        remote = request.remote_of(1, message.message_id)
        assert remote is not None
        assert remote.kind == "photo"


async def test_an_edit_on_a_deleted_message_is_refused():
    request = FakeRequest()
    async with make_application(request) as app:
        message = await app.bot.send_message(1, "Original")
        request.delete_remote(1, message.message_id)
        with pytest.raises(BadRequest, match="not found"):
            await app.bot.edit_message_text("Changed", 1, message.message_id)
        with pytest.raises(BadRequest, match="not found"):
            await app.bot.edit_message_media(InputMediaPhoto(photo()), 1, message.message_id)
        assert request.remote_of(1, message.message_id) is None


async def test_a_scripted_rejection_is_not_applied():
    request = FakeRequest()
    async with make_application(request) as app:
        request.script("sendMessage", Reject(400, "Bad Request: refused"))
        with pytest.raises(BadRequest, match="(?i)refused"):
            await app.bot.send_message(1, "Original")
        assert request.remote == {}


async def test_a_scripted_lost_response_is_applied_and_raises_timed_out():
    request = FakeRequest()
    async with make_application(request) as app:
        request.script("sendPhoto", LoseResponse())
        with pytest.raises(TimedOut):
            await app.bot.send_photo(1, photo())
        remote = request.remote_of(1, 1000)
        assert remote is not None
        assert remote.kind == "photo"
        assert request.attempts("sendPhoto")[0].message_id == 1000
        request.script("editMessageText", LoseResponse())
        with pytest.raises(BadRequest, match="no text"):
            await app.bot.edit_message_text("Changed", 1, 1000)


async def test_a_scripted_timeout_before_apply_is_not_applied():
    request = FakeRequest()
    async with make_application(request) as app:
        request.script("sendMessage", TimeoutBeforeApply())
        with pytest.raises(TimedOut):
            await app.bot.send_message(1, "Original")
        assert request.remote == {}


async def test_a_scripted_429_raises_retry_after_with_its_delay():
    request = FakeRequest()
    async with make_application(request) as app:
        request.script("sendMessage", Reject(429, "Too Many Requests", retry_after=12))
        with pytest.raises(RetryAfter) as error:
            await app.bot.send_message(1, "Original")
        assert error.value.retry_after == 12
        assert request.remote == {}


async def test_scripts_are_consumed_in_order_and_only_by_their_method():
    request = FakeRequest()
    async with make_application(request) as app:
        request.script("sendPhoto", Reject(400, "first"))
        request.script("sendPhoto", TimeoutBeforeApply())
        await app.bot.send_message(1, "Unrelated")
        with pytest.raises(BadRequest, match="first"):
            await app.bot.send_photo(1, photo())
        with pytest.raises(TimedOut):
            await app.bot.send_photo(1, photo())
        message = await app.bot.send_photo(1, photo())
        remote = request.remote_of(1, message.message_id)
        assert remote is not None
        assert remote.kind == "photo"


async def test_not_modified_is_simulated_only_when_enabled():
    request = FakeRequest()
    async with make_application(request) as app:
        message = await app.bot.send_message(1, "Original")
        await app.bot.edit_message_text("Original", 1, message.message_id)
        request.simulate_not_modified = True
        with pytest.raises(BadRequest, match="not modified"):
            await app.bot.edit_message_text("Original", 1, message.message_id)
        await app.bot.edit_message_text(
            "Original",
            1,
            message.message_id,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        await app.bot.edit_message_text(
            "Original",
            1,
            message.message_id,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Open", callback_data="x")]]),
        )


async def test_attempts_include_failed_calls():
    request = FakeRequest()
    async with make_application(request) as app:
        request.fail_next_call_to(1)
        with pytest.raises(Exception, match="blocked"):
            await app.bot.send_message(1, "Blocked")
        await app.bot.send_message(1, "Allowed")
        assert len(request.attempts("sendMessage")) == 2
        assert len(request.calls_of("sendMessage")) == 1
        assert request.attempts("sendMessage")[0].failed


async def test_a_held_call_waits_and_records_start_and_end():
    request = FakeRequest()
    async with make_application(request) as app:
        release = request.hold_next("sendMessage")
        task = asyncio.create_task(app.bot.send_message(1, "Held"))
        for _ in range(100):
            if request.attempts("sendMessage"):
                break
            await asyncio.sleep(0)
        assert not task.done()
        assert request.attempts("sendMessage")[0].finished is None
        await app.bot.send_photo(1, photo())
        assert overlapping([*request.attempts("sendMessage"), *request.attempts("sendPhoto")])
        release.set()
        await task
        calls = request.attempts("sendMessage")
        assert calls[0].finished is not None
        assert calls[0].finished > calls[0].started
        assert not overlapping(calls)


async def test_an_upload_over_10_mb_is_a_violation():
    request = FakeRequest()
    async with make_application(request) as app:
        await app.bot.send_photo(1, photo() + b"x" * 10_000_000)
        assert any("size" in violation for violation in request.violations)


async def test_an_upload_with_ratio_over_20_is_a_violation():
    request = FakeRequest()
    async with make_application(request) as app:
        await app.bot.send_photo(1, photo((21, 1)))
        assert any("ratio" in violation for violation in request.violations)
        request.violations.clear()
        await app.bot.edit_message_media(InputMediaPhoto(photo((5001, 5000))), 1, 1000)
        assert any("dimensions" in violation for violation in request.violations)
        request.violations.clear()
        await app.bot.send_photo(1, b"invalid")
        assert any("decode" in violation for violation in request.violations)
        remote = request.remote_of(1, 1001)
        assert remote is not None
        assert remote.photo_size == (1, 1)
        request.violations.clear()
        await app.bot.send_photo(1, photo(fmt="GIF"))
        assert any("decode" in violation for violation in request.violations)


async def test_a_png_chart_photo_upload_is_accepted():
    request = FakeRequest()
    async with make_application(request) as app:
        stream = BytesIO(photo(fmt="PNG"))
        await app.bot.send_photo(1, stream)
        assert request.violations == []
        remote = request.remote_of(1, 1000)
        assert remote is not None
        assert remote.photo_size == (32, 24)


async def test_a_csv_document_upload_is_not_checked_as_a_photo():
    request = FakeRequest()
    async with make_application(request) as app:
        await app.bot.send_document(1, b"name,price\nitem,10\n", filename="items.csv")
        assert request.violations == []


async def test_link_preview_and_keyboard_helpers():
    request = FakeRequest()
    async with make_application(request) as app:
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Open", callback_data="x")]])
        await app.bot.send_message(
            1,
            "Original",
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        call = request.calls_of("sendMessage")[0]
        assert call.has_keyboard()
        assert call.link_preview() == {"is_disabled": True}
        remote = request.remote_of(1, 1000)
        assert remote is not None
        assert remote.link_preview == {"is_disabled": True}
        assert remote.reply_markup == {
            "inline_keyboard": [[{"text": "Open", "callback_data": "x"}]]
        }
        assert not request.calls_of("getMe")[0].has_keyboard()
        assert request.calls_of("getMe")[0].link_preview() is None


@given(
    st.lists(st.tuples(st.integers(0, 5), st.integers(998, 1005), st.integers(0, 3)), max_size=40)
)
@settings(max_examples=100, deadline=None)
def test_remote_state_matches_a_model_for_random_sequences(steps):
    async def exercise() -> None:
        request = FakeRequest()
        model: dict[tuple[int, int], tuple[Any, ...]] = {}
        deleted = set()
        scripts: dict[str, deque[int]] = defaultdict(deque)
        next_id = 1000
        attempts = 0
        data = photo()
        behaviours = [None, Reject(400, "scripted"), LoseResponse(), TimeoutBeforeApply()]
        async with make_application(request) as app:
            for action, target, outcome in steps:
                if action == 4:
                    request.delete_remote(1, target)
                    model.pop((1, target), None)
                    deleted.add(target)
                elif action == 5:
                    script_outcome = 1 + outcome % 3
                    request.script("sendMessage", behaviours[script_outcome])
                    scripts["sendMessage"].append(script_outcome)
                else:
                    method = ["sendMessage", "sendPhoto", "editMessageText", "editMessageMedia"][
                        action
                    ]
                    if action != 0 and outcome:
                        request.script(method, behaviours[outcome])
                        scripts[method].append(outcome)
                    expected_outcome = scripts[method].popleft() if scripts[method] else 0
                    normal_rejection = action >= 2 and (
                        target in deleted
                        or (action == 2 and model.get((1, target), ("text",))[0] == "photo")
                    )
                    applies = expected_outcome not in (1, 3) and not normal_rejection
                    if applies:
                        message_id = next_id if action < 2 else target
                        if action < 2:
                            next_id += 1
                        model[(1, message_id)] = (
                            ("text", str(target), None, None, None, None, None)
                            if action in (0, 2)
                            else (
                                "photo",
                                None,
                                None,
                                None,
                                None,
                                (32, 24),
                                hashlib.sha256(data).hexdigest(),
                            )
                        )
                    try:
                        if action == 0:
                            await app.bot.send_message(1, str(target))
                        elif action == 1:
                            await app.bot.send_photo(1, data)
                        elif action == 2:
                            await app.bot.edit_message_text(str(target), 1, target)
                        else:
                            await app.bot.edit_message_media(InputMediaPhoto(data), 1, target)
                    except (BadRequest, TimedOut):
                        assert expected_outcome or normal_rejection
                    else:
                        assert not expected_outcome
                        assert not normal_rejection
                    attempts += 1
                actual = {
                    key: (
                        value.kind,
                        value.text,
                        value.caption,
                        value.reply_markup,
                        value.link_preview,
                        value.photo_size,
                        value.photo_sha256,
                    )
                    for key, value in request.remote.items()
                }
                assert actual == model
                assert (
                    sum(
                        len(request.attempts(m))
                        for m in ("sendMessage", "sendPhoto", "editMessageText", "editMessageMedia")
                    )
                    == attempts
                )

    asyncio.run(exercise())
