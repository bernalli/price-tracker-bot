"""Build a pseudo-catalog for a locale without a real translation.

Every msgid found under a source tree gets ``[msgid msgid msgid]`` as its
translation (triplicated, bracketed), so a snapshot rendered with it is
visibly different from the English source while still round-tripping every
placeholder — enough to prove a locale is wired up without hand-writing
translations for it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from babel.messages.catalog import Catalog, Message
from babel.messages.extract import extract_from_dir
from babel.messages.mofile import write_mo as _compile_mo

if TYPE_CHECKING:
    from pathlib import Path

_KEYWORDS = {"_": None, "N_": None, "ngettext": (1, 2)}


def extract_messages(root: Path) -> list[Message]:
    """Every distinct ``_``/``N_``/``ngettext`` msgid under ``root``."""
    seen: list[Message] = []
    seen_ids: list[object] = []
    for _filename, _lineno, msgid, _comments, _context in extract_from_dir(
        root, method_map=[("**.py", "python")], keywords=_KEYWORDS
    ):
        if msgid not in seen_ids:
            seen_ids.append(msgid)
            seen.append(Message(id=msgid, string=""))
    return seen


def _pseudo(text: str) -> str:
    return f"[{text} {text} {text}]"


def write_pseudo_catalog(path: Path, locale: str, messages: list[Message]) -> None:
    """Write a compiled ``.mo`` at ``path`` translating every message pseudo-style."""
    catalog = Catalog(locale=locale)
    for message in messages:
        msg_id = message.id
        if isinstance(msg_id, str):
            catalog.add(msg_id, _pseudo(msg_id))
        else:
            singular, plural = msg_id[0], msg_id[1]
            forms = tuple(
                _pseudo(singular) if index == 0 else _pseudo(plural)
                for index in range(catalog.num_plurals)
            )
            catalog.add((singular, plural), forms)
    write_mo(path, catalog)


def write_mo(path: Path, catalog: Catalog) -> None:
    """Compile ``catalog`` to ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as handle:
        _compile_mo(handle, catalog)
