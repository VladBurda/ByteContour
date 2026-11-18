"""SQLite staging, hash cache, and paged duplicate results."""

import json
from dataclasses import dataclass
from time import time

from sqlalchemy import and_, case, delete, distinct, func, insert, literal, select, tuple_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from bytecontour.database import (
    DuplicateGroup,
    DuplicateMember,
    DuplicateRun,
    HashCache,
    InventoryEntry,
    Scan,
    ScanRepository,
)
from bytecontour.file_hashes import FileVersion

BATCH = 128
PAGE = 75
_CACHE_TTL = 30 * 24 * 60 * 60
_CACHE_LIMIT = 250_000


@dataclass(frozen=True, slots=True)
class DuplicateRequest:
    scan_id: int | None = None
    minimum: int = 1
    exclusions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class DuplicateProgress:
    phase: str
    examined: int
    skipped: int
    excluded: int
    bytes_read: int
    cache_hits: int
    elapsed: float


@dataclass(frozen=True, slots=True)
class Group:
    id: int
    size: int
    paths: int
    copies: int
    keeper: str
    logical: int
    allocated: int | None


@dataclass(frozen=True, slots=True)
class Member:
    version: FileVersion
    stamp: str
    links: int
    identity: str | None
    allocated: int | None
    keeper: bool


@dataclass(frozen=True, slots=True)
class DuplicateReport:
    scan_id: int | None = None
    root: str = ""
    run_id: int | None = None
    groups: tuple[Group, ...] = ()
    next_page: int | None = None
    group_count: int = 0
    logical: int = 0
    allocated: int | None = None
    skipped: int = 0
    elapsed: float = 0
    bytes_read: int = 0
    cache_hits: int = 0
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class MembersPage:
    run_id: int
    group_id: int
    group: Group
    rows: tuple[Member, ...]
    next_page: str | None
    report: DuplicateReport | None = None


def _version(entry: InventoryEntry) -> FileVersion:
    return FileVersion(
        entry.path, entry.logical_bytes, entry.modified_ns, entry.change_ns, entry.identity
    )


def _physical() -> ColumnElement[str]:
    return func.coalesce(DuplicateMember.identity, ":" + DuplicateMember.path)


def _group(group: DuplicateGroup, keeper: DuplicateMember) -> Group:
    eligible = keeper.identity is not None and keeper.links == 1
    unknown = group.unknown - int(eligible and keeper.allocated is None)
    return Group(
        group.id,
        group.size,
        group.paths,
        group.copies,
        group.keeper,
        group.logical - (group.size if eligible else 0),
        None if unknown else group.allocated - ((keeper.allocated or 0) if eligible else 0),
    )


class DuplicateStore:
    def __init__(self, repository: ScanRepository) -> None:
        self.engine = repository.engine

    @staticmethod
    def _snapshot(session: Session, scan_id: int | None) -> Scan | None:
        query = select(Scan).where(Scan.state.in_(("completed", "partial")))
        if scan_id is not None:
            query = query.where(Scan.id == scan_id)
        return session.scalar(query.order_by(Scan.id.desc()).limit(1))

    def begin(self, request: DuplicateRequest) -> tuple[int, int, str]:
        with Session(self.engine) as session, session.begin():
            scan = self._snapshot(session, request.scan_id)
            if scan is None:
                raise ValueError("Select a saved inventory before finding duplicates.")
            run = DuplicateRun(
                scan_id=scan.id,
                complete=False,
                exclusions=json.dumps(request.exclusions),
                minimum=request.minimum,
            )
            session.add(run)
            session.flush()
            return run.id, scan.id, scan.root

    def stage(self, run_id: int, scan_id: int, minimum: int) -> None:
        file_scope = and_(
            InventoryEntry.scan_id == scan_id,
            InventoryEntry.kind == "file",
            InventoryEntry.logical_bytes >= minimum,
        )
        sizes = (
            select(InventoryEntry.logical_bytes)
            .where(file_scope)
            .group_by(InventoryEntry.logical_bytes)
            .having(func.count() > 1)
        )
        paths = select(literal(run_id), InventoryEntry.path, InventoryEntry.logical_bytes).where(
            file_scope, InventoryEntry.logical_bytes.in_(sizes)
        )
        with Session(self.engine) as session, session.begin():
            session.execute(insert(DuplicateMember).from_select(("run_id", "path", "size"), paths))

    def candidates(
        self, run_id: int, scan_id: int, after: str, full: bool
    ) -> list[tuple[DuplicateMember, FileVersion]]:
        query = (
            select(DuplicateMember, InventoryEntry)
            .join(
                InventoryEntry,
                and_(
                    InventoryEntry.scan_id == scan_id,
                    InventoryEntry.path == DuplicateMember.path,
                ),
            )
            .where(DuplicateMember.run_id == run_id, DuplicateMember.path > after)
            .order_by(DuplicateMember.path)
            .limit(BATCH)
        )
        if full:
            query = query.where(DuplicateMember.eligible.is_(True))
        with Session(self.engine) as session:
            return [(member, _version(entry)) for member, entry in session.execute(query)]

    def cache(self, paths: list[str]) -> dict[str, HashCache]:
        with Session(self.engine) as session:
            return {
                item.path: item
                for item in session.scalars(
                    select(HashCache).where(
                        HashCache.path.in_(paths), HashCache.touched >= time() - _CACHE_TTL
                    )
                )
            }

    def save(
        self,
        updates: list[dict[str, str | int | None]],
        hashes: list[dict[str, str | float | None]],
    ) -> None:
        with Session(self.engine) as session, session.begin():
            if updates:
                session.execute(update(DuplicateMember), updates)
            if hashes:
                proposed = sqlite_insert(HashCache)
                session.execute(
                    proposed.on_conflict_do_update(
                        index_elements=[HashCache.path],
                        set_={
                            key: proposed.excluded[key]
                            for key in ("stamp", "sample", "digest", "touched")
                        },
                    ),
                    hashes,
                )

    def choose_full_candidates(self, run_id: int) -> None:
        with Session(self.engine) as session, session.begin():
            matches = (
                select(DuplicateMember.size, DuplicateMember.sample)
                .where(DuplicateMember.run_id == run_id, DuplicateMember.sample.is_not(None))
                .group_by(DuplicateMember.size, DuplicateMember.sample)
                .having(func.count(distinct(_physical())) > 1)
            )
            candidate = tuple_(DuplicateMember.size, DuplicateMember.sample)
            session.execute(
                update(DuplicateMember)
                .where(DuplicateMember.run_id == run_id, candidate.in_(matches))
                .values(eligible=True)
            )

    def publish(self, run_id: int, scan_id: int, progress: DuplicateProgress) -> None:
        member = DuplicateMember
        can_free = and_(member.identity.is_not(None), member.links == 1)
        copies = func.count(distinct(_physical()))
        groups = (
            select(
                member.run_id,
                member.size,
                member.digest,
                func.count(),
                copies,
                func.coalesce(func.min(case((~can_free, member.path))), func.min(member.path)),
                func.sum(case((can_free, member.size), else_=0)),
                func.sum(case((can_free, func.coalesce(member.allocated, 0)), else_=0)),
                func.count().filter(can_free, member.allocated.is_(None)),
            )
            .where(member.run_id == run_id, member.digest.is_not(None))
            .group_by(member.run_id, member.size, member.digest)
            .having(copies > 1)
            .order_by(member.size.desc(), member.digest)
        )
        matching = and_(
            DuplicateGroup.run_id == member.run_id,
            DuplicateGroup.size == member.size,
            DuplicateGroup.digest == member.digest,
        )
        with Session(self.engine) as session, session.begin():
            session.execute(
                insert(DuplicateGroup).from_select(
                    (
                        "run_id",
                        "size",
                        "digest",
                        "paths",
                        "copies",
                        "keeper",
                        "logical",
                        "allocated",
                        "unknown",
                    ),
                    groups,
                )
            )
            session.execute(
                delete(member).where(
                    member.run_id == run_id,
                    ~select(DuplicateGroup.id).where(matching).exists(),
                )
            )
            session.execute(
                update(DuplicateRun)
                .where(DuplicateRun.id == run_id)
                .values(
                    complete=True,
                    skipped=progress.skipped,
                    bytes_read=progress.bytes_read,
                    cache_hits=progress.cache_hits,
                    elapsed=progress.elapsed,
                )
            )
            session.execute(
                delete(DuplicateRun).where(
                    DuplicateRun.scan_id == scan_id,
                    DuplicateRun.id != run_id,
                    DuplicateRun.complete.is_(True),
                )
            )

    def discard(self, run_id: int) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(
                delete(DuplicateRun).where(
                    DuplicateRun.id == run_id,
                    DuplicateRun.complete.is_(False),
                )
            )

    def prune_cache(self) -> None:
        with Session(self.engine) as session, session.begin():
            session.execute(delete(HashCache).where(HashCache.touched < time() - _CACHE_TTL))
            # A single indexed cutoff avoids collecting path names in Python.
            cutoff = session.execute(
                select(HashCache.touched, HashCache.path)
                .order_by(HashCache.touched.desc(), HashCache.path.desc())
                .offset(_CACHE_LIMIT)
                .limit(1)
            ).one_or_none()
            if cutoff is not None:
                session.execute(
                    delete(HashCache).where(
                        tuple_(HashCache.touched, HashCache.path)
                        <= tuple_(literal(cutoff[0]), literal(cutoff[1]))
                    )
                )

    def report(self, scan_id: int | None, after: int | None = None) -> DuplicateReport:
        with Session(self.engine) as session:
            scan = self._snapshot(session, scan_id)
            if scan is None:
                return DuplicateReport()
            run = session.scalar(
                select(DuplicateRun)
                .where(
                    DuplicateRun.scan_id == scan.id,
                    DuplicateRun.complete.is_(True),
                )
                .order_by(DuplicateRun.id.desc())
                .limit(1)
            )
            if run is None:
                return DuplicateReport(scan.id, scan.root)
            group, member = DuplicateGroup, DuplicateMember
            join = and_(group.run_id == member.run_id, group.keeper == member.path)
            scope = group.run_id == run.id
            keeper_frees = and_(member.identity.is_not(None), member.links == 1)
            count, logical, allocation, unknown = session.execute(
                select(
                    func.count(),
                    func.sum(group.logical - case((keeper_frees, group.size), else_=0)),
                    func.sum(
                        group.allocated
                        - case((keeper_frees, func.coalesce(member.allocated, 0)), else_=0)
                    ),
                    func.sum(
                        group.unknown
                        - case((and_(keeper_frees, member.allocated.is_(None)), 1), else_=0)
                    ),
                )
                .select_from(group)
                .join(member, join)
                .where(scope)
            ).one()
            page = session.execute(
                select(group, member)
                .join(member, join)
                .where(scope, group.id > (after or 0))
                .order_by(group.id)
                .limit(PAGE + 1)
            ).all()
            rows = tuple(_group(group_row, keeper) for group_row, keeper in page[:PAGE])
            return DuplicateReport(
                scan.id,
                scan.root,
                run.id,
                rows,
                rows[-1].id if len(page) > PAGE else None,
                count,
                logical or 0,
                None if unknown else allocation or 0,
                run.skipped,
                run.elapsed,
                run.bytes_read,
                run.cache_hits,
            )

    @staticmethod
    def _get_group(
        session: Session, run_id: int, group_id: int
    ) -> tuple[DuplicateRun, DuplicateGroup]:
        rows = session.execute(
            select(DuplicateRun, DuplicateGroup)
            .join(DuplicateGroup)
            .where(
                DuplicateRun.id == run_id,
                DuplicateRun.complete.is_(True),
                DuplicateGroup.id == group_id,
            )
        ).one_or_none()
        if rows is None:
            raise ValueError("This duplicate group is no longer available.")
        return rows[0], rows[1]

    @staticmethod
    def _member_filter(group: DuplicateGroup) -> ColumnElement[bool]:
        return and_(
            DuplicateMember.run_id == group.run_id,
            DuplicateMember.size == group.size,
            DuplicateMember.digest == group.digest,
        )

    @staticmethod
    def _member(row: DuplicateMember, entry: InventoryEntry, group: DuplicateGroup) -> Member:
        assert row.stamp is not None and row.links is not None
        return Member(
            _version(entry),
            row.stamp,
            row.links,
            row.identity,
            row.allocated,
            row.path == group.keeper,
        )

    def members(
        self,
        run_id: int,
        group_id: int,
        after: str | None,
        keep: str | None = None,
    ) -> MembersPage:
        with Session(self.engine) as session, session.begin():
            run, group = self._get_group(session, run_id, group_id)
            member = DuplicateMember
            scope = self._member_filter(group)
            if keep is not None:
                chosen = session.scalar(select(member).where(scope, member.path == keep))
                if chosen is None:
                    raise ValueError("The keeper must be a member of this duplicate group.")
                group.keeper = keep
            keeper = session.get(member, (run_id, group.keeper))
            assert keeper is not None
            pairs = session.execute(
                select(member, InventoryEntry)
                .join(
                    InventoryEntry,
                    and_(InventoryEntry.scan_id == run.scan_id, InventoryEntry.path == member.path),
                )
                .where(scope, member.path > (after or ""))
                .order_by(member.path)
                .limit(PAGE + 1)
            ).all()
            rows = tuple(self._member(row, entry, group) for row, entry in pairs[:PAGE])
            next_page = rows[-1].version.path if len(pairs) > PAGE else None
            return MembersPage(run_id, group_id, _group(group, keeper), rows, next_page)

    def review(
        self,
        run_id: int,
        group_id: int,
        path: str,
    ) -> tuple[str, Member, Member]:
        with Session(self.engine) as session:
            run, group = self._get_group(session, run_id, group_id)
            scan = session.get(Scan, run.scan_id)
            assert scan is not None
            paired = {
                row.path: self._member(row, entry, group)
                for row, entry in session.execute(
                    select(DuplicateMember, InventoryEntry)
                    .join(
                        InventoryEntry,
                        and_(
                            InventoryEntry.scan_id == run.scan_id,
                            InventoryEntry.path == DuplicateMember.path,
                        ),
                    )
                    .where(
                        self._member_filter(group), DuplicateMember.path.in_((group.keeper, path))
                    )
                )
            }
            if path not in paired or group.keeper not in paired:
                raise ValueError("The selected file is no longer in this duplicate group.")
            return scan.root, paired[path], paired[group.keeper]
