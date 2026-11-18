"""Main window composition, navigation, and local UI preferences."""

from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths, QTimer, Slot
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShowEvent
from PySide6.QtWidgets import QHBoxLayout, QMainWindow, QStackedWidget, QTabWidget, QWidget

from disksense.database import ScanSummary
from disksense.monitor import MonitorData
from disksense.ui.cleanup_view import CleanupView
from disksense.ui.duplicate_view import DuplicateView
from disksense.ui.explorer import Dashboard, Explorer
from disksense.ui.monitor_view import MonitorView
from disksense.ui.pages import PAGES, build_page
from disksense.ui.recommendation_view import RecommendationView
from disksense.ui.scan_controller import ScanController
from disksense.ui.scan_views import HistoryView, ScanView, _size
from disksense.ui.sidebar import Sidebar

_COLLAPSED_KEY = "ui/sidebarCollapsed"
_EXPLORER = 1
_DUPLICATES = 2
_CLEANUP = 3
_MONITOR = 4
_RECOMMENDATIONS = 5
_HISTORY = 6


class MainWindow(QMainWindow):
    def __init__(
        self, settings: QSettings | None = None, database_path: Path | None = None
    ) -> None:
        super().__init__()
        self._settings = settings if settings is not None else QSettings("DiskSense", "DiskSense")
        self.setWindowTitle("DiskSense — Intelligent Disk & File Manager")
        self.setMinimumSize(600, 490)
        self.resize(1080, 700)
        if database_path is None:
            data = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppLocalDataLocation
            )
            database_path = Path(data) / "inventory.sqlite3"
        self.scan_controller = ScanController(self, database_path)
        self.scan_view = ScanView(self.scan_controller)
        self.explorer = Explorer(self.scan_controller)
        self.dashboard = Dashboard(self.scan_controller)
        self.duplicates = DuplicateView(self.scan_controller, self._settings)
        self.cleanup = CleanupView(self.scan_controller, self._settings)
        self.monitor = MonitorView(self.scan_controller)
        self.recommendations = RecommendationView(self.scan_controller, self._settings)
        self.scan_controller.monitor_ready.connect(self._growth_alert)
        self._explorer_tabs = QTabWidget()
        self._explorer_tabs.addTab(self.scan_view, "Scan")
        self._explorer_tabs.addTab(self.explorer, "Inventory")
        self._explorer_tabs.currentChanged.connect(self._show_inventory)
        self.history_view = HistoryView(self.scan_controller)
        self.history_view.inventory_requested.connect(self._open_inventory)
        self.history_view.duplicates_requested.connect(self._open_duplicates)
        self.history_view.cleanup_requested.connect(self._open_cleanup)
        self.dashboard.browse_requested.connect(self._open_chart)
        self.scan_controller.scan_finished.connect(self._refresh_history)
        self.scan_controller.idle.connect(self._close_when_idle)
        self._close_requested = False

        workspace = QWidget()
        workspace.setObjectName("workspace")
        layout = QHBoxLayout(workspace)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._sidebar = Sidebar()
        layout.addWidget(self._sidebar)
        self._pages = QStackedWidget()
        self._pages.setObjectName("pages")
        layout.addWidget(self._pages, stretch=1)
        self.setCentralWidget(workspace)
        self._sidebar.page_selected.connect(self.navigate)

        view_menu = self.menuBar().addMenu("&View")
        toggle_sidebar = QAction("&Collapse Sidebar", self)
        toggle_sidebar.setCheckable(True)
        toggle_sidebar.setShortcut(QKeySequence("Ctrl+B"))
        toggle_sidebar.setChecked(bool(self._settings.value(_COLLAPSED_KEY, False, type=bool)))
        self._sidebar.set_collapsed(toggle_sidebar.isChecked())
        toggle_sidebar.toggled.connect(self._set_sidebar_collapsed)
        view_menu.addAction(toggle_sidebar)

        navigate_menu = self.menuBar().addMenu("&Navigate")
        widget: QWidget
        for index, page in enumerate(PAGES):
            if index == 0:
                widget = self.dashboard
            elif index == _EXPLORER:
                widget = self._explorer_tabs
            elif index == _DUPLICATES:
                widget = self.duplicates
            elif index == _CLEANUP:
                widget = self.cleanup
            elif index == _MONITOR:
                widget = self.monitor
            elif index == _RECOMMENDATIONS:
                widget = self.recommendations
            elif index == _HISTORY:
                widget = self.history_view
            else:
                widget = build_page(page)
            self._pages.addWidget(widget)
            action = QAction(page.title, self)
            action.setShortcut(QKeySequence(f"Alt+{index + 1}"))
            action.setData(index)
            navigate_menu.addAction(action)
        navigate_menu.triggered.connect(self._navigate_from_action)
        self.navigate(0)

    def navigate(self, index: int) -> None:
        self._pages.setCurrentIndex(index)
        self._sidebar.select(index)
        if index == _HISTORY:
            self.history_view.reload()
        elif index == 0 and self.isVisible():
            self.dashboard.refresh()
        elif index == _EXPLORER:
            self._show_inventory(self._explorer_tabs.currentIndex())
        elif index == _DUPLICATES:
            self.duplicates.activate()
        elif index == _CLEANUP:
            self.cleanup.activate()
        elif index == _MONITOR:
            self.monitor.activate()
        elif index == _RECOMMENDATIONS:
            self.recommendations.activate()

    @Slot(int)
    def _show_inventory(self, tab: int) -> None:
        if tab == 1 and self.explorer.isVisible():
            self.explorer.activate()

    @Slot(object)
    def _refresh_history(self, summary: ScanSummary) -> None:
        if self._pages.currentIndex() == _HISTORY:
            self.history_view.reload()
        elif self._pages.currentIndex() == 0:
            self.dashboard.refresh()
        if summary.state in ("completed", "partial"):
            self.explorer.invalidate_latest()
            self.duplicates.invalidate_latest()
            self.cleanup.invalidate_latest()

    @Slot(int)
    def _open_inventory(self, scan_id: int) -> None:
        self.explorer.open_snapshot(scan_id)
        self.navigate(_EXPLORER)
        self._explorer_tabs.setCurrentIndex(1)

    @Slot(int)
    def _open_duplicates(self, scan_id: int) -> None:
        self.duplicates.open_snapshot(scan_id)
        self.navigate(_DUPLICATES)

    @Slot(int)
    def _open_cleanup(self, scan_id: int) -> None:
        self.cleanup.open_snapshot(scan_id)
        self.navigate(_CLEANUP)

    @Slot(int, str, bool, object)
    def _open_chart(self, scan_id: int, path: str, folder: bool, extension: str | None) -> None:
        self.explorer.open_chart(scan_id, path, folder, extension)
        self.navigate(_EXPLORER)
        self._explorer_tabs.setCurrentIndex(1)

    def showEvent(self, event: QShowEvent, /) -> None:
        super().showEvent(event)
        self.scan_controller.start_monitor()
        QTimer.singleShot(0, self._load_dashboard)

    @Slot(object)
    def _growth_alert(self, data: MonitorData) -> None:
        if data.alert is not None:
            root, growth = data.alert
            self.statusBar().showMessage(f"Storage grew by {_size(growth)} · {root}", 15000)

    def _load_dashboard(self) -> None:
        if self.isVisible() and self._pages.currentIndex() == 0:
            self.dashboard.refresh()

    @Slot()
    def _close_when_idle(self) -> None:
        if self._close_requested:
            self.close()

    def closeEvent(self, event: QCloseEvent, /) -> None:
        if self.scan_controller.close():
            super().closeEvent(event)
        else:
            self._close_requested = True
            event.ignore()

    def _navigate_from_action(self, action: QAction) -> None:
        self.navigate(int(action.data()))

    def _set_sidebar_collapsed(self, collapsed: bool) -> None:
        self._sidebar.set_collapsed(collapsed)
        self._settings.setValue(_COLLAPSED_KEY, collapsed)
        self._settings.sync()
