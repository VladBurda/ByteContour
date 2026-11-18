"""Read-only discovery and preview, plus audited execution of a confirmed plan."""

import os
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, time_ns
from uuid import uuid4

from sqlalchemy import and_, literal, or_, select, update
from sqlalchemy.orm import Session

from bytecontour.database import (
    CleanupItem,
    CleanupOperation,
    DuplicateGroup,
    DuplicateMember,
    DuplicateRun,
    ScanRepository,
    _utc_now,
)
from bytecontour.database import (
    InventoryEntry as Entry,
)
from bytecontour.duplicate_store import DuplicateStore, _version
from bytecontour.file_hashes import (
    StaleFile,
    _check_ancestors,
    candidate_path,
    checked_file,
    compare_files,
)
from bytecontour.recycle import RecycleUncertain, recycle_file
from bytecontour.scanner import ScanCancelled, ScanControl, _linked, _native_path

PAGE = 100
_DAY_NS = 86_400 * 10**9
_SENSITIVE = {
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "system volume information",
    "$recycle.bin",
    "recovery",
    "appdata",
    ".git",
    ".ssh",
    ".gnupg",
    ".aws",
    ".azure",
    ".kube",
    ".config",
    ".codex",
    ".totp",
    "credentials",
}
_SYSTEM_FILES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "bootmgr",
    "bootnxt",
    "ntuser.dat",
    "usrclass.dat",
    "pagefile.sys",
    "hiberfil.sys",
    "swapfile.sys",
    ".env",
}


@dataclass(frozen=True, slots=True)
class Rules:
    temporary_days: int = 30
    large_days: int = 180
    large_mib: int = 512
    empty_folders: bool = True
    duplicates: bool = True


@dataclass(frozen=True, slots=True)
class Candidate:
    path: str
    reason: str
    size: int
    allocated: int | None
    group_id: int | None
    run_id: int | None
    keeper: str | None
    directory: bool


@dataclass(frozen=True, slots=True)
class Candidates:
    scan_id: int | None
    root: str
    rows: tuple[Candidate, ...] = ()
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class PreviewItem:
    candidate: Candidate
    stamp: str | None
    detail: str


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    token: str
    scan_id: int
    root: str
    items: tuple[PreviewItem, ...]

    @property
    def eligible(self) -> tuple[PreviewItem, ...]:
        return tuple(item for item in self.items if item.stamp is not None)


@dataclass(frozen=True, slots=True)
class AuditLine:
    operation: str
    approved_at: str
    root: str
    path: str
    state: str
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class CleanupAudit:
    rows: tuple[AuditLine, ...]
    cursor: tuple[str, str, str] | None = None
    operation: str | None = None


def _protected(root: Path, relative: str) -> Path:
    target = candidate_path(root, relative)
    _check_ancestors(target)
    canonical_root = root.resolve(strict=True)
    info = os.lstat(_native_path(str(target)))
    if _linked(info) or not stat.S_ISREG(info.st_mode):
        raise StaleFile("Cleanup requires a regular file.")
    if getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_SYSTEM:
        raise StaleFile("Windows marks this file as a system file.")
    canonical = target.resolve(strict=True)
    if not canonical.is_relative_to(canonical_root):
        raise StaleFile("The path leaves the saved scan root.")
    if os.name == "nt" and any(part.rstrip(" .") != part for part in canonical.parts[1:]):
        raise StaleFile("Windows Shell normalization could change this path.")
    parts = {part.casefold() for part in canonical.parts[1:]}
    protected_roots = tuple(
        Path(value).resolve()
        for name in ("SystemRoot", "ProgramFiles", "ProgramData", "APPDATA", "LOCALAPPDATA")
        if (value := os.environ.get(name))
    )
    name = canonical.name.casefold()
    if (
        parts & _SENSITIVE
        or name in _SYSTEM_FILES
        or name.startswith((".env.", "ntuser.dat."))
        or any(canonical.is_relative_to(parent) for parent in protected_roots)
    ):
        raise StaleFile("This path is protected.")
    return canonical


def _review_directory(root: Path, relative: str) -> str:
    path = candidate_path(root, relative)
    _check_ancestors(path)
    native = _native_path(str(path))
    info = os.lstat(native)
    if _linked(info) or not stat.S_ISDIR(info.st_mode):
        return "The path is no longer an ordinary folder."
    with os.scandir(native) as entries:
        if next(entries, None) is not None:
            return "The live folder contains entries. Review only."
    return "Empty on inspection. Review only."


class CleanupService:
    def __init__(
        self,
        repository: ScanRepository,
        recycle: Callable[[Path, Callable[[], None]], None] = recycle_file,
    ) -> None:
        self.engine = repository.engine
        self._duplicates = DuplicateStore(repository)
        self._recycle = recycle
        self._supported = recycle is not recycle_file or sys.platform == "win32"
        self._pending: tuple[CleanupPlan, float] | None = None

    def discover(
        self,
        scan_id: int | None,
        rules: Rules,
        after: str = "",
        paths: tuple[str, ...] | None = None,
    ) -> Candidates:
        if not (
            0 <= rules.temporary_days <= 36500
            and 0 <= rules.large_days <= 36500
            and 1 <= rules.large_mib <= 2**30
        ):
            raise ValueError("Cleanup thresholds are outside the supported range.")
        with Session(self.engine) as session:
            scan = DuplicateStore._snapshot(session, scan_id)
            if scan is None:
                return Candidates(None, "")
            now = time_ns()
            temp = and_(
                Entry.kind == "file",
                Entry.extension.in_((".tmp", ".temp")),
                Entry.modified_ns <= now - rules.temporary_days * _DAY_NS,
            )
            large = and_(
                Entry.kind == "file",
                Entry.logical_bytes >= rules.large_mib * 1024**2,
                Entry.modified_ns <= now - rules.large_days * _DAY_NS,
            )
            child = Entry.__table__.alias("child")
            empty = and_(
                literal(rules.empty_folders),
                Entry.kind == "directory",
                Entry.incomplete.is_(False),
                ~select(child.c.path)
                .where(child.c.scan_id == scan.id, child.c.parent == Entry.path)
                .exists(),
            )
            run = (
                select(DuplicateRun.id)
                .where(DuplicateRun.scan_id == scan.id, DuplicateRun.complete.is_(True))
                .order_by(DuplicateRun.id.desc())
                .limit(1)
                .scalar_subquery()
            )
            duplicate = and_(
                literal(rules.duplicates),
                DuplicateGroup.id.is_not(None),
                DuplicateGroup.keeper != Entry.path,
            )
            query = (
                select(
                    Entry,
                    temp,
                    large,
                    empty,
                    duplicate,
                    DuplicateGroup.id,
                    DuplicateMember.run_id,
                    DuplicateGroup.keeper,
                )
                .select_from(Entry)
                .outerjoin(
                    DuplicateMember,
                    and_(DuplicateMember.run_id == run, DuplicateMember.path == Entry.path),
                )
                .outerjoin(
                    DuplicateGroup,
                    and_(
                        DuplicateGroup.run_id == DuplicateMember.run_id,
                        DuplicateGroup.size == DuplicateMember.size,
                        DuplicateGroup.digest == DuplicateMember.digest,
                    ),
                )
                .where(
                    Entry.scan_id == scan.id, Entry.path > after, or_(temp, large, empty, duplicate)
                )
                .order_by(Entry.path)
                .limit(PAGE + 1)
            )
            if paths is not None:
                query = query.where(Entry.path.in_(paths))
            rows = session.execute(query).all()
            candidates = tuple(
                Candidate(
                    entry.path,
                    "Duplicate copy"
                    if dup
                    else "Old temporary file"
                    if tmp
                    else "Old large file"
                    if big
                    else "Empty folder",
                    entry.logical_bytes,
                    entry.allocated_bytes,
                    group,
                    run_id,
                    keeper,
                    bool(folder),
                )
                for entry, tmp, big, folder, dup, group, run_id, keeper in rows[:PAGE]
            )
            return Candidates(
                scan.id,
                scan.root,
                candidates,
                candidates[-1].path if len(rows) > PAGE else None,
            )

    def _validate(
        self,
        scan_id: int,
        root: Path,
        item: Candidate,
        stamp: str | None,
        control: ScanControl,
        *,
        compare: bool,
    ) -> tuple[Path, str]:
        control.checkpoint()
        path = _protected(root, item.path)
        with Session(self.engine) as session:
            row = session.get(Entry, (scan_id, item.path))
            if row is None or row.kind != "file":
                raise StaleFile("The file is no longer in the saved inventory.")
            source = _version(row)
            is_keeper = session.scalar(
                select(DuplicateGroup.id)
                .join(
                    DuplicateMember,
                    and_(
                        DuplicateMember.run_id == DuplicateGroup.run_id,
                        DuplicateMember.path == DuplicateGroup.keeper,
                    ),
                )
                .where(
                    DuplicateMember.identity == row.identity, DuplicateMember.identity.is_not(None)
                )
                .limit(1)
            )
        if is_keeper is not None:
            raise StaleFile("This file is a saved duplicate keeper.")
        with checked_file(root, source, stamp) as (_, file):
            if not file.cacheable or file.links != 1:
                raise StaleFile("Cleanup requires a known file version and a single hard link.")
        if item.group_id is not None and item.run_id is not None:
            _, candidate, keeper = self._duplicates.review(item.run_id, item.group_id, item.path)
            if keeper.version.path != item.keeper:
                raise StaleFile("The keeper choice changed. Create a new preview.")
            if candidate.keeper or not keeper.identity or keeper.identity == candidate.identity:
                raise StaleFile("Keep a separate verified copy before recycling this candidate.")
            if compare:
                if not compare_files(
                    root, candidate.version, keeper.version, candidate.stamp, keeper.stamp, control
                ):
                    raise StaleFile("The candidate differs from the keeper.")
            else:
                with checked_file(root, keeper.version, keeper.stamp):
                    pass
        return path, file.fingerprint

    def preview(self, scan_id: int, rules: Rules, paths: tuple[str, ...]) -> CleanupPlan:
        self._pending = None
        if not 1 <= len(paths) <= PAGE or len(set(paths)) != len(paths):
            raise ValueError("Select between 1 and 100 different candidates.")
        found = self.discover(scan_id, rules, paths=paths)
        if found.scan_id != scan_id or {row.path for row in found.rows} != set(paths):
            raise ValueError("Candidates changed. Refresh the cleanup list and try again.")
        root = Path(found.root)
        plan: list[PreviewItem] = []
        for row in found.rows:
            if row.directory:
                try:
                    detail = _review_directory(root, row.path)
                except OSError as error:
                    detail = str(error)
                plan.append(PreviewItem(row, None, detail))
                continue
            if not self._supported:
                plan.append(PreviewItem(row, None, "Native recycling is unavailable on this OS."))
                continue
            try:
                _, stamp = self._validate(scan_id, root, row, None, ScanControl(), compare=False)
            except (OSError, ValueError) as error:
                plan.append(PreviewItem(row, None, str(error)))
            else:
                detail = f"Keep {row.keeper}" if row.keeper else "Eligible after live revalidation."
                plan.append(PreviewItem(row, stamp, detail))
        preview = CleanupPlan(uuid4().hex, scan_id, found.root, tuple(plan))
        self._pending = preview, monotonic() + 600
        return preview

    def execute(self, token: str, control: ScanControl) -> CleanupAudit:
        pending = self._pending
        if pending is None or pending[0].token != token or monotonic() > pending[1]:
            raise ValueError("This preview is no longer valid. Create a new preview.")
        self._pending = None
        plan = pending[0]
        eligible = plan.eligible
        if not eligible:
            raise ValueError("There are no eligible files in this preview.")
        with Session(self.engine) as session, session.begin():
            session.add(
                CleanupOperation(
                    id=token,
                    scan_id=plan.scan_id,
                    root=plan.root,
                    approved_at=_utc_now(),
                    state="running",
                )
            )
            session.flush()
            session.add_all(
                CleanupItem(
                    operation_id=token,
                    path=item.candidate.path,
                    reason=item.candidate.reason,
                    state="pending",
                    detail="",
                )
                for item in eligible
            )
        for item in eligible:
            row = item.candidate
            root = Path(plan.root)
            try:
                path, _ = self._validate(plan.scan_id, root, row, item.stamp, control, compare=True)
                self._record(token, row.path, "attempting", "Handing the file to Windows.")

                def revalidate(
                    root: Path = root,
                    row: Candidate = row,
                    stamp: str | None = item.stamp,
                    original: Path = path,
                ) -> None:
                    current, _ = self._validate(
                        plan.scan_id, root, row, stamp, control, compare=False
                    )
                    if current != original:
                        raise StaleFile("The file path changed before recycling.")

                self._recycle(path, revalidate)
            except ScanCancelled:
                self._record(token, row.path, "skipped", "Cancelled before recycling this file.")
            except RecycleUncertain as error:
                self._record(token, row.path, "indeterminate", str(error))
            except (OSError, ValueError) as error:
                self._record(token, row.path, "failed", str(error))
            else:
                self._record(
                    token, row.path, "recycled", "To recover, locate the file in Recycle Bin."
                )
        with Session(self.engine) as session, session.begin():
            session.execute(
                update(CleanupOperation)
                .where(CleanupOperation.id == token)
                .values(state="finished")
            )
        return self.audit(operation=token)

    def _record(self, token: str, path: str, state: str, detail: str) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(
                update(CleanupItem)
                .where(CleanupItem.operation_id == token, CleanupItem.path == path)
                .values(state=state, detail=detail)
            )

    def audit(
        self,
        after: tuple[str, str, str] | None = None,
        *,
        operation: str | None = None,
    ) -> CleanupAudit:
        op = CleanupOperation
        item = CleanupItem
        query = (
            select(op, item)
            .select_from(op)
            .join(item)
            .order_by(op.approved_at.desc(), op.id.desc(), item.path.desc())
            .limit(PAGE + 1)
        )
        if operation:
            query = query.where(op.id == operation)
        if after:
            timestamp, key, path = after
            query = query.where(
                or_(
                    op.approved_at < timestamp,
                    and_(op.approved_at == timestamp, op.id < key),
                    and_(op.approved_at == timestamp, op.id == key, item.path < path),
                )
            )
        with Session(self.engine) as session:
            records = session.execute(query).all()
            rows = tuple(
                AuditLine(
                    operation_row.id,
                    operation_row.approved_at,
                    operation_row.root,
                    row.path,
                    row.state,
                    row.reason,
                    row.detail,
                )
                for operation_row, row in records[:PAGE]
            )
        cursor = (
            (rows[-1].approved_at, rows[-1].operation, rows[-1].path)
            if len(records) > PAGE
            else None
        )
        return CleanupAudit(rows, cursor, operation)
