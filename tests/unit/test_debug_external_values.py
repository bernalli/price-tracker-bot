"""``/debug`` and ``/health`` show values from pages and errors as plain text.

Every value that comes from the fetched page, from an exception or from the
scraper is cut to its field limit and then HTML-escaped, so a hostile page can
neither inject markup nor break the message Telegram has to parse.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.bot.handlers.debug import cmd_debug, health_command
from price_tracker.core.health import HealthManager
from price_tracker.core.textlimits import _is_valid_telegram_markup, visible_length
from price_tracker.db.models import ScraperHealth

if TYPE_CHECKING:
    from collections.abc import Callable

_URL = "https://shop.example/p/1"
_HOSTILE = (
    "<b>x",
    '<a href="http://h.example">l</a>',
    "&amp;<i>",
    "</b><u>",
    "&",
    "<script>",
)
_TAG = re.compile(r"<[^>]*>")


def _ld_json(data: object) -> str:
    """Encode ``data`` as a JSON-LD script whose body never contains raw ``<``, ``>``, ``&``."""
    body = json.dumps(data).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return f'<script type="application/ld+json">{body}</script>'


def _page(head: str = "", body: str = "<p>nothing here</p>") -> str:
    return f"<html><head>{head}</head><body>{body}</body></html>"


def _attr(value: str) -> str:
    return html.escape(value, quote=True)


class _Run:
    """One ``/debug`` run: the page served, what the HTTP layer raises, what the scraper says."""

    def __init__(
        self,
        page: str = _page(),
        *,
        shared_error: str | None = None,
        fresh_error: str | None = None,
        loads_error: str | None = None,
        scrape: SimpleNamespace | None = None,
    ) -> None:
        self.page = page
        self.shared_error = shared_error
        self.fresh_error = fresh_error
        self.loads_error = loads_error
        self.scrape = scrape or SimpleNamespace(name="Kettle", price=None, error="e")

    async def report(self) -> str:
        scraper = SimpleNamespace(scrape=AsyncMock(return_value=self.scrape))
        registry = SimpleNamespace(resolve=lambda _url: scraper)
        db = MagicMock()
        db.is_user_admin = AsyncMock(return_value=True)
        db.is_user_allowed = AsyncMock(return_value=True)
        db.get_user = AsyncMock(return_value=None)
        context = MagicMock()
        context.args = [_URL]
        context.bot_data = {"db": db, "scraper": registry, "http_client": MagicMock()}
        placeholder = MagicMock()
        placeholder.edit_text = AsyncMock()
        update = MagicMock()
        update.effective_user.id = 1
        update.effective_user.language_code = "en"
        update.message.reply_text = AsyncMock(return_value=placeholder)

        response = SimpleNamespace(status_code=200, text=self.page)
        calls: list[int] = []

        async def fake_request(*_args: object, **_kwargs: object) -> SimpleNamespace:
            calls.append(1)
            if len(calls) == 1 and self.shared_error is not None:
                raise RuntimeError(self.shared_error)
            if len(calls) == 2 and self.fresh_error is not None:
                raise RuntimeError(self.fresh_error)
            return response

        fresh = MagicMock()
        fresh.__aenter__ = AsyncMock(return_value=MagicMock())
        fresh.__aexit__ = AsyncMock(return_value=False)
        real_loads = json.loads

        def fake_loads(raw: str, *args: Any, **kwargs: Any) -> Any:
            if self.loads_error is not None:
                raise ValueError(self.loads_error)
            return real_loads(raw, *args, **kwargs)

        with (
            patch("price_tracker.bot.handlers.debug.public_request", fake_request),
            patch("price_tracker.bot.handlers.debug.build_client", return_value=fresh),
            patch("price_tracker.bot.handlers.debug._json.loads", fake_loads),
        ):
            await cmd_debug(update, context)
        args: Any = placeholder.edit_text.await_args
        return str(args.args[0])


def _offers_page(**offers: object) -> str:
    return _page(_ld_json({"@type": "Product", "offers": offers}))


# One builder per place the report shows an external value, with that field's limit.
_SITES: dict[str, tuple[Callable[[str], _Run], int]] = {
    "ld_type": (lambda v: _Run(_page(_ld_json({"@type": v, "offers": {}}))), 40),
    "ld_price": (lambda v: _Run(_offers_page(price=v, priceCurrency="EUR")), 40),
    "ld_currency": (lambda v: _Run(_offers_page(price="1", priceCurrency=v)), 40),
    "ld_offers_list_price": (
        lambda v: _Run(_page(_ld_json({"@type": "Product", "offers": [{"price": v}]}))),
        40,
    ),
    "og_price_meta": (
        lambda v: _Run(_page(f'<meta property="og:price:amount" content="{_attr(v)}">')),
        40,
    ),
    "product_price_meta": (
        lambda v: _Run(_page(f'<meta property="product:price:amount" content="{_attr(v)}">')),
        40,
    ),
    "itemprop_price": (
        lambda v: _Run(_page(body=f'<span itemprop="price" content="{_attr(v)}">x</span>')),
        30,
    ),
    "shared_client_error": (lambda v: _Run(shared_error=v), 80),
    "fresh_client_error": (lambda v: _Run(fresh_error=v), 60),
    "ld_parse_error": (
        lambda v: _Run(_page(_ld_json({"@type": "Product"})), loads_error=v),
        40,
    ),
    "scraper_error": (
        lambda v: _Run(scrape=SimpleNamespace(name="Kettle", price=None, error=v)),
        40,
    ),
    "scraper_price": (
        lambda v: _Run(scrape=SimpleNamespace(name="Kettle", price=v, error=None)),
        40,
    ),
}


def _assert_plain(report: str, value: str, limit: int) -> None:
    assert _is_valid_telegram_markup(report), report
    assert set(_TAG.findall(report)) <= {"<b>", "</b>"}, report
    assert value[:limit] in html.unescape(report), report


@pytest.mark.parametrize("site", sorted(_SITES))
@pytest.mark.parametrize("value", _HOSTILE)
@pytest.mark.asyncio
async def test_hostile_value_is_shown_as_plain_text(site: str, value: str) -> None:
    build, limit = _SITES[site]
    _assert_plain(await build(value).report(), value, limit)


@pytest.mark.parametrize("site", sorted(_SITES))
@pytest.mark.asyncio
async def test_value_is_cut_before_it_is_escaped(site: str) -> None:
    """Escaping first and cutting after would split an entity such as ``&amp;``."""
    build, limit = _SITES[site]
    value = "&<" * 50
    report = await build(value).report()
    _assert_plain(report, value, limit)
    assert value[: limit + 1] not in html.unescape(report)


@pytest.mark.parametrize("site", sorted(_SITES))
@pytest.mark.asyncio
async def test_huge_value_keeps_the_report_within_the_message_limit(site: str) -> None:
    build, limit = _SITES[site]
    value = "x" * 5000
    report = await build(value).report()
    _assert_plain(report, value, limit)
    assert visible_length(report) <= 4096


_NOT_WELL_FORMED = {
    "type_list": _page(_ld_json({"@type": ["<b>", "Product"], "offers": {}})),
    "type_dict": _page(_ld_json({"@type": {"<i>": "&"}, "offers": {}})),
    "type_int": _page(_ld_json({"@type": 7, "offers": {}})),
    "type_null": _page(_ld_json({"@type": None, "offers": {}})),
    "offers_string": _page(_ld_json({"@type": "Product", "offers": "<b>&"})),
    "offers_int": _page(_ld_json({"@type": "Product", "offers": 3})),
    "offers_list_of_strings": _page(_ld_json({"@type": "Product", "offers": ["<b>", "&"]})),
    "offers_null": _page(_ld_json({"@type": "Product", "offers": None})),
    "root_list": _page(_ld_json([{"@type": "<b>"}])),
    "price_nested_dict": _page(
        _ld_json({"@type": "Product", "offers": {"price": {"<b>": "&"}, "priceCurrency": []}})
    ),
    "meta_without_content": _page('<meta property="og:price:amount">'),
    "itemprop_without_content": _page(body='<span itemprop="price">&lt;b&gt;&amp;</span>'),
    "ld_empty": _page('<script type="application/ld+json"></script>'),
    "ld_not_json": _page('<script type="application/ld+json">{&lt;b&gt;</script>'),
}


@pytest.mark.parametrize("case", sorted(_NOT_WELL_FORMED))
@pytest.mark.asyncio
async def test_not_well_formed_page_never_breaks_the_report(case: str) -> None:
    report = await _Run(_NOT_WELL_FORMED[case]).report()
    assert _is_valid_telegram_markup(report), report
    assert set(_TAG.findall(report)) <= {"<b>", "</b>"}, report


_TEXT = st.text(alphabet=st.characters(blacklist_categories=("Cs", "Cc")), min_size=1)


@settings(max_examples=25, deadline=None)
@given(site=st.sampled_from(sorted(_SITES)), value=_TEXT)
def test_any_text_in_any_field_is_shown_as_plain_text(site: str, value: str) -> None:
    build, limit = _SITES[site]
    report = asyncio.run(build(value).report())
    assert _is_valid_telegram_markup(report), report
    assert set(_TAG.findall(report)) <= {"<b>", "</b>"}, report
    shown = html.unescape(report)
    if site in {"og_price_meta", "product_price_meta", "itemprop_price"}:
        # The HTML parser may normalise attribute whitespace; the rest must be literal.
        assert value[:limit].strip() in shown or not value[:limit].strip(), report
    else:
        assert value[:limit] in shown, report


@pytest.mark.asyncio
async def test_health_report_shows_domain_and_block_reason_as_plain_text() -> None:
    hostile = '<b>&"'
    now = datetime.now(UTC)
    record = ScraperHealth(
        domain=hostile,
        state="LOCKED_T1",
        consecutive_blocks=3,
        locked_until=now + timedelta(hours=1),
        last_block_at=now,
        last_block_reason=f"{hostile} reason",
    )
    db = MagicMock()
    db.is_user_admin = AsyncMock(return_value=True)
    db.is_user_allowed = AsyncMock(return_value=True)
    db.get_user = AsyncMock(return_value=None)
    manager = HealthManager(repo=MagicMock())
    probing = ScraperHealth(domain="<i>&x", state="HALF_OPEN_T1", consecutive_blocks=1)
    manager._records = {record.domain: record, probing.domain: probing}
    context = MagicMock()
    context.bot_data = {"db": db, "health_manager": manager}
    update = MagicMock()
    update.effective_user.id = 1
    update.effective_user.language_code = "en"
    update.message.reply_html = AsyncMock()

    await health_command(update, context)

    report = str(update.message.reply_html.call_args.args[0])
    assert _is_valid_telegram_markup(report), report
    assert set(_TAG.findall(report)) <= {"<b>", "</b>"}, report
    shown = html.unescape(report)
    assert f"• {hostile} — T1" in shown
    assert f"• {hostile} — {hostile} reason —" in shown
    assert "• <i>&x — probing on next tick" in shown
