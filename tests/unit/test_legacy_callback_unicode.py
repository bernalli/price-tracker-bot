"""Every legacy decoder rejects all unencodable Unicode strings."""

from hypothesis import example, given, settings
from hypothesis import strategies as st

from price_tracker.bot.handlers.callbacks import _legacy


@settings(max_examples=1000)
@example(prefix="setsoglia_", before="", point=0xD800, after="")
@example(prefix="pause_", before="", point=0xDFFF, after="")
@given(
    prefix=st.sampled_from(["", *_legacy._EXACT, *_legacy._PREFIXES]),
    before=st.text(max_size=64),
    point=st.integers(min_value=0xD800, max_value=0xDFFF),
    after=st.text(max_size=64),
)
def test_unencodable_callbacks_are_rejected(
    prefix: str, before: str, point: int, after: str
) -> None:
    wire = prefix + before + chr(point) + after
    for decoder in (
        _legacy.decode_legacy,
        _legacy.decode_legacy_entry,
        _legacy.resolve_callback,
    ):
        assert decoder(wire) is None
