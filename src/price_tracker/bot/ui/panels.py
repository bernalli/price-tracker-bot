"""The Home screen and the settings panels: pure screens built from a view.

The values with presets (mute, digest, quiet hours and language) get a button per
preset; every section shows its current value and leads back to Settings and Home.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Final

from price_tracker.bot.callbacks import MUTE_PRESETS, Action, encode
from price_tracker.bot.commands import COMMANDS, GROUP_TITLES, GROUPS, HELP_HEADER
from price_tracker.bot.messages import _, current_locale, ngettext
from price_tracker.bot.ui.escape import escape_html
from price_tracker.bot.ui.labels import button, layout_rows
from price_tracker.bot.ui.screens import Button, Screen
from price_tracker.bot.ui.width import sanitize_label, truncate_to_width
from price_tracker.core.textlimits import NAME_BUDGET
from price_tracker.i18n.format import duration, when
from price_tracker.i18n.locales import AVAILABLE_LANGUAGES, endonym

if TYPE_CHECKING:
    from collections.abc import Callable

    from price_tracker.app.views import HomeView, PrefsView

# Wire value of the quiet-hours preset -> the HH:MM window it stands for.
QUIET_WINDOWS: Final = {"2208": ("22:00", "08:00")}


def _mute_ends_at(view: PrefsView, now: datetime) -> datetime | None:
    """The end of a running timed mute; ``None`` when muted forever or not muted."""
    if view.mute and view.mute_until is not None and view.mute_until > now:
        return view.mute_until
    return None


def _muted_forever(view: PrefsView) -> bool:
    return view.mute and view.mute_until is None


def _mute_value(view: PrefsView, now: datetime, loc: str) -> str:
    ends_at = _mute_ends_at(view, now)
    if ends_at is not None:
        return _("until {when}").format(when=when(ends_at, tz=view.timezone, locale=loc))
    return _("forever") if _muted_forever(view) else _("off")


def _digest_value(view: PrefsView, loc: str) -> str:
    if not view.digest_mode:
        return _("off")
    interval = duration(view.digest_interval_minutes, locale=loc)
    return _("on, every {interval}").format(interval=interval)


def _quiet_window(view: PrefsView) -> tuple[str, str] | None:
    if view.quiet_hours_start is None or view.quiet_hours_end is None:
        return None
    return view.quiet_hours_start, view.quiet_hours_end


def _quiet_value(view: PrefsView) -> str:
    window = _quiet_window(view)
    if window is None:
        return _("off")
    return f"{escape_html(window[0])}–{escape_html(window[1])}"


def _throttle_value(view: PrefsView) -> str:
    if view.throttle_per_hour is None:
        return _("unlimited")
    return _("{n} per hour").format(n=view.throttle_per_hour)


def _language_value(language: str | None) -> str:
    if language is not None and language in AVAILABLE_LANGUAGES:
        return endonym(language)
    # Automatic: name the language the replies are in right now.
    active = endonym(current_locale().partition("_")[0])
    return _("Automatic ({language})").format(language=active)


def home_button() -> Button:
    """The button that returns to the Home screen."""
    return button(_("🏠 Home"), callback=encode(Action("home")))


def add_button() -> Button:
    """The button that explains how to add a product."""
    return button(_("➕ Add"), callback=encode(Action("add")))


def add_screen() -> Screen:
    """How to add a product: paste its link. Back to the product list, or Home."""
    back = button(_("⬅️ List"), callback=encode(Action("list.page", ("a", 1))))
    text = _(
        "➕ <b>Add a product</b>\n\nPaste the link of a product page here to start tracking it."
    )
    return Screen(text=text, rows=layout_rows([back, home_button()]))


def _preset(label: str, action: Action, *, current: bool) -> Button:
    return button(f"{label} ✓" if current else label, callback=encode(action))


def _check_now(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError(f"now: must be an aware datetime, got {now!r}")


def _value_lines(view: PrefsView, now: datetime, loc: str) -> list[str]:
    """Mute, digest, quiet hours, throttle and time zone, one line each."""
    return [
        _("🔕 Mute: {mute}").format(mute=_mute_value(view, now, loc)),
        _("📬 Digest: {digest}").format(digest=_digest_value(view, loc)),
        _("🌙 Quiet hours: {quiet}").format(quiet=_quiet_value(view)),
        _("🚦 Throttle: {throttle}").format(throttle=_throttle_value(view)),
        _("🌍 Timezone: {timezone}").format(timezone=escape_html(view.timezone)),
    ]


def settings_screen(view: PrefsView, *, now: datetime, language: str | None = None) -> Screen:
    """The settings overview. Pure: the caller supplies the clock and the stored language.

    ``language`` is the stored choice; ``None`` (or a code without a catalogue) is Automatic.
    """
    _check_now(now)
    loc = current_locale()
    lines = [
        _("⚙️ <b>Settings</b>"),
        "",
        *_value_lines(view, now, loc),
        _("🗣 Language: {language}").format(language=_language_value(language)),
    ]
    sections = [
        button(_("🔕 Mute"), callback=encode(Action("settings.section", ("mu",)))),
        button(_("📬 Digest"), callback=encode(Action("settings.section", ("dg",)))),
        button(_("🌙 Quiet hours"), callback=encode(Action("settings.section", ("qh",)))),
        button(_("🚦 Throttle"), callback=encode(Action("settings.section", ("th",)))),
        button(_("🌍 Timezone"), callback=encode(Action("settings.section", ("tz",)))),
        button(_("🗣 Language"), callback=encode(Action("settings.section", ("lang",)))),
    ]
    return Screen(text="\n".join(lines), rows=layout_rows(sections, [home_button()]))


def _mute_presets(
    view: PrefsView, now: datetime, loc: str, make: Callable[[str], Action]
) -> list[Button]:
    """The mute presets; ``make`` turns a preset value into the action that sets it."""
    off = _mute_ends_at(view, now) is None and not _muted_forever(view)
    presets = [
        _preset(duration(int(value) * 60, locale=loc), make(value), current=False)
        for value in MUTE_PRESETS
        if value != "0"
    ]
    presets.append(_preset(_("Forever"), make("0"), current=_muted_forever(view)))
    presets.append(_preset(_("🔔 Unmute"), make("off"), current=off))
    return presets


def _settings_mute(value: str) -> Action:
    return Action("settings.mute", (value,))


def _ask(label: str, setting: str) -> Button:
    """The button that asks for a typed value of ``setting``."""
    return button(label, callback=encode(Action("settings.ask", (setting,))))


def _digest_presets(view: PrefsView) -> list[Button]:
    return [
        _preset(_("On"), Action("settings.digest", ("on",)), current=view.digest_mode),
        _preset(_("Off"), Action("settings.digest", ("off",)), current=not view.digest_mode),
    ]


def _quiet_presets(view: PrefsView) -> list[Button]:
    window = _quiet_window(view)
    presets = [
        _preset(f"{start}–{end}", Action("settings.quiet", (wire,)), current=window == (start, end))
        for wire, (start, end) in QUIET_WINDOWS.items()
    ]
    presets.append(_preset(_("Off"), Action("settings.quiet", ("off",)), current=window is None))
    return presets


def _language_presets(language: str | None) -> list[Button]:
    automatic = language not in AVAILABLE_LANGUAGES
    presets = [_preset(_("Automatic"), Action("settings.language", ("auto",)), current=automatic)]
    presets += [
        _preset(endonym(code), Action("settings.language", (code,)), current=code == language)
        for code in AVAILABLE_LANGUAGES
    ]
    return presets


def settings_section_screen(
    section: str, view: PrefsView, *, now: datetime, language: str | None = None
) -> Screen:
    """One preference's presets, the current one marked.

    ``section``: ``mu``, ``dg``, ``qh``, ``lang``, ``tz`` or ``th``; ``language`` is the
    stored choice.
    """
    _check_now(now)
    loc = current_locale()
    extra: list[Button] = []
    presets: list[Button] = []
    if section == "mu":
        title, value = _("🔕 <b>Mute</b>"), _mute_value(view, now, loc)
        presets = _mute_presets(view, now, loc, _settings_mute)
        extra = [_ask(_("✏️ Other duration"), "mu")]
    elif section == "dg":
        title, value = _("📬 <b>Digest</b>"), _digest_value(view, loc)
        presets = _digest_presets(view)
        extra = [
            _ask(_("✏️ Interval"), "dg"),
            button(_("📨 Send now"), callback=encode(Action("settings.digest_now"))),
        ]
    elif section == "qh":
        title, value = _("🌙 <b>Quiet hours</b>"), _quiet_value(view)
        presets = _quiet_presets(view)
        extra = [_ask(_("✏️ Other hours"), "qh")]
    elif section == "lang":
        title, value = _("🗣 <b>Language</b>"), _language_value(language)
        presets = _language_presets(language)
    elif section == "tz":
        title, value = _("🌍 <b>Timezone</b>"), escape_html(view.timezone)
        extra = [_ask(_("✏️ Change"), "tz")]
    elif section == "th":
        title, value = _("🚦 <b>Throttle</b>"), _throttle_value(view)
        extra = [_ask(_("✏️ Change"), "th")]
    else:
        raise ValueError(f"section: must be one of mu, dg, qh, lang, tz, th, got {section!r}")
    back = button(_("⬅️ Settings"), callback=encode(Action("settings")))
    current = _("Current: {value}").format(value=value)
    return Screen(
        text=f"{title}\n\n{current}", rows=layout_rows(presets, extra, [back, home_button()])
    )


def product_prefs_screen(name: str, product_id: int, view: PrefsView, *, now: datetime) -> Screen:
    """The notifications of one product: its effective values and its mute presets.

    ``view`` holds the preferences resolved for this product; ``name`` is shown escaped.
    """
    _check_now(now)
    if not isinstance(product_id, int) or isinstance(product_id, bool) or product_id < 1:
        raise ValueError(f"product_id: must be a positive int, got {product_id!r}")
    loc = current_locale()
    shown = escape_html(truncate_to_width(sanitize_label(name), NAME_BUDGET))
    lines = [
        _("🔔 <b>Notifications</b>"),
        _("📦 <b>{name}</b>").format(name=f"\u2068{shown}\u2069"),
        "",
        *_value_lines(view, now, loc),
    ]

    def mute(value: str) -> Action:
        return Action("product.mute", (product_id, value))

    presets = _mute_presets(view, now, loc, mute)
    other = button(
        _("✏️ Other duration"), callback=encode(Action("product.mute_ask", (product_id,)))
    )
    links = [
        button(_("⚙️ Settings"), callback=encode(Action("settings"))),
        button(_("⬅️ Product"), callback=encode(Action("product.card", (product_id,)))),
    ]
    return Screen(text="\n".join(lines), rows=layout_rows(presets, [other], links, [home_button()]))


def home_screen(view: HomeView) -> Screen:
    """The Home screen: counts, and a way into every area."""
    counts = _("📦 {active} · ⏸ {paused}").format(
        # The English adjective does not change; other languages inflect it.
        active=ngettext("{n} active", "{n} active", view.active).format(n=view.active),
        paused=_("{n} paused").format(n=view.paused),
    )
    text = _(
        "🏠 <b>Price Tracker</b>\n\n{counts}\n\nPaste a product link to start tracking."
    ).format(counts=counts)
    areas = [
        button(_("📦 Products"), callback=encode(Action("list.page", ("a", 1)))),
        button(_("💶 Prices"), callback=encode(Action("prices"))),
        button(_("🔔 Notifications"), callback=encode(Action("notifications"))),
        button(_("💾 Data"), callback=encode(Action("data"))),
        button(_("ℹ️ Status & info"), callback=encode(Action("stats"))),
        button(_("⚙️ Settings"), callback=encode(Action("settings"))),
    ]
    groups = [areas]
    if view.is_admin:
        groups.append([button(_("👑 Admin"), callback=encode(Action("admin")))])
    return Screen(text=text, rows=layout_rows(*groups))


def help_screen(is_admin: bool) -> Screen:
    """Every command by area, the admin area only for administrators."""
    lines = [_(HELP_HEADER)]
    for group in GROUPS:
        if group == "admin" and not is_admin:
            continue
        lines += ["", f"<b>{_(GROUP_TITLES[group])}</b>"]
        lines += [
            f"/{spec.name} — {_(spec.description)}" for spec in COMMANDS if spec.group == group
        ]
    return Screen(text="\n".join(lines), rows=layout_rows([home_button()]))
