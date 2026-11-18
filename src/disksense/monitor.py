"""Watchdog ingress and SQLite-backed reconciliation on the storage worker."""

import os
import stat
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from queue import Full
from threading import Lock
from time import monotonic, time

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver, EventDispatcher, EventQueue, ObservedWatch

from disksense.database import ScanRepository, StorageSnapshot, WatchEvent, WatchRoot
from disksense.scanner import Inventory, ScanCancelled, ScanControl, _linked, _native_path

ROOT_LIMIT = 32
HISTORY = 120
_EVENT_LIMIT = 2000
_SNAPSHOT_LIMIT = 2000


@dataclass(frozen=True, slots=True)
class Change:
    root_id: int
    observed: float
    kind: str
    source: str
    destination: str | None = None


@dataclass(frozen=True, slots=True)
class Root:
    id: int
    path: str
    enabled: bool
    interval: int
    threshold: int
    cooldown: int
    status: str


@dataclass(frozen=True, slots=True)
class Snapshot:
    observed: float
    files: int
    logical: int
    allocated: int | None
    error: str | None
    growth_alert: int | None


@dataclass(frozen=True, slots=True)
class MonitorData:
    roots: tuple[Root, ...]
    selected: int | None
    snapshots: tuple[Snapshot, ...]
    events: tuple[Change, ...]
    alert: tuple[str, int] | None = None


class EventBuffer:
    """Small bounded handoff from watchdog threads to the storage worker."""

    def __init__(self, capacity: int = 2048) -> None:
        self._capacity = capacity
        self._events: deque[Change] = deque()
        self._overflow: set[int] = set()
        self._backend_overflow = False
        self._lock = Lock()

    def push(self, change: Change) -> None:
        with self._lock:
            if len(self._events) >= self._capacity:
                self._overflow.add(change.root_id)
            else:
                self._events.append(change)

    def backend_overflow(self) -> None:
        with self._lock:
            self._backend_overflow = True

    def drain(self) -> tuple[list[Change], set[int], bool]:
        with self._lock:
            events, overflow = list(self._events), self._overflow
            backend_overflow = self._backend_overflow
            self._events.clear()
            self._overflow = set()
            self._backend_overflow = False
        return events, overflow, backend_overflow

    def discard(self, root_id: int) -> None:
        with self._lock:
            self._events = deque(change for change in self._events if change.root_id != root_id)
            self._overflow.discard(root_id)


class _ObserverQueue(EventQueue):
    def __init__(self, on_overflow: Callable[[], None]) -> None:
        super().__init__(maxsize=4096)
        self._on_overflow = on_overflow

    def put(self, item: object, block: bool = True, timeout: float | None = None) -> None:
        # Emitters must never block a watchdog stop/join waiting for the dispatcher.
        try:
            super().put(item, block=False)
        except Full:
            if item is not EventDispatcher.stop_event:
                self._on_overflow()


def _key(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _validate_root(root: Path) -> None:
    for path in (*reversed(root.parents), root):
        info = os.lstat(_native_path(str(path)))
        if _linked(info) or not stat.S_ISDIR(info.st_mode):
            raise OSError(f"Monitoring requires an ordinary directory: {path}")


class _Handler(FileSystemEventHandler):
    def __init__(
        self, root_id: int, root: Path, ignored: frozenset[str], buffer: EventBuffer
    ) -> None:
        super().__init__()
        self._root_id = root_id
        self._root = root
        self._ignored = ignored
        self._buffer = buffer

    def _relative(self, value: str | bytes) -> str | None:
        if not value:
            return None
        path = Path(os.fsdecode(value))
        if _key(path) in self._ignored or not path.is_relative_to(self._root):
            return None
        return path.relative_to(self._root).as_posix()

    def on_any_event(self, event: FileSystemEvent) -> None:
        kind = event.event_type
        if kind not in ("created", "deleted", "moved", "modified"):
            return
        if kind == "modified" and event.is_directory:
            return
        source = self._relative(event.src_path)
        dest = self._relative(event.dest_path)
        if kind == "moved":
            if source is None:
                kind, source, dest = "created", dest, None
            elif dest is None:
                kind = "deleted"
        if source is not None:
            self._buffer.push(Change(self._root_id, time(), kind, source, dest))


class MonitorService:
    def __init__(self, repo: ScanRepository, database: Path, *, capacity: int = 2048) -> None:
        self.engine = repo.engine
        self.buffer = EventBuffer(capacity)
        self._ignored = frozenset(
            _key(path)
            for path in (
                database,
                database.with_suffix(".lock"),
                *(Path(str(database) + suffix) for suffix in ("-wal", "-shm", "-journal")),
            )
        )
        self._observer: BaseObserver | None = None
        self._watches: dict[int, ObservedWatch] = {}
        self._last_run: dict[int, float] = {}
        self._dirty: set[int] = set()
        self._modified: dict[tuple[int, str], float] = {}

    def configure(
        self,
        root_id: int | None,
        path: str,
        enabled: bool,
        interval: int,
        threshold: int,
        cooldown: int,
    ) -> int:
        if not (5 <= interval <= 86400 and 0 <= threshold < 2**63 and 1 <= cooldown <= 86400):
            raise ValueError("Invalid interval or growth alert settings.")
        with Session(self.engine) as session, session.begin():
            if root_id is None:
                root = Path(path)
                if not root.is_absolute():
                    raise ValueError("Select an absolute folder path.")
                root = Path(os.path.abspath(root))
                _validate_root(root)
                if session.scalar(select(WatchRoot.id).where(WatchRoot.key == _key(root))):
                    raise ValueError("This folder is already in Storage Monitor.")
                if len(list(session.scalars(select(WatchRoot.id).limit(ROOT_LIMIT)))) == ROOT_LIMIT:
                    raise ValueError(f"Storage Monitor supports up to {ROOT_LIMIT} folders.")
                row = WatchRoot(path=str(root), key=_key(root))
                session.add(row)
            else:
                existing = session.get(WatchRoot, root_id)
                if existing is None:
                    raise ValueError("The watched folder no longer exists.")
                row = existing
            row.enabled, row.interval, row.threshold, row.cooldown = (
                enabled,
                interval,
                threshold,
                cooldown,
            )
            row.status = "Pending reconciliation" if enabled else "Paused"
            session.flush()
            identifier = row.id
        self._last_run.pop(identifier, None)
        if not enabled:
            self._detach(identifier)
            self.buffer.discard(identifier)
            self._dirty.discard(identifier)
        return identifier

    def remove(self, root_id: int) -> MonitorData:
        with Session(self.engine) as session, session.begin():
            session.execute(delete(WatchRoot).where(WatchRoot.id == root_id))
        self._detach(root_id)
        self.buffer.discard(root_id)
        self._dirty.discard(root_id)
        self._last_run.pop(root_id, None)
        return self.read(None)

    def _attach(self, row: WatchRoot) -> None:
        observer = self._observer
        watch = self._watches.get(row.id)
        if watch is not None and observer is not None:
            if any(e.watch == watch and e.is_alive() for e in observer.emitters):
                return
            self._detach(row.id)
            observer = self._observer
        if observer is None:
            observer = Observer()
            # Watchdog exposes the dispatcher queue as a read-only property.
            observer._event_queue = _ObserverQueue(self.buffer.backend_overflow)
            observer.start()
            self._observer = observer
        root = Path(row.path)
        self._watches[row.id] = observer.schedule(
            _Handler(row.id, root, self._ignored, self.buffer), str(root), recursive=True
        )

    def _detach(self, root_id: int) -> None:
        watch = self._watches.pop(root_id, None)
        observer = self._observer
        if watch is not None and observer is not None:
            observer.unschedule(watch)
        if observer is not None and not self._watches:
            self.close()

    def close(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        self._watches.clear()

    @staticmethod
    def _trim(
        session: Session, model: type[WatchEvent] | type[StorageSnapshot], root_id: int, limit: int
    ) -> None:
        cutoff = session.scalar(
            select(model.id)
            .where(model.root_id == root_id)
            .order_by(model.id.desc())
            .offset(limit)
            .limit(1)
        )
        if cutoff is not None:
            session.execute(delete(model).where(model.root_id == root_id, model.id <= cutoff))

    def _ingest(self, session: Session, roots: dict[int, WatchRoot]) -> None:
        events, overflow, backend_overflow = self.buffer.drain()
        if backend_overflow:
            overflow.update(roots)
        now = time()
        self._modified = {key: stamp for key, stamp in self._modified.items() if now - stamp < 1}
        records = []
        touched: set[int] = set()
        for change in events:
            if change.root_id not in roots:
                continue
            self._dirty.add(change.root_id)
            key = change.root_id, change.source
            if change.kind == "modified":
                if change.observed - self._modified.get(key, 0) < 0.5:
                    continue
                if len(self._modified) < 4096:
                    self._modified[key] = change.observed
            records.append(
                dict(
                    root_id=change.root_id,
                    observed=change.observed,
                    kind=change.kind,
                    source=change.source,
                    destination=change.destination,
                )
            )
            touched.add(change.root_id)
        for root_id in overflow.intersection(roots):
            roots[root_id].status = "Stale · event buffer overflow"
            self._dirty.add(root_id)
            records.append(
                dict(root_id=root_id, observed=now, kind="overflow", source=".", destination=None)
            )
            touched.add(root_id)
        if records:
            session.execute(insert(WatchEvent), records)
            for root_id in touched:
                self._trim(session, WatchEvent, root_id, _EVENT_LIMIT)

    def _snapshot(self, row: WatchRoot, control: ScanControl) -> tuple[str, int] | None:
        root = Path(row.path)
        inventory = Inventory(root, lambda relative: _key(root / relative) in self._ignored)
        watch_error = None
        try:
            _validate_root(root)
            try:
                self._attach(row)
            except OSError as error:
                self._detach(row.id)
                watch_error = str(error)
            inventory.run(control, lambda _: None, lambda _: None)
            error_text = inventory.first_error
        except ScanCancelled:
            self._last_run[row.id] = monotonic()
            return None
        except OSError as error:
            self._detach(row.id)
            error_text = str(error)
        sample = inventory.progress()
        observed = time()
        alert = None
        with Session(self.engine) as session, session.begin():
            active = session.get(WatchRoot, row.id)
            assert active is not None
            if error_text is not None:
                active.status = f"Stale · {error_text}"
            else:
                active.status = f"Snapshots only · {watch_error}" if watch_error else "Watching"
                if active.baseline is None or sample.logical_bytes < active.baseline:
                    active.baseline = sample.logical_bytes
                growth = sample.logical_bytes - active.baseline
                if (
                    active.threshold
                    and growth >= active.threshold
                    and observed - (active.last_alert or 0) >= active.cooldown
                ):
                    alert = growth
                    active.baseline = sample.logical_bytes
                    active.last_alert = observed
            session.add(
                StorageSnapshot(
                    root_id=row.id,
                    observed=observed,
                    files=sample.files,
                    logical=sample.logical_bytes,
                    allocated=sample.allocated_bytes,
                    error=error_text,
                    growth_alert=alert,
                )
            )
            session.flush()
            self._trim(session, StorageSnapshot, row.id, _SNAPSHOT_LIMIT)
        self._last_run[row.id] = monotonic()
        self._dirty.discard(row.id)
        return None if alert is None else (row.path, alert)

    def poll(
        self, selected: int | None, control: ScanControl, *, force: int | None = None
    ) -> MonitorData:
        with Session(self.engine) as session, session.begin():
            active = {
                row.id: row
                for row in session.scalars(select(WatchRoot).where(WatchRoot.enabled.is_(True)))
            }
            self._ingest(session, active)
            session.flush()
            for row in active.values():
                session.expunge(row)
        now = monotonic()
        due = [
            row
            for row in active.values()
            if row.id == force
            or row.id not in self._last_run
            or now - self._last_run[row.id]
            >= (min(row.interval, 10) if row.id in self._dirty else row.interval)
        ]
        due.sort(key=lambda row: (row.id != force, self._last_run.get(row.id, 0), row.id))
        alert = self._snapshot(due[0], control) if due else None
        return self.read(selected, alert)

    def read(self, selected: int | None, alert: tuple[str, int] | None = None) -> MonitorData:
        with Session(self.engine) as session:
            roots = tuple(
                Root(
                    row.id,
                    row.path,
                    row.enabled,
                    row.interval,
                    row.threshold,
                    row.cooldown,
                    row.status,
                )
                for row in session.scalars(select(WatchRoot).order_by(WatchRoot.id))
            )
            if selected not in {root.id for root in roots}:
                selected = roots[0].id if roots else None
            snapshots = tuple(
                Snapshot(
                    row.observed, row.files, row.logical, row.allocated, row.error, row.growth_alert
                )
                for row in session.scalars(
                    select(StorageSnapshot)
                    .where(StorageSnapshot.root_id == selected)
                    .order_by(StorageSnapshot.id.desc())
                    .limit(HISTORY)
                )
            )
            events = tuple(
                Change(row.root_id, row.observed, row.kind, row.source, row.destination)
                for row in session.scalars(
                    select(WatchEvent)
                    .where(WatchEvent.root_id == selected)
                    .order_by(WatchEvent.id.desc())
                    .limit(HISTORY)
                )
            )
            return MonitorData(roots, selected, snapshots, events, alert)
