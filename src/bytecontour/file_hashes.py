"""Read version-checked file contents for duplicate detection and review."""

import ctypes
import hashlib
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

import xxhash

from bytecontour.scanner import ScanControl, _linked, _native_path

SAMPLE_BYTES = 64 * 1024
CHUNK_BYTES = 1024 * 1024
CACHE_VERSION = 1


@dataclass(frozen=True, slots=True)
class FileVersion:
    path: str
    size: int
    modified: int
    change: int
    identity: str | None


@dataclass(frozen=True, slots=True)
class LiveFile:
    fingerprint: str
    cacheable: bool
    identity: str | None
    links: int
    allocated: int | None


class StaleFile(OSError):
    """The saved file no longer matches its live path or metadata."""


if sys.platform == "win32":
    from ctypes import wintypes
    from msvcrt import get_osfhandle

    class _BasicInfo(ctypes.Structure):
        _fields_ = [
            ("creation", ctypes.c_int64),
            ("access", ctypes.c_int64),
            ("write", ctypes.c_int64),
            ("change", ctypes.c_int64),
            ("attributes", wintypes.DWORD),
        ]

    _get_info = ctypes.WinDLL("kernel32", use_last_error=True).GetFileInformationByHandleEx
    _get_info.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    _get_info.restype = wintypes.BOOL


def _native_change(fd: int, info: os.stat_result) -> int | None:
    if sys.platform == "win32":
        basic = _BasicInfo()
        if _get_info(get_osfhandle(fd), 0, ctypes.byref(basic), ctypes.sizeof(basic)):
            return int(basic.change) or None
        return None
    return info.st_ctime_ns


def _metadata(info: os.stat_result) -> tuple[str | None, int, int, int, int]:
    identity = f"{info.st_dev:x}:{info.st_ino:x}" if info.st_ino else None
    change = (
        getattr(info, "st_birthtime_ns", info.st_ctime_ns) if os.name == "nt" else info.st_ctime_ns
    )
    return identity, info.st_size, info.st_mtime_ns, change, info.st_nlink


def candidate_path(root: Path, relative: str) -> Path:
    parts = relative.split("/")
    if not root.is_absolute() or any(
        part in ("", ".", "..") or Path(part).anchor or len(Path(part).parts) != 1 for part in parts
    ):
        raise StaleFile("Invalid inventory path.")
    if os.name == "nt" and any(":" in part for part in parts):
        raise StaleFile("Invalid inventory path.")
    return root.joinpath(*parts)


def _check_ancestors(path: Path) -> None:
    for parent in reversed(path.parents):
        info = os.lstat(_native_path(str(parent)))
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise StaleFile("A parent folder is no longer a regular directory.")


@contextmanager
def checked_file(
    root: Path,
    source: FileVersion,
    expected: str | None = None,
) -> Iterator[tuple[BinaryIO, LiveFile]]:
    path = candidate_path(root, source.path)
    native = _native_path(str(path))
    _check_ancestors(path)
    before = os.lstat(native)
    if _linked(before) or not stat.S_ISREG(before.st_mode):
        raise StaleFile("The path is no longer a regular file.")
    meta = _metadata(before)
    identity, size, modified, change, links = meta
    if (size, modified, change) != (source.size, source.modified, source.change) or (
        source.identity is not None and source.identity != identity
    ):
        raise StaleFile("The file changed since the inventory. Rescan the folder.")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    fd = os.open(native, flags)
    try:
        initial = os.fstat(fd)
        if _linked(initial) or not stat.S_ISREG(initial.st_mode) or _metadata(initial) != meta:
            raise StaleFile("The file was replaced while it was opened.")
        native_change = _native_change(fd, initial)
        stamp = repr((CACHE_VERSION, meta, native_change))
        if expected is not None and expected != stamp:
            raise StaleFile("The file changed since duplicate detection. Run detection again.")
        blocks = getattr(initial, "st_blocks", None)
        live = LiveFile(
            stamp,
            identity is not None and native_change is not None,
            identity,
            links,
            blocks * 512 if isinstance(blocks, int) else None,
        )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            yield stream, live
        after = os.fstat(fd)
        _check_ancestors(path)
        current = os.lstat(native)
        if (
            _metadata(after) != meta
            or _native_change(fd, after) != native_change
            or _linked(current)
            or _metadata(current) != meta
        ):
            raise StaleFile("The file changed during the read.")
    finally:
        os.close(fd)


def sampled_hash(stream: BinaryIO, size: int, control: ScanControl) -> tuple[str, int]:
    digest = xxhash.xxh3_128()
    digest.update(size.to_bytes(8, "little"))
    spans: tuple[tuple[int, int], ...]
    if size <= 3 * SAMPLE_BYTES:
        spans = ((0, size),)
    else:
        spans = (
            (0, SAMPLE_BYTES),
            ((size - SAMPLE_BYTES) // 2, SAMPLE_BYTES),
            (size - SAMPLE_BYTES, SAMPLE_BYTES),
        )
    read = 0
    for offset, count in spans:
        control.checkpoint()
        stream.seek(offset)
        data = stream.read(count)
        if len(data) != count:
            raise StaleFile("The file ended during sampling.")
        digest.update(data)
        read += len(data)
    return digest.hexdigest(), read


def full_hash(stream: BinaryIO, size: int, control: ScanControl) -> tuple[str, int]:
    digest = hashlib.sha256()
    read = 0
    while True:
        control.checkpoint()
        data = stream.read(CHUNK_BYTES)
        if not data:
            break
        read += len(data)
        if read > size:
            raise StaleFile("The file grew during hashing.")
        digest.update(data)
    if read != size:
        raise StaleFile("The file ended during hashing.")
    return digest.hexdigest(), read


def compare_files(
    root: Path,
    left: FileVersion,
    right: FileVersion,
    left_stamp: str,
    right_stamp: str,
    control: ScanControl,
) -> bool:
    with (
        checked_file(root, left, left_stamp) as (first, _),
        checked_file(root, right, right_stamp) as (second, _),
    ):
        while True:
            control.checkpoint()
            block = first.read(CHUNK_BYTES)
            if block != second.read(CHUNK_BYTES):
                return False
            if not block:
                return True
