"""User and notification-preference operations for the SQLite repository."""

from __future__ import annotations

from price_tracker.db.models import NotificationPrefs, UserRecord
from price_tracker.db.repository_common import (
    _TELEGRAM_TAG_RE,
    _USER_COLS,
    _is_one,
    _parse_ts,
    _RepositoryBase,
    _row_to_user,
)
from price_tracker.i18n.locales import AVAILABLE_LANGUAGES


class UserRepositoryMixin(_RepositoryBase):
    async def ensure_user(self, user_id: int, *, is_admin: bool = False) -> None:
        await self._conn.execute(
            "INSERT INTO users(user_id, is_admin) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET is_admin = excluded.is_admin",
            (user_id, 1 if is_admin else 0),
        )
        await self._conn.commit()

    async def is_user_allowed(self, user_id: int) -> bool:
        cursor = await self._conn.execute(
            "SELECT is_active FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row is not None and _is_one(row[0])

    async def get_user(self, user_id: int) -> UserRecord | None:
        cursor = await self._conn.execute(
            f"SELECT {_USER_COLS} FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return _row_to_user(row) if row else None

    async def update_user_info(
        self,
        user_id: int,
        *,
        display_name: str | None = None,
        username: str | None = None,
    ) -> None:
        await self._conn.execute(
            "UPDATE users SET display_name = COALESCE(?, display_name), "
            "username = COALESCE(?, username) WHERE user_id = ?",
            (display_name, username, user_id),
        )
        await self._conn.commit()

    async def set_admin(self, user_id: int, is_admin: bool) -> None:
        await self._conn.execute(
            "UPDATE users SET is_admin = ? WHERE user_id = ?",
            (1 if is_admin else 0, user_id),
        )
        await self._conn.commit()

    async def remove_user(self, user_id: int) -> bool:
        """Deactivate a user; True only if an active user was deactivated by this call."""
        cursor = await self._conn.execute(
            "UPDATE users SET is_active = 0 WHERE user_id = ? AND is_active = 1", (user_id,)
        )
        await self._conn.commit()
        return int(cursor.rowcount) > 0

    async def list_users(self) -> list[UserRecord]:
        cursor = await self._conn.execute(f"SELECT {_USER_COLS} FROM users")
        return [_row_to_user(r) for r in await cursor.fetchall()]

    async def list_active_users(self) -> list[UserRecord]:
        cursor = await self._conn.execute(f"SELECT {_USER_COLS} FROM users WHERE is_active = 1")
        return [_row_to_user(r) for r in await cursor.fetchall()]

    async def set_user_language(self, user_id: int, code: str | None) -> bool:
        """Store the chosen interface language (``None`` = automatic).

        Raises ``ValueError`` for a code without a catalogue. Returns False when the user
        does not exist.
        """
        if code is not None and code not in AVAILABLE_LANGUAGES:
            raise ValueError(f"unsupported language: {code!r}")
        cursor = await self._conn.execute(
            "UPDATE users SET language = ? WHERE user_id = ?", (code, user_id)
        )
        await self._conn.commit()
        return int(cursor.rowcount) > 0

    async def set_user_telegram_tag(self, user_id: int, tag: object) -> bool:
        """Record the IETF tag Telegram last reported for the user, verbatim.

        Anything that is not a well-formed tag is ignored and reported as False, as is an
        unknown user.
        """
        if not isinstance(tag, str) or _TELEGRAM_TAG_RE.fullmatch(tag) is None:
            return False
        cursor = await self._conn.execute(
            "UPDATE users SET telegram_language_tag = ? WHERE user_id = ?", (tag, user_id)
        )
        await self._conn.commit()
        return int(cursor.rowcount) > 0

    async def ensure_admin_users(self, user_ids: tuple[int, ...]) -> None:
        for uid in user_ids:
            await self.ensure_user(user_id=uid, is_admin=True)

    async def create_user(self, *, user_id: int) -> None:
        """Create a user if it does not exist. Test/admin helper."""
        await self.ensure_user(user_id=user_id)

    async def get_notification_prefs(
        self, *, user_id: int, product_id: int | None
    ) -> NotificationPrefs | None:
        if product_id is None:
            cursor = await self._conn.execute(
                "SELECT user_id, product_id, mute, mute_until, digest_mode, "
                "digest_interval_minutes, quiet_hours_start, quiet_hours_end, "
                "throttle_per_hour, timezone, throttle_state_json, updated_at "
                "FROM notification_prefs WHERE user_id = ? AND product_id IS NULL",
                (user_id,),
            )
        else:
            cursor = await self._conn.execute(
                "SELECT user_id, product_id, mute, mute_until, digest_mode, "
                "digest_interval_minutes, quiet_hours_start, quiet_hours_end, "
                "throttle_per_hour, timezone, throttle_state_json, updated_at "
                "FROM notification_prefs WHERE user_id = ? AND product_id = ?",
                (user_id, product_id),
            )
        row = await cursor.fetchone()
        if row is None:
            return None
        return NotificationPrefs(
            user_id=row[0],
            product_id=row[1],
            mute=bool(row[2]),
            mute_until=_parse_ts(row[3]),
            digest_mode=bool(row[4]),
            digest_interval_minutes=row[5],
            quiet_hours_start=row[6],
            quiet_hours_end=row[7],
            throttle_per_hour=row[8],
            timezone=row[9],
            throttle_state_json=row[10],
            updated_at=_parse_ts(row[11]),
        )

    async def upsert_notification_prefs(self, prefs: NotificationPrefs) -> None:
        if prefs.product_id is None:
            # The (user_id, product_id) PK never fires for NULL product_id
            # because SQLite treats NULLs as distinct; the partial unique
            # index ux_notification_prefs_global (migration 011) makes a
            # single atomic upsert possible — the previous SELECT-then-INSERT
            # emulation raced under concurrency and duplicated rows (#58).
            await self._conn.execute(
                """
                INSERT INTO notification_prefs (
                    user_id, product_id, mute, mute_until, digest_mode,
                    digest_interval_minutes, quiet_hours_start, quiet_hours_end,
                    throttle_per_hour, timezone, throttle_state_json
                )
                VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) WHERE product_id IS NULL DO UPDATE SET
                    mute = excluded.mute,
                    mute_until = excluded.mute_until,
                    digest_mode = excluded.digest_mode,
                    digest_interval_minutes = excluded.digest_interval_minutes,
                    quiet_hours_start = excluded.quiet_hours_start,
                    quiet_hours_end = excluded.quiet_hours_end,
                    throttle_per_hour = excluded.throttle_per_hour,
                    timezone = excluded.timezone,
                    throttle_state_json = excluded.throttle_state_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    prefs.user_id,
                    int(prefs.mute),
                    prefs.mute_until.isoformat() if prefs.mute_until else None,
                    int(prefs.digest_mode),
                    prefs.digest_interval_minutes,
                    prefs.quiet_hours_start,
                    prefs.quiet_hours_end,
                    prefs.throttle_per_hour,
                    prefs.timezone,
                    prefs.throttle_state_json,
                ),
            )
        else:
            await self._conn.execute(
                """
                INSERT INTO notification_prefs (
                    user_id, product_id, mute, mute_until, digest_mode,
                    digest_interval_minutes, quiet_hours_start, quiet_hours_end,
                    throttle_per_hour, timezone, throttle_state_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, product_id) DO UPDATE SET
                    mute = excluded.mute,
                    mute_until = excluded.mute_until,
                    digest_mode = excluded.digest_mode,
                    digest_interval_minutes = excluded.digest_interval_minutes,
                    quiet_hours_start = excluded.quiet_hours_start,
                    quiet_hours_end = excluded.quiet_hours_end,
                    throttle_per_hour = excluded.throttle_per_hour,
                    timezone = excluded.timezone,
                    throttle_state_json = excluded.throttle_state_json,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    prefs.user_id,
                    prefs.product_id,
                    int(prefs.mute),
                    prefs.mute_until.isoformat() if prefs.mute_until else None,
                    int(prefs.digest_mode),
                    prefs.digest_interval_minutes,
                    prefs.quiet_hours_start,
                    prefs.quiet_hours_end,
                    prefs.throttle_per_hour,
                    prefs.timezone,
                    prefs.throttle_state_json,
                ),
            )
        await self._conn.commit()

    async def is_user_admin(self, user_id: int) -> bool:
        cursor = await self._conn.execute(
            "SELECT is_admin FROM users WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row is not None and _is_one(row[0])

    async def add_user(self, user_id: int, *, is_admin: bool = False) -> None:
        """Like :meth:`ensure_user`, but also reactivates a deactivated user."""
        await self._conn.execute(
            "INSERT INTO users(user_id, is_admin, is_active) VALUES(?, ?, 1) "
            "ON CONFLICT(user_id) DO UPDATE SET is_admin = excluded.is_admin, is_active = 1",
            (user_id, 1 if is_admin else 0),
        )
        await self._conn.commit()

    async def get_all_users(self) -> list[UserRecord]:
        """Alias of :meth:`list_users`."""
        return await self.list_users()
