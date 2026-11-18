"""Scan controls and bounded scan history views."""

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    Qt,
    Signal,
    SignalInstance,
    Slot,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from bytecontour.database import ScanSummary
from bytecontour.scanner import ScanProgress
from bytecontour.ui.scan_controller import HistoryPage, ScanController

_INVALID_INDEX = QModelIndex()


def _size(value: int | None) -> str:
    if value is None:
        return "Unavailable"
    if value < 1024:
        return f"{value:,} B"
    amount = float(value)
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB", "EiB"):
        amount /= 1024
        if amount < 1024 or unit == "EiB":
            return f"{amount:,.1f} {unit}"
    raise AssertionError("Unreachable size unit")


def _layout(widget: QWidget, title: str) -> QVBoxLayout:
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(30, 26, 30, 26)
    layout.setSpacing(12)
    heading = QLabel(title)
    heading.setObjectName("pageTitle")
    layout.addWidget(heading)
    return layout


def _note(value: str = "") -> QLabel:
    label = QLabel(value)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    return label


def _totals(summary: ScanSummary | ScanProgress) -> str:
    rate = summary.files / summary.elapsed if summary.elapsed else 0
    return (
        f"{summary.files:,} files · {summary.directories:,} folders · "
        f"{summary.skipped:,} skipped ({summary.errors:,} errors)\n"
        f"Logical size  {_size(summary.logical_bytes)}    "
        f"Allocated size  {_size(summary.allocated_bytes)}\n"
        f"{summary.elapsed:,.1f} s elapsed · {rate:,.0f} files/s average"
    )


def _result(summary: ScanSummary) -> str:
    parts = [f"{summary.state.capitalize()} · {summary.root}", _totals(summary)]
    if summary.added is not None:
        parts.append(
            f"Since scan #{summary.baseline_id}   "
            f"+{summary.added:,} added · {summary.changed or 0:,} changed · "
            f"−{summary.removed or 0:,} removed"
        )
    elif summary.baseline_id is not None and summary.state == "partial":
        parts.append("Change counts are unavailable for partial inventories.")
    if summary.detail:
        parts.append(summary.detail)
    return "\n\n".join(parts)


class ScanView(QWidget):
    def __init__(self, controller: ScanController) -> None:
        super().__init__()
        self._controller = controller
        self._scanning = self._paused = self._cancelling = False
        layout = _layout(self, "Disk Explorer")
        layout.addWidget(_note("Choose a folder or drive to create a storage inventory."))

        root_row = QHBoxLayout()
        self.root = QLineEdit()
        self.root.setAccessibleName("Scan folder or drive")
        self.root.setPlaceholderText("Folder or drive path")
        self.root.textChanged.connect(self._update_actions)
        root_row.addWidget(self.root, 1)
        self.browse = QPushButton("Browse…")
        self.browse.clicked.connect(self._choose)
        root_row.addWidget(self.browse)
        layout.addLayout(root_row)

        self.incremental = QCheckBox("Compare with the last complete scan")
        self.incremental.setChecked(True)
        layout.addWidget(self.incremental)
        buttons = QHBoxLayout()
        self.start = QPushButton("Start scan")
        self.start.clicked.connect(self._start)
        self.root.returnPressed.connect(self._start)
        self.pause = QPushButton("Pause")
        self.pause.clicked.connect(self._pause)
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(self._cancel)
        for button in (self.start, self.pause, self.cancel):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)

        self.bar = QProgressBar()
        self.bar.setAccessibleName("Scan activity")
        self.bar.setRange(0, 0)
        self.bar.hide()
        layout.addWidget(self.bar)
        self.status = _note("Ready to scan")
        self.status.setObjectName("cardTitle")
        layout.addWidget(self.status)
        self.details = _note()
        layout.addWidget(self.details)
        layout.addStretch()
        self._busy = False
        self._update_actions()
        controller.busy_changed.connect(self._set_busy)
        controller.progress.connect(self._progress)
        controller.scan_finished.connect(self._finished)
        controller.failed.connect(self._failed)

    @Slot()
    def _choose(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose a folder or drive", self.root.text())
        if path:
            self.root.setText(path)

    @Slot()
    def _start(self) -> None:
        value = self.root.text()
        if not value.strip() or self._busy:
            return
        root = Path(value)
        if not root.is_absolute():
            self.status.setText("Enter an absolute folder or drive path.")
            return
        if self._controller.start(root, self.incremental.isChecked()):
            self._scanning = True
            self.status.setText("Scanning…")
            self.details.clear()
            self.bar.show()
            self._update_actions()

    @Slot()
    def _pause(self) -> None:
        paused = not self._paused
        if self._controller.pause(paused):
            self._paused = paused
            self.status.setText("Paused" if paused else "Scanning…")
            self.bar.setVisible(not paused)
        else:
            self._cancelling = True
            self.status.setText("Saving scan…")
        self._update_actions()

    @Slot()
    def _cancel(self) -> None:
        self._cancelling = True
        self.status.setText("Cancelling…" if self._controller.cancel() else "Saving scan…")
        self.bar.show()
        self._update_actions()

    @Slot(bool)
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_actions()

    @Slot()
    def _update_actions(self) -> None:
        self.start.setEnabled(not self._busy and bool(self.root.text().strip()))
        for widget in (self.root, self.browse, self.incremental):
            widget.setEnabled(not self._busy)
        self.pause.setText("Resume" if self._paused else "Pause")
        self.pause.setEnabled(self._scanning and not self._cancelling)
        self.cancel.setEnabled(self._scanning and not self._cancelling)

    @Slot(object)
    def _progress(self, value: ScanProgress) -> None:
        self.details.setText(f"{_totals(value)}\n\n{value.current}")

    def _reset(self) -> None:
        self._scanning = self._paused = self._cancelling = False
        self.bar.hide()
        self._update_actions()

    @Slot(object)
    def _finished(self, summary: ScanSummary) -> None:
        self._reset()
        self.status.setText(f"Scan {summary.state}")
        self.details.setText(_result(summary))

    @Slot(str)
    def _failed(self, message: str) -> None:
        if self._scanning:
            self._reset()
            self.status.setText("Could not run scan")
            self.details.setText(message)


class _HistoryModel(QAbstractTableModel):
    _HEADERS = ("Folder or drive", "State", "Files", "Logical size", "Started")

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.rows: list[ScanSummary] = []

    def replace(self, rows: list[ScanSummary]) -> None:
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, /, parent: QModelIndex | QPersistentModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, /, parent: QModelIndex | QPersistentModelIndex = _INVALID_INDEX) -> int:
        return 0 if parent.isValid() else len(self._HEADERS)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, /, role: int = Qt.ItemDataRole.DisplayRole
    ) -> str | None:
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return row.root
        if role == Qt.ItemDataRole.DisplayRole:
            local_time = datetime.fromisoformat(row.started_at).astimezone()
            return (
                row.root,
                row.state.capitalize(),
                f"{row.files:,}",
                _size(row.logical_bytes),
                local_time.strftime("%Y-%m-%d %H:%M"),
            )[index.column()]
        return None

    def headerData(
        self, section: int, orientation: Qt.Orientation, /, role: int = Qt.ItemDataRole.DisplayRole
    ) -> str | None:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return self._HEADERS[section]
        return None


class HistoryView(QWidget):
    inventory_requested = Signal(int)
    duplicates_requested = Signal(int)
    cleanup_requested = Signal(int)

    def __init__(self, controller: ScanController) -> None:
        super().__init__()
        self._controller = controller
        self._loading = self._has_older = self._waiting = False
        layout = _layout(self, "Scan History")
        buttons = QHBoxLayout()
        self.newest = QPushButton("Load newest")
        self.newest.clicked.connect(self.reload)
        self.older = QPushButton("Older scans")
        self.older.setEnabled(False)
        self.older.clicked.connect(self._older)
        buttons.addWidget(self.newest)
        buttons.addWidget(self.older)
        self.browse_inventory = QPushButton("Browse inventory")
        self.browse_inventory.setEnabled(False)
        self.browse_inventory.clicked.connect(self._browse)
        buttons.addWidget(self.browse_inventory)
        self.find_duplicates = QPushButton("Find duplicates")
        self.find_duplicates.setEnabled(False)
        self.find_duplicates.clicked.connect(self._duplicates)
        buttons.addWidget(self.find_duplicates)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.status = _note("Load scan history to see saved results.")
        layout.addWidget(self.status)

        self.model = _HistoryModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.selectionModel().currentChanged.connect(self._selection)
        self.table.activated.connect(self._browse)
        layout.addWidget(self.table, 1)
        self.details = _note()
        layout.addWidget(self.details)
        self.review_cleanup = QPushButton("Review cleanup candidates")
        self.review_cleanup.setEnabled(False)
        self.review_cleanup.clicked.connect(self._cleanup)
        layout.addWidget(self.review_cleanup)
        controller.history_ready.connect(self._ready)
        controller.failed.connect(self._failed)
        controller.busy_changed.connect(self._set_busy)
        controller.idle.connect(self._pump)

    @Slot()
    def reload(self) -> None:
        self._waiting = True
        self._pump()

    def _pump(self) -> None:
        if self._waiting and not self._loading and self.isVisible():
            self._load(None)

    @Slot()
    def _older(self) -> None:
        if self.model.rows:
            self._load(self.model.rows[-1].id)

    def _load(self, before: int | None) -> None:
        if self._controller.load_history(before):
            self._waiting = False
            self._loading = True
            self.status.setText("Loading history…")
        elif not self.model.rows and not self._loading:
            self.status.setText("Scan history updates when the current task finishes.")

    @Slot(bool)
    def _set_busy(self, busy: bool) -> None:
        self.newest.setEnabled(not busy)
        self.older.setEnabled(not busy and self._has_older)

    @Slot(object)
    def _ready(self, page: HistoryPage) -> None:
        self._loading = False
        self._has_older = page.has_older
        self.older.setEnabled(page.has_older)
        self.model.replace(page.rows)
        self.status.setText(
            f"{len(page.rows)} saved scans on this page" if page.rows else "No saved scans yet"
        )
        self.details.clear()
        if page.rows:
            self.table.setCurrentIndex(self.model.index(0, 0))
        else:
            self.browse_inventory.setEnabled(False)
            self.find_duplicates.setEnabled(False)
            self.review_cleanup.setEnabled(False)

    @Slot(str)
    def _failed(self, message: str) -> None:
        if self._loading:
            self._loading = False
            self.status.setText(f"Could not load scan history. {message}")

    @Slot(QModelIndex)
    def _selection(self, index: QModelIndex) -> None:
        row = self.model.rows[index.row()] if index.isValid() else None
        self.details.setText(_result(row) if row else "")
        self.browse_inventory.setEnabled(bool(row and row.state in ("completed", "partial")))
        self.find_duplicates.setEnabled(self.browse_inventory.isEnabled())
        self.review_cleanup.setEnabled(self.browse_inventory.isEnabled())

    @Slot()
    def _browse(self) -> None:
        self._open_saved(self.inventory_requested)

    @Slot()
    def _duplicates(self) -> None:
        self._open_saved(self.duplicates_requested)

    @Slot()
    def _cleanup(self) -> None:
        self._open_saved(self.cleanup_requested)

    def _open_saved(self, signal: SignalInstance) -> None:
        index = self.table.currentIndex()
        if index.isValid():
            row = self.model.rows[index.row()]
            if row.state in ("completed", "partial"):
                signal.emit(row.id)
