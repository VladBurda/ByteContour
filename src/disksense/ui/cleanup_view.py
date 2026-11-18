"""Cleanup selection, explicit confirmation, and audit history."""

from collections.abc import Callable

from PySide6.QtCore import QModelIndex, QSettings
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from disksense.cleanup import Candidates, CleanupAudit, CleanupPlan, Rules
from disksense.ui.duplicate_view import _fill, _table
from disksense.ui.scan_controller import ScanController
from disksense.ui.scan_views import _layout, _note, _size


class CleanupView(QWidget):
    def __init__(self, controller: ScanController, settings: QSettings) -> None:
        super().__init__()
        self._controller = controller
        self._settings = settings
        self._operation = ""
        self._busy = False
        self._loaded = False
        self._wanted = False
        self._scan_id: int | None = None
        self._candidates = Candidates(None, "")
        self._rules = Rules()
        self._audit: CleanupAudit | None = None
        self.confirmation: QDialog | None = None

        layout = _layout(self, "Cleanup Center")
        layout.setContentsMargins(24, 18, 24, 18)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        candidates = QWidget()
        listing = QVBoxLayout(candidates)
        fields = QGridLayout()
        self.temporary = self._number("Temporary file age in days", "temporaryDays", 30, 36500)
        self.large_age = self._number("Large file age in days", "largeDays", 180, 36500)
        self.large_size = self._number("Large file size in MiB", "largeMiB", 512, 2**30, 1)
        for index, (title, widget) in enumerate(
            (
                ("Temp age (days)", self.temporary),
                ("Large age (days)", self.large_age),
                ("Large size (MiB)", self.large_size),
            )
        ):
            fields.addWidget(QLabel(title), 0, index)
            fields.addWidget(widget, 1, index)
        self.empty = QCheckBox("Empty folders")
        self.empty.setChecked(bool(settings.value("cleanup/empty", True, type=bool)))
        self.duplicates = QCheckBox("Duplicate copies")
        self.duplicates.setChecked(bool(settings.value("cleanup/duplicates", True, type=bool)))
        fields.addWidget(self.empty, 2, 0)
        fields.addWidget(self.duplicates, 2, 1)
        listing.addLayout(fields)
        self.scope = _note()
        listing.addWidget(self.scope)
        self.files, self.model = _table(self, ("Path", "Reason", "Logical size", "Allocated"))
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.files.selectionModel().selectionChanged.connect(self._buttons)
        listing.addWidget(self.files, 1)
        buttons = QHBoxLayout()
        self.refresh = QPushButton("Refresh candidates")
        self.refresh.clicked.connect(self._refresh)
        self.latest = QPushButton("Latest inventory")
        self.latest.clicked.connect(lambda: self.open_snapshot(None))
        self.next = QPushButton("Next page")
        self.next.clicked.connect(self._next)
        self.preview = QPushButton("Preview selected")
        self.preview.clicked.connect(self._preview)
        for button in (self.refresh, self.latest, self.next, self.preview):
            buttons.addWidget(button)
        buttons.addStretch()
        listing.addLayout(buttons)
        self.tabs.addTab(candidates, "Candidates")

        history = QWidget()
        history_layout = QVBoxLayout(history)
        self.audit_table, self.audit_model = _table(self, ("File", "Outcome", "Approved"))
        self.audit_table.selectionModel().currentChanged.connect(self._audit_detail)
        history_layout.addWidget(self.audit_table, 1)
        self.detail = _note()
        history_layout.addWidget(self.detail)
        audit_buttons = QHBoxLayout()
        self.audit_latest = QPushButton("Load audit history")
        self.audit_latest.clicked.connect(lambda: self._load_audit(None))
        self.audit_next = QPushButton("Older entries")
        self.audit_next.clicked.connect(
            lambda: self._load_audit(self._audit.cursor if self._audit else None)
        )
        audit_buttons.addWidget(self.audit_latest)
        audit_buttons.addWidget(self.audit_next)
        audit_buttons.addStretch()
        history_layout.addLayout(audit_buttons)
        self.tabs.addTab(history, "Audit")
        self.status = _note("Select candidates and review the preview before recycling.")
        layout.addWidget(self.status)
        self.cancel = QPushButton("Cancel remaining items")
        self.cancel.clicked.connect(self._cancel)
        layout.addWidget(self.cancel)

        controller.busy_changed.connect(self._busy_changed)
        controller.cleanup_ready.connect(self._ready)
        controller.failed.connect(self._failed)
        controller.idle.connect(self._pump)
        self._buttons()

    def _number(
        self, label: str, key: str, default: int, maximum: int, minimum: int = 0
    ) -> QSpinBox:
        widget = QSpinBox()
        widget.setAccessibleName(label)
        widget.setRange(minimum, maximum)
        value = self._settings.value(f"cleanup/{key}", default, type=int)
        widget.setValue(value if isinstance(value, int) else default)
        return widget

    def activate(self) -> None:
        if not self._loaded and not self._operation:
            self._wanted = True
        self._pump()

    def open_snapshot(self, scan_id: int | None) -> None:
        self._scan_id = scan_id
        self._loaded = False
        self._wanted = True
        self._pump()

    def invalidate_latest(self) -> None:
        if self._scan_id is None:
            self._loaded = False
            if self.isVisible():
                self.activate()

    def _pump(self) -> None:
        if self._wanted and self.isVisible() and not self._busy and self.confirmation is None:
            self._refresh()

    def _submit(self, label: str, job: Callable[[], bool]) -> bool:
        if self._busy or self._operation:
            return False
        self._operation = label
        if not job():
            self._operation = ""
            return False
        self._buttons()
        return True

    def _refresh(self) -> None:
        rules = Rules(
            self.temporary.value(),
            self.large_age.value(),
            self.large_size.value(),
            self.empty.isChecked(),
            self.duplicates.isChecked(),
        )
        if self._submit("candidates", lambda: self._controller.load_cleanup(self._scan_id, rules)):
            self._rules = rules
            self._wanted = False
            for key, value in zip(
                ("temporaryDays", "largeDays", "largeMiB", "empty", "duplicates"),
                (
                    rules.temporary_days,
                    rules.large_days,
                    rules.large_mib,
                    rules.empty_folders,
                    rules.duplicates,
                ),
                strict=True,
            ):
                self._settings.setValue(f"cleanup/{key}", value)
            self.status.setText("Finding cleanup candidates…")

    def _next(self) -> None:
        scan, cursor = self._candidates.scan_id, self._candidates.cursor
        if scan is not None and cursor is not None:
            self._submit(
                "candidates",
                lambda: self._controller.load_cleanup(
                    scan,
                    self._rules,
                    cursor,
                ),
            )

    def _selection(self) -> tuple[str, ...]:
        return tuple(
            self._candidates.rows[index.row()].path
            for index in self.files.selectionModel().selectedRows()
        )

    def _preview(self) -> None:
        scan, paths = self._candidates.scan_id, self._selection()
        if scan is not None and paths:
            if self._submit(
                "preview",
                lambda: self._controller.preview_cleanup(
                    scan,
                    self._rules,
                    paths,
                ),
            ):
                self.status.setText("Checking the selected items…")

    def _ready(self, result: Candidates | CleanupPlan | CleanupAudit) -> None:
        self._operation = ""
        if isinstance(result, Candidates):
            self._candidates = result
            self._loaded = True
            self.scope.setText(
                f"Scan #{result.scan_id} · {result.root}"
                if result.scan_id
                else "Scan a folder or drive to find cleanup candidates."
            )
            _fill(
                self.model,
                [
                    (row.path, row.reason, _size(row.size), _size(row.allocated))
                    for row in result.rows
                ],
            )
            self.status.setText(
                f"{len(result.rows)} candidates on this page"
                if result.rows
                else "No matching cleanup candidates."
            )
        elif isinstance(result, CleanupPlan):
            self._show_preview(result)
        else:
            self._audit = result
            _fill(
                self.audit_model,
                [(row.path, row.state.capitalize(), row.approved_at) for row in result.rows],
            )
            self.tabs.setCurrentIndex(1)
            self.detail.clear()
            if result.rows:
                self.audit_table.setCurrentIndex(self.audit_model.index(0, 0))
            self.status.setText(
                "Cleanup finished. Review the item outcomes."
                if result.operation
                else f"{len(result.rows)} audit entries on this page."
            )
        self._buttons()

    def _show_preview(self, plan: CleanupPlan) -> None:
        dialog = QDialog(self)
        self.confirmation = dialog
        dialog.setWindowTitle("Confirm Recycle Bin operation")
        dialog.resize(815, 465)
        layout = QVBoxLayout(dialog)
        eligible = plan.eligible
        logical = sum(item.candidate.size for item in eligible)
        allocation = [item.candidate.allocated for item in eligible]
        allocated = None if None in allocation else sum(value or 0 for value in allocation)
        layout.addWidget(
            _note(
                f"{plan.root}\n{len(eligible)} eligible files · {_size(logical)} logical · "
                f"Allocated estimate {_size(allocated)}"
            )
        )
        table, model = _table(dialog, ("Path", "Reason", "Size", "Validation"))
        _fill(
            model,
            [
                (row.candidate.path, row.candidate.reason, _size(row.candidate.size), row.detail)
                for row in plan.items
            ],
        )
        layout.addWidget(table, 1)
        selected = _note()
        layout.addWidget(selected)

        def show_item(index: QModelIndex) -> None:
            if index.isValid():
                item = plan.items[index.row()]
                selected.setText(f"{item.candidate.path}\n{item.detail}")

        table.selectionModel().currentChanged.connect(show_item)
        table.setCurrentIndex(model.index(0, 0))
        layout.addWidget(
            _note(
                "Restore recycled files while Windows retains them. "
                "Space is reclaimed when Windows removes the files from Recycle Bin."
            )
        )
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        accept = buttons.addButton(
            "Send eligible files to Recycle Bin", QDialogButtonBox.ButtonRole.AcceptRole
        )
        accept.setEnabled(bool(eligible))
        accept.setAutoDefault(False)
        buttons.rejected.connect(dialog.reject)
        buttons.accepted.connect(dialog.accept)
        dialog.finished.connect(lambda result: self._confirmed(dialog, plan, result))
        layout.addWidget(buttons)
        dialog.open()

    def _confirmed(self, dialog: QDialog, plan: CleanupPlan, result: int) -> None:
        self.confirmation = None
        dialog.deleteLater()
        if result == QDialog.DialogCode.Accepted and plan.eligible:
            if self._submit("recycle", lambda: self._controller.recycle_cleanup(plan.token)):
                self.status.setText("Revalidating and recycling the confirmed files…")
            else:
                self.status.setText("Storage is busy. Open a fresh preview to retry.")
        else:
            self.status.setText("Preview closed.")
        self._buttons()

    def _load_audit(self, after: tuple[str, str, str] | None) -> None:
        self._submit("audit", lambda: self._controller.load_cleanup_audit(after))

    def _audit_detail(self, index: QModelIndex, _: QModelIndex) -> None:
        if self._audit and index.isValid():
            row = self._audit.rows[index.row()]
            self.detail.setText(f"{row.root} / {row.path}\n{row.reason} · {row.detail}")

    def _cancel(self) -> None:
        if self._controller.cancel():
            self.status.setText("Cancelling remaining items…")
            self.cancel.setEnabled(False)

    def _busy_changed(self, busy: bool) -> None:
        self._busy = busy
        self._buttons()

    def _failed(self, message: str) -> None:
        if self._operation:
            self._operation = ""
            self.status.setText(f"Cleanup Center · {message}")
            self._buttons()

    def _buttons(self, *_: object) -> None:
        ready = not self._busy and not self._operation and self.confirmation is None
        self.refresh.setEnabled(ready)
        self.latest.setEnabled(ready)
        self.next.setEnabled(ready and self._candidates.cursor is not None)
        self.preview.setEnabled(ready and bool(self._selection()))
        self.audit_latest.setEnabled(ready)
        self.audit_next.setEnabled(ready and bool(self._audit and self._audit.cursor))
        self.cancel.setVisible(self._operation == "recycle")
        self.cancel.setEnabled(self._operation == "recycle" and self._busy)
