"""The guided-flow coordinator wired into the real application.

``register_handlers`` builds the production layout (coordinator in group 0,
``/cancel`` in group 1, every legacy handler in group 2) on a real
``Application`` with a job queue; the repository is a migrated in-memory
database. Updates go through ``Application.process_update``; the bot talks to a
fake HTTP layer.
"""

from __future__ import annotations

import contextlib
import dataclasses
import itertools
import json
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
from price_tracker.config import Config
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
        # main.py always provides the runtime Config; /lista reads the global interval from it.
        self.app.bot_data["config"] = Config(
            telegram_bot_token="123456:TEST-TOKEN",
            admin_users=(),
            check_interval_minutes=360,
            database_path=":memory:",
            default_threshold_type="percentage",
            default_threshold_value="10",
            max_consecutive_errors=10,
            check_delay_seconds=0.0,
            notification_cooldown_hours=24,
            request_timeout=5,
            log_level="WARNING",
            lang="en",
        )
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


@contextlib.asynccontextmanager
async def _wired() -> AsyncIterator[Wired]:
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
async def w() -> AsyncIterator[Wired]:
    async with _wired() as wired:
        yield wired


@pytest.fixture
def debug_runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, str]]:
    """Stand-in for the scraper debug run; patched before the application is built."""
    runs: list[tuple[int, str]] = []

    async def fake_debug_url(update: Update, context: Any, url: str) -> None:
        del context
        assert update.effective_user is not None
        assert update.message is not None
        runs.append((update.effective_user.id, url))
        await update.message.reply_text("debug ran")

    monkeypatch.setattr("price_tracker.bot.handlers.debug.debug_url", fake_debug_url)
    return runs


@pytest.fixture
async def wd(debug_runs: list[tuple[int, str]]) -> AsyncIterator[Wired]:
    """``w`` with the debug run replaced by ``debug_runs``."""
    del debug_runs
    async with _wired() as wired:
        yield wired


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
    assert "error" in str(replies[0].params["text"])
    assert len(w.calls_since(before)) == 1


# --- /start and the Help button ---------------------------------------------------

STRANGER = 12
HELP_DATA = "hp"


def _start_update(w: Wired, user_id: int, first_name: str) -> Update:
    from telegram import Update as TelegramUpdate

    payload = {
        "update_id": next(_message_ids) + 90_000,
        "message": {
            "message_id": next(_message_ids) + 90_000,
            "date": int(datetime.now(tz=UTC).timestamp()),
            "chat": {"id": PRIVATE, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": first_name},
            "text": "/start",
            "entities": [{"type": "bot_command", "offset": 0, "length": 6}],
        },
    }
    update = TelegramUpdate.de_json(payload, w.app.bot)
    assert update is not None
    return update


def _sent_since(w: Wired, before: int) -> list[Call]:
    return [c for c in w.calls_since(before) if c.method == "sendMessage"]


@pytest.mark.parametrize("user_id", [OWNER, ADMIN])
async def test_start_greets_and_shows_the_home_screen_of_menu(w: Wired, user_id: int) -> None:
    await w.text(PRIVATE, user_id, "/menu")
    (menu,) = w.sent(PRIVATE)
    before = len(w.request.calls)

    await w.text(PRIVATE, user_id, "/start")

    (start,) = _sent_since(w, before)
    assert start.params["text"] == f"👋 <b>Hello U{user_id}!</b>\n\n{menu.params['text']}"
    assert start.callback_data() == menu.callback_data()
    assert ("menu_admin" in start.callback_data()) is (user_id == ADMIN)
    assert w.errors == []


async def test_start_escapes_a_hostile_first_name(w: Wired) -> None:
    before = len(w.request.calls)

    await w.process(_start_update(w, OWNER, '<b>x</b>&"'))

    (start,) = _sent_since(w, before)
    assert str(start.params["text"]).startswith("👋 <b>Hello &lt;b&gt;x&lt;/b&gt;&amp;")
    assert "<b>x</b>" not in str(start.params["text"])


async def test_start_twice_answers_twice_without_errors(w: Wired) -> None:
    await w.text(PRIVATE, OWNER, "/start")
    await w.text(PRIVATE, OWNER, "/start")

    assert len(w.sent(PRIVATE)) == 2
    assert w.errors == []


async def test_start_for_a_stranger_is_still_the_refusal(w: Wired) -> None:
    await w.text(PRIVATE, STRANGER, "/start")

    assert w.texts_to(PRIVATE) == [
        "⛔ You are not authorized.\n"
        "Your Telegram ID: <code>12</code>\n\n"
        "Ask the administrator to add you with:\n"
        "<code>/adduser 12</code>"
    ]


@pytest.mark.parametrize("user_id", [OWNER, ADMIN])
async def test_help_button_shows_the_text_of_the_help_command(w: Wired, user_id: int) -> None:
    await w.text(PRIVATE, user_id, "/help")
    (help_reply,) = w.sent(PRIVATE)

    await w.press(PRIVATE, user_id, HELP_DATA)

    (edit,) = w.request.calls_of("editMessageText")
    assert edit.params["text"] == help_reply.params["text"]
    assert edit.callback_data() == help_reply.callback_data() == ["h"]
    assert ("Admin" in str(edit.params["text"])) is (user_id == ADMIN)
    assert w.errors == []


async def test_help_button_follows_the_language(w: Wired) -> None:
    await w.press(PRIVATE, OWNER, HELP_DATA, language_code="it")

    (edit,) = w.request.calls_of("editMessageText")
    assert str(edit.params["text"]).startswith("❓ <b>Comandi</b>")


@pytest.mark.parametrize("user_id", [STRANGER, OWNER])
async def test_help_button_edits_nothing_for_a_user_without_access(w: Wired, user_id: int) -> None:
    await w.repo.remove_user(OWNER)

    await w.press(PRIVATE, user_id, HELP_DATA)

    assert w.request.calls_of("editMessageText") == []


@pytest.mark.parametrize(
    ("user_id", "language_code", "label", "errors"),
    [
        (OWNER, "en", "❓ Help", "⚠️ Errors"),
        (ADMIN, "en", "❓ Help", "⚠️ Errors"),
        (OWNER, "it", "❓ Aiuto", "⚠️ Errori"),
    ],
)
async def test_status_and_info_offers_help_errors_and_the_way_back(
    w: Wired, user_id: int, language_code: str, label: str, errors: str
) -> None:
    await w.press(PRIVATE, user_id, "menu_info", language_code=language_code)

    (edit,) = w.request.calls_of("editMessageText")
    markup = edit.params["reply_markup"]
    markup = json.loads(markup) if isinstance(markup, str) else markup
    rows = [[(b["text"], b["callback_data"]) for b in row] for row in markup["inline_keyboard"]]
    assert rows == [[(label, HELP_DATA), (errors, "er")], [("◀️ Menu", "menu_main")]]


# --- the admin debug prompt ----------------------------------------------------

DEBUG_ENTRY = "menu_admin_debug"
DEBUG_PROMPT = "Send the link of the product page to analyse."
NOT_A_LINK = "That is not a link."
SUPERSEDED = "Replaced by a newer prompt."
BUTTON_EXPIRED = "This button has expired."


async def _product_count(w: Wired) -> int:
    cursor = await w.conn.execute("SELECT COUNT(*) FROM products")
    row = await cursor.fetchone()
    assert row is not None
    return int(row[0])


async def _open_debug(w: Wired, *, chat_id: int = PRIVATE) -> Call:
    return await _open(w, DEBUG_ENTRY, chat_id=chat_id, user_id=ADMIN)


async def test_debug_button_opens_a_guided_prompt(wd: Wired) -> None:
    before = len(wd.request.calls)

    prompt = await _open_debug(wd)

    assert prompt.params["text"] == DEBUG_PROMPT
    assert wd.flow.registry.keys() == {(PRIVATE, ADMIN)}
    flow = wd.flow.registry.get((PRIVATE, ADMIN))
    assert flow is not None
    assert flow.kind == "debug"
    assert wd.pending(ADMIN) is None
    methods = [c.method for c in wd.calls_since(before)]
    assert "editMessageText" not in methods
    assert methods.count("answerCallbackQuery") == 1


@pytest.mark.parametrize("user_id", [OWNER, OTHER, ADMIN, STRANGER])
async def test_debug_entry_press_is_answered_once(wd: Wired, user_id: int) -> None:
    await wd.press(PRIVATE, user_id, DEBUG_ENTRY)

    assert len(wd.request.calls_of("answerCallbackQuery")) == 1


async def test_link_at_the_debug_prompt_runs_the_debug_once_and_adds_nothing(
    wd: Wired, debug_runs: list[tuple[int, str]], legacy_adds: list[str]
) -> None:
    await _open_debug(wd)
    products = await _product_count(wd)

    await wd.text(PRIVATE, ADMIN, URL)

    assert debug_runs == [(ADMIN, URL)]
    assert legacy_adds == []
    assert await _product_count(wd) == products
    assert len(wd.flow.registry) == 0
    assert wd.errors == []

    await wd.text(PRIVATE, ADMIN, URL)
    assert debug_runs == [(ADMIN, URL)]
    assert legacy_adds == [URL]


async def test_link_in_another_chat_is_an_add_and_the_prompt_stays(
    wd: Wired, debug_runs: list[tuple[int, str]], legacy_adds: list[str]
) -> None:
    await _open_debug(wd)

    await wd.text(GROUP, ADMIN, URL)

    assert legacy_adds == [URL]
    assert debug_runs == []
    assert wd.flow.registry.keys() == {(PRIVATE, ADMIN)}


@pytest.mark.parametrize(
    "answer",
    ["hello", "http:/x", "ftp://x/y", " .,;! ", "\x07\x1b abc", "https://.", "(http://)"],
)
async def test_an_answer_without_a_link_is_rejected_and_counted(
    wd: Wired, debug_runs: list[tuple[int, str]], answer: str
) -> None:
    await _open_debug(wd)

    await wd.text(PRIVATE, ADMIN, answer)

    assert wd.texts_to(PRIVATE)[-1] == NOT_A_LINK
    flow = wd.flow.registry.get((PRIVATE, ADMIN))
    assert flow is not None
    assert flow.attempts == 1
    assert debug_runs == []
    assert wd.errors == []


async def test_three_answers_without_a_link_close_the_debug_prompt(
    wd: Wired, debug_runs: list[tuple[int, str]]
) -> None:
    await _open_debug(wd)

    for answer in ("one", "two", "three"):
        await wd.text(PRIVATE, ADMIN, answer)

    assert wd.texts_to(PRIVATE)[-3:] == [NOT_A_LINK, NOT_A_LINK, TOO_MANY]
    assert len(wd.flow.registry) == 0
    assert debug_runs == []


async def test_the_debug_prompt_and_its_hint_follow_the_language(
    wd: Wired, debug_runs: list[tuple[int, str]]
) -> None:
    await wd.press(PRIVATE, ADMIN, DEBUG_ENTRY, language_code="it")

    await wd.process(message_update(wd.app.bot, PRIVATE, ADMIN, "ciao", language_code="it"))

    assert wd.texts_to(PRIVATE) == [
        "Invia il link della pagina prodotto da analizzare.",
        "Non è un link.",
    ]
    assert debug_runs == []


@pytest.mark.parametrize(
    ("answer", "url"),
    [
        ("guarda https://a.example/p ora", "https://a.example/p"),
        ("https://a.example/one https://b.example/two", "https://a.example/one"),
        ("(see https://a.example/p).", "https://a.example/p"),
        ("https://a.example/" + "x" * 3000, "https://a.example/" + "x" * 3000),
    ],
    ids=["inside-text", "two-links", "trailing-punctuation", "very-long"],
)
async def test_the_link_is_taken_from_the_answer(
    wd: Wired, debug_runs: list[tuple[int, str]], answer: str, url: str
) -> None:
    await _open_debug(wd)

    await wd.text(PRIVATE, ADMIN, answer)

    assert debug_runs == [(ADMIN, url)]
    assert wd.errors == []


@pytest.mark.parametrize("user_id", [OWNER, STRANGER, "inactive-admin"])
async def test_only_an_active_admin_opens_the_debug_prompt(wd: Wired, user_id: Any) -> None:
    if user_id == "inactive-admin":
        await wd.repo.remove_user(ADMIN)
        user_id = ADMIN

    await wd.press(PRIVATE, user_id, DEBUG_ENTRY)

    assert wd.toasts() == [NOT_AUTHORISED]
    assert len(wd.flow.registry) == 0
    assert wd.prompts(PRIVATE) == []


@pytest.mark.parametrize("change", ["demoted", "deactivated"])
async def test_an_admin_who_lost_the_role_gets_no_debug_run(
    wd: Wired, debug_runs: list[tuple[int, str]], legacy_adds: list[str], change: str
) -> None:
    await _open_debug(wd)
    if change == "demoted":
        await wd.repo.set_admin(ADMIN, False)
    else:
        await wd.repo.remove_user(ADMIN)
    products = await _product_count(wd)

    await wd.text(PRIVATE, ADMIN, URL)

    assert wd.texts_to(PRIVATE)[-1] == NOT_AUTHORISED
    assert debug_runs == []
    assert legacy_adds == []
    assert await _product_count(wd) == products
    assert len(wd.flow.registry) == 0


async def test_the_debug_prompt_expires_and_a_later_link_is_an_add(
    wd: Wired, debug_runs: list[tuple[int, str]], legacy_adds: list[str]
) -> None:
    prompt = await _open_debug(wd)
    flow = wd.flow.registry.get((PRIVATE, ADMIN))
    assert flow is not None
    assert wd.app.job_queue is not None
    snapshot = flow.snapshot((PRIVATE, ADMIN))
    (job,) = wd.app.job_queue.get_jobs_by_name(f"guided-flow:{PRIVATE}:{ADMIN}:{snapshot.token}")
    delay = (job.job.trigger.run_date - datetime.now(tz=UTC)).total_seconds()
    assert 295 < delay <= 300

    await wd.flow.on_timeout(snapshot)
    await wd.text(PRIVATE, ADMIN, URL)

    assert wd.edits_of(prompt) == [EXPIRED]
    assert legacy_adds == [URL]
    assert debug_runs == []


async def test_cancel_closes_the_debug_prompt(wd: Wired, debug_runs: list[tuple[int, str]]) -> None:
    prompt = await _open_debug(wd)

    await wd.text(PRIVATE, ADMIN, "/cancel")

    assert wd.edits_of(prompt) == [CANCELLED]
    assert len(wd.flow.registry) == 0
    assert debug_runs == []


async def test_the_debug_prompt_disarms_a_legacy_admin_prompt(wd: Wired) -> None:
    await _arm_admin_interval(wd)

    prompt = await _open_debug(wd)
    flow = wd.flow.registry.get((PRIVATE, ADMIN))
    assert flow is not None
    await wd.flow.on_timeout(flow.snapshot((PRIVATE, ADMIN)))
    await wd.text(PRIVATE, ADMIN, "60")

    assert wd.pending(ADMIN) is None
    assert wd.edits_of(prompt) == [EXPIRED]
    assert await wd.repo.get_config("check_interval_minutes") is None


async def test_a_second_debug_prompt_replaces_the_first(wd: Wired) -> None:
    first = await _open_debug(wd)
    second = await _open_debug(wd)
    (first_cancel,) = first.callback_data()

    await wd.press(PRIVATE, ADMIN, first_cancel)

    assert wd.edits_of(first) == [SUPERSEDED]
    assert wd.toasts()[-1] == BUTTON_EXPIRED
    flow = wd.flow.registry.get((PRIVATE, ADMIN))
    assert flow is not None
    assert second.callback_data() == [f"p:{flow.token}:x"]


async def test_a_value_prompt_replaces_the_debug_prompt(
    wd: Wired, debug_runs: list[tuple[int, str]]
) -> None:
    debug_prompt = await _open_debug(wd)

    await _open(wd, f"setsoglia_{wd.product}", user_id=ADMIN)
    await wd.text(PRIVATE, ADMIN, "20%")

    assert wd.edits_of(debug_prompt) == [SUPERSEDED]
    assert wd.texts_to(PRIVATE)[-1] == SAVED
    assert debug_runs == []


@pytest.mark.parametrize("with_prompt", [False, True])
async def test_debug_command_runs_once_and_closes_the_prompt(
    wd: Wired, debug_runs: list[tuple[int, str]], with_prompt: bool
) -> None:
    prompt = await _open_debug(wd) if with_prompt else None

    await wd.text(PRIVATE, ADMIN, f"/debug {URL}")

    assert debug_runs == [(ADMIN, URL)]
    assert len(wd.flow.registry) == 0
    if prompt is not None:
        assert wd.edits_of(prompt) == [CANCELLED]


async def test_a_failing_debug_run_reaches_the_error_handler_and_adds_nothing(
    wd: Wired, legacy_adds: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(update: Update, context: Any, url: str) -> None:
        raise RuntimeError("scraper exploded")

    await _open_debug(wd)
    monkeypatch.setattr(wd.flow, "_debug_runner", broken)
    products = await _product_count(wd)

    await wd.text(PRIVATE, ADMIN, URL)

    assert [type(e) for e in wd.errors] == [RuntimeError]
    assert legacy_adds == []
    assert await _product_count(wd) == products
    assert len(wd.flow.registry) == 0


async def test_a_failing_admin_check_at_the_answer_reaches_the_error_handler_and_adds_nothing(
    wd: Wired,
    debug_runs: list[tuple[int, str]],
    legacy_adds: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def locked(user_id: int) -> bool:
        del user_id
        raise RuntimeError("database is locked")

    await _open_debug(wd)
    monkeypatch.setattr(wd.repo, "is_user_admin", locked)
    products = await _product_count(wd)

    await wd.text(PRIVATE, ADMIN, URL)

    assert [type(e) for e in wd.errors] == [RuntimeError]
    assert debug_runs == []
    assert legacy_adds == []
    assert await _product_count(wd) == products
    assert len(wd.flow.registry) == 0
    assert "error" in wd.texts_to(PRIVATE)[-1]
