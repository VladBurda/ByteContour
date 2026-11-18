"""Size, sample, and full-hash duplicate detection with live review."""

import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path
from time import monotonic, time

from bytecontour.database import DuplicateMember, HashCache
from bytecontour.duplicate_store import (
    DuplicateProgress,
    DuplicateReport,
    DuplicateRequest,
    DuplicateStore,
)
from bytecontour.file_hashes import (
    FileVersion,
    LiveFile,
    StaleFile,
    checked_file,
    compare_files,
    full_hash,
    sampled_hash,
)
from bytecontour.scanner import ScanCancelled, ScanControl

_PREVIEW_BYTES = 4096


@dataclass(frozen=True, slots=True)
class Review:
    path: str
    title: str
    text: str
    verified: bool | None = None


@dataclass(slots=True)
class _Counters:
    started: float
    examined: int = 0
    skipped: int = 0
    excluded: int = 0
    read: int = 0
    hits: int = 0

    def progress(self, phase: str) -> DuplicateProgress:
        return DuplicateProgress(
            phase,
            self.examined,
            self.skipped,
            self.excluded,
            self.read,
            self.hits,
            monotonic() - self.started,
        )


@dataclass(frozen=True, slots=True)
class _HashResult:
    live: LiveFile
    sample: str
    digest: str | None
    read: int
    hit: bool


def _hash_candidate(
    root: Path,
    source: FileVersion,
    member: DuplicateMember,
    cached: HashCache | None,
    full: bool,
    control: ScanControl,
) -> _HashResult:
    with checked_file(root, source, member.stamp if full else None) as (stream, live):
        trusted = cached if live.cacheable and cached and cached.stamp == live.fingerprint else None
        if not full:
            if trusted:
                return _HashResult(live, trusted.sample, trusted.digest, 0, True)
            sample, read = sampled_hash(stream, source.size, control)
            return _HashResult(live, sample, None, read, False)
        if member.sample is None:
            raise StaleFile("The file was never sampled.")
        if trusted and trusted.sample == member.sample and trusted.digest:
            return _HashResult(live, trusted.sample, trusted.digest, 0, True)
        digest, read = full_hash(stream, source.size, control)
        return _HashResult(live, member.sample, digest, read, False)


def _matches(relative: str, patterns: tuple[str, ...]) -> bool:
    path = relative.casefold() if os.name == "nt" else relative
    return any(fnmatchcase(path, pattern) for pattern in patterns)


class DuplicateService:
    def __init__(self, store: DuplicateStore) -> None:
        self.store = store

    def detect(
        self,
        request: DuplicateRequest,
        control: ScanControl,
        report: Callable[[DuplicateProgress], None],
    ) -> DuplicateReport:
        if not 0 <= request.minimum < 2**63:
            raise ValueError("Minimum size is outside the supported range.")
        patterns = tuple(
            pattern.casefold() if os.name == "nt" else pattern for pattern in request.exclusions
        )
        counters = _Counters(monotonic())
        run, scan, root = self.store.begin(request)
        try:
            control.checkpoint()
            report(counters.progress("Grouping by size"))
            self.store.stage(run, scan, request.minimum)
            for full, phase in ((False, "Sampling candidates"), (True, "Hashing full files")):
                control.checkpoint()
                report(counters.progress(phase))
                self._pass(Path(root), run, scan, full, patterns, counters, phase, control, report)
                if not full:
                    control.checkpoint()
                    self.store.choose_full_candidates(run)
            control.checkpoint(seal=True)
            summary = counters.progress("Saving groups")
            report(summary)
            self.store.prune_cache()
            self.store.publish(run, scan, summary)
        except ScanCancelled:
            self.store.discard(run)
            return replace(self.store.report(scan), cancelled=True)
        except Exception:
            self.store.discard(run)
            raise
        return self.store.report(scan)

    def _pass(
        self,
        root: Path,
        run: int,
        scan: int,
        full: bool,
        patterns: tuple[str, ...],
        counters: _Counters,
        phase: str,
        control: ScanControl,
        report: Callable[[DuplicateProgress], None],
    ) -> None:
        after = ""
        last_report = monotonic()
        while True:
            control.checkpoint()
            batch = self.store.candidates(run, scan, after, full)
            if not batch:
                return
            after = batch[-1][1].path
            # These strings are database keys. checked_file separately validates the live path.
            keys = [str(root.joinpath(*source.path.split("/"))) for _, source in batch]
            cache = self.store.cache(keys)
            updates: list[dict[str, str | int | None]] = []
            hashes: list[dict[str, str | float | None]] = []
            for key, (member, source) in zip(keys, batch, strict=True):
                control.checkpoint()
                if monotonic() - last_report >= 0.2:
                    report(counters.progress(phase))
                    last_report = monotonic()
                if not full and _matches(source.path, patterns):
                    counters.excluded += 1
                    continue
                counters.examined += 1
                try:
                    result = _hash_candidate(root, source, member, cache.get(key), full, control)
                except OSError:
                    counters.skipped += 1
                    continue
                counters.read += result.read
                counters.hits += result.hit
                updates.append(
                    {
                        "run_id": run,
                        "path": source.path,
                        "sample": result.sample,
                        # A cached digest becomes eligible only after the full-pass check.
                        "digest": result.digest if full else None,
                        "stamp": result.live.fingerprint,
                        "identity": result.live.identity,
                        "links": result.live.links,
                        "allocated": result.live.allocated,
                    }
                )
                if result.live.cacheable:
                    hashes.append(
                        {
                            "path": key,
                            "stamp": result.live.fingerprint,
                            "sample": result.sample,
                            "digest": result.digest,
                            "touched": time(),
                        }
                    )
            self.store.save(updates, hashes)

    def preview(self, run: int, group: int, path: str, control: ScanControl) -> Review:
        root, chosen, _ = self.store.review(run, group, path)
        with checked_file(Path(root), chosen.version, chosen.stamp) as (stream, _):
            control.checkpoint()
            data = stream.read(_PREVIEW_BYTES)
        decoded = data.decode("utf-8-sig", errors="replace")
        if not data:
            title, text = "Empty file", ""
        elif "\ufffd" not in decoded and all(c.isprintable() or c in "\n\r\t" for c in decoded):
            title, text = f"UTF-8 preview · first {len(data):,} bytes", decoded
        else:
            title, text = "Binary preview · first 128 bytes or fewer", data[:128].hex(" ")
        return Review(path, title, text)

    def verify(self, run: int, group: int, path: str, control: ScanControl) -> Review:
        root, chosen, keeper = self.store.review(run, group, path)
        if chosen.keeper:
            raise ValueError("Select another member to compare with the keeper.")
        equal = compare_files(
            Path(root),
            chosen.version,
            keeper.version,
            chosen.stamp,
            keeper.stamp,
            control,
        )
        message = "Bytes match the current keeper." if equal else "Bytes differ from the keeper."
        return Review(path, "Live comparison", f"{message}\nKeeper  {keeper.version.path}", equal)
