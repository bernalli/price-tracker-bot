"""Atomic, size-bounded thumbnail storage that never follows managed symlinks."""

from __future__ import annotations

import errno
import hashlib
import os
import re
import secrets
import stat
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Final

from price_tracker.media.decode import MAX_THUMB_BYTES

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

_MANAGED: Final = re.compile(r"[0-9a-f]{64}\.jpg")
_TEMPORARY: Final = re.compile(r"\.[0-9a-f]{64}\.[0-9a-f]{16}\.tmp")
_JPEG_PREFIX: Final = b"\xff\xd8\xff"


class ThumbnailCache:
    """Thumbnails on disk, one JPEG per image URL, bounded in bytes and in files."""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int = 64 * 1024 * 1024,
        max_files: int = 2000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        for value in (max_bytes, max_files):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError("cache limits must be positive integers")
        self.root = root
        self._max_bytes = max_bytes
        self._max_files = max_files
        self._clock = clock
        self._lock = threading.Lock()

    @staticmethod
    def key(url: str) -> str:
        """The image URL's UTF-8 digest, independent of path syntax."""
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def path_for(self, url: str) -> Path:
        return self.root / f"{self.key(url)}.jpg"

    def ensure_dir(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    @staticmethod
    def _regular(path: Path) -> os.stat_result | None:
        try:
            info = path.stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        return info if stat.S_ISREG(info.st_mode) else None

    def has(self, url: str) -> bool:
        with self._lock:
            return self._regular(self.path_for(url)) is not None

    @classmethod
    def _remove(cls, path: Path, expected: os.stat_result | None = None) -> bool:
        current = cls._regular(path)
        if current is None or (
            expected is not None
            and (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def read(self, url: str) -> bytes | None:
        with self._lock:
            path = self.path_for(url)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                return None
            except OSError as exc:
                if exc.errno == errno.ELOOP:
                    return None
                raise
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    return None
                if info.st_size > MAX_THUMB_BYTES:
                    self._remove(path, info)
                    return None
                data = os.read(fd, MAX_THUMB_BYTES + 1)
                if len(data) > MAX_THUMB_BYTES or not data.startswith(_JPEG_PREFIX):
                    self._remove(path, info)
                    return None
                return data
            finally:
                os.close(fd)

    @staticmethod
    def _refuse_symlink(path: Path) -> None:
        if path.is_symlink():
            raise OSError(errno.ELOOP, "thumbnail destination is a symlink")

    def write(self, url: str, data: bytes) -> None:
        if (
            not isinstance(data, bytes)
            or not data.startswith(_JPEG_PREFIX)
            or len(data) > min(MAX_THUMB_BYTES, self._max_bytes)
        ):
            raise ValueError("invalid thumbnail bytes")
        with self._lock:
            self.ensure_dir()
            path = self.path_for(url)
            self._refuse_symlink(path)
            temporary = self.root / f".{self.key(url)}.{secrets.token_hex(8)}.tmp"
            created = False
            try:
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                created = True
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                self._refuse_symlink(path)
                os.replace(temporary, path)
            except OSError:
                if created:
                    self._remove(temporary)
                raise
            self._evict(protected=path)

    def _files(self) -> list[tuple[Path, os.stat_result]]:
        files = []
        try:
            with os.scandir(self.root) as entries:
                for entry in entries:
                    if _MANAGED.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                        try:
                            info = entry.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            continue
                        if stat.S_ISREG(info.st_mode):
                            files.append((Path(entry.path), info))
        except FileNotFoundError:
            pass
        return files

    def _evict(self, *, protected: Path | None = None) -> int:
        files = sorted(self._files(), key=lambda item: (item[1].st_mtime, item[0].name))
        total = sum(info.st_size for _, info in files)
        count = len(files)
        removed = 0
        for path, info in files:
            if total <= self._max_bytes and count <= self._max_files:
                break
            if path != protected and self._remove(path, info):
                total -= info.st_size
                count -= 1
                removed += 1
        return removed

    def prune(self, referenced_urls: Iterable[str]) -> int:
        with self._lock:
            names = {f"{self.key(url)}.jpg" for url in referenced_urls}
            removed = 0
            for path, info in self._files():
                if path.name not in names and self._remove(path, info):
                    removed += 1
            try:
                with os.scandir(self.root) as entries:
                    for entry in entries:
                        if _TEMPORARY.fullmatch(entry.name) and entry.is_file(
                            follow_symlinks=False
                        ):
                            try:
                                info = entry.stat(follow_symlinks=False)
                            except FileNotFoundError:
                                continue
                            if info.st_mtime < self._clock() - 3600 and self._remove(
                                Path(entry.path), info
                            ):
                                removed += 1
            except FileNotFoundError:
                pass
            return removed + self._evict()
