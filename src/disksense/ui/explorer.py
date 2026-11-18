"""Inventory table, folder navigation, and dashboard backed by the scan worker."""

from dataclasses import replace
from datetime import date, datetime, time
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import QDesktopServices, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMenu,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableView,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from disksense.inventory import Cursor, InventoryQuery, InventoryResult, Item, Sort
from disksense.ui.scan_controller import ScanController
from disksense.ui.scan_views import _layout, _note, _size
from disksense.ui.storage_charts import UsagePanel

_INDEX = QModelIndex()
_COLUMNS = ("Path", "Logical size", "Allocated size", "Modified", "Type")
_MIB = 1024**2


class InventoryModel(QAbstractTableModel):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.rows: tuple[Item, ...] = ()
        self.folder = ""

    def load(self, rows: tuple[Item, ...], folder: str) -> None:
        self.beginResetModel()
        self.rows = rows
        self.folder = folder
        self.endResetModel()

    def rowCount(self, /, parent: QModelIndex | QPersistentModelIndex = _INDEX) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, /, parent: QModelIndex | QPersistentModelIndex = _INDEX) -> int:
        return 0 if parent.isValid() else len(_COLUMNS)

    def data(
        self, index: QModelIndex | QPersistentModelIndex, /, role: int = Qt.ItemDataRole.DisplayRole
    ) -> str | None:
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        item = self.rows[index.row()]
        relative = item.path.removeprefix(self.folder + "/") if self.folder else item.path
        if index.column() == 3:
            try:
                return datetime.fromtimestamp(item.modified // 10**9).strftime("%Y-%m-%d %H:%M")
            except (ValueError, OSError, OverflowError):
                return "Unavailable"
        return (
            relative,
            _size(item.size),
            _size(item.allocated),
            "",
            ("Folder (partial)" if item.incomplete else "Folder")
            if item.kind == "directory"
            else (item.extension or "File"),
        )[index.column()]

    def headerData(
        self, section: int, orientation: Qt.Orientation, /, role: int = Qt.ItemDataRole.DisplayRole
    ) -> str | None:
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return _COLUMNS[section]
        return None


def _summary(result: InventoryResult) -> str:
    scan, overview = result.scan, result.overview
    if scan is None or overview is None:
        return "No published inventory. Start a scan in Disk Explorer."
    current = overview.folder
    title = f"Scan #{scan.id} · {scan.root}"
    if current.path:
        title += f" / {current.path}"
    if scan.state == "partial":
        title += " · Partial inventory"
    sizes = f"{current.file_count:,} files · {_size(current.size)} logical"
    if not current.path:
        sizes += f" · {_size(scan.allocated_bytes)} allocated"
    capacity = (
        "Volume capacity unavailable"
        if overview.capacity is None
        else "Live volume  "
        + " · ".join(
            f"{_size(value)} {label}"
            for value, label in zip(overview.capacity, ("total", "used", "available"), strict=True)
        )
    )
    return f"{title}\n{sizes}\n{capacity}"


class Dashboard(QWidget):
    browse_requested = Signal(int, str, bool, object)

    def __init__(self, controller: ScanController) -> None:
        super().__init__()
        self._controller = controller
        self._waiting = self._loading = False
        self._scan_id: int | None = None
        layout = _layout(self, "Dashboard")
        self.status = _note("Loading latest inventory…")
        layout.addWidget(self.status)
        row = QHBoxLayout()
        self._open = QPushButton("Browse inventory")
        self._open.setEnabled(False)
        self._open.clicked.connect(lambda: self._browse("", True))
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        row.addWidget(self._open)
        row.addWidget(refresh)
        row.addStretch()
        layout.addLayout(row)
        self.charts = UsagePanel()
        self.charts.entry_selected.connect(self._browse)
        self.charts.extension_selected.connect(lambda ext: self._browse("", True, ext))
        layout.addWidget(self.charts, 1)
        controller.inventory_ready.connect(self._ready)
        controller.failed.connect(self._failed)
        controller.idle.connect(self._pump)

    def refresh(self) -> None:
        self._waiting = True
        self._pump()

    def _pump(self) -> None:
        if self.isVisible() and self._waiting and not self._loading:
            if self._controller.load_inventory(InventoryQuery(dashboard=True)):
                self._waiting = False
                self._loading = True
                self.status.setText("Loading latest inventory…")

    def _ready(self, result: InventoryResult) -> None:
        if not result.query.dashboard:
            return
        self._loading = False
        self._scan_id = result.scan.id if result.scan else None
        self._open.setEnabled(self._scan_id is not None)
        self.status.setText(_summary(result))
        self.charts.load(result.overview)

    def _failed(self, message: str) -> None:
        if self._loading:
            self._loading = False
            self.status.setText(f"Could not load dashboard. {message}")

    def _browse(self, path: str, folder: bool, extension: str | None = None) -> None:
        if self._scan_id is not None:
            self.browse_requested.emit(self._scan_id, path, folder, extension)


class Explorer(QWidget):
    def __init__(self, controller: ScanController) -> None:
        super().__init__()
        self._controller = controller
        self._query = InventoryQuery()
        self._pending: InventoryQuery | None = None
        self._loading = False
        self._trail: list[Cursor | None] = []
        self._next: Cursor | None = None
        self._root: Path | None = None
        self._loaded = False
        self._follow_latest = True
        self._tree_scope: tuple[int, str] | None = None

        layout = QVBoxLayout(self)
        toolbar = QHBoxLayout()
        self.latest = QPushButton("Latest inventory")
        self.latest.clicked.connect(lambda: self.open_snapshot(None))
        self.up = QPushButton("Up one folder")
        self.up.clicked.connect(lambda: self._folder(self._query.folder.rpartition("/")[0]))
        toolbar.addWidget(self.latest)
        toolbar.addWidget(self.up)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.summary = _note()
        layout.addWidget(self.summary)

        splitter = QSplitter()
        self.tree = QTreeView()
        self.tree.setAccessibleName("Folder tree")
        self.tree_model = QStandardItemModel(self)
        self.tree.setModel(self.tree_model)
        self.tree.setHeaderHidden(True)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.clicked.connect(self._tree_selected)
        self.tree.activated.connect(self._tree_selected)
        splitter.addWidget(self.tree)

        self.tabs = QTabWidget()
        listing = QWidget()
        listing_layout = QVBoxLayout(listing)
        listing_layout.addLayout(self._filters())
        self.status = _note()
        listing_layout.addWidget(self.status)
        self.model = InventoryModel(self)
        self.table = QTableView()
        self.table.setAccessibleName("Inventory entries")
        self.table.setModel(self.model)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column, width in enumerate((104, 111, 125, 74), start=1):
            self.table.setColumnWidth(column, width)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.activated.connect(self._entry_activated)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._entry_menu)
        listing_layout.addWidget(self.table, 1)
        pager = QHBoxLayout()
        self.previous = QPushButton("Previous page")
        self.previous.clicked.connect(self._previous_page)
        self.next = QPushButton("Next page")
        self.next.clicked.connect(self._next_page)
        pager.addWidget(self.previous)
        pager.addWidget(self.next)
        pager.addStretch()
        listing_layout.addLayout(pager)
        self.tabs.addTab(listing, "Entries")
        self.charts = UsagePanel()
        self.charts.entry_selected.connect(self.open_entry)
        self.charts.extension_selected.connect(self._file_type)
        self.tabs.addTab(self.charts, "Usage")
        splitter.addWidget(self.tabs)
        splitter.setSizes([185, 630])
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)
        self._buttons()
        controller.inventory_ready.connect(self._ready)
        controller.failed.connect(self._failed)
        controller.idle.connect(self._pump)

    def _filters(self) -> QGridLayout:
        layout = QGridLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search paths")
        self.search.setAccessibleName("Search paths")
        self.search.returnPressed.connect(self._apply_filters)
        self.extension = QLineEdit()
        self.extension.setPlaceholderText("Extension (e.g. .pdf)")
        self.extension.setAccessibleName("File extension")
        self.extension.returnPressed.connect(self._apply_filters)
        self.minimum = self._size_filter("Minimum")
        self.maximum = self._size_filter("Maximum")
        self.modified = QLineEdit()
        self.modified.setPlaceholderText("Modified since YYYY-MM-DD")
        self.modified.setAccessibleName("Modified since")
        self.modified.returnPressed.connect(self._apply_filters)
        self.sort = QComboBox()
        self.sort.setAccessibleName("Sort order")
        self.sort.addItems(
            (
                "Path A–Z",
                "Path Z–A",
                "Largest first",
                "Smallest first",
                "Newest first",
                "Oldest first",
                "Extension A–Z",
            )
        )
        self.files = QCheckBox("Files only")
        apply = QPushButton("Apply")
        apply.clicked.connect(self._apply_filters)
        clear = QPushButton("Clear")
        clear.clicked.connect(self._clear_filters)
        layout.addWidget(self.search, 0, 0, 1, 2)
        layout.addWidget(self.extension, 0, 2)
        layout.addWidget(self.modified, 1, 0, 1, 2)
        layout.addWidget(self.sort, 1, 2)
        layout.addWidget(self.minimum, 2, 0)
        layout.addWidget(self.maximum, 2, 1)
        layout.addWidget(self.files, 2, 2)
        layout.addWidget(clear, 3, 1)
        layout.addWidget(apply, 3, 2)
        return layout

    @staticmethod
    def _size_filter(label: str) -> QSpinBox:
        field = QSpinBox()
        field.setAccessibleName(f"{label} file size in MiB")
        field.setRange(0, 2_147_483_647)
        field.setSpecialValueText(f"{label} size")
        field.setSuffix(" MiB")
        return field

    def activate(self) -> None:
        if not self._loaded and self._pending is None and not self._loading:
            self.open_snapshot(None)
        else:
            self._pump()

    def invalidate_latest(self) -> None:
        if self._follow_latest:
            self._loaded = False
            if self.isVisible():
                self.open_snapshot(None)

    def open_snapshot(self, scan_id: int | None) -> None:
        self._follow_latest = scan_id is None
        self._reset_fields()
        self._trail.clear()
        self._queue(InventoryQuery(scan_id=scan_id))

    def open_entry(self, path: str, folder: bool) -> None:
        self._reset_fields()
        if folder:
            self._folder(path)
        else:
            self._select(
                InventoryQuery(
                    scan_id=self._query.scan_id, folder=path.rpartition("/")[0], exact_path=path
                )
            )

    def open_chart(self, scan_id: int, path: str, folder: bool, extension: str | None) -> None:
        self._follow_latest = False
        self._query = InventoryQuery(scan_id=scan_id)
        self._reset_fields()
        if extension is None:
            self.open_entry(path, folder)
        else:
            self._file_type(extension)

    def _file_type(self, extension: str) -> None:
        self._reset_fields()
        self.extension.setText(extension or "(no extension)")
        self._apply_filters()

    def _folder(self, path: str) -> None:
        self._reset_fields()
        self._select(InventoryQuery(scan_id=self._query.scan_id, folder=path))

    def _reset_fields(self) -> None:
        self.search.clear()
        self.extension.clear()
        self.modified.clear()
        self.minimum.setValue(0)
        self.maximum.setValue(0)
        self.sort.setCurrentIndex(0)
        self.files.setChecked(False)

    def _clear_filters(self) -> None:
        self._reset_fields()
        self._apply_filters()

    def _apply_filters(self) -> None:
        since = self.modified.text().strip()
        try:
            modified = (
                int(datetime.combine(date.fromisoformat(since), time()).timestamp()) * 10**9
                if since
                else None
            )
            if modified is not None and not -(2**63) <= modified < 2**63:
                raise ValueError("Date is outside the inventory timestamp range.")
        except (ValueError, OSError, OverflowError):
            self.status.setText("Enter a valid date as YYYY-MM-DD.")
            return
        low, high = self.minimum.value() * _MIB, self.maximum.value() * _MIB
        if high and low > high:
            self.status.setText("Minimum size must be at or below maximum size.")
            return
        value = self.extension.text().strip().lower()
        extension = (
            ("" if value == "(no extension)" else "." + value.lstrip(".")) if value else None
        )
        orders: tuple[tuple[Sort, bool], ...] = (
            ("path", False),
            ("path", True),
            ("size", True),
            ("size", False),
            ("modified", True),
            ("modified", False),
            ("extension", False),
        )
        order, descending = orders[self.sort.currentIndex()]
        self._select(
            replace(
                self._query,
                search=self.search.text().strip(),
                extension=extension,
                minimum=low or None,
                maximum=high or None,
                modified_from=modified,
                exact_path=None,
                files_only=self.files.isChecked(),
                sort=order,
                descending=descending,
            )
        )

    def _select(self, query: InventoryQuery) -> None:
        self._trail.clear()
        self.tabs.setCurrentIndex(0)
        scope = (query.scan_id, query.folder)
        self._queue(replace(query, after=None, overview=self._tree_scope != scope))

    def _queue(self, query: InventoryQuery) -> None:
        self._query = query
        self._pending = query
        self._next = None
        self.status.setText("Loading inventory…")
        self._buttons()
        self._pump()

    def _pump(self) -> None:
        if self._pending is not None and self.isVisible() and not self._loading:
            if self._controller.load_inventory(self._pending):
                self._pending = None
                self._loading = True

    def _buttons(self) -> None:
        settled = not self._loading and self._pending is None
        self.up.setEnabled(settled and bool(self._query.folder))
        self.previous.setEnabled(settled and bool(self._trail))
        self.next.setEnabled(settled and self._next is not None)
        for view in (self.tree, self.table, self.charts):
            view.setEnabled(settled)

    def _next_page(self) -> None:
        if self._next is not None:
            self._trail.append(self._query.after)
            self._queue(replace(self._query, after=self._next, overview=False))

    def _previous_page(self) -> None:
        if self._trail:
            self._queue(replace(self._query, after=self._trail.pop(), overview=False))

    def _ready(self, result: InventoryResult) -> None:
        if result.query.dashboard:
            return
        self._loading = False
        if self._pending is not None:
            return
        self._loaded = True
        scan = result.scan
        if scan is None:
            self._root = None
            self._tree_scope = None
            self.tree_model.clear()
            self.charts.load(None)
            self.summary.setText(_summary(result))
            self.status.setText("Choose Latest inventory or select a saved scan in Scan History.")
        else:
            self._root = Path(scan.root)
            self._query = replace(result.query, scan_id=scan.id)
            if result.overview:
                self._update_tree(result)
                self.charts.load(result.overview)
                self.summary.setText(_summary(result))
            self.status.setText(
                f"Page {len(self._trail) + 1} · {len(result.entries):,} entries"
                if result.entries
                else "No matching entries"
            )
        self._next = result.next_page
        self.model.load(result.entries, result.query.folder)
        self.table.scrollToTop()
        self._buttons()

    def _failed(self, message: str) -> None:
        if self._loading:
            self._loading = False
            self._tree_scope = None
            self._root = None
            self.model.load((), "")
            self.tree_model.clear()
            self.charts.load(None)
            self.summary.clear()
            self.status.setText(f"Could not load inventory. {message}")
            self._buttons()

    def _update_tree(self, result: InventoryResult) -> None:
        assert result.overview and result.scan
        self._tree_scope = (result.scan.id, result.query.folder)
        self.tree_model.clear()
        parent = self.tree_model.invisibleRootItem()
        path = ""
        root = Path(result.scan.root)
        names = [root.name or result.scan.root]
        if result.query.folder:
            names.extend(result.query.folder.split("/"))
        for index, name in enumerate(names):
            if index:
                path = f"{path}/{name}" if path else name
            item = QStandardItem(name)
            if not index:
                item.setToolTip(result.scan.root)
            item.setData(path, Qt.ItemDataRole.UserRole)
            parent.appendRow(item)
            parent = item
        for child in result.overview.folders:
            item = QStandardItem(child.name)
            item.setData(child.path, Qt.ItemDataRole.UserRole)
            parent.appendRow(item)
        if result.overview.more_folders:
            overflow = QStandardItem("See all entries…")
            overflow.setData(True, Qt.ItemDataRole.UserRole + 1)
            parent.appendRow(overflow)
        self.tree.expandAll()
        self.tree.setCurrentIndex(parent.index())

    def _tree_selected(self, index: QModelIndex) -> None:
        if index.data(Qt.ItemDataRole.UserRole + 1):
            self._folder(self._query.folder)
            return
        path = index.data(Qt.ItemDataRole.UserRole)
        if isinstance(path, str) and path != self._query.folder:
            self._folder(path)

    def _entry_activated(self, index: QModelIndex) -> None:
        if not index.isValid() or self._pending or self._loading:
            return
        item = self.model.rows[index.row()]
        if item.kind == "directory":
            self._folder(item.path)
        else:
            self._reveal(item)

    def _entry_menu(self, position: QPoint) -> None:
        index = self.table.indexAt(position)
        if not index.isValid() or self._pending or self._loading:
            return
        item = self.model.rows[index.row()]
        menu = QMenu(self)
        action = menu.addAction("Reveal in file manager")
        viewport = self.table.viewport()
        if menu.exec(viewport.mapToGlobal(position)) == action:
            self._reveal(item)
        menu.deleteLater()

    def _reveal(self, item: Item) -> None:
        parts = item.path.split("/")
        if self._root is None or any(
            part in ("", ".", "..") or Path(part).anchor for part in parts
        ):
            return
        path = self._root.joinpath(*parts)
        folder = path if item.kind == "directory" else path.parent
        directory = folder.as_posix().rstrip("/") + "/"
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(directory)):
            self.status.setText("Could not open this folder in the file manager.")
