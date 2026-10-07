"""Messages are written in the language of the user who receives them.

Interactive replies (``with_locale``) read the user's row on every update; the
periodic sweep, the operational notices, the quarantine notice and the digest
read the row of each recipient. The stored choice wins, then the Telegram
language last seen, then the configured default. Real repository in memory,
recording notifier and bot.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiosqlite
import httpx
import pytest_asyncio
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.bot.decorators import with_locale
from price_tracker.bot.messages import _, reset_locale, set_locale
from price_tracker.core.alert import (
    PriceAlert,
    format_alert,
    format_back_in_stock,
    format_quarantine_notification,
)
from price_tracker.core.exceptions import ParseError
from price_tracker.core.health import HealthManager
from price_tracker.core.notices import NoticeCollector
from price_tracker.core.registry import ScraperRegistry
from price_tracker.core.scheduler import Scheduler, SchedulerDeps
from price_tracker.core.scraper_base import AbstractScraper, ProductInfo
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from price_tracker.notifier.digest import DigestService

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    import pytest

MIGRATIONS_DIR = Path("src/price_tracker/db/migrations")

A, B, C, D = 10, 11, 12, 13  # it chosen; en seen; ja seen; unusable stored choice
ITALIAN_USERS = {A}
BASE = Decimal("100.00")
# Seeding marks every product as just checked; the sweep runs once all are due.
LATER = datetime.now(UTC) + timedelta(days=1)

DROP_EN, DROP_IT = "Price drop!", "Prezzo in calo!"
STOCK_EN, STOCK_IT = "Back in stock!", "Di nuovo disponibile!"
NOTICE_EN, NOTICE_IT = "Price unreadable on", "Prezzo illeggibile su"
PAUSE_EN, PAUSE_IT = "Site paused automatically", "Sito in pausa automatica"
DIGEST_EN, DIGEST_IT = "Use /lista for full state.", "Usa /lista per lo stato completo."


class _ByUrl(AbstractScraper):
    name = "by-url"
    priority = 100

    def __init__(self, answers: dict[str, ProductInfo | Exception]) -> None:
        self.answers = answers

    def can_handle(self, url: str) -> bool:
        return True

    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        answer = self.answers[url]
        if isinstance(answer, Exception):
            raise answer
        return answer


@dataclass
class _Notifier:
    sent: list[tuple[int, str]] = field(default_factory=list)

    async def __call__(
        self,
        user_id: int,
        text: str,
        *,
        product_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        self.sent.append((user_id, text))
        return True

    def texts(self, user_id: int) -> list[str]:
        return [text for uid, text in self.sent if uid == user_id]


@pytest_asyncio.fixture
async def repo() -> AsyncIterator[Repository]:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, MIGRATIONS_DIR)
    repository = Repository(conn)
    for uid in (A, B, C, D):
        await repository.ensure_user(uid)
    await repository.set_user_language(A, "it")
    await repository.set_user_telegram_tag(B, "en")
    await repository.set_user_telegram_tag(C, "ja")
    # A value no catalogue serves, as an older or hand-edited row could hold.
    await conn.execute("UPDATE users SET language = 'xx' WHERE user_id = ?", (D,))
    await conn.commit()
    try:
        yield repository
    finally:
        await conn.close()


async def _product(repo: Repository, user_id: int, slug: str, *, available: bool = True) -> int:
    url = f"https://example.com/{user_id}/{slug}"
    pid = await repo.add_product(
        user_id=user_id,
        url=url,
        name=f"Kettle {slug}",
        domain="example.com",
        initial_price=BASE,
        currency="EUR",
    )
    await repo.update_price(pid, BASE)
    for _reading in range(10):
        await repo.add_price_history(pid, BASE)
    if not available:
        await repo.set_availability(pid, available=False)
    return pid


async def _sweep_world(repo: Repository) -> dict[str, ProductInfo | Exception]:
    answers: dict[str, ProductInfo | Exception] = {}
    for uid in (A, B, C, D):
        await _product(repo, uid, "drop")
        await _product(repo, uid, "restock", available=False)
        await _product(repo, uid, "broken")
        base = f"https://example.com/{uid}"
        answers[f"{base}/drop"] = ProductInfo(name="Kettle", price=Decimal("80.00"), currency="EUR")
        answers[f"{base}/restock"] = ProductInfo(name="Kettle", price=BASE, currency="EUR")
        answers[f"{base}/broken"] = ParseError("no price on the page")
    return answers


def _scheduler(
    repo: Repository,
    client: httpx.AsyncClient,
    notifier: _Notifier,
    answers: dict[str, ProductInfo | Exception],
    *,
    lang: str = "en",
) -> Scheduler:
    registry = ScraperRegistry()
    registry.register(_ByUrl(answers))
    return Scheduler(
        SchedulerDeps(
            repo=repo,
            registry=registry,
            client=client,
            notifier=notifier,
            max_consecutive_errors=1,
            delay_between_products=0.0,
            read_confirmations=1,
            lang=lang,
            health_mgr=HealthManager(repo),
        )
    )


def _expect(user_id: int, english: str, italian: str) -> str:
    return italian if user_id in ITALIAN_USERS else english


# --- the periodic sweep ---------------------------------------------------------


async def test_one_sweep_writes_to_each_recipient_in_their_own_language(
    repo: Repository,
) -> None:
    answers = await _sweep_world(repo)
    notifier = _Notifier()
    async with httpx.AsyncClient() as client:
        await _scheduler(repo, client, notifier, answers).run_check_due(
            global_interval_minutes=5, now=LATER
        )

    for uid in (A, B, C, D):
        texts = "\n".join(notifier.texts(uid))
        for english, italian in ((DROP_EN, DROP_IT), (STOCK_EN, STOCK_IT), (NOTICE_EN, NOTICE_IT)):
            expected = _expect(uid, english, italian)
            other = italian if expected == english else english
            assert expected in texts, (uid, expected)
            assert other not in texts, (uid, other)


async def test_a_user_without_a_usable_language_gets_the_configured_default(
    repo: Repository,
) -> None:
    answers = await _sweep_world(repo)
    notifier = _Notifier()
    async with httpx.AsyncClient() as client:
        await _scheduler(repo, client, notifier, answers, lang="it").run_check_due(
            global_interval_minutes=5, now=LATER
        )

    assert DROP_IT in "\n".join(notifier.texts(D))
    assert DROP_EN in "\n".join(notifier.texts(B))


async def test_a_failed_language_read_falls_back_to_the_default_and_the_sweep_goes_on(
    repo: Repository, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    answers = await _sweep_world(repo)

    async def broken(user_id: int) -> None:
        raise aiosqlite.OperationalError("database is locked")

    monkeypatch.setattr(repo, "get_user", broken)
    notifier = _Notifier()
    with caplog.at_level(logging.WARNING):
        async with httpx.AsyncClient() as client:
            await _scheduler(repo, client, notifier, answers, lang="it").run_check_due(
                global_interval_minutes=5, now=LATER
            )

    for uid in (A, B, C, D):
        assert DROP_IT in "\n".join(notifier.texts(uid))
    assert "could not read the language" in caplog.text


async def test_the_quarantine_notice_follows_the_recipient_across_a_restart(
    repo: Repository,
) -> None:
    products = {uid: await _product(repo, uid, "blocked") for uid in (A, B, C, D)}
    for round_ in range(2):
        notifier = _Notifier()
        async with httpx.AsyncClient() as client:
            scheduler = _scheduler(repo, client, notifier, {})
            for pid in products.values():
                record = await repo.get_product(pid)
                assert record is not None
                await scheduler._notify_quarantine_entry(record, "example.com", reason="403")
        for uid in (A, B, C, D):
            assert _expect(uid, PAUSE_EN, PAUSE_IT) in notifier.texts(uid)[0], (round_, uid)


async def test_changing_the_stored_language_changes_the_next_message(repo: Repository) -> None:
    pid = await _product(repo, A, "blocked")
    record = await repo.get_product(pid)
    assert record is not None
    notifier = _Notifier()
    async with httpx.AsyncClient() as client:
        scheduler = _scheduler(repo, client, notifier, {})
        await scheduler._notify_quarantine_entry(record, "example.com", reason="403")
        await repo.set_user_language(A, "en")
        await scheduler._notify_quarantine_entry(record, "example.com", reason="403")
    first, second = notifier.texts(A)
    assert PAUSE_IT in first
    assert PAUSE_EN in second


async def _suspended_collector(scheduler: Scheduler, repo: Repository, *uids: int) -> Any:
    collector = NoticeCollector()
    for uid in uids:
        record = await repo.get_product(await _product(repo, uid, f"gone-{len(uids)}"))
        assert record is not None
        collector.add(scheduler._event("suspended", record, reason="parse_error", detail=None))
    return collector


async def test_each_notice_group_is_rendered_in_its_recipients_language(
    repo: Repository,
) -> None:
    notifier = _Notifier()
    async with httpx.AsyncClient() as client:
        scheduler = _scheduler(repo, client, notifier, {})
        await scheduler._flush_notices(await _suspended_collector(scheduler, repo, A, B, A))
    assert all(NOTICE_IT in text for text in notifier.texts(A))
    assert all(NOTICE_EN in text for text in notifier.texts(B))
    assert len(notifier.sent) == 2


async def test_two_concurrent_flushes_in_different_languages_do_not_mix(
    repo: Repository,
) -> None:
    both_rendering = asyncio.Barrier(2)

    @dataclass
    class _Interleaving(_Notifier):
        async def __call__(self, user_id: int, text: str, **kwargs: Any) -> bool:
            await both_rendering.wait()
            return await super().__call__(user_id, text, **kwargs)

    notifier = _Interleaving()
    async with httpx.AsyncClient() as client:
        scheduler = _scheduler(repo, client, notifier, {})
        italian = await _suspended_collector(scheduler, repo, A)
        english = await _suspended_collector(scheduler, repo, B)
        await asyncio.wait_for(
            asyncio.gather(scheduler._flush_notices(italian), scheduler._flush_notices(english)), 2
        )
    assert NOTICE_IT in notifier.texts(A)[0]
    assert NOTICE_EN in notifier.texts(B)[0]
    assert _("Saved.") == "Saved."


# --- the digest ------------------------------------------------------------------


@dataclass
class _Bot:
    sent: list[tuple[int, str]] = field(default_factory=list)

    async def send_message(self, *, chat_id: int, text: str, parse_mode: str) -> None:
        self.sent.append((chat_id, text))


async def _enqueue_all(service: DigestService) -> None:
    payload = {
        "kind": "price",
        "product_name": "Kettle",
        "url": "https://example.com/k",
        "old_price": "100",
        "new_price": "80",
        "currency": "EUR",
        "domain": "example.com",
    }
    for uid in (A, B, C, D):
        await service.enqueue(user_id=uid, product_id=None, payload=payload)


async def test_the_digest_follows_each_recipient_across_a_restart(repo: Repository) -> None:
    for round_ in range(2):
        bot = _Bot()
        service = DigestService(repo=repo, bot=bot, lang="en")  # type: ignore[arg-type]
        await _enqueue_all(service)
        await service.flush_due(interval_minutes=0)
        by_user = dict(bot.sent)
        for uid in (A, B, C, D):
            assert _expect(uid, DIGEST_EN, DIGEST_IT) in by_user[uid], (round_, uid)


async def test_flush_user_alone_writes_in_the_recipients_language(repo: Repository) -> None:
    bot = _Bot()
    service = DigestService(repo=repo, bot=bot, lang="en")  # type: ignore[arg-type]
    await _enqueue_all(service)
    token = set_locale("en")
    try:
        await service.flush_user(user_id=A)
    finally:
        reset_locale(token)
    assert DIGEST_IT in bot.sent[0][1]


# --- interactive replies: with_locale ---------------------------------------------


@dataclass
class _User:
    id: int
    language_code: str | None


@dataclass
class _Update:
    effective_user: _User | None


@dataclass
class _Context:
    bot_data: dict[str, Any]


@with_locale
async def _reply(update: Any, context: Any) -> str:
    return _("Saved.")


async def _answer(repo: Any, user_id: int, tag: str | None) -> str:
    result: str = await _reply(_Update(_User(user_id, tag)), _Context({"db": repo}))
    set_locale("en")
    return result


async def test_the_stored_choice_wins_over_the_telegram_language(repo: Repository) -> None:
    assert await _answer(repo, A, "en") == "Salvato."
    assert await _answer(repo, B, "en") == "Saved."
    assert await _answer(repo, D, "it") == "Salvato."


async def test_an_unknown_user_is_answered_in_the_telegram_language(repo: Repository) -> None:
    assert await _answer(repo, 404, "it") == "Salvato."
    assert await repo.get_user(404) is None


async def _tag_updates(repo: Repository) -> list[str]:
    statements: list[str] = []
    await repo._conn.set_trace_callback(statements.append)
    return statements


async def test_the_telegram_language_is_stored_only_when_valid_and_new(repo: Repository) -> None:
    statements = await _tag_updates(repo)

    def updates() -> int:
        return sum("telegram_language_tag" in s and s.startswith("UPDATE") for s in statements)

    await _answer(repo, B, "pt-BR")
    assert updates() == 1
    await _answer(repo, B, "pt-BR")
    assert updates() == 1
    for invalid in ("it IT", "", "x" * 36, None):
        await _answer(repo, B, invalid)
    assert updates() == 1
    row = await repo.get_user(B)
    assert row is not None
    assert row.telegram_language_tag == "pt-BR"


async def test_a_failed_read_answers_in_the_telegram_language_and_logs(
    repo: Repository, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(user_id: int) -> None:
        raise aiosqlite.OperationalError("database is locked")

    monkeypatch.setattr(repo, "get_user", broken)
    with caplog.at_level(logging.WARNING):
        assert await _answer(repo, A, "en") == "Saved."
        assert await _answer(repo, A, "it") == "Salvato."
    assert "could not read the language" in caplog.text


async def test_a_failed_tag_write_still_answers(
    repo: Repository, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(user_id: int, tag: object) -> bool:
        raise aiosqlite.OperationalError("database is locked")

    monkeypatch.setattr(repo, "set_user_telegram_tag", broken)
    with caplog.at_level(logging.WARNING):
        assert await _answer(repo, B, "it") == "Salvato."
    assert "could not store the Telegram language" in caplog.text


# --- the three formatters: English output is exactly the old one -----------------


def _old_alert(alert: PriceAlert, sym: str) -> str:
    name = html.escape(alert.product_name.replace("\r", " ").replace("\n", " "), quote=True)
    url = html.escape(alert.url.replace("\r", " ").replace("\n", " "), quote=True)
    old, new = alert.old_price, alert.new_price
    drop = old - new
    drop_pct = (drop / old * 100) if old > 0 else Decimal("0")
    return (
        f"📉 <b>Price drop!</b>\n\n"
        f"<b>{name}</b>\n"
        f'<a href="{url}">View product</a>\n\n'
        f"Was: <s>{old} {sym}</s>\n"
        f"Now: <b>{new} {sym}</b>\n"
        f"Drop: -{drop} {sym} ({drop_pct:.1f}%)"
    )


def _old_back_in_stock(name: str, url: str, price: Decimal, sym: str) -> str:
    esc_name = html.escape(name.replace("\r", " ").replace("\n", " "), quote=True)
    esc_url = html.escape(url.replace("\r", " ").replace("\n", " "), quote=True)
    return (
        f"📦 <b>Back in stock!</b>\n\n"
        f"<b>{esc_name}</b>\n"
        f'<a href="{esc_url}">View product</a>\n\n'
        f"Price: <b>{price} {sym}</b>"
    )


NAMES = st.text(alphabet=st.sampled_from("Kettle {}<>&\"'\n%é😀"), min_size=1, max_size=40)
PRICES = st.decimals(min_value=Decimal("0.01"), max_value=Decimal("99999"), places=2)


@settings(max_examples=200, deadline=None)
@given(NAMES, PRICES, PRICES)
def test_the_english_alert_is_byte_identical_to_the_old_text(
    name: str, old: Decimal, new: Decimal
) -> None:
    alert = PriceAlert(
        product_id=1,
        product_name=name,
        url="https://example.com/k?a=1&b={x}",
        old_price=old,
        new_price=new,
        currency="EUR",
        threshold_type="percentage",
        threshold_value=Decimal("10"),
    )
    token = set_locale("en")
    try:
        assert format_alert(alert) == _old_alert(alert, "€")
        assert format_back_in_stock(
            product_name=name, url=alert.url, price=new, currency="EUR"
        ) == _old_back_in_stock(name, alert.url, new, "€")
    finally:
        reset_locale(token)


def test_the_italian_quarantine_text_is_the_one_users_already_know() -> None:
    token = set_locale("it")
    try:
        text = format_quarantine_notification(
            domain="example.com",
            reason="HTTP 403",
            locked_until=datetime(2026, 3, 1, 12, 30, tzinfo=UTC),
        )
    finally:
        reset_locale(token)
    assert text == (
        "🔒 <b>Sito in pausa automatica</b>\n\n"
        "<b>example.com</b> ha fallito troppi controlli (HTTP 403).\n"
        "Sospendo temporaneamente i check su questo sito per non insistere "
        "contro un blocco.\n🔁 Riprovo da solo dopo: 2026-03-01 12:30 UTC\n\n"
        "Dettagli con /errori."
    )
