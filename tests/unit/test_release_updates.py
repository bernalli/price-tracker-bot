"""Release announcements use persisted state and each recipient's language."""

from __future__ import annotations

import asyncio
from collections import Counter
from html import unescape
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import aiosqlite
import pytest
from telegram.error import Forbidden, RetryAfter

import price_tracker
from price_tracker import release_notes
from price_tracker.bot import release_updates
from price_tracker.bot.messages import current_locale, get_translation, reset_locale, set_locale
from price_tracker.bot.ui.width import display_width
from price_tracker.db.migrator import apply_migrations
from price_tracker.db.repository import Repository
from price_tracker.main import MIGRATIONS_DIR

CATALOG_LOCALES = sorted(
    path.parent.parent.name
    for path in (Path(release_notes.__file__).parent / "locale").glob("*/LC_MESSAGES/messages.po")
)


def assert_short_release_notes(locale: str) -> None:
    translation = get_translation(locale)
    for version, bullets in release_notes.RELEASE_NOTES.items():
        for bullet in bullets:
            for text in (bullet, translation.gettext(bullet)):
                assert len(text.splitlines()) == 1, (version, locale, text)
                assert display_width(f"• {text}") <= 32, (version, locale, text)


@pytest.mark.parametrize("locale", CATALOG_LOCALES)
def test_release_notes_are_short_phrases(locale):
    assert_short_release_notes(locale)


@pytest.mark.parametrize("bullet", ["x" * 31, "🔔" * 16, "First\nSecond"])
def test_release_notes_guard_rejects_long_or_multiline_notes(monkeypatch, bullet):
    monkeypatch.setattr(release_notes, "RELEASE_NOTES", {"1.8.0": (bullet,)})
    with pytest.raises(AssertionError):
        assert_short_release_notes("en")


@pytest.mark.parametrize("locale", CATALOG_LOCALES)
@pytest.mark.parametrize("version", release_notes.RELEASE_NOTES)
@pytest.mark.parametrize("catch_up", [False, True])
def test_every_release_message_line_fits_small_phones(locale, version, catch_up):
    # Exhaust the finite version/locale/mode domain, including empty releases.
    token = set_locale(locale)
    try:
        assert current_locale() == locale
        previous = "1.0.0" if catch_up else version
        text = release_notes.render_release_notes(previous, version)
        assert len(text) <= release_notes.MESSAGE_LIMIT
        for line in unescape(text).splitlines():
            assert display_width(line) <= 32, (version, locale, line)
    finally:
        reset_locale(token)


@pytest.fixture
async def repo(memory_db):
    await apply_migrations(memory_db, MIGRATIONS_DIR)
    return Repository(memory_db)


@pytest.fixture(autouse=True)
def no_delay(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("price_tracker.bot.release_updates.sleep", sleep)
    get_translation.cache_clear()
    token = set_locale("en")
    yield sleep
    reset_locale(token)
    get_translation.cache_clear()


async def test_first_start_catches_up_each_active_user_and_persists(repo, no_delay):
    for uid in (123456789, 900000001):
        await repo.ensure_user(uid)
    bot = Mock(send_message=AsyncMock())
    await release_updates.announce_release(bot, repo, "en")
    calls = bot.send_message.await_args_list
    assert [call.kwargs["chat_id"] for call in calls] == [123456789, 900000001]
    for call in calls:
        assert call.kwargs["parse_mode"] == "HTML"
        assert f"Updated to version {price_tracker.__version__}" in call.kwargs["text"]
        assert "Earlier:" in call.kwargs["text"]
        assert "• Every command has a button" in call.kwargs["text"]
        assert "1.0.0:" not in call.kwargs["text"]
    assert await repo.get_config("last_announced_version") == price_tracker.__version__
    assert no_delay.await_count == 1
    assert no_delay.await_args.args[0] >= 0.05
    bot.send_message.reset_mock()
    # A fresh repository wrapper sees the stored announcement on the next start.
    await release_updates.announce_release(bot, Repository(repo._conn), "en")
    bot.send_message.assert_not_awaited()


@pytest.mark.parametrize("stored", ["1.8.0", "1.8.1", "1.10.0"])
async def test_equal_or_older_running_version_does_nothing(repo, monkeypatch, stored):
    monkeypatch.setattr(price_tracker, "__version__", "1.8.0")
    await repo.ensure_user(123456789)
    await repo.set_config("last_announced_version", stored)
    bot = Mock(send_message=AsyncMock())
    await release_updates.announce_release(bot, repo, "en")
    bot.send_message.assert_not_awaited()
    assert await repo.get_config("last_announced_version") == stored


@pytest.mark.parametrize("skipped", [False, True])
async def test_upgrade_only_includes_unseen_releases(repo, monkeypatch, skipped):
    notes = {
        "1.7.1": ("Already seen", "Also seen"),
        "1.7.2": ("Intermediate improvement", "Intermediate detail"),
        "1.8.0": ("Newest improvement", "Newest detail"),
        "1.10.0": ("Future improvement", "Future detail"),
    }
    monkeypatch.setattr(release_notes, "RELEASE_NOTES", notes)
    monkeypatch.setattr(price_tracker, "__version__", "1.8.0")
    await repo.ensure_user(123456789)
    await repo.set_config("last_announced_version", "1.7.1" if skipped else "1.7.2")
    bot = Mock(send_message=AsyncMock())
    await release_updates.announce_release(bot, repo, "en")
    text = bot.send_message.await_args.kwargs["text"]
    assert "Newest improvement" in text
    assert "Newest detail" in text
    assert "Already seen" not in text
    assert "Future improvement" not in text
    assert ("• Intermediate improvement" in text) == skipped
    assert ("Earlier:" in text) == skipped
    assert "Intermediate detail" not in text
    assert await repo.get_config("last_announced_version") == "1.8.0"


async def test_empty_skipped_release_is_omitted_from_catch_up(repo, monkeypatch):
    notes = {
        "1.7.1": ("Already seen",),
        "1.7.2": (),
        "1.7.3": ("Intermediate improvement",),
        "1.8.0": ("Newest improvement",),
    }
    monkeypatch.setattr(release_notes, "RELEASE_NOTES", notes)
    monkeypatch.setattr(price_tracker, "__version__", "1.8.0")
    await repo.ensure_user(123456789)
    await repo.set_config("last_announced_version", "1.7.1")
    bot = Mock(send_message=AsyncMock())

    await release_updates.announce_release(bot, repo, "en")

    text = bot.send_message.await_args.kwargs["text"]
    assert "• Intermediate improvement" in text
    assert "1.7.2:" not in text


async def test_empty_newest_and_skipped_releases_are_recorded_without_message(repo, monkeypatch):
    notes = {"1.7.1": ("Already seen",), "1.7.2": (), "1.8.0": ()}
    monkeypatch.setattr(release_notes, "RELEASE_NOTES", notes)
    monkeypatch.setattr(price_tracker, "__version__", "1.8.0")
    await repo.ensure_user(123456789)
    await repo.set_config("last_announced_version", "1.7.1")
    bot = Mock(send_message=AsyncMock())

    await release_updates.announce_release(bot, repo, "en")

    bot.send_message.assert_not_awaited()
    assert await repo.get_config("last_announced_version") == "1.8.0"


async def test_upgrade_from_1_8_0_to_1_8_1_is_silent_and_stores_version(repo, monkeypatch):
    monkeypatch.setattr(price_tracker, "__version__", "1.8.1")
    assert release_notes.RELEASE_NOTES["1.8.1"] == ()
    for uid, locale in enumerate(CATALOG_LOCALES, start=1):
        await repo.ensure_user(uid)
        await repo.set_user_telegram_tag(uid, locale.replace("_", "-"))
    await repo.set_config("last_announced_version", "1.8.0")
    bot = Mock(send_message=AsyncMock())

    await release_updates.announce_release(bot, repo, "en")

    bot.send_message.assert_not_awaited()
    assert await repo.get_config("last_announced_version") == "1.8.1"


def test_cap_drops_oldest_lines_and_escapes_html(monkeypatch):
    notes = {f"1.{minor}.0": (f"Improvement {minor}", "Detail") for minor in range(1, 100)}
    notes["1.100.0"] = ("Prices < 10 & > 5", "Keep <b>literal</b> text")
    monkeypatch.setattr(release_notes, "RELEASE_NOTES", notes)
    text = release_notes.render_release_notes("1.0.0", "1.100.0")
    assert len(text) <= 1200
    assert "Prices &lt; 10 &amp; &gt; 5" in text
    assert "&lt;b&gt;literal&lt;/b&gt;" in text
    assert (
        text.index("• Improvement 99")
        < text.index("• Improvement 98")
        < text.index("• Improvement 97")
    )
    assert "• Improvement 1" not in text.splitlines()
    assert text.endswith("…and earlier improvements.")


async def test_recipient_language_and_locale_restoration(repo):
    for uid in (123456789, 900000001, 900000002):
        await repo.ensure_user(uid)
    await repo.set_user_language(123456789, "it")
    await repo.set_user_telegram_tag(123456789, "en")
    await repo.set_user_language(900000001, "en")
    await repo.set_user_telegram_tag(900000002, "it-IT")
    bot = Mock(send_message=AsyncMock())
    await release_updates.announce_release(bot, repo, "en")
    texts = {
        call.kwargs["chat_id"]: call.kwargs["text"] for call in bot.send_message.await_args_list
    }
    assert f"Aggiornato alla versione {price_tracker.__version__}" in texts[123456789]
    assert "In precedenza:" in texts[123456789]
    assert f"Updated to version {price_tracker.__version__}" in texts[900000001]
    assert texts[900000002] == texts[123456789]
    assert current_locale() == "en"


@pytest.mark.parametrize("failure", [Forbidden("private error details"), RetryAfter(2)])
async def test_send_failure_does_not_stop_others_or_repeat(repo, no_delay, failure, capsys):
    for uid in (123456789, 900000001):
        await repo.ensure_user(uid)
    bot = Mock(send_message=AsyncMock(side_effect=[failure, None]))
    await release_updates.announce_release(bot, repo, "en")
    assert bot.send_message.await_count == 2
    assert bot.send_message.await_args.kwargs["chat_id"] == 900000001
    assert await repo.get_config("last_announced_version") == price_tracker.__version__
    assert "private error details" not in capsys.readouterr().out
    if isinstance(failure, RetryAfter):
        assert any(call.args[0] >= 2 for call in no_delay.await_args_list)
    await release_updates.announce_release(bot, repo, "en")
    assert bot.send_message.await_count == 2


async def test_inactive_removed_and_unknown_users_receive_nothing(repo, memory_db):
    for uid in (123456789, 900000001, 900000002):
        await repo.ensure_user(uid)
    await repo.remove_user(900000001)
    await memory_db.execute("DELETE FROM users WHERE user_id = ?", (900000002,))
    await memory_db.commit()
    bot = Mock(send_message=AsyncMock())
    await release_updates.announce_release(bot, repo, "en")
    assert [call.kwargs["chat_id"] for call in bot.send_message.await_args_list] == [123456789]


async def test_no_users_still_records_version(repo):
    bot = Mock(send_message=AsyncMock())
    await release_updates.announce_release(bot, repo, "en")
    bot.send_message.assert_not_awaited()
    assert await repo.get_config("last_announced_version") == price_tracker.__version__


def test_latest_bullets_also_fit_without_broken_html(monkeypatch):
    monkeypatch.setattr(
        release_notes,
        "RELEASE_NOTES",
        {"1.1.0": ("Older improvement", "Detail"), "1.2.0": ("<&>" * 400,) * 4},
    )
    text = release_notes.render_release_notes("1.0.0", "1.2.0")
    assert len(text) <= 1200
    assert text.count("• ") >= 4
    assert all(display_width(line) <= 32 for line in unescape(text).splitlines())
    # Every ampersand belongs to a complete entity, even in shortened bullets.
    assert "&" not in text.replace("&lt;", "").replace("&gt;", "").replace("&amp;", "")


def test_every_release_bullet_has_an_italian_translation():
    italian = get_translation("it")
    for bullets in release_notes.RELEASE_NOTES.values():
        for bullet in bullets:
            assert italian.gettext(bullet) != bullet


async def test_post_init_leaves_release_announcement_for_background_task(memory_db, monkeypatch):
    from price_tracker import main

    bot = Mock(send_message=AsyncMock())
    client = Mock()
    monkeypatch.setattr(main, "build_client", Mock(return_value=client))
    monkeypatch.setattr(main, "sync_command_menus", AsyncMock())
    application = Mock(
        bot=bot,
        bot_data={
            "config": Mock(admin_users=(123456789,), request_timeout=30, lang="en"),
            "db_conn": memory_db,
            "registry": Mock(),
        },
    )
    await main.post_init(application)
    bot.send_message.assert_not_awaited()
    assert await application.bot_data["repo"].is_user_allowed(123456789)


@pytest.mark.parametrize("crash_after", [1, 3])
async def test_announce_crash_then_restart_resumes_without_duplicates(tmp_path, crash_after):
    database = tmp_path / "announcements.db"
    received = []

    async def send_then_crash(*, chat_id, **kwargs):
        received.append(chat_id)
        if len(received) == crash_after:
            raise asyncio.CancelledError

    async with aiosqlite.connect(database) as conn:
        await apply_migrations(conn, MIGRATIONS_DIR)
        repo = Repository(conn)
        for uid in range(1, 5):
            await repo.ensure_user(uid)
        with pytest.raises(asyncio.CancelledError):
            await release_updates.announce_release(
                Mock(send_message=AsyncMock(side_effect=send_then_crash)), repo, "en"
            )
        assert await repo.get_config("last_announced_version") is None

    async with aiosqlite.connect(database) as conn:
        repo = Repository(conn)
        bot = Mock(send_message=AsyncMock())
        await release_updates.announce_release(bot, repo, "en")
        received.extend(call.kwargs["chat_id"] for call in bot.send_message.await_args_list)
        assert Counter(received) == Counter(range(1, 5))
        assert await repo.get_config("last_announced_version") == price_tracker.__version__


async def test_announce_claim_is_committed_before_send_and_survives_lost_message(tmp_path):
    database = tmp_path / "announcements.db"
    async with aiosqlite.connect(database) as conn, aiosqlite.connect(database) as observer:
        await apply_migrations(conn, MIGRATIONS_DIR)
        repo = Repository(conn)
        await repo.ensure_user(1)
        await repo.ensure_user(2)

        async def crash_before_delivery(*, chat_id, **kwargs):
            assert await Repository(observer).get_config(f"announced:{chat_id}") == (
                price_tracker.__version__
            )
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await release_updates.announce_release(
                Mock(send_message=AsyncMock(side_effect=crash_before_delivery)), repo, "en"
            )

    async with aiosqlite.connect(database) as conn:
        bot = Mock(send_message=AsyncMock())
        await release_updates.announce_release(bot, Repository(conn), "en")
        assert [call.kwargs["chat_id"] for call in bot.send_message.await_args_list] == [2]


async def test_concurrent_announcements_claim_each_recipient_once(tmp_path, monkeypatch):
    database = tmp_path / "announcements.db"
    async with aiosqlite.connect(database) as first, aiosqlite.connect(database) as second:
        await apply_migrations(first, MIGRATIONS_DIR)
        repos = [Repository(first), Repository(second)]
        for uid in range(1, 9):
            await repos[0].ensure_user(uid)
        # Both instances must read the pending version and take their user snapshot.
        barrier = asyncio.Barrier(2)
        list_users = Repository.list_active_users

        async def snapshot(repo):
            users = await list_users(repo)
            await barrier.wait()
            return users

        monkeypatch.setattr(Repository, "list_active_users", snapshot)
        bot = Mock(send_message=AsyncMock())
        await asyncio.wait_for(
            asyncio.gather(*(release_updates.announce_release(bot, repo, "en") for repo in repos)),
            timeout=5,
        )
        received = [call.kwargs["chat_id"] for call in bot.send_message.await_args_list]
        assert Counter(received) == Counter(range(1, 9))


@pytest.mark.parametrize("delete", [False, True])
@pytest.mark.parametrize("rate_limited", [False, True])
async def test_announce_rechecks_revoked_user_after_wait(repo, no_delay, delete, rate_limited):
    await repo.ensure_user(1)
    await repo.ensure_user(2)

    async def revoke_during_wait(delay):
        if delete:
            await repo._conn.execute("DELETE FROM users WHERE user_id = 2")
            await repo._conn.commit()
        else:
            await repo.remove_user(2)

    no_delay.side_effect = revoke_during_wait
    bot = Mock(send_message=AsyncMock(side_effect=RetryAfter(3600) if rate_limited else None))
    await release_updates.announce_release(bot, repo, "en")
    assert [call.kwargs["chat_id"] for call in bot.send_message.await_args_list] == [1]
    assert await repo.get_config("last_announced_version") == price_tracker.__version__


async def test_announce_skips_current_claims_and_cleans_only_older_versions(repo, monkeypatch):
    monkeypatch.setattr(price_tracker, "__version__", "1.10.0")
    monkeypatch.setitem(release_notes.RELEASE_NOTES, "1.10.0", ("New improvement",))
    for uid in (1, 2):
        await repo.ensure_user(uid)
    await repo.set_config("announced:1", "1.10.0")
    await repo.set_config("announced:2", "1.9.0")
    await repo.set_config("announced:3", "1.9.0")
    await repo.set_config("announced:4", "1.11.0")
    await repo.set_config("other_setting", "1.9.0")
    bot = Mock(send_message=AsyncMock())
    await release_updates.announce_release(bot, repo, "en")
    assert [call.kwargs["chat_id"] for call in bot.send_message.await_args_list] == [2]
    assert await repo.get_config("announced:1") == "1.10.0"
    assert await repo.get_config("announced:2") == "1.10.0"
    assert await repo.get_config("announced:3") is None
    assert await repo.get_config("announced:4") == "1.11.0"
    assert await repo.get_config("other_setting") == "1.9.0"


def test_release_notes_omit_all_older_notes_when_none_fit(monkeypatch):
    monkeypatch.setattr(release_notes, "MESSAGE_LIMIT", 75)
    monkeypatch.setattr(
        release_notes,
        "RELEASE_NOTES",
        {"1.1.0": ("Older improvement fills space",), "1.2.0": ("Newest improvement",)},
    )
    text = release_notes.render_release_notes("1.0.0", "1.2.0")
    assert text == ("Updated to version 1.2.0\n• Newest improvement\n…and earlier improvements.")
    assert len(text) <= release_notes.MESSAGE_LIMIT
