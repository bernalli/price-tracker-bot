"""Disk cache boundaries, atomic writes and non-following file operations."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from price_tracker.media.cache import ThumbnailCache
from price_tracker.media.decode import MAX_THUMB_BYTES
from tests.support.images import jpeg_bytes

JPEG = b"\xff\xd8\xffpayload"
MANAGED = re.compile(r"^[0-9a-f]{64}\.jpg$")
TEMP = re.compile(r"^\.[0-9a-f]{64}\.[0-9a-f]{16}\.tmp$")


@given(st.text(max_size=10000))
@settings(max_examples=100)
def test_url_text_never_becomes_a_path(url: str) -> None:
    root = Path("/cache-root")
    cache = ThumbnailCache(root)
    path = cache.path_for(url)
    assert path.parent == root
    assert MANAGED.fullmatch(path.name)
    assert path.name == hashlib.sha256(url.encode("utf-8")).hexdigest() + ".jpg"


@pytest.mark.parametrize("url", ["../../etc/passwd", "\0", "x" * 10000])
def test_hostile_url_text_is_hashed(tmp_path: Path, url: str) -> None:
    cache = ThumbnailCache(tmp_path)
    cache.write(url, JPEG)
    assert cache.path_for(url).parent == tmp_path
    assert cache.read(url) == JPEG


@pytest.mark.parametrize("field", ["max_bytes", "max_files"])
@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "2", None, float("inf"), float("nan")])
def test_invalid_limits_have_no_filesystem_effect(tmp_path: Path, field: str, value: Any) -> None:
    root = tmp_path / "absent"
    with pytest.raises(ValueError, match="positive integers"):
        ThumbnailCache(root, **{field: value})
    assert not root.exists()


def test_directory_and_files_are_private_and_missing_reads_are_empty(tmp_path: Path) -> None:
    root = tmp_path / "nested" / "thumbs"
    cache = ThumbnailCache(root)
    assert cache.read("missing") is None
    assert not cache.has("missing")
    cache.ensure_dir()
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    cache.write("url", JPEG)
    assert stat.S_IMODE(cache.path_for("url").stat().st_mode) == 0o600
    assert cache.has("url")
    assert cache.read("url") == JPEG


def test_ensure_dir_propagates_errors(tmp_path: Path) -> None:
    root = tmp_path / "file"
    root.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        ThumbnailCache(root).ensure_dir()
    assert root.read_bytes() == b"keep"


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"not jpeg",
        b"\xff\xd8",
        b"\xff\xd8\xff" + b"x" * MAX_THUMB_BYTES,
        None,
        "jpeg",
        bytearray(JPEG),
    ],
    ids=["empty", "not-jpeg", "two-byte-soi", "over-cap", "none", "str", "bytearray"],
)
def test_invalid_bytes_are_refused_before_creating_files(tmp_path: Path, data: Any) -> None:
    root = tmp_path / "absent"
    cache = ThumbnailCache(root)
    with pytest.raises(ValueError, match="invalid thumbnail bytes"):
        cache.write("url", data)
    assert not root.exists()


def test_a_file_cannot_exceed_the_whole_cache_budget(tmp_path: Path) -> None:
    cache = ThumbnailCache(tmp_path, max_bytes=len(JPEG) - 1)
    with pytest.raises(ValueError, match="invalid thumbnail bytes"):
        cache.write("url", JPEG)
    assert list(tmp_path.iterdir()) == []
    cache = ThumbnailCache(tmp_path, max_bytes=len(JPEG), max_files=1)
    cache.write("url", JPEG)
    assert cache.read("url") == JPEG


def test_write_fsyncs_before_atomic_replacement_and_cleans_up_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = ThumbnailCache(tmp_path)
    synced = []
    real_fsync = os.fsync

    def sync(fd: int) -> None:
        assert stat.S_ISREG(os.fstat(fd).st_mode)
        real_fsync(fd)
        synced.append(True)

    def fail_replace(source: Any, destination: Any) -> None:
        assert TEMP.fullmatch(Path(source).name)
        assert stat.S_IMODE(Path(source).stat().st_mode) == 0o600
        assert synced == [True]
        raise OSError("replacement failed")

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replacement failed"):
        cache.write("url", JPEG)
    assert list(tmp_path.iterdir()) == []


def test_failed_overwrite_preserves_the_previous_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = ThumbnailCache(tmp_path)
    cache.write("url", JPEG)

    def fail(*args: Any) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="disk failure"):
        cache.write("url", JPEG + b"new")
    assert cache.read("url") == JPEG
    assert list(tmp_path.iterdir()) == [cache.path_for("url")]


@pytest.mark.parametrize(
    "data",
    [b"bad", b"", b"\xff\xd8\xff" + b"x" * MAX_THUMB_BYTES],
    ids=["bad", "empty", "over-cap"],
)
def test_corrupt_or_oversized_files_are_removed(tmp_path: Path, data: bytes) -> None:
    cache = ThumbnailCache(tmp_path)
    path = cache.path_for("url")
    path.write_bytes(data)
    assert cache.read("url") is None
    assert not path.exists()


def test_huge_file_is_not_read_beyond_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = ThumbnailCache(tmp_path)
    path = cache.path_for("url")
    with path.open("wb") as output:
        output.write(b"\xff\xd8\xff")
        output.truncate(100_000_000)
    real_read = os.read
    requested = []

    def read(fd: int, size: int) -> bytes:
        requested.append(size)
        assert size <= MAX_THUMB_BYTES + 1
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", read)
    assert cache.read("url") is None
    assert sum(requested) <= MAX_THUMB_BYTES + 1
    assert not path.exists()


def test_truncated_jpeg_is_returned_for_the_renderer_to_validate(tmp_path: Path) -> None:
    data = jpeg_bytes(320, 320)[:20]
    cache = ThumbnailCache(tmp_path)
    cache.write("url", data)
    assert cache.read("url") == data


def test_symlinks_and_directories_are_never_followed_replaced_or_removed(tmp_path: Path) -> None:
    root = tmp_path / "cache"
    root.mkdir()
    target = tmp_path / "target"
    target.write_bytes(JPEG)
    cache = ThumbnailCache(root)
    link = cache.path_for("link")
    link.symlink_to(target)
    directory = cache.path_for("directory")
    directory.mkdir()
    assert not cache.has("link")
    assert not cache.has("directory")
    assert cache.read("link") is None
    assert cache.read("directory") is None
    with pytest.raises(OSError, match="symlink"):
        cache.write("link", JPEG + b"new")
    assert cache.prune([]) == 0
    assert link.is_symlink()
    assert directory.is_dir()
    assert target.read_bytes() == JPEG


def test_prune_only_removes_unreferenced_managed_files_and_old_temporary_files(
    tmp_path: Path,
) -> None:
    cache = ThumbnailCache(tmp_path, clock=lambda: 10000)
    for url in ("kept", "removed"):
        cache.write(url, JPEG)
    old = tmp_path / ("." + "a" * 64 + "." + "b" * 16 + ".tmp")
    fresh = tmp_path / ("." + "c" * 64 + "." + "d" * 16 + ".tmp")
    boundary = tmp_path / ("." + "e" * 64 + "." + "f" * 16 + ".tmp")
    for path, mtime in ((old, 6399), (fresh, 9999), (boundary, 6400)):
        path.write_bytes(b"partial")
        os.utime(path, (mtime, mtime))
    unrelated = [tmp_path / name for name in ("README", "x.jpg", "A" * 64 + ".jpg")]
    for path in unrelated:
        path.write_bytes(b"keep")
    (tmp_path / "subdir").mkdir()
    old_link = tmp_path / ("." + "1" * 64 + "." + "2" * 16 + ".tmp")
    old_link.symlink_to(unrelated[0])
    os.utime(old_link, (1, 1), follow_symlinks=False)
    assert cache.prune(iter(["kept"])) == 2
    assert cache.has("kept")
    assert not cache.has("removed")
    assert not old.exists()
    assert fresh.exists()
    assert boundary.exists()
    assert old_link.is_symlink()
    assert all(path.read_bytes() == b"keep" for path in unrelated)
    assert (tmp_path / "subdir").is_dir()


def test_startup_prune_enforces_limits_even_for_referenced_files(tmp_path: Path) -> None:
    cache = ThumbnailCache(tmp_path, max_files=2, max_bytes=2 * len(JPEG))
    for n in range(5):
        path = cache.path_for(str(n))
        path.write_bytes(JPEG)
        os.utime(path, (n, n))
    assert cache.prune(map(str, range(5))) == 3
    assert {path.name for path in tmp_path.iterdir()} == {
        cache.path_for("3").name,
        cache.path_for("4").name,
    }


@given(
    st.lists(st.tuples(st.integers(3, 150), st.integers(0, 2**32)), max_size=25),
    st.integers(1, 10),
    st.integers(20, 600),
)
@settings(max_examples=50, deadline=None)
def test_random_sizes_and_mtimes_stay_within_both_limits(
    entries: list[tuple[int, int]], max_files: int, max_bytes: int
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cache = ThumbnailCache(root, max_files=max_files, max_bytes=max_bytes)
        for index, (size, mtime) in enumerate(entries):
            path = cache.path_for(str(index))
            path.write_bytes(b"\xff\xd8\xff" + b"x" * (size - 3))
            os.utime(path, (mtime, mtime))
        cache.write("new", JPEG)
        paths = list(root.iterdir())
        assert len(paths) <= max_files
        assert sum(path.stat().st_size for path in paths) <= max_bytes
        assert cache.read("new") == JPEG


def test_writes_and_prune_share_one_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = ThumbnailCache(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    pruned = threading.Event()
    real_replace = os.replace
    errors = []

    def held_replace(*args: Any) -> None:
        entered.set()
        assert release.wait(5)
        real_replace(*args)

    def write() -> None:
        try:
            cache.write("url", JPEG)
        except Exception as exc:
            errors.append(exc)

    def prune() -> None:
        cache.prune(["url"])
        pruned.set()

    monkeypatch.setattr(os, "replace", held_replace)
    writer = threading.Thread(target=write)
    cleaner = threading.Thread(target=prune)
    writer.start()
    assert entered.wait(5)
    cleaner.start()
    assert not pruned.wait(0.2)
    release.set()
    writer.join(5)
    cleaner.join(5)
    assert not writer.is_alive()
    assert not cleaner.is_alive()
    assert not errors
    assert pruned.is_set()
    assert cache.read("url") == JPEG


def test_has_never_waits_for_the_cache_lock(tmp_path: Path) -> None:
    cache = ThumbnailCache(tmp_path)
    cache.write("url", JPEG)
    result: list[bool] = []
    with cache._lock:
        worker = threading.Thread(target=lambda: result.append(cache.has("url")))
        worker.start()
        worker.join(2)
        assert not worker.is_alive()
    worker.join(5)
    assert result == [True]
