"""Duplicate group and member review for a selected inventory."""

from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import QModelIndex, QSettings, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from disksense.duplicate_store import (
    DuplicateProgress,
    DuplicateReport,
    DuplicateRequest,
    Member,
    MembersPage,
)
from disksense.duplicates import Review
from disksense.ui.scan_controller import ScanController
from disksense.ui.scan_views import _layout, _note, _size

_RULES = "duplicates/exclusions"
_MINIMUM = "duplicates/minimumBytes"


def _table(owner: QWidget, headers: tuple[str, ...]) -> tuple[QTableView, QStandardItemModel]:
    view = QTableView()
    model = QStandardItemModel(0, len(headers), owner)
    model.setHorizontalHeaderLabels(headers)
    view.setModel(model)
    view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    view.verticalHeader().hide()
    view.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    view.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    return view, model


def _fill(model: QStandardItemModel, rows: list[tuple[str, ...]]) -> None:
    model.removeRows(0, model.rowCount())
    for row in rows:
        model.appendRow([QStandardItem(value) for value in row])


def _modified(member: Member) -> str:
    try:
        return datetime.fromtimestamp(member.version.modified // 10**9).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return "Unavailable"


class DuplicateView(QWidget):
    def __init__(self, controller: ScanController, settings: QSettings) -> None:
        super().__init__()
        self._controller = controller
        self._settings = settings
        self._operation: str | None = None
        self._busy = False
        self._waiting = False
        self._loaded = False
        self._scan_choice: int | None = None
        self._group_after: int | None = None
        self._member_after: str | None = None
        self._report = DuplicateReport()
        self._members: MembersPage | None = None

        layout = _layout(self, "Duplicate Finder")
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(7)
        self.scope = _note("Select a saved inventory to find duplicate files.")
        layout.addWidget(self.scope)
        form = QFormLayout()
        self.minimum = QLineEdit(str(settings.value(_MINIMUM, "1")))
        self.minimum.setMaximumWidth(180)
        form.addRow("Minimum size (bytes)", self.minimum)
        self.exclusions = QPlainTextEdit(str(settings.value(_RULES, "")))
        self.exclusions.setMaximumHeight(50)
        self.exclusions.setPlaceholderText("One relative-path glob per line, e.g. downloads/*.tmp")
        form.addRow("Exclude paths", self.exclusions)
        layout.addLayout(form)
        actions = QHBoxLayout()
        self.latest = QPushButton("Latest inventory")
        self.latest.clicked.connect(lambda: self.open_snapshot(None))
        self.find_button = QPushButton("Find duplicates")
        self.find_button.clicked.connect(self._detect)
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(self._cancel)
        for button in (self.latest, self.find_button, self.cancel):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.status = _note()
        layout.addWidget(self.status)
        self.activity = QProgressBar()
        self.activity.setRange(0, 0)
        self.activity.hide()
        layout.addWidget(self.activity)

        splitter = QSplitter(Qt.Orientation.Vertical)
        groups = QWidget()
        groups_layout = QVBoxLayout(groups)
        groups_layout.setContentsMargins(0, 0, 0, 0)
        self.groups, self.groups_model = _table(
            self, ("Duplicate group", "Copies", "Paths", "File size", "Logical potential")
        )
        self.groups.setAccessibleName("Duplicate groups")
        self.groups.clicked.connect(self._choose_group)
        self.groups.activated.connect(self._choose_group)
        groups_layout.addWidget(self.groups)
        group_pager = QHBoxLayout()
        self.group_first = QPushButton("First groups")
        self.group_next = QPushButton("Next groups")
        self.group_first.clicked.connect(lambda: self._load_report(None))
        self.group_next.clicked.connect(lambda: self._load_report(self._report.next_page))
        group_pager.addWidget(self.group_first)
        group_pager.addWidget(self.group_next)
        group_pager.addStretch()
        groups_layout.addLayout(group_pager)
        splitter.addWidget(groups)

        members = QWidget()
        members_layout = QVBoxLayout(members)
        members_layout.setContentsMargins(0, 0, 0, 0)
        self.group_detail = _note("Select a group to review its members.")
        members_layout.addWidget(self.group_detail)
        self.files, self.files_model = _table(self, ("Path", "Role", "Links", "Modified"))
        for column, width in enumerate((76, 50, 132), start=1):
            self.files.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.Interactive
            )
            self.files.setColumnWidth(column, width)
        self.files.setAccessibleName("Duplicate group members")
        self.files.selectionModel().currentChanged.connect(self._buttons)
        self.files.activated.connect(self._preview)
        members_layout.addWidget(self.files)
        choices = QHBoxLayout()
        self.member_first = QPushButton("First files")
        self.member_next = QPushButton("Next files")
        self.preview = QPushButton("Preview")
        self.verify = QPushButton("Verify bytes")
        self.keeper = QPushButton("Keep this copy")
        self.member_first.clicked.connect(lambda: self._page_members(None))
        self.member_next.clicked.connect(
            lambda: self._page_members(self._members.next_page if self._members else None)
        )
        self.preview.clicked.connect(self._preview)
        self.verify.clicked.connect(lambda: self._review(True))
        self.keeper.clicked.connect(self._keep)
        for button in (self.member_first, self.member_next, self.preview, self.verify, self.keeper):
            choices.addWidget(button)
        choices.addStretch()
        members_layout.addLayout(choices)
        splitter.addWidget(members)
        splitter.setSizes([145, 200])
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        layout.addWidget(splitter, 1)
        controller.busy_changed.connect(self._busy_changed)
        controller.idle.connect(self._pump)
        controller.duplicate_progress.connect(self._progress)
        controller.duplicate_ready.connect(self._ready)
        controller.members_ready.connect(self._members_ready)
        controller.review_ready.connect(self._review_ready)
        controller.failed.connect(self._failed)
        self._buttons()

    def activate(self) -> None:
        if not self._loaded and self._operation is None:
            self._waiting = True
        self._pump()

    def invalidate_latest(self) -> None:
        if self._scan_choice is None:
            self._loaded = False
            if self.isVisible():
                self.activate()

    def open_snapshot(self, scan_id: int | None) -> None:
        self._scan_choice = scan_id
        self._loaded = False
        self._waiting = True
        self._pump()

    def _pump(self) -> None:
        if self._waiting and not self._busy and self.isVisible():
            self._load_report(None)

    def _submit(self, operation: str, start: Callable[[], bool]) -> bool:
        if self._busy or self._operation is not None:
            return False
        self._operation = operation
        if not start():
            self._operation = None
            return False
        self._buttons()
        return True

    def _load_report(self, after: int | None) -> None:
        if self._submit(
            "results", lambda: self._controller.load_duplicates(self._scan_choice, after)
        ):
            self._group_after = after
            self._waiting = False
            self.status.setText("Loading duplicate groups…")

    def _detect(self) -> None:
        try:
            minimum = int(self.minimum.text())
            if not 0 <= minimum < 2**63:
                raise ValueError
        except ValueError:
            self.status.setText("Minimum size must be a non-negative byte count.")
            return
        patterns = tuple(
            rule.strip() for rule in self.exclusions.toPlainText().splitlines() if rule.strip()
        )
        request = DuplicateRequest(self._report.scan_id, minimum, patterns)
        if self._submit("detect", lambda: self._controller.find_duplicates(request)):
            self._settings.setValue(_MINIMUM, str(minimum))
            self._settings.setValue(_RULES, self.exclusions.toPlainText())
            self._group_after = None
            self.status.setText("Starting duplicate detection…")

    def _cancel(self) -> None:
        message = "Cancelling…" if self._controller.cancel() else "Saving results…"
        self.status.setText(message)
        self.cancel.setEnabled(False)

    def _progress(self, value: DuplicateProgress) -> None:
        if self._operation == "detect":
            self.status.setText(
                f"{value.phase} · {value.examined:,} checks · {value.skipped:,} skipped · "
                f"{value.excluded:,} excluded · {_size(value.bytes_read)} read · "
                f"{value.elapsed:.1f}s"
            )

    def _ready(self, report: DuplicateReport) -> None:
        self._loaded = True
        self._operation = None
        self._report = report
        self._draw_groups()
        self._members = None
        self._member_after = None
        _fill(self.files_model, [])
        self.group_detail.setText("Select a group to review its members.")
        self._buttons()

    def _draw_groups(self, selected: int | None = None) -> None:
        report = self._report
        self.scope.setText(
            f"Scan #{report.scan_id} · {report.root}"
            if report.scan_id is not None
            else "No saved inventory. Scan a folder or drive in Disk Explorer."
        )
        _fill(
            self.groups_model,
            [
                (
                    f"Group {group.id}",
                    str(group.copies),
                    str(group.paths),
                    _size(group.size),
                    _size(group.logical),
                )
                for group in report.groups
            ],
        )
        if selected is not None:
            for index, group in enumerate(report.groups):
                if group.id == selected:
                    self.groups.setCurrentIndex(self.groups_model.index(index, 0))
                    break
        lines = []
        if report.cancelled:
            lines.append("Detection cancelled.")
        if report.run_id is not None:
            lines.append(
                f"{report.group_count:,} groups · {_size(report.logical)} logical potential · "
                f"Allocated estimate {_size(report.allocated)}"
            )
            lines.append(
                f"{report.skipped:,} skipped · {_size(report.bytes_read)} read · "
                f"{report.cache_hits:,} cache hits · {report.elapsed:.1f}s"
            )
        elif report.scan_id is not None and not report.cancelled:
            lines.append("Run duplicate detection for this inventory.")
        self.status.setText("\n".join(lines))

    def _choose_group(self, index: QModelIndex) -> None:
        if index.isValid() and not self._busy:
            self._load_members(self._report.groups[index.row()].id, None)

    def _page_members(self, after: str | None) -> None:
        if self._members:
            self._load_members(self._members.group_id, after)

    def _load_members(self, group_id: int, after: str | None, keep: str | None = None) -> None:
        run_id, scan_id = self._report.run_id, self._report.scan_id
        if run_id is None or scan_id is None:
            return
        if self._submit(
            "members",
            lambda: self._controller.load_members(
                run_id, group_id, after, scan_id, self._group_after, keep
            ),
        ):
            self._member_after = after
            self.group_detail.setText("Loading members…")

    def _members_ready(self, page: MembersPage) -> None:
        self._operation = None
        self._members = page
        if page.report:
            self._report = page.report
            self._draw_groups(page.group_id)
        self.group_detail.setText(
            f"Keeper  {page.group.keeper} · Logical potential  {_size(page.group.logical)} · "
            f"Allocated estimate  {_size(page.group.allocated)}"
        )
        _fill(
            self.files_model,
            [
                (
                    row.version.path,
                    "Keeper" if row.keeper else "Linked" if row.links != 1 else "",
                    str(row.links),
                    _modified(row),
                )
                for row in page.rows
            ],
        )
        if page.rows:
            self.files.setCurrentIndex(self.files_model.index(0, 0))
        self._buttons()

    def _selected(self) -> Member | None:
        row = self.files.currentIndex().row()
        if self._members and 0 <= row < len(self._members.rows):
            return self._members.rows[row]
        return None

    def _keep(self) -> None:
        row = self._selected()
        if self._members and row and not row.keeper:
            self._load_members(self._members.group_id, self._member_after, row.version.path)

    def _preview(self) -> None:
        self._review(False)

    def _review(self, verify: bool) -> None:
        page, row = self._members, self._selected()
        if page and row and (not verify or not row.keeper):
            if self._submit(
                "review",
                lambda: self._controller.review_duplicate(
                    page.run_id,
                    page.group_id,
                    row.version.path,
                    verify=verify,
                ),
            ):
                self.status.setText(
                    "Reading the selected file…"
                    if not verify
                    else "Comparing bytes with the keeper…"
                )

    def _review_ready(self, review: Review) -> None:
        self._operation = None
        self._buttons()
        if review.verified is not None or review.title == "Review cancelled":
            self.status.setText(f"{review.title}\n{review.path}\n{review.text}")
            return
        self._draw_review(review)

    def _draw_review(self, review: Review) -> None:
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle(review.title)
        dialog.resize(640, 330)
        layout = QVBoxLayout(dialog)
        layout.addWidget(_note(review.path))
        content = QPlainTextEdit()
        content.setPlainText(review.text)
        content.setReadOnly(True)
        layout.addWidget(content)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        self.status.setText(f"Previewed {review.path}")
        dialog.open()

    def _failed(self, message: str) -> None:
        if self._operation is not None:
            self._operation = None
            self.status.setText(f"Duplicate Finder · {message}")
            self._buttons()

    def _busy_changed(self, busy: bool) -> None:
        self._busy = busy
        self._buttons()

    def _buttons(self, *_: object) -> None:
        settled = not self._busy and self._operation is None
        self.find_button.setEnabled(settled and self._report.scan_id is not None)
        self.latest.setEnabled(settled)
        self.cancel.setEnabled(self._busy and self._operation in ("detect", "review"))
        self.activity.setVisible(self._busy and self._operation in ("detect", "review"))
        self.group_first.setEnabled(settled and self._group_after is not None)
        self.group_next.setEnabled(settled and self._report.next_page is not None)
        self.member_first.setEnabled(settled and self._member_after is not None)
        self.member_next.setEnabled(settled and bool(self._members and self._members.next_page))
        chosen = self._selected()
        self.preview.setEnabled(settled and chosen is not None)
        self.keeper.setEnabled(settled and bool(chosen and not chosen.keeper))
        self.verify.setEnabled(settled and bool(chosen and not chosen.keeper))
