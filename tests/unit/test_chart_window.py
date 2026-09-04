"""The chart must show a time window, not the last N readings.

The scheduler writes one price_history row per check, whether or not the price
moved, so `LIMIT 100` is a window measured in samples: at the production tick
rate it covered about four days, and in four days almost no price moves. Ten of
twelve tracked products rendered a flat line while their 90-day history held
between two and 134 distinct prices.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

from price_tracker.bot.handlers import history

if TYPE_CHECKING:
    import io


def _history_rows(
    *, days: int, per_day: int, price_by_day: dict[int, float]
) -> list[dict[str, Any]]:
    """Readings for `days` days, newest first, mimicking the repository order.

    `price_by_day` maps a day offset (0 = oldest) to the price from that day on.
    """
    now = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
    rows: list[dict[str, Any]] = []
    price = 0.0
    for day in range(days):
        price = price_by_day.get(day, price)
        for slot in range(per_day):
            ts = now - timedelta(days=days - 1 - day) + timedelta(minutes=30 * slot)
            rows.append({"checked_at": ts.isoformat(), "price": str(price)})
    rows.sort(key=lambda r: r["checked_at"], reverse=True)
    return rows


def _fake_db(rows: list[dict[str, Any]]) -> AsyncMock:
    """A db whose chart method returns ordered, server-compressed window points."""

    async def get_price_change_points(product_id: int, *, since: str) -> list[dict[str, Any]]:
        selected = [
            {**record, "id": record.get("id", index)}
            for index, record in enumerate(rows, start=1)
            if not isinstance(record.get("checked_at"), str)
            or record["checked_at"].replace("T", " ").replace("Z", "") >= since
        ]
        selected.sort(
            key=lambda record: (
                record["checked_at"].replace("T", " ").replace("Z", "")
                if isinstance(record.get("checked_at"), str)
                else "",
                record["id"],
            )
        )
        if not selected:
            return []

        points = [selected[0]]
        for record in selected[1:]:
            if record.get("price") != points[-1].get("price"):
                points.append(record)
        if points[-1] is not selected[-1]:
            points.append(selected[-1])
        return points

    db = AsyncMock()
    db.get_price_change_points = AsyncMock(side_effect=get_price_change_points)
    return db


def _spy_on_render(monkeypatch) -> dict[str, Any]:
    captured: dict[str, Any] = {}
    real_render = history._render_chart

    def spy(dates: list[datetime], prices: list[float], target: object, name: str) -> io.BytesIO:
        captured["dates"] = dates
        captured["prices"] = prices
        return real_render(dates, prices, target, name)

    monkeypatch.setattr(history, "_render_chart", spy)
    return captured


async def test_chart_shows_price_changes_older_than_the_last_hundred_readings(
    monkeypatch,
) -> None:
    # 30 days of readings every 30 minutes: the last 100 rows cover ~2 days, in
    # which the price never moved. The three earlier changes must still show.
    rows = _history_rows(
        days=30, per_day=48, price_by_day={0: 349.0, 10: 329.0, 20: 299.0, 26: 319.0}
    )
    db = _fake_db(rows)
    captured = _spy_on_render(monkeypatch)

    buf = await history._generate_chart(db, 1, {"name": "LEGO"})

    assert buf is not None
    assert sorted(set(captured["prices"])) == [299.0, 319.0, 329.0, 349.0]


async def test_chart_series_is_chronological(monkeypatch) -> None:
    # The repository returns chronological points, including its id tie-breaker.
    # The renderer must preserve that order rather than reinterpreting the data.
    rows = _history_rows(days=10, per_day=4, price_by_day={0: 100.0, 5: 80.0})
    db = _fake_db(rows)
    captured = _spy_on_render(monkeypatch)

    await history._generate_chart(db, 1, {"name": "Widget"})

    assert captured["dates"] == sorted(captured["dates"])


async def test_unparsable_price_is_dropped_not_plotted_as_zero(monkeypatch) -> None:
    # The repository turns a non-decimal price into Decimal("0"); plotting that
    # drags the y-axis down to zero and squashes the real variation into a band.
    rows = [
        {"checked_at": "2026-09-01T10:00:00+00:00", "price": "352.00"},
        {"checked_at": "2026-09-02T10:00:00+00:00", "price": "0"},
        {"checked_at": "2026-09-03T10:00:00+00:00", "price": "345.00"},
        {"checked_at": "2026-09-04T10:00:00+00:00", "price": "349.00"},
    ]
    db = _fake_db(rows)
    captured = _spy_on_render(monkeypatch)

    await history._generate_chart(db, 1, {"name": "Sigma"})

    assert 0.0 not in captured["prices"]
    assert sorted(captured["prices"]) == [345.0, 349.0, 352.0]


async def test_repeated_readings_arrive_as_price_changes(monkeypatch) -> None:
    # The query, rather than the handler, removes per-check repeats. The handler
    # receives only corners plus the window's final point.
    rows = _history_rows(days=20, per_day=48, price_by_day={0: 50.0, 10: 40.0})
    db = _fake_db(rows)
    captured = _spy_on_render(monkeypatch)

    await history._generate_chart(db, 1, {"name": "Widget"})

    assert len(captured["prices"]) < 10
    assert sorted(set(captured["prices"])) == [40.0, 50.0]
    assert captured["prices"][0] == 50.0
    assert captured["prices"][-1] == 40.0


async def test_chart_asks_the_repository_for_a_time_window(monkeypatch) -> None:
    rows = _history_rows(days=5, per_day=8, price_by_day={0: 10.0, 3: 12.0})
    db = _fake_db(rows)
    _spy_on_render(monkeypatch)

    await history._generate_chart(db, 7, {"name": "Widget"})

    kwargs = db.get_price_change_points.await_args.kwargs
    assert kwargs.get("since") is not None, "the chart must request a time window"


async def test_chart_drops_malformed_and_non_finite_points(monkeypatch) -> None:
    rows: list[dict[str, Any]] = [
        {"checked_at": "2026-09-01T10:00:00Z", "price": "352.00"},
        {"checked_at": "2026-09-01T11:00:00Z", "price": "NaN"},
        {"checked_at": "2026-09-01T12:00:00Z", "price": "Infinity"},
        {"checked_at": "2026-09-01T13:00:00Z", "price": "0"},
        {"checked_at": "2026-09-01T14:00:00Z", "price": "-1"},
        {"checked_at": "2026-09-01T15:00:00Z"},
        {"checked_at": 123, "price": "345.00"},
        {"checked_at": "2026-09-01T16:00:00+00:00", "price": "349.00"},
    ]
    db = _fake_db(rows)
    captured = _spy_on_render(monkeypatch)

    buf = await history._generate_chart(db, 1, {"name": "Sigma"})

    assert buf is not None
    assert captured["prices"] == [352.0, 349.0]
    assert all(dt.tzinfo is UTC for dt in captured["dates"])


async def test_generate_chart_works_against_a_real_repository() -> None:
    """The handler must call the repository it is actually given.

    Every other test here hands `_generate_chart` a mock whose methods live on the
    instance. A real `Repository` keeps them on the class, and a dispatch that
    reaches for the class attribute gets an unbound function: the first argument
    becomes `self` and the call dies with a TypeError on every /history. Only a
    real repository exercises that path.
    """
    import aiosqlite

    from price_tracker.db.migrator import apply_migrations
    from price_tracker.db.repository import Repository

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await apply_migrations(conn, Path("src/price_tracker/db/migrations"))
    try:
        repo = Repository(conn)
        pid = await repo.add_product(
            user_id=1,
            url="https://example.com/p/1",
            name="Widget",
            domain="example.com",
            initial_price=Decimal("100"),
            currency="EUR",
        )
        now = datetime.now(UTC)
        for offset_days, price in ((40, "100"), (20, "90"), (1, "95")):
            await conn.execute(
                "INSERT INTO price_history (product_id, price, checked_at) VALUES (?, ?, ?)",
                (
                    pid,
                    price,
                    (now - timedelta(days=offset_days)).strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
        await conn.commit()

        buf = await history._generate_chart(repo, pid, {"name": "Widget"})

        assert buf is not None
        assert buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"
    finally:
        await conn.close()
