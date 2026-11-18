"""Versioned SQLite schema and scan repository."""

from collections.abc import Callable
from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from sqlite3 import Connection as SQLiteConnection
from typing import Self

from sqlalchemy import (
    URL,
    BigInteger,
    ForeignKey,
    Index,
    and_,
    create_engine,
    delete,
    event,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Connection
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, aliased, mapped_column

from bytecontour.scanner import Entry, ScanProgress, ScanState

HISTORY_PAGE_SIZE = 50


class Base(DeclarativeBase):
    pass


class Scan(Base):
    __tablename__ = "scans"
    __table_args__ = (Index("ix_scans_root_state_id", "root_key", "state", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    root: Mapped[str]
    root_key: Mapped[str]
    state: Mapped[ScanState]
    started_at: Mapped[str]
    finished_at: Mapped[str | None]
    baseline_id: Mapped[int | None]
    files: Mapped[int] = mapped_column(default=0)
    directories: Mapped[int] = mapped_column(default=0)
    logical_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    allocated_bytes: Mapped[int | None] = mapped_column(BigInteger)
    skipped: Mapped[int] = mapped_column(default=0)
    errors: Mapped[int] = mapped_column(default=0)
    elapsed: Mapped[float] = mapped_column(default=0.0)
    detail: Mapped[str | None]
    added: Mapped[int | None]
    changed: Mapped[int | None]
    removed: Mapped[int | None]


class InventoryEntry(Base):
    __tablename__ = "entries"
    __table_args__ = (
        Index("ix_entries_parent", "scan_id", "parent", "path"),
        Index("ix_entries_size", "scan_id", "kind", "logical_bytes", "path"),
        Index("ix_entries_extension", "scan_id", "extension"),
        Index("ix_entries_modified", "scan_id", "modified_ns"),
    )

    scan_id: Mapped[int] = mapped_column(
        ForeignKey("scans.id", ondelete="CASCADE"), primary_key=True
    )
    path: Mapped[str] = mapped_column(primary_key=True)
    parent: Mapped[str | None]
    kind: Mapped[str]
    extension: Mapped[str]
    logical_bytes: Mapped[int] = mapped_column(BigInteger)
    allocated_bytes: Mapped[int | None] = mapped_column(BigInteger)
    modified_ns: Mapped[int] = mapped_column(BigInteger)
    change_ns: Mapped[int] = mapped_column(BigInteger)
    identity: Mapped[str | None]
    links: Mapped[int]
    file_count: Mapped[int]
    incomplete: Mapped[bool]


class DuplicateRun(Base):
    __tablename__ = "duplicate_runs"
    __table_args__ = (Index("ix_duplicate_runs_scan_state", "scan_id", "complete", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_id: Mapped[int] = mapped_column(ForeignKey("scans.id", ondelete="CASCADE"))
    complete: Mapped[bool]
    exclusions: Mapped[str]
    minimum: Mapped[int] = mapped_column(BigInteger)
    skipped: Mapped[int] = mapped_column(default=0)
    bytes_read: Mapped[int] = mapped_column(BigInteger, default=0)
    cache_hits: Mapped[int] = mapped_column(default=0)
    elapsed: Mapped[float] = mapped_column(default=0.0)


class DuplicateMember(Base):
    __tablename__ = "duplicate_members"
    __table_args__ = (
        Index("ix_members_sample", "run_id", "size", "sample"),
        Index("ix_members_digest", "run_id", "size", "digest", "path"),
    )

    run_id: Mapped[int] = mapped_column(
        ForeignKey("duplicate_runs.id", ondelete="CASCADE"), primary_key=True
    )
    path: Mapped[str] = mapped_column(primary_key=True)
    size: Mapped[int] = mapped_column(BigInteger)
    sample: Mapped[str | None]
    digest: Mapped[str | None]
    stamp: Mapped[str | None]
    identity: Mapped[str | None]
    links: Mapped[int | None]
    allocated: Mapped[int | None] = mapped_column(BigInteger)
    eligible: Mapped[bool] = mapped_column(default=False)


class DuplicateGroup(Base):
    __tablename__ = "duplicate_groups"
    __table_args__ = (Index("ix_groups_run_size_digest", "run_id", "size", "digest", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("duplicate_runs.id", ondelete="CASCADE"))
    size: Mapped[int] = mapped_column(BigInteger)
    digest: Mapped[str]
    paths: Mapped[int]
    copies: Mapped[int]
    keeper: Mapped[str]
    logical: Mapped[int] = mapped_column(BigInteger)
    allocated: Mapped[int] = mapped_column(BigInteger)
    unknown: Mapped[int]


class HashCache(Base):
    __tablename__ = "hash_cache"
    __table_args__ = (Index("ix_hash_cache_touched", "touched"),)

    path: Mapped[str] = mapped_column(primary_key=True)
    stamp: Mapped[str]
    sample: Mapped[str]
    digest: Mapped[str | None]
    touched: Mapped[float]


class CleanupOperation(Base):
    __tablename__ = "cleanup_operations"
    __table_args__ = (Index("ix_cleanup_approved", "approved_at", "id"),)

    id: Mapped[str] = mapped_column(primary_key=True)
    scan_id: Mapped[int]
    root: Mapped[str]
    approved_at: Mapped[str]
    state: Mapped[str]


class CleanupItem(Base):
    __tablename__ = "cleanup_items"

    operation_id: Mapped[str] = mapped_column(
        ForeignKey("cleanup_operations.id", ondelete="CASCADE"), primary_key=True
    )
    path: Mapped[str] = mapped_column(primary_key=True)
    reason: Mapped[str]
    state: Mapped[str]
    detail: Mapped[str]


class WatchRoot(Base):
    __tablename__ = "watch_roots"
    __table_args__ = {"sqlite_autoincrement": True}

    id: Mapped[int] = mapped_column(primary_key=True)
    path: Mapped[str]
    key: Mapped[str] = mapped_column(unique=True)
    enabled: Mapped[bool]
    interval: Mapped[int]
    threshold: Mapped[int] = mapped_column(BigInteger)
    cooldown: Mapped[int]
    status: Mapped[str]
    baseline: Mapped[int | None] = mapped_column(BigInteger)
    last_alert: Mapped[float | None]


class WatchEvent(Base):
    __tablename__ = "watch_events"
    __table_args__ = (Index("ix_watch_events_root_id", "root_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    root_id: Mapped[int] = mapped_column(ForeignKey("watch_roots.id", ondelete="CASCADE"))
    observed: Mapped[float]
    kind: Mapped[str]
    source: Mapped[str]
    destination: Mapped[str | None]


class StorageSnapshot(Base):
    __tablename__ = "storage_snapshots"
    __table_args__ = (Index("ix_storage_snapshots_root_id", "root_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    root_id: Mapped[int] = mapped_column(ForeignKey("watch_roots.id", ondelete="CASCADE"))
    observed: Mapped[float]
    files: Mapped[int]
    logical: Mapped[int] = mapped_column(BigInteger)
    allocated: Mapped[int | None] = mapped_column(BigInteger)
    error: Mapped[str | None]
    growth_alert: Mapped[int | None] = mapped_column(BigInteger)


class RecommendationDismissal(Base):
    __tablename__ = "recommendation_dismissals"

    key: Mapped[str] = mapped_column(primary_key=True)
    fingerprint: Mapped[str] = mapped_column(primary_key=True)
    dismissed_at: Mapped[str]


@dataclass(frozen=True, slots=True)
class ScanSummary:
    id: int
    root: str
    state: ScanState
    started_at: str
    finished_at: str | None
    baseline_id: int | None
    files: int
    directories: int
    logical_bytes: int
    allocated_bytes: int | None
    skipped: int
    errors: int
    elapsed: float
    detail: str | None
    added: int | None
    changed: int | None
    removed: int | None

    @classmethod
    def from_scan(cls, scan: Scan) -> Self:
        return cls(
            scan.id,
            scan.root,
            scan.state,
            scan.started_at,
            scan.finished_at,
            scan.baseline_id,
            scan.files,
            scan.directories,
            scan.logical_bytes,
            scan.allocated_bytes,
            scan.skipped,
            scan.errors,
            scan.elapsed,
            scan.detail,
            scan.added,
            scan.changed,
            scan.removed,
        )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _create_schema(connection: Connection) -> None:
    Base.metadata.create_all(
        connection, tables=[Base.metadata.tables[name] for name in ("scans", "entries")]
    )


def _create_duplicate_schema(connection: Connection) -> None:
    Base.metadata.create_all(
        connection,
        tables=[
            Base.metadata.tables[name]
            for name in ("duplicate_runs", "duplicate_members", "duplicate_groups", "hash_cache")
        ],
    )


def _create_cleanup_schema(connection: Connection) -> None:
    Base.metadata.create_all(
        connection,
        tables=[Base.metadata.tables[name] for name in ("cleanup_operations", "cleanup_items")],
    )


def _create_monitor_schema(connection: Connection) -> None:
    Base.metadata.create_all(
        connection,
        tables=[
            Base.metadata.tables[name]
            for name in (
                "watch_roots",
                "watch_events",
                "storage_snapshots",
            )
        ],
    )


def _create_recommendation_schema(connection: Connection) -> None:
    Base.metadata.tables["recommendation_dismissals"].create(connection, checkfirst=True)


_MIGRATIONS: tuple[Callable[[Connection], None], ...] = (
    _create_schema,
    _create_duplicate_schema,
    _create_cleanup_schema,
    _create_monitor_schema,
    _create_recommendation_schema,
)
SCHEMA_VERSION = len(_MIGRATIONS)
_ENTRY_FIELDS = tuple(field.name for field in fields(Entry))
_SNAPSHOT_STATES = ("completed", "partial")


def _configure_connection(connection: SQLiteConnection, _: object) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")


class ScanRepository:
    def __init__(self, path: Path, retain_per_root: int = 5) -> None:
        if retain_per_root < 1:
            raise ValueError("At least one scan per root must be retained.")
        self.retain_per_root = retain_per_root
        self.engine = create_engine(URL.create("sqlite", database=str(path)))
        event.listen(self.engine, "connect", _configure_connection)

    def initialize(self) -> None:
        """Migrate and recover once the app owns the database writer lock."""
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode = WAL")
            connection.commit()
        with self.engine.begin() as connection:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
            version = int(connection.exec_driver_sql("PRAGMA user_version").scalar_one())
            if version > len(_MIGRATIONS):
                raise RuntimeError("This database was created by a newer version of ByteContour.")
            for index in range(version, len(_MIGRATIONS)):
                _MIGRATIONS[index](connection)
                connection.exec_driver_sql(f"PRAGMA user_version = {index + 1}")
        with Session(self.engine) as session, session.begin():
            session.execute(
                update(CleanupItem)
                .where(CleanupItem.state == "attempting")
                .values(
                    state="indeterminate",
                    detail="Interrupted during recycling. Check the Recycle Bin.",
                )
            )
            session.execute(
                update(CleanupItem)
                .where(CleanupItem.state == "pending")
                .values(state="skipped", detail="Interrupted before this item was attempted.")
            )
            session.execute(
                update(CleanupOperation)
                .where(CleanupOperation.state == "running")
                .values(state="interrupted")
            )
            cutoff = (datetime.now(UTC) - timedelta(days=365)).isoformat(timespec="seconds")
            session.execute(delete(CleanupOperation).where(CleanupOperation.approved_at < cutoff))
            session.execute(delete(DuplicateRun).where(DuplicateRun.complete.is_(False)))
            running = select(Scan.id).where(Scan.state == "running")
            session.execute(delete(InventoryEntry).where(InventoryEntry.scan_id.in_(running)))
            session.execute(
                update(Scan)
                .where(Scan.state == "running")
                .values(
                    state="interrupted",
                    finished_at=_utc_now(),
                    detail="ByteContour closed before publishing this scan.",
                )
            )

    def begin(self, root: str, root_key: str, incremental: bool) -> tuple[int, int | None]:
        with Session(self.engine) as session, session.begin():
            baseline = self._latest_completed(session, root_key) if incremental else None
            scan = Scan(
                root=root,
                root_key=root_key,
                state="running",
                started_at=_utc_now(),
                baseline_id=baseline,
            )
            session.add(scan)
            session.flush()
            return scan.id, baseline

    @staticmethod
    def _latest_completed(session: Session, root_key: str) -> int | None:
        return session.scalar(
            select(Scan.id)
            .where(Scan.root_key == root_key, Scan.state == "completed")
            .order_by(Scan.id.desc())
            .limit(1)
        )

    def append(self, scan_id: int, entries: list[Entry]) -> None:
        rows = [
            {"scan_id": scan_id, **{name: getattr(entry, name) for name in _ENTRY_FIELDS}}
            for entry in entries
        ]
        with Session(self.engine) as session, session.begin():
            session.execute(insert(InventoryEntry).execution_options(render_nulls=True), rows)

    def finish(
        self, scan_id: int, state: ScanState, progress: ScanProgress, detail: str | None
    ) -> ScanSummary:
        with Session(self.engine) as session, session.begin():
            scan = session.get(Scan, scan_id)
            if scan is None or scan.state != "running":
                raise RuntimeError("The scan is no longer writable.")
            scan.state = state
            scan.finished_at = _utc_now()
            scan.files = progress.files
            scan.directories = progress.directories
            scan.logical_bytes = progress.logical_bytes
            scan.allocated_bytes = progress.allocated_bytes
            scan.skipped = progress.skipped
            scan.errors = progress.errors
            scan.elapsed = progress.elapsed
            scan.detail = detail
            if state not in _SNAPSHOT_STATES:
                session.execute(delete(InventoryEntry).where(InventoryEntry.scan_id == scan_id))
            elif state == "completed" and scan.baseline_id is not None:
                scan.added, scan.changed, scan.removed = self._diff(
                    session, scan_id, scan.baseline_id
                )
            session.flush()
            self._prune(session, scan.root_key)
            return ScanSummary.from_scan(scan)

    @staticmethod
    def _diff(session: Session, scan_id: int, baseline: int) -> tuple[int, int, int]:
        old = aliased(InventoryEntry)
        new = aliased(InventoryEntry)
        fields_to_compare = (
            "logical_bytes",
            "allocated_bytes",
            "modified_ns",
            "change_ns",
            "identity",
            "links",
        )
        changed = or_(
            *(getattr(new, name).is_distinct_from(getattr(old, name)) for name in fields_to_compare)
        )
        paired = and_(old.path == new.path, old.scan_id == baseline, old.kind == "file")
        additions, changes = session.execute(
            select(
                func.count().filter(old.scan_id.is_(None)),
                func.count().filter(old.scan_id.is_not(None), changed),
            )
            .select_from(new)
            .outerjoin(old, paired)
            .where(new.scan_id == scan_id, new.kind == "file")
        ).one()
        removed = session.scalar(
            select(func.count())
            .select_from(old)
            .outerjoin(new, and_(new.path == old.path, new.scan_id == scan_id, new.kind == "file"))
            .where(old.scan_id == baseline, old.kind == "file", new.scan_id.is_(None))
        )
        return int(additions), int(changes), removed or 0

    def _prune(self, session: Session, root_key: str) -> None:
        keep = (
            select(Scan.id)
            .where(Scan.root_key == root_key, Scan.state != "running")
            .order_by(Scan.id.desc())
            .limit(self.retain_per_root)
        )
        latest_completed = self._latest_completed(session, root_key)
        stale = delete(Scan).where(
            Scan.root_key == root_key, Scan.state != "running", Scan.id.not_in(keep)
        )
        if latest_completed is not None:
            stale = stale.where(Scan.id != latest_completed)
        session.execute(stale)

    def history(self, before: int | None = None) -> tuple[list[ScanSummary], bool]:
        query = select(Scan).where(Scan.state != "running").order_by(Scan.id.desc())
        if before is not None:
            query = query.where(Scan.id < before)
        with Session(self.engine) as session:
            rows = list(session.scalars(query.limit(HISTORY_PAGE_SIZE + 1)))
            return [ScanSummary.from_scan(row) for row in rows[:HISTORY_PAGE_SIZE]], (
                len(rows) > HISTORY_PAGE_SIZE
            )

    def close(self) -> None:
        self.engine.dispose()
