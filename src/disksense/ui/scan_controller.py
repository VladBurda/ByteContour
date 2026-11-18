"""Own the SQLite writer lock and bridge scan work to queued Qt signals."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QRunnable, Qt, QThreadPool, Signal, Slot

from disksense.cleanup import Candidates, CleanupAudit, CleanupPlan, CleanupService, Rules
from disksense.database import ScanRepository, ScanSummary
from disksense.duplicate_store import (
    DuplicateProgress,
    DuplicateReport,
    DuplicateRequest,
    DuplicateStore,
    MembersPage,
)
from disksense.duplicates import DuplicateService, Review
from disksense.inventory import InventoryQuery, InventoryReader, InventoryResult
from disksense.scan_service import ScanService
from disksense.scanner import ScanCancelled, ScanControl, ScanProgress

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HistoryPage:
    rows: list[ScanSummary]
    has_older: bool


type CleanupResult = Candidates | CleanupPlan | CleanupAudit
type Result = (
    ScanSummary
    | HistoryPage
    | InventoryResult
    | DuplicateReport
    | MembersPage
    | Review
    | CleanupResult
)
type Progress = ScanProgress | DuplicateProgress
type Job = Callable[[Callable[[Progress], None]], Result]


class _Signals(QObject):
    progress = Signal(object)
    succeeded = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, job: Job) -> None:
        super().__init__()
        self.job = job
        self.signals = _Signals()

    def run(self) -> None:
        try:
            result = self.job(self.signals.progress.emit)
        except Exception as error:
            _logger.exception("storage_task_failed")
            self.signals.failed.emit(str(error))
        else:
            self.signals.succeeded.emit(result)


class ScanController(QObject):
    progress = Signal(object)
    scan_finished = Signal(object)
    history_ready = Signal(object)
    inventory_ready = Signal(object)
    duplicate_progress = Signal(object)
    duplicate_ready = Signal(object)
    members_ready = Signal(object)
    review_ready = Signal(object)
    cleanup_ready = Signal(object)
    failed = Signal(str)
    busy_changed = Signal(bool)
    idle = Signal()

    def __init__(self, parent: QObject, database_path: Path) -> None:
        super().__init__(parent)
        self._path = database_path
        self._lock = QLockFile(str(database_path.with_suffix(".lock")))
        self._lock.setStaleLockTime(0)
        self._repository = ScanRepository(database_path)
        self._service = ScanService(self._repository)
        self._reader = InventoryReader(self._repository)
        self._duplicate_store = DuplicateStore(self._repository)
        self._duplicates = DuplicateService(self._duplicate_store)
        self._cleanup = CleanupService(self._repository)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._task: _Task | None = None
        self._control: ScanControl | None = None
        self._closing = False

    def _initialize(self) -> None:
        # Only the worker accesses the database and acquires this lock.
        if self._lock.isLocked():
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._lock.tryLock(0):
            raise RuntimeError("Scan history is locked. Close other DiskSense windows and retry.")
        try:
            self._repository.initialize()
        except Exception:
            self._lock.unlock()
            raise

    def _submit(self, job: Job) -> None:
        task = _Task(job)
        self._task = task
        queued = Qt.ConnectionType.QueuedConnection
        task.signals.progress.connect(self._report, queued)
        task.signals.succeeded.connect(self._succeeded, queued)
        task.signals.failed.connect(self._failed, queued)
        self.busy_changed.emit(True)
        self._pool.start(task)

    def start(self, root: Path, incremental: bool) -> bool:
        if self._task is not None or self._closing:
            return False
        control = ScanControl()
        self._control = control

        def scan(report: Callable[[Progress], None]) -> ScanSummary:
            self._initialize()
            return self._service.scan(root, incremental, control, report)

        self._submit(scan)
        return True

    def load_history(self, before: int | None = None) -> bool:
        if self._task is not None or self._closing:
            return False

        def load(_: Callable[[Progress], None]) -> HistoryPage:
            self._initialize()
            rows, more = self._repository.history(before)
            return HistoryPage(rows, more)

        self._submit(load)
        return True

    def pause(self, paused: bool) -> bool:
        return self._control is not None and self._control.set_paused(paused)

    def load_inventory(self, query: InventoryQuery) -> bool:
        if self._task is not None or self._closing:
            return False

        def load(_: Callable[[Progress], None]) -> InventoryResult:
            self._initialize()
            return self._reader.read(query)

        self._submit(load)
        return True

    def find_duplicates(self, request: DuplicateRequest) -> bool:
        if self._task is not None or self._closing:
            return False
        control = ScanControl()
        self._control = control

        def detect(report: Callable[[Progress], None]) -> DuplicateReport:
            self._initialize()
            return self._duplicates.detect(request, control, report)

        self._submit(detect)
        return True

    def load_duplicates(self, scan_id: int | None, after: int | None) -> bool:
        if self._task is not None or self._closing:
            return False

        def load(_: Callable[[Progress], None]) -> DuplicateReport:
            self._initialize()
            return self._duplicate_store.report(scan_id, after)

        self._submit(load)
        return True

    def load_members(
        self,
        run_id: int,
        group_id: int,
        after: str | None,
        scan_id: int,
        group_after: int | None,
        keep: str | None = None,
    ) -> bool:
        if self._task is not None or self._closing:
            return False

        def load(_: Callable[[Progress], None]) -> MembersPage:
            page = self._duplicate_store.members(run_id, group_id, after, keep)
            if keep is not None:
                return replace(page, report=self._duplicate_store.report(scan_id, group_after))
            return page

        self._submit(load)
        return True

    def review_duplicate(self, run: int, group: int, path: str, *, verify: bool) -> bool:
        if self._task is not None or self._closing:
            return False
        control = ScanControl()
        self._control = control

        def review(_: Callable[[Progress], None]) -> Review:
            action = self._duplicates.verify if verify else self._duplicates.preview
            try:
                return action(run, group, path, control)
            except ScanCancelled:
                return Review(path, "Review cancelled", "")

        self._submit(review)
        return True

    def cancel(self) -> bool:
        return self._control is not None and self._control.cancel()

    def _cleanup_job(
        self, action: Callable[[ScanControl], CleanupResult], *, cancellable: bool = False
    ) -> bool:
        if self._task is not None or self._closing:
            return False
        control = ScanControl()
        if cancellable:
            self._control = control

        def run(_: Callable[[Progress], None]) -> CleanupResult:
            self._initialize()
            return action(control)

        self._submit(run)
        return True

    def load_cleanup(self, scan_id: int | None, rules: Rules, after: str = "") -> bool:
        return self._cleanup_job(lambda _: self._cleanup.discover(scan_id, rules, after))

    def preview_cleanup(self, scan_id: int, rules: Rules, paths: tuple[str, ...]) -> bool:
        return self._cleanup_job(lambda _: self._cleanup.preview(scan_id, rules, paths))

    def recycle_cleanup(self, token: str) -> bool:
        return self._cleanup_job(
            lambda control: self._cleanup.execute(token, control), cancellable=True
        )

    def load_cleanup_audit(self, after: tuple[str, str, str] | None = None) -> bool:
        return self._cleanup_job(lambda _: self._cleanup.audit(after))

    @Slot(object)
    def _report(self, progress: Progress) -> None:
        if isinstance(progress, ScanProgress):
            self.progress.emit(progress)
        else:
            self.duplicate_progress.emit(progress)

    def _complete(self) -> None:
        self._task = None
        self._control = None
        self.busy_changed.emit(False)

    @Slot(object)
    def _succeeded(self, result: Result) -> None:
        self._complete()
        if not self._closing:
            if isinstance(result, ScanSummary):
                self.scan_finished.emit(result)
            elif isinstance(result, InventoryResult):
                self.inventory_ready.emit(result)
            elif isinstance(result, DuplicateReport):
                self.duplicate_ready.emit(result)
            elif isinstance(result, MembersPage):
                self.members_ready.emit(result)
            elif isinstance(result, Review):
                self.review_ready.emit(result)
            elif isinstance(result, (Candidates, CleanupPlan, CleanupAudit)):
                self.cleanup_ready.emit(result)
            else:
                self.history_ready.emit(result)
        if self._task is None:
            self.idle.emit()

    @Slot(str)
    def _failed(self, error: str) -> None:
        self._complete()
        if not self._closing:
            self.failed.emit(error)
        if self._task is None:
            self.idle.emit()

    def close(self) -> bool:
        self._closing = True
        if self._task is not None:
            self.cancel()
            return False
        self.shutdown()
        return True

    def shutdown(self) -> None:
        """Drain the worker if the application event loop has already exited."""
        self._closing = True
        self.cancel()
        self._pool.waitForDone()
        self._repository.close()
        self._lock.unlock()
