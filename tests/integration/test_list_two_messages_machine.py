"""Stateful navigation keeps observed outcomes separate from remote delivery."""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from collections import defaultdict, deque
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import patch

from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from price_tracker.bot import list_media
from price_tracker.bot.list_grid import GridRegistry
from price_tracker.media.cache import ThumbnailCache
from price_tracker.media.decode import make_thumbnail
from tests.integration.test_guided_flow_wired import OWNER, PRIVATE, _wired
from tests.support.fake_telegram import LoseResponse, Reject, TimeoutBeforeApply
from tests.support.images import jpeg_bytes
from tests.support.two_messages import assert_two_message_invariants, press_on, wire_two_messages

URL = "https://shop.example/image.jpg"
BEHAVIORS = ("reject", "rate", "lost", "timeout")
METHODS = ("sendPhoto", "sendMessage", "editMessageText", "editMessageMedia")


class PairedListMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.loop = asyncio.new_event_loop()
        self.temp = tempfile.TemporaryDirectory()
        self.context = _wired()
        self.w = self.run(self.context.__aenter__())
        self.clock = 0.0
        self.mode = wire_two_messages(self.w, Path(self.temp.name), clock=lambda: self.clock)
        self.run(self.w.repo.update_price_and_image(self.w.product, Decimal(100), URL))
        self.callbacks = []
        self.text_id = None
        self.pending = defaultdict(deque)
        self.texts = {}
        self.confirmed_pairs = {}
        self.fingerprints: dict[str, Any] = {}
        original = list_media.grid_image

        async def capture(*args, **kwargs):
            image = await original(*args, **kwargs)
            if image and image.jpeg:
                self.fingerprints[hashlib.sha256(image.jpeg).hexdigest()] = image.fingerprint
            return image

        self.patch = patch.object(list_media, "grid_image", capture)
        self.patch.start()

    def run(self, awaitable: Any) -> Any:
        return self.loop.run_until_complete(awaitable)

    def account(self, calls: Any, *, command: bool = False) -> None:
        observed = {}
        for call in calls:
            if call.method not in METHODS:
                continue
            kind = self.pending[call.method].popleft() if self.pending[call.method] else "success"
            observed[call.method] = (kind, call)
            if call.method in ("sendMessage", "editMessageText") and kind in ("success", "lost"):
                assert call.message_id is not None
                key = (PRIVATE, call.message_id)
                self.texts[key] = call.params["text"]
                if command:
                    self.text_id = call.message_id
        if command:
            assert [call.method for call in calls] in (
                ["sendPhoto", "sendMessage"],
                ["sendMessage"],
            )
            photo = observed.get("sendPhoto")
            text = observed["sendMessage"]
            if photo and photo[0] == text[0] == "success":
                self.confirmed_pairs[(PRIVATE, text[1].message_id)] = photo[1].message_id
        else:
            self.callbacks.append(calls)

    @rule()
    def new_list(self):
        start = len(self.w.request.calls)
        self.run(self.w.text(PRIVATE, OWNER, "/list"))
        self.account(self.w.request.calls[start:], command=True)

    @rule(data=st.sampled_from(("l:a:1", "l:a:2", "l:p:1", "l:e:1", "l:o:1", "card", "back")))
    def navigate(self, data):
        if self.text_id is None:
            return
        if data == "card":
            data = f"l:a:1:{self.w.product}"
        elif data == "back":
            data = "l:a:1"
        calls = self.run(press_on(self.w, PRIVATE, OWNER, data, message_id=self.text_id))
        self.account(calls)

    @rule(method=st.sampled_from(METHODS), behavior=st.sampled_from(BEHAVIORS))
    def script_next(self, method, behavior):
        values = {
            "reject": Reject(400, "Bad Request: refused"),
            "rate": Reject(429, "Too Many Requests", retry_after=30),
            "lost": LoseResponse(),
            "timeout": TimeoutBeforeApply(),
        }
        self.w.request.script(method, values[behavior])
        self.pending[method].append(behavior)

    @rule()
    def delete_photo(self):
        if self.text_id is None:
            return
        entry = self.mode.grids.get((PRIVATE, self.text_id))
        if entry:
            self.w.request.delete_remote(PRIVATE, entry.grid_message_id)

    @rule()
    def restart(self):
        grids = GridRegistry(clock=lambda: self.clock)
        self.w.app.bot_data["list_grids"] = grids
        self.mode = list_media.TwoMessageMode(self.mode.thumbnails, grids)

    @rule(seconds=st.integers(0, 4000))
    def advance(self, seconds):
        before = len(self.w.request.calls)
        self.clock += seconds
        self.run(asyncio.sleep(0))
        assert len(self.w.request.calls) == before

    @rule()
    def thumbnail_arrives(self):
        ThumbnailCache(Path(self.temp.name) / "thumbs").write(URL, make_thumbnail(jpeg_bytes()))

    @invariant()
    def presentation_matches_model(self):
        assert_two_message_invariants(self.w.request, self.callbacks)
        assert len(self.mode.grids) <= 256
        for key, entry in self.mode.grids._entries.items():
            assert self.confirmed_pairs[key] == entry.grid_message_id
            remote = self.w.request.remote_of(key[0], entry.grid_message_id)
            if entry.fingerprint is not None and remote is not None:
                assert self.fingerprints[remote.photo_sha256] == entry.fingerprint
        for key, text in self.texts.items():
            remote = self.w.request.remote_of(*key)
            assert remote.text == text
        assert self.w.errors == []

    def teardown(self):
        self.patch.stop()
        self.run(self.w.app.bot_data["http_client"].aclose())
        self.run(self.context.__aexit__(None, None, None))
        self.loop.close()
        self.temp.cleanup()


TestPairedListMachine = PairedListMachine.TestCase
TestPairedListMachine.settings = settings(max_examples=50, stateful_step_count=30, deadline=None)
