"""Smoke test — verifies the package imports and version is set."""

from price_tracker import __version__


def test_version_is_set():
    assert __version__ == "0.1.0.dev0"


def test_version_is_dev_pre_release():
    assert "dev" in __version__
