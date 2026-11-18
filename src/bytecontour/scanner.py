"""Streaming filesystem inventory and cooperative scan controls."""

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Condition
from time import monotonic
from typing import Literal, Protocol

ScanState = Literal["running", "completed", "partial", "cancelled", "failed", "interrupted"]
BATCH_SIZE = 512


class ScanCancelled(Exception):
    """The caller requested cooperative cancellation."""


class ScanControl:
    def __init__(self) -> None:
        self._condition = Condition()
        self._paused = False
        self._cancelled = False
        self._sealed = False

    def set_paused(self, paused: bool) -> bool:
        with self._condition:
            if self._cancelled or self._sealed:
                return False
            self._paused = paused
            self._condition.notify_all()
            return True

    def cancel(self) -> bool:
        with self._condition:
            if self._sealed:
                return False
            self._cancelled = True
            self._condition.notify_all()
            return True

    def checkpoint(self, *, seal: bool = False) -> None:
        with self._condition:
            self._condition.wait_for(lambda: not self._paused or self._cancelled)
            if self._cancelled:
                raise ScanCancelled
            self._sealed = seal


@dataclass(frozen=True, slots=True)
class Entry:
    path: str
    parent: str | None
    kind: Literal["file", "directory"]
    extension: str
    logical_bytes: int
    allocated_bytes: int | None
    modified_ns: int
    change_ns: int
    identity: str | None
    links: int
    file_count: int
    incomplete: bool = False


@dataclass(frozen=True, slots=True)
class ScanProgress:
    files: int
    directories: int
    logical_bytes: int
    allocated_bytes: int | None
    skipped: int
    errors: int
    elapsed: float
    current: str
    first_error: str | None


class DirectoryEntries(Protocol):
    def __next__(self) -> os.DirEntry[str]: ...
    def close(self) -> None: ...


@dataclass(slots=True)
class _Directory:
    path: str
    relative: str
    info: os.stat_result
    children: DirectoryEntries
    files: int = 0
    logical: int = 0
    incomplete: bool = False


def _linked(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _native_path(path: str) -> str:
    """Use extended Win32 syntax before reaching the legacy path limit."""
    if os.name != "nt" or len(path) < 120 or path.startswith(("\\\\?\\", "\\\\.\\")):
        return path
    if len(path.encode("utf-16-le", errors="surrogatepass")) < 480:
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _entry(path: str, info: os.stat_result, directory: _Directory | None = None) -> Entry:
    parent = path.rpartition("/")[0] if path else None
    identity = f"{info.st_dev:x}:{info.st_ino:x}" if info.st_ino else None
    changed = (
        getattr(info, "st_birthtime_ns", info.st_ctime_ns)
        if os.name == "nt"
        else (info.st_ctime_ns)
    )
    if directory is not None:
        return Entry(
            path,
            parent,
            "directory",
            "",
            directory.logical,
            None,
            info.st_mtime_ns,
            changed,
            identity,
            info.st_nlink,
            directory.files,
            directory.incomplete,
        )
    blocks = getattr(info, "st_blocks", None)
    allocated = blocks * 512 if isinstance(blocks, int) else None
    return Entry(
        path,
        parent,
        "file",
        os.path.splitext(path)[1].lower(),
        info.st_size,
        allocated,
        info.st_mtime_ns,
        changed,
        identity,
        info.st_nlink,
        1,
    )


class Inventory:
    """A single scan. Live iterators scale with tree depth and batches have a fixed cap."""

    def __init__(self, root: Path, exclude: Callable[[str], bool] | None = None) -> None:
        self.root = root
        self._exclude = exclude
        self.started = monotonic()
        self.files = self.directories = self.logical = self.skipped = self.errors = 0
        self.allocated: int | None = 0 if os.name != "nt" else None
        self.current = str(root)
        self.first_error: str | None = None
        self._last_report = self.started

    def progress(self) -> ScanProgress:
        return ScanProgress(
            self.files,
            self.directories,
            self.logical,
            self.allocated,
            self.skipped,
            self.errors,
            monotonic() - self.started,
            self.current,
            self.first_error,
        )

    def _error(self, path: str, error: OSError) -> None:
        self.errors += 1
        self.skipped += 1
        if self.first_error is None:
            self.first_error = f"{path} — {error.strerror or error}"

    def run(
        self,
        control: ScanControl,
        save: Callable[[list[Entry]], None],
        report: Callable[[ScanProgress], None],
    ) -> None:
        root = self.root
        for component in reversed(root.parents):
            control.checkpoint()
            info = Path(_native_path(str(component))).lstat()
            if _linked(info) or not stat.S_ISDIR(info.st_mode):
                raise OSError(f"Scan path is not a regular directory: {component}")
        control.checkpoint()
        root_path = _native_path(str(root))
        root_info = Path(root_path).lstat()
        if _linked(root_info) or not stat.S_ISDIR(root_info.st_mode):
            raise OSError(f"Scan path is not a regular directory: {root}")
        stack = [_Directory(str(root), "", root_info, os.scandir(root_path))]
        ancestors = {(root_info.st_dev, root_info.st_ino)}
        self.directories = 1
        batch: list[Entry] = []
        try:
            while stack:
                control.checkpoint()
                frame = stack[-1]
                entry = self._advance(stack, ancestors, root_info.st_dev)
                if entry is not None:
                    batch.append(entry)
                    if len(batch) == BATCH_SIZE:
                        save(batch)
                        batch = []
                now = monotonic()
                if now - self._last_report >= 0.2:
                    self.current = frame.path
                    report(self.progress())
                    self._last_report = now
            current = Path(root_path).lstat()
            if (
                _linked(current)
                or not stat.S_ISDIR(current.st_mode)
                or (current.st_dev, current.st_ino) != (root_info.st_dev, root_info.st_ino)
            ):
                raise OSError("The scan root was replaced during the scan.")
            if batch:
                save(batch)
        finally:
            for frame in reversed(stack):
                frame.children.close()

    def _advance(
        self, stack: list[_Directory], ancestors: set[tuple[int, int]], device: int
    ) -> Entry | None:
        frame = stack[-1]
        try:
            child = next(frame.children)
        except StopIteration:
            return self._finish_directory(stack, ancestors)
        except OSError as error:
            frame.incomplete = True
            self._error(frame.path, error)
            return self._finish_directory(stack, ancestors)

        relative = f"{frame.relative}/{child.name}" if frame.relative else child.name
        if self._exclude is not None and self._exclude(relative):
            return None
        native_path = _native_path(child.path)
        try:
            # On Windows DirEntry.stat omits file identity and link count.
            info = (
                os.stat(native_path, follow_symlinks=False)
                if os.name == "nt"
                else (child.stat(follow_symlinks=False))
            )
            if _linked(info) or info.st_dev != device:
                self.skipped += 1
                return None
            if stat.S_ISDIR(info.st_mode):
                key = (info.st_dev, info.st_ino)
                if info.st_ino and key in ancestors:
                    self.skipped += 1
                    return None
                self.directories += 1
                try:
                    children = os.scandir(native_path)
                except OSError as error:
                    self._error(child.path, error)
                    frame.incomplete = True
                    return _entry(
                        relative,
                        info,
                        _Directory(child.path, relative, info, _EmptyDirectory(), incomplete=True),
                    )
                stack.append(_Directory(child.path, relative, info, children))
                ancestors.add(key)
                return None
            if not stat.S_ISREG(info.st_mode):
                self.skipped += 1
                return None
        except OSError as error:
            self._error(child.path, error)
            frame.incomplete = True
            return None

        entry = _entry(relative, info)
        frame.files += 1
        frame.logical += entry.logical_bytes
        self.files += 1
        self.logical += entry.logical_bytes
        if self.allocated is not None:
            self.allocated = (
                self.allocated + entry.allocated_bytes
                if entry.allocated_bytes is not None
                else None
            )
        return entry

    @staticmethod
    def _finish_directory(stack: list[_Directory], ancestors: set[tuple[int, int]]) -> Entry:
        frame = stack.pop()
        frame.children.close()
        ancestors.discard((frame.info.st_dev, frame.info.st_ino))
        if stack:
            parent = stack[-1]
            parent.files += frame.files
            parent.logical += frame.logical
            parent.incomplete |= frame.incomplete
        return _entry(frame.relative, frame.info, frame)


class _EmptyDirectory:
    def __next__(self) -> os.DirEntry[str]:
        raise StopIteration

    def close(self) -> None:
        pass
