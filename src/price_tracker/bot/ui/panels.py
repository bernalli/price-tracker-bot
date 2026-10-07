"""The Home screen and the settings panels: pure screens built from a view.

Only the values the code already has presets for get buttons (mute, digest,
quiet hours and language); timezone and throttle are shown with the command that
changes them.
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
from price_tracker.i18n.format import duration, when
from price_tracker.i18n.locales import AVAILABLE_LANGUAGES, endonym

if TYPE_CHECKING:
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


def _preset(label: str, action: Action, *, current: bool) -> Button:
    return button(f"{label} ✓" if current else label, callback=encode(action))


def _check_now(now: datetime) -> None:
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError(f"now: must be an aware datetime, got {now!r}")


def settings_screen(view: PrefsView, *, now: datetime, language: str | None = None) -> Screen:
    """The settings overview. Pure: the caller supplies the clock and the stored language.

    ``language`` is the stored choice; ``None`` (or a code without a catalogue) is Automatic.
    """
    _check_now(now)
    loc = current_locale()
    lines = [
        _("⚙️ <b>Settings</b>"),
        "",
        _("🔕 Mute: {mute}").format(mute=_mute_value(view, now, loc)),
        _("📬 Digest: {digest}").format(digest=_digest_value(view, loc)),
        _("🌙 Quiet hours: {quiet}").format(quiet=_quiet_value(view)),
        _("⏱ Throttle: {throttle} · change with /throttle &lt;N&gt;|off").format(
            throttle=_throttle_value(view)
        ),
        _("🌍 Timezone: {timezone} · change with /timezone &lt;zone&gt;").format(
            timezone=escape_html(view.timezone)
        ),
        _("🗣 Language: {language}").format(language=_language_value(language)),
        "",
        _("Other values: /digest_mode on|off &lt;minutes&gt;, /quiet_hours HH:MM-HH:MM"),
    ]
    sections = [
        button(_("🔕 Mute"), callback=encode(Action("settings.section", ("mu",)))),
        button(_("📬 Digest"), callback=encode(Action("settings.section", ("dg",)))),
        button(_("🌙 Quiet hours"), callback=encode(Action("settings.section", ("qh",)))),
        button(_("🗣 Language"), callback=encode(Action("settings.section", ("lang",)))),
    ]
    return Screen(text="\n".join(lines), rows=layout_rows(sections, [home_button()]))


def _mute_presets(view: PrefsView, now: datetime, loc: str) -> list[Button]:
    off = _mute_ends_at(view, now) is None and not _muted_forever(view)
    presets = [
        _preset(
            duration(hours * 60, locale=loc),
            Action("settings.mute", (str(hours),)),
            current=False,
        )
        for value in MUTE_PRESETS
        if value != "0"
        for hours in (int(value),)
    ]
    presets.append(
        _preset(_("Forever"), Action("settings.mute", ("0",)), current=_muted_forever(view))
    )
    presets.append(_preset(_("🔔 Unmute"), Action("settings.mute", ("off",)), current=off))
    return presets


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

    ``section``: ``mu``, ``dg``, ``qh`` or ``lang``; ``language`` is the stored choice.
    """
    _check_now(now)
    loc = current_locale()
    if section == "mu":
        title, value = _("🔕 <b>Mute</b>"), _mute_value(view, now, loc)
        presets = _mute_presets(view, now, loc)
    elif section == "dg":
        title, value = _("📬 <b>Digest</b>"), _digest_value(view, loc)
        presets = _digest_presets(view)
    elif section == "qh":
        title, value = _("🌙 <b>Quiet hours</b>"), _quiet_value(view)
        presets = _quiet_presets(view)
    elif section == "lang":
        title, value = _("🗣 <b>Language</b>"), _language_value(language)
        presets = _language_presets(language)
    else:
        raise ValueError(f"section: must be one of mu, dg, qh, lang, got {section!r}")
    back = button(_("◀️ Settings"), callback=encode(Action("settings")))
    current = _("Current: {value}").format(value=value)
    return Screen(text=f"{title}\n\n{current}", rows=layout_rows(presets, [back]))


def home_screen(view: HomeView) -> Screen:
    """The Home screen: counts, and a way into every area. Legacy buttons stay for the areas
    that have no screen of their own yet."""
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
        button(_("🔍 Check all"), callback="menu_checkall"),
        button(_("📊 Statistics"), callback="menu_info"),
        button(_("💾 Data"), callback="menu_dati"),
    ]
    settings = button(_("⚙️ Settings"), callback=encode(Action("settings")))
    groups = [areas, [settings]]
    if view.is_admin:
        groups.append([button(_("👑 Admin"), callback="menu_admin")])
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
