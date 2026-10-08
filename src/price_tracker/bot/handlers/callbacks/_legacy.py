"""Compatibility decoder for callback data sent before the action registry.

Old Telegram messages remain clickable indefinitely, so their payloads must stay
accepted even though no current keyboard emits them. All legacy string parsing is
kept here and normalized to the same :class:`Action` objects used by new buttons.
"""

from __future__ import annotations

from typing import Final

from price_tracker.bot.callbacks import ID_MAX, MAX_CALLBACK_BYTES, Action, decode

_EXACT: Final = {
    "cancel_delete": Action("delete.cancel"),
    "delete_all": Action("list.remove_all"),
    "confirmdeleteall": Action("list.remove_all_ok"),
    "cmd_lista": Action("products.command"),
    "menu_main": Action("home"),
    "menu_prodotti": Action("products"),
    "menu_paused": Action("paused"),
    "menu_prezzi": Action("prices"),
    "menu_checkall": Action("check_all"),
    "menu_storia": Action("history"),
    "menu_notifiche": Action("notifications"),
    "menu_dati": Action("data"),
    "menu_esporta": Action("data.export"),
    "menu_importa_info": Action("data.import"),
    "menu_info": Action("stats"),
    "menu_admin": Action("admin"),
    "menu_admin_users": Action("admin.users"),
    "menu_admin_adduser": Action("admin.add_user"),
    "menu_admin_removeuser": Action("admin.remove_user"),
    "menu_admin_nick": Action("admin.nick"),
    "menu_admin_interval": Action("admin.interval"),
    "menu_admin_debug": Action("admin.debug"),
}

_PREFIXES: Final = {
    "ops_delok_": ("ops.delete_ok", None),
    "track_threshold_": ("product.threshold", None),
    "confirm_delete_": ("product.remove_ok", None),
    "track_default_": ("product.threshold_default", None),
    "track_target_": ("product.target", None),
    "track_any_": ("product.threshold_any", None),
    "pref_anyseller_": ("product.offer_filter", "s0"),
    "pref_default_": ("product.offer_filter", "0"),
    "pref_amazon_": ("product.offer_filter", "s1"),
    "pref_used_": ("product.offer_filter", "u"),
    "pref_new_": ("product.offer_filter", "n"),
    "admin_nick_": ("admin.nick_id", None),
    "admin_rm_": ("admin.remove_user_id", None),
    "reactivate_": ("product.reactivate", None),
    "setrefresh_": ("product.interval", None),
    "setsoglia_": ("product.threshold", None),
    "settarget_": ("product.target", None),
    "ops_react_": ("ops.reactivate", None),
    "ops_del_": ("ops.delete", None),
    "remove_": ("product.remove", None),
    "pause_": ("product.pause", None),
    "reset_": ("product.reset", None),
    "check_": ("product.check", None),
    "chart_": ("product.chart", "all"),
    "edit_": ("product.edit", None),
}


def _id(raw: str) -> int | str:
    if raw and raw.isascii() and raw.isdigit() and raw[0] != "0" and len(raw) <= 19:
        value = int(raw)
        if value <= ID_MAX:
            return value
    return raw


def decode_legacy(data: object) -> Action | None:
    """Normalize any known legacy payload; malformed legacy ids remain routable."""
    if not isinstance(data, str) or not data:
        return None
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        return None
    exact = _EXACT.get(data)
    if exact is not None:
        return exact
    for prefix, (name, extra) in _PREFIXES.items():
        if data.startswith(prefix):
            value = _id(data[len(prefix) :])
            args: tuple[int | str, ...] = (value,) if extra is None else (value, extra)
            return Action(name, args)
    return None


_ENTRY_NAMES: Final = frozenset({"product.threshold", "product.target", "product.interval"})


def decode_legacy_entry(data: object) -> Action | None:
    """Strict subset used only by the guided-flow entry compatibility route."""
    action = decode_legacy(data)
    if action is None or action.name not in _ENTRY_NAMES:
        return None
    if len(action.args) != 1 or not isinstance(action.args[0], int):
        return None
    return action


def resolve_callback(data: object) -> Action | None:
    """Decode current registry data first, then the compatibility language."""
    if isinstance(data, Action):
        return data
    current = decode(data)
    return current if isinstance(current, Action) else decode_legacy(data)
