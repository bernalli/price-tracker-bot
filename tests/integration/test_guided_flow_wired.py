"""The guided-flow coordinator wired into the real application.

``register_handlers`` builds the production layout (coordinator in group 0,
``/cancel`` in group 1, every legacy handler in group 2) on a real
``Application`` with a job queue; the repository is a migrated in-memory
database. Updates go through ``Application.process_update``; the bot talks to a
fake HTTP layer.
"""

from __future__ import annotations

import dataclasses
import itertools
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import pytest

from price_tracker.bot.flow_services import RepositoryFlowServices
from price_tracker.bot.flows import GuidedFlow
from price_tracker.bot.handlers import register_handlers
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from tests.support.fake_telegram import (
    Call,
    FakeRequest,
    callback_update,
    make_application,
    message_update,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from telegram import Update
    from telegram.ext import Application, CallbackContext

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "src" / "price_tracker" / "db" / "migrations"

ADMIN = 1
OWNER = 10
OTHER = 11
PRIVATE = 100
GROUP = -500
CHANNEL = -1001
URL = "https://shop.example/item/9"
LEGACY_ENTRIES = {
    "setsoglia": "th",
    "track_threshold": "th",
    "settarget": "tg",
    "track_target": "tg",
    "setrefresh": "iv",
}
CANCEL_BUTTON_RE = re.compile(r"^p:([0-9a-f]{32}):x$")
_message_ids = itertools.count(1)

SAVED = "Saved."
CANCELLED = "Cancelled - nothing changed."
EXPIRED = "Expired - nothing changed."
NOT_FOUND = "Product not found."
NOT_AUTHORISED = "Not authorised."
NOTHING_TO_CANCEL = "Nothing to cancel."
TOO_MANY = "Too many invalid answers - cancelled, nothing changed."


class Wired:
    """The production handler layout around a fake Telegram and a real repository."""

    def __init__(self, conn: aiosqlite.Connection, repo: Repository) -> None:
        self.conn = conn
        self.repo = repo
        self.request = FakeRequest()
        self.app: Application[Any, Any, Any, Any, Any, Any] = make_application(
            self.request, with_job_queue=True
        )
        register_handlers(self.app)
        self.app.bot_data["db"] = repo
        self.errors: list[BaseException] = []
        self.app.add_error_handler(self._record_error)
        flows = [
            h
            for handlers in self.app.handlers.values()
            for h in handlers
            if isinstance(h, GuidedFlow)
        ]
        self.flow: GuidedFlow = flows[0] if flows else GuidedFlow(None, None)  # type: ignore[arg-type]
        self.product = 0
        self.other_product = 0

    async def _record_error(
        self, update: object, context: CallbackContext[Any, Any, Any, Any]
    ) -> None:
        del update
        assert context.error is not None
        self.errors.append(context.error)

    async def process(self, update: Update) -> None:
        await self.app.update_processor.process_update(update, self.app.process_update(update))

    async def press(
        self, chat_id: int, user_id: int, data: str, *, language_code: str | None = "en"
    ) -> None:
        await self.process(
            callback_update(
                self.app.bot,
                chat_id,
                user_id,
                data,
                message_id=next(_message_ids),
                language_code=language_code,
            )
        )

    async def text(self, chat_id: int, user_id: int, text: str, *, kind: str = "message") -> None:
        await self.process(message_update(self.app.bot, chat_id, user_id, text, kind=kind))

    def calls_since(self, index: int) -> list[Call]:
        return [c for c in self.request.calls[index:] if not c.failed]

    def sent(self, chat_id: int) -> list[Call]:
        return [c for c in self.request.calls_of("sendMessage") if c.chat_id == chat_id]

    def texts_to(self, chat_id: int) -> list[str]:
        return [str(c.params["text"]) for c in self.sent(chat_id)]

    def prompts(self, chat_id: int) -> list[Call]:
        return [
            c
            for c in self.sent(chat_id)
            if any(CANCEL_BUTTON_RE.match(d) for d in c.callback_data())
        ]

    def edits_of(self, call: Call) -> list[str]:
        return [
            str(c.params["text"])
            for c in self.request.calls_of("editMessageText")
            if int(c.params["message_id"]) == call.message_id
        ]

    def toasts(self) -> list[str | None]:
        return [c.params.get("text") for c in self.request.calls_of("answerCallbackQuery")]

    async def row(self, product_id: int) -> dict[str, Any]:
        product = await self.repo.get_product(product_id)
        assert product is not None
        return dataclasses.asdict(product)

    def pending(self, user_id: int) -> object:
        return self.app.user_data[user_id].get("pending_action")


@pytest.fixture
async def w(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Wired]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repo = Repository(conn)
    await repo.ensure_user(ADMIN, is_admin=True)
    await repo.ensure_user(OWNER)
    await repo.ensure_user(OTHER)
    wired = Wired(conn, repo)
    wired.product = await repo.add_product(
        user_id=OWNER,
        url="https://shop.example/item/1",
        name="Kettle",
        domain="shop.example",
        initial_price=Decimal("100"),
        currency="EUR",
    )
    wired.other_product = await repo.add_product(
        user_id=OTHER,
        url="https://shop.example/item/2",
        name="Fan",
        domain="shop.example",
        initial_price=Decimal("50"),
        currency="EUR",
    )
    await wired.app.initialize()
    try:
        yield wired
        assert wired.request.violations == []
    finally:
        await wired.app.shutdown()
        await conn.close()


@pytest.fixture
def prepare_add_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    """Spy on the add-flow service; it keeps raising as the real one does."""
    calls: list[tuple[int, str]] = []
    original = RepositoryFlowServices.prepare_add

    async def spy(self: RepositoryFlowServices, user_id: int, url: str) -> Any:
        calls.append((user_id, url))
        return await original(self, user_id, url)

    monkeypatch.setattr(RepositoryFlowServices, "prepare_add", spy)
    return calls


@pytest.fixture
def legacy_adds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stand-in for the legacy scrape-and-insert step behind ``handle_url``/``/add``."""
    urls: list[str] = []

    async def fake_add_product(update: Update, context: Any, url: str) -> None:
        del context
        urls.append(url)
        assert update.message is not None
        await update.message.reply_text("legacy add")

    monkeypatch.setattr("price_tracker.bot.handlers.product._add_product", fake_add_product)
    return urls


async def _open(w: Wired, data: str, *, chat_id: int = PRIVATE, user_id: int = OWNER) -> Call:
    before = len(w.prompts(chat_id))
    await w.press(chat_id, user_id, data)
    prompts = w.prompts(chat_id)
    assert len(prompts) == before + 1, w.texts_to(chat_id)
    return prompts[-1]


# --- entry buttons, answers --------------------------------------------------


@pytest.mark.parametrize("prefix", ["setsoglia", "track_threshold"])
async def test_legacy_threshold_button_opens_the_guided_prompt_and_saves(
    w: Wired, prefix: str
) -> None:
    prompt = await _open(w, f"{prefix}_{w.product}")

    assert w.pending(OWNER) is None
    assert w.flow.registry.keys() == {(PRIVATE, OWNER)}
    await w.text(PRIVATE, OWNER, "20%")

    row = await w.row(w.product)
    assert (row["threshold_type"], str(row["threshold_value"])) == ("percentage", "20")
    assert w.texts_to(PRIVATE)[-1] == SAVED
    assert str(prompt.params["text"]).startswith("Kettle\n")


async def test_legacy_target_buttons_clear_and_set_the_target(w: Wired) -> None:
    await w.repo.set_target_price(w.product, Decimal("10"))

    await _open(w, f"track_target_{w.product}")
    await w.text(PRIVATE, OWNER, "0")
    assert (await w.row(w.product))["target_price"] is None

    await _open(w, f"settarget_{w.product}")
    await w.text(PRIVATE, OWNER, "49,90")
    assert str((await w.row(w.product))["target_price"]) == "49.90"
    assert w.texts_to(PRIVATE).count(SAVED) == 2


async def test_interval_prompt_ends_after_three_invalid_answers(w: Wired) -> None:
    await w.repo.set_product_interval(w.product, 60)

    await _open(w, f"setrefresh_{w.product}")
    for answer in ("4", "abc", "-1"):
        await w.text(PRIVATE, OWNER, answer)

    texts = w.texts_to(PRIVATE)
    assert texts[-3:] == [
        "That value is out of range.",
        "Use whole minutes, e.g. 30.",
        TOO_MANY,
    ]
    assert (await w.row(w.product))["check_interval_minutes"] == 60
    assert len(w.flow.registry) == 0

    await _open(w, f"setrefresh_{w.product}")
    await w.text(PRIVATE, OWNER, "0")
    assert (await w.row(w.product))["check_interval_minutes"] is None


# --- coexistence with the legacy handlers ------------------------------------


async def test_command_closes_the_prompt_and_still_reaches_its_handler(w: Wired) -> None:
    prompt = await _open(w, f"setsoglia_{w.product}")
    before = len(w.request.calls)

    await w.text(PRIVATE, OWNER, "/list")

    assert w.edits_of(prompt) == [CANCELLED]
    replies = [str(c.params["text"]) for c in w.calls_since(before) if c.method == "sendMessage"]
    assert any("Kettle" in text for text in replies), replies
    assert len(w.flow.registry) == 0


async def test_link_closes_the_prompt_and_the_legacy_add_takes_it(
    w: Wired, prepare_add_calls: list[tuple[int, str]], legacy_adds: list[str]
) -> None:
    prompt = await _open(w, f"setsoglia_{w.product}")

    await w.text(PRIVATE, OWNER, URL)

    assert w.edits_of(prompt) == [CANCELLED]
    assert legacy_adds == [URL]
    assert prepare_add_calls == []
    assert w.errors == []


@pytest.mark.parametrize("text", [URL, f"/add {URL}", f"/aggiungi {URL}"])
async def test_link_without_prompt_goes_to_the_legacy_add(
    w: Wired, prepare_add_calls: list[tuple[int, str]], legacy_adds: list[str], text: str
) -> None:
    await w.text(PRIVATE, OWNER, text)

    assert legacy_adds == [URL]
    assert len(w.flow.registry) == 0
    assert prepare_add_calls == []
    assert w.errors == []


@pytest.mark.parametrize("with_prompt", [False, True])
async def test_add_for_another_bot_gets_no_answer_from_any_group(
    w: Wired, legacy_adds: list[str], with_prompt: bool
) -> None:
    if with_prompt:
        await _open(w, f"setsoglia_{w.product}")
    before = len(w.request.calls)

    await w.text(PRIVATE, OWNER, f"/add@OtherBot {URL}")

    assert w.calls_since(before) == []
    assert legacy_adds == []
    assert len(w.flow.registry) == int(with_prompt)


# --- access -----------------------------------------------------------------


async def test_other_users_product_is_not_found_but_the_admin_opens_it(w: Wired) -> None:
    await w.press(PRIVATE, OTHER, f"setsoglia_{w.product}")

    assert w.toasts() == [NOT_FOUND]
    assert w.prompts(PRIVATE) == []
    assert len(w.flow.registry) == 0

    await _open(w, f"setsoglia_{w.product}", user_id=ADMIN)
    await w.text(PRIVATE, ADMIN, "5.50")
    row = await w.row(w.product)
    assert (row["threshold_type"], str(row["threshold_value"])) == ("absolute", "5.50")


async def test_other_users_answer_never_writes_the_owners_product(w: Wired) -> None:
    before = await w.row(w.product)

    await w.press(PRIVATE, OTHER, f"setsoglia_{w.product}")
    await w.text(PRIVATE, OTHER, "20%")

    after = await w.row(w.product)
    assert (after["threshold_type"], after["threshold_value"]) == (
        before["threshold_type"],
        before["threshold_value"],
    )
    assert w.prompts(PRIVATE) == []
    assert len(w.flow.registry) == 0


async def test_inactive_user_is_not_authorised(w: Wired) -> None:
    await w.repo.remove_user(OWNER)

    await w.press(PRIVATE, OWNER, f"setsoglia_{w.product}")

    assert w.toasts() == [NOT_AUTHORISED]
    assert w.prompts(PRIVATE) == []


async def test_product_deleted_before_the_answer_is_not_written(w: Wired) -> None:
    await _open(w, f"setsoglia_{w.product}")
    assert await w.repo.delete_product(w.product, user_id=OWNER)
    before = w.conn.total_changes

    await w.text(PRIVATE, OWNER, "20%")

    assert w.texts_to(PRIVATE)[-1] == NOT_FOUND
    assert await w.repo.get_product(w.product) is None
    assert w.conn.total_changes == before


# --- /cancel ------------------------------------------------------------------


async def test_cancel_without_anything_open(w: Wired) -> None:
    await w.text(PRIVATE, OWNER, "/cancel")

    assert w.texts_to(PRIVATE) == [NOTHING_TO_CANCEL]


async def _arm_admin_interval(w: Wired) -> None:
    await w.press(PRIVATE, ADMIN, "menu_admin_interval")
    assert w.pending(ADMIN) == ("admin_interval", 0)


async def test_cancel_disarms_a_legacy_admin_prompt(w: Wired) -> None:
    await _arm_admin_interval(w)

    await w.text(PRIVATE, ADMIN, "/cancel")
    assert w.pending(ADMIN) is None
    assert w.texts_to(PRIVATE)[-1] == CANCELLED

    before = len(w.request.calls)
    await w.text(PRIVATE, ADMIN, "60")
    assert w.calls_since(before) == []
    assert await w.repo.get_config("check_interval_minutes") is None


async def test_legacy_admin_prompt_answers_without_cancel(w: Wired) -> None:
    """Positive control of the case above: the armed prompt does answer."""
    await _arm_admin_interval(w)
    before = len(w.request.calls)

    await w.text(PRIVATE, ADMIN, "60")

    assert [c.method for c in w.calls_since(before)] == ["sendMessage"]


async def test_cancel_closes_the_open_guided_prompt(w: Wired) -> None:
    prompt = await _open(w, f"setsoglia_{w.product}")
    before = len(w.request.calls)

    await w.text(PRIVATE, OWNER, "/cancel")

    assert [c.method for c in w.calls_since(before)] == ["editMessageText"]
    assert w.edits_of(prompt) == [CANCELLED]
    assert len(w.flow.registry) == 0


async def test_legacy_admin_button_closes_the_prompt_and_arms_its_own(w: Wired) -> None:
    prompt = await _open(w, f"setsoglia_{w.product}", user_id=ADMIN)

    await _arm_admin_interval(w)
    assert w.edits_of(prompt) == [CANCELLED]
    assert len(w.flow.registry) == 0

    before = len(w.request.calls)
    await w.text(PRIVATE, ADMIN, "60")
    assert [c.method for c in w.calls_since(before)] == ["sendMessage"]
    assert len(w.flow.registry) == 0
    row = await w.row(w.product)
    assert (row["threshold_type"], str(row["threshold_value"])) == ("percentage", "10")


# --- isolation --------------------------------------------------------------


async def test_answer_in_another_chat_does_not_reach_the_prompt(w: Wired) -> None:
    await _open(w, f"setsoglia_{w.product}")

    await w.text(GROUP, OWNER, "20%")

    row = await w.row(w.product)
    assert (row["threshold_type"], str(row["threshold_value"])) == ("percentage", "10")
    assert w.flow.registry.keys() == {(PRIVATE, OWNER)}


@pytest.mark.parametrize("prefix", sorted(LEGACY_ENTRIES))
@pytest.mark.parametrize("user_id", [OWNER, OTHER, ADMIN])
async def test_each_legacy_entry_press_is_answered_once(
    w: Wired, prefix: str, user_id: int
) -> None:
    before = len(w.request.calls_of("answerCallbackQuery"))

    await w.press(PRIVATE, user_id, f"{prefix}_{w.product}")

    assert len(w.request.calls_of("answerCallbackQuery")) == before + 1


@pytest.mark.parametrize("kind", ["channel_post", "edited_channel_post", "guest_message"])
async def test_channel_and_guest_posts_reach_nothing(w: Wired, kind: str) -> None:
    await _open(w, f"setsoglia_{w.product}", chat_id=CHANNEL)
    before = len(w.request.calls)

    await w.text(CHANNEL, OWNER, "20%", kind=kind)

    assert w.calls_since(before) == []
    row = await w.row(w.product)
    assert (row["threshold_type"], str(row["threshold_value"])) == ("percentage", "10")
    assert w.flow.registry.keys() == {(CHANNEL, OWNER)}


# --- timeout and failures --------------------------------------------------------


async def test_prompt_arms_a_five_minute_job_and_expires(w: Wired) -> None:
    prompt = await _open(w, f"setsoglia_{w.product}")
    flow = w.flow.registry.get((PRIVATE, OWNER))
    assert flow is not None
    snapshot = flow.snapshot((PRIVATE, OWNER))
    assert w.app.job_queue is not None

    jobs = w.app.job_queue.get_jobs_by_name(f"guided-flow:{PRIVATE}:{OWNER}:{snapshot.token}")

    assert len(jobs) == 1
    delay = (jobs[0].job.trigger.run_date - datetime.now(tz=UTC)).total_seconds()
    assert 295 < delay <= 300
    await w.flow.on_timeout(snapshot)
    assert w.edits_of(prompt) == [EXPIRED]
    assert len(w.flow.registry) == 0


async def test_repository_failure_reaches_the_error_handler_once(
    w: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _open(w, f"setsoglia_{w.product}")
    attempts: list[int] = []

    async def failing_set_threshold(product_id: int, *args: Any) -> None:
        attempts.append(product_id)
        raise RuntimeError("database is locked")

    monkeypatch.setattr(w.repo, "set_threshold", failing_set_threshold)
    before = len(w.request.calls)

    await w.text(PRIVATE, OWNER, "20%")

    assert [type(e) for e in w.errors] == [RuntimeError]
    assert attempts == [w.product]
    assert len(w.flow.registry) == 0
    replies = [c for c in w.calls_since(before) if c.method == "sendMessage"]
    assert len(replies) == 1
    assert "errore" in str(replies[0].params["text"])
    assert len(w.calls_since(before)) == 1
