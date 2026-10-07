"""The list and Home buttons inside the production handler layout.

``register_handlers`` builds the real layout (guided-flow coordinator first, legacy
handlers after); a press on a registered navigation action must close an open
prompt and still reach its handler.
"""

from __future__ import annotations

from tests.integration.test_guided_flow_wired import CANCELLED, OWNER, PRIVATE, Wired, _open
from tests.integration.test_guided_flow_wired import w as w  # fixture


async def test_a_list_button_closes_an_open_prompt_and_renders_the_list(w: Wired) -> None:
    prompt = await _open(w, f"setsoglia_{w.product}")
    assert len(w.flow.registry) == 1
    before = len(w.request.calls)

    await w.press(PRIVATE, OWNER, "l:a:1")

    assert len(w.flow.registry) == 0
    assert w.edits_of(prompt) == [CANCELLED]
    edits = [c for c in w.calls_since(before) if c.method == "editMessageText"]
    pages = [c for c in edits if "Your products" in str(c.params["text"])]
    assert len(pages) == 1
    assert "Kettle" in str(pages[0].params["text"])
    assert w.errors == []


async def test_home_button_closes_an_open_prompt_and_renders_the_home(w: Wired) -> None:
    await _open(w, f"setsoglia_{w.product}")
    before = len(w.request.calls)

    await w.press(PRIVATE, OWNER, "h")

    assert len(w.flow.registry) == 0
    texts = [str(c.params["text"]) for c in w.calls_since(before) if c.method == "editMessageText"]
    assert any("Price Tracker" in text for text in texts), texts
    assert w.errors == []
