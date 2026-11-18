import os
from collections.abc import Callable, Iterator
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from time import time

import pytest
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QToolButton,
)
from pytestqt.qtbot import QtBot

from disksense.database import ScanRepository
from disksense.scanner import BATCH_SIZE, Entry, Inventory, ScanControl, ScanProgress
from disksense.ui.sidebar import Sidebar
from disksense.ui.storage_charts import StorageChart
from disksense.ui.window import MainWindow

DESTINATIONS = (
    "Dashboard",
    "Disk Explorer",
    "Duplicate Finder",
    "Cleanup Center",
    "Storage Monitor",
    "Recommendations",
    "Scan History",
    "Settings",
)


@pytest.fixture
def settings(tmp_path: Path) -> QSettings:
    return QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)


@pytest.fixture
def window(qtbot: QtBot, settings: QSettings, tmp_path: Path) -> Iterator[MainWindow]:
    widget = MainWindow(settings, tmp_path / "inventory.sqlite3")
    widget.show()
    yield widget
    widget.close()
    qtbot.waitUntil(lambda: not widget.isVisible())
    widget.deleteLater()


def test_sidebar_selects_each_page(window: MainWindow) -> None:
    pages = window.findChild(QStackedWidget, "pages")
    sidebar = window.findChild(Sidebar, "sidebar")
    assert pages is not None
    assert sidebar is not None
    assert pages.count() == len(DESTINATIONS)
    assert pages.currentIndex() == 0
    buttons = {button.text(): button for button in sidebar.findChildren(QToolButton)}

    for index, title in enumerate(DESTINATIONS):
        button = buttons[title]
        QTest.mouseClick(button, Qt.MouseButton.LeftButton)
        assert button.isChecked()
        assert pages.currentIndex() == index
        current = pages.currentWidget()
        assert current is not None
        heading = current.findChild(QLabel, "pageTitle")
        assert heading is not None
        assert heading.text() == title


def test_shortcuts_select_pages_and_persist_sidebar(
    qtbot: QtBot, window: MainWindow, settings: QSettings
) -> None:
    pages = window.findChild(QStackedWidget, "pages")
    sidebar = window.findChild(Sidebar, "sidebar")
    assert pages is not None
    assert sidebar is not None
    window.activateWindow()
    qtbot.waitUntil(window.isActiveWindow)
    shortcuts = (
        Qt.Key.Key_2,
        Qt.Key.Key_3,
        Qt.Key.Key_4,
        Qt.Key.Key_5,
        Qt.Key.Key_6,
        Qt.Key.Key_7,
        Qt.Key.Key_8,
        Qt.Key.Key_1,
    )
    for index, key in enumerate(shortcuts, start=1):
        QTest.keyClick(window, key, Qt.KeyboardModifier.AltModifier)
        assert pages.currentIndex() == index % len(DESTINATIONS)

    expanded_width = sidebar.width()
    QTest.keyClick(window, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    assert sidebar.width() < expanded_width
    reopened = MainWindow(
        QSettings(settings.fileName(), QSettings.Format.IniFormat),
        Path(settings.fileName()).with_name("reopened.sqlite3"),
    )
    qtbot.addWidget(reopened)
    reopened_sidebar = reopened.findChild(Sidebar, "sidebar")
    assert reopened_sidebar is not None
    assert reopened_sidebar.width() == sidebar.width()
    QTest.keyClick(window, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    assert sidebar.width() == expanded_width


def test_scan_progress_pause_resume_and_history(
    qtbot: QtBot, window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sample"
    root.mkdir()
    (root / "document.txt").write_text("sample", encoding="utf-8")
    entered = Event()
    release = Event()
    original = Inventory.run

    def run(
        inventory: Inventory,
        control: ScanControl,
        save: Callable[[list[Entry]], None],
        report: Callable[[ScanProgress], None],
    ) -> None:
        report(inventory.progress())
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Test did not release the scanner.")
        original(inventory, control, save, report)

    monkeypatch.setattr(Inventory, "run", run)
    window.navigate(1)
    view = window.scan_view
    view.root.setText(str(root))
    qtbot.waitUntil(view.start.isEnabled)
    view.start.click()
    try:
        qtbot.waitUntil(entered.is_set)
        qtbot.waitUntil(lambda: "files/s average" in view.details.text())
        assert view.bar.isVisible()
        view.pause.click()
        assert view.status.text() == "Paused"
        assert view.pause.text() == "Resume"
        release.set()
        QTest.qWait(50)
        assert view.status.text() == "Paused"
        assert not view.start.isEnabled()
        view.pause.click()
        qtbot.waitUntil(lambda: view.status.text() == "Scan completed")
    finally:
        release.set()
    window.navigate(6)
    history = window.history_view
    qtbot.waitUntil(lambda: history.model.rowCount() == 1)
    assert "1 files" in history.details.text()
    assert str(root) in history.details.text()


def test_cancel_scan_while_paused(
    qtbot: QtBot, window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "files"
    root.mkdir()
    for number in range(BATCH_SIZE + 1):
        (root / str(number)).touch()
    entered = Event()
    release = Event()
    original = ScanRepository.append

    def append(repo: ScanRepository, scan_id: int, batch: list[Entry]) -> None:
        original(repo, scan_id, batch)
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Test did not release the scan batch.")

    monkeypatch.setattr(ScanRepository, "append", append)
    window.navigate(1)
    view = window.scan_view
    view.root.setText(str(root))
    qtbot.waitUntil(view.start.isEnabled)
    view.start.click()
    try:
        qtbot.waitUntil(entered.is_set)
        view.pause.click()
        assert view.status.text() == "Paused"
        view.cancel.click()
        release.set()
        qtbot.waitUntil(lambda: view.status.text() == "Scan cancelled")
    finally:
        release.set()
    assert view.start.isEnabled()
    assert not view.cancel.isEnabled()


def test_missing_folder_shows_error(qtbot: QtBot, window: MainWindow, tmp_path: Path) -> None:
    window.navigate(1)
    view = window.scan_view
    missing = tmp_path / "missing"
    view.root.setText(str(missing))
    qtbot.waitUntil(view.start.isEnabled)
    view.start.click()
    qtbot.waitUntil(lambda: view.status.text() == "Scan failed")
    assert "missing" in view.details.text()


def test_database_writer_lock_released_on_close(
    qtbot: QtBot, window: MainWindow, settings: QSettings, tmp_path: Path
) -> None:
    window.navigate(6)
    qtbot.waitUntil(lambda: window.history_view.status.text() == "No saved scans yet")
    other = MainWindow(settings, tmp_path / "inventory.sqlite3")
    try:
        other.show()
        other.navigate(6)
        qtbot.waitUntil(lambda: "history is locked" in other.history_view.status.text())
        window.close()
        qtbot.waitUntil(lambda: not window.isVisible())
        other.history_view.newest.click()
        qtbot.waitUntil(lambda: other.history_view.status.text() == "No saved scans yet")
    finally:
        other.close()
        qtbot.waitUntil(lambda: not other.isVisible())
        other.deleteLater()


def test_close_cancels_paused_worker(
    qtbot: QtBot, window: MainWindow, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "sample"
    root.mkdir()
    entered = Event()
    release = Event()
    original = Inventory.run

    def run(
        inventory: Inventory,
        control: ScanControl,
        save: Callable[[list[Entry]], None],
        report: Callable[[ScanProgress], None],
    ) -> None:
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Test did not release the scanner.")
        original(inventory, control, save, report)

    monkeypatch.setattr(Inventory, "run", run)
    window.navigate(1)
    window.scan_view.root.setText(str(root))
    qtbot.waitUntil(window.scan_view.start.isEnabled)
    window.scan_view.start.click()
    try:
        qtbot.waitUntil(entered.is_set)
        window.scan_view.pause.click()
        assert window.scan_view.status.text() == "Paused"
        assert not window.close()
        release.set()
        qtbot.waitUntil(lambda: not window.isVisible())
    finally:
        release.set()
    repo = ScanRepository(tmp_path / "inventory.sqlite3")
    try:
        assert repo.history()[0][0].state == "cancelled"
    finally:
        repo.close()


def test_explorer_filters_folder_and_dashboard_drill_down(
    qtbot: QtBot, window: MainWindow, tmp_path: Path
) -> None:
    root = tmp_path / "sample"
    (root / "nested").mkdir(parents=True)
    (root / "nested/bigger.log").write_bytes(b"abcde")
    (root / "smaller.txt").write_bytes(b"a")
    window.navigate(1)
    view = window.scan_view
    view.root.setText(str(root))
    qtbot.waitUntil(view.start.isEnabled)
    view.start.click()
    qtbot.waitUntil(lambda: view.status.text() == "Scan completed")
    window.navigate(6)
    qtbot.waitUntil(window.history_view.browse_inventory.isEnabled)
    window.history_view.browse_inventory.click()
    explorer = window.explorer
    qtbot.waitUntil(lambda: explorer.model.rowCount() == 2)
    tree_root = explorer.tree_model.index(0, 0)
    folder = explorer.tree_model.index(0, 0, tree_root)
    assert folder.data() == "nested"
    explorer.tree.clicked.emit(folder)
    qtbot.waitUntil(lambda: explorer.model.folder == "nested")
    assert [item.path for item in explorer.model.rows] == ["nested/bigger.log"]
    explorer.extension.setText(".txt")
    QTest.keyClick(explorer.extension, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: explorer.status.text() == "No matching entries")

    window.navigate(0)
    qtbot.waitUntil(lambda: "Scan #" in window.dashboard.status.text())
    chart = window.dashboard.charts.currentWidget().findChild(StorageChart)
    assert chart is not None
    QTest.keyClick(chart, Qt.Key.Key_Return)
    qtbot.waitUntil(lambda: explorer.isVisible() and explorer.model.rowCount() == 1)
    assert explorer.model.folder == "nested"
    window.navigate(0)
    qtbot.waitUntil(lambda: window.dashboard.charts.isVisible())
    window.dashboard.charts.extension_selected.emit(".txt")
    qtbot.waitUntil(lambda: explorer.isVisible() and explorer.model.folder == "")
    assert [item.path for item in explorer.model.rows] == ["smaller.txt"]


def test_duplicate_finder_verifies_and_selects_keeper(
    qtbot: QtBot, window: MainWindow, tmp_path: Path
) -> None:
    folder = tmp_path / "copies"
    folder.mkdir()
    for name in ("a.txt", "b.txt"):
        (folder / name).write_text("same file", encoding="utf-8")
    window.navigate(1)
    view = window.scan_view
    view.root.setText(str(folder))
    qtbot.waitUntil(view.start.isEnabled)
    view.start.click()
    qtbot.waitUntil(lambda: view.status.text() == "Scan completed")

    window.navigate(6)
    qtbot.waitUntil(window.history_view.find_duplicates.isEnabled)
    window.history_view.find_duplicates.click()
    duplicates = window.duplicates
    qtbot.waitUntil(duplicates.find_button.isEnabled)
    duplicates.find_button.click()
    qtbot.waitUntil(lambda: duplicates.groups_model.rowCount() == 1)
    duplicates.groups.clicked.emit(duplicates.groups_model.index(0, 0))
    qtbot.waitUntil(lambda: duplicates.files_model.rowCount() == 2)
    duplicates.files.setCurrentIndex(duplicates.files_model.index(1, 0))
    assert duplicates.verify.isEnabled()
    duplicates.verify.click()
    qtbot.waitUntil(lambda: "Bytes match" in duplicates.status.text())
    duplicates.keeper.click()
    qtbot.waitUntil(lambda: "Keeper  b.txt" in duplicates.group_detail.text())
    duplicates.preview.click()
    qtbot.waitUntil(lambda: "Previewed" in duplicates.status.text())
    dialog = duplicates.findChild(QDialog)
    assert dialog is not None
    content = dialog.findChild(QPlainTextEdit)
    assert content is not None and content.toPlainText() == "same file"
    dialog.close()
    assert sorted(path.name for path in folder.iterdir()) == ["a.txt", "b.txt"]


def test_cleanup_confirmation_and_audit(
    qtbot: QtBot, window: MainWindow, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TemporaryDirectory(prefix="disksense-cleanup-test-", dir=Path.cwd()) as directory:
        folder = Path(directory)
        source = folder / "sample.tmp"
        source.write_bytes(b"temporary")
        destination = folder / "moved.fixture"

        def recycle(path: Path, revalidate: Callable[[], None]) -> None:
            revalidate()
            assert path == source
            path.rename(destination)

        service = window.scan_controller._cleanup
        monkeypatch.setattr(service, "_recycle", recycle)
        monkeypatch.setattr(service, "_supported", True)
        window.navigate(1)
        window.scan_view.root.setText(str(folder))
        qtbot.waitUntil(window.scan_view.start.isEnabled)
        window.scan_view.start.click()
        qtbot.waitUntil(lambda: window.scan_view.status.text() == "Scan completed")
        window.navigate(6)
        qtbot.waitUntil(window.history_view.review_cleanup.isEnabled)
        window.history_view.review_cleanup.click()
        view = window.cleanup
        qtbot.waitUntil(view.refresh.isEnabled)
        view.temporary.setValue(0)
        view.refresh.click()
        qtbot.waitUntil(lambda: view.model.rowCount() == 1)
        view.files.selectRow(0)
        view.preview.click()
        qtbot.waitUntil(lambda: view.confirmation is not None)
        assert source.exists() and not destination.exists()
        assert view.confirmation is not None
        view.confirmation.reject()
        assert source.exists()
        view.preview.click()
        qtbot.waitUntil(lambda: view.confirmation is not None)
        assert view.confirmation is not None
        confirm = next(
            button
            for button in view.confirmation.findChildren(QPushButton)
            if button.text() == "Send eligible files to Recycle Bin"
        )
        confirm.click()
        qtbot.waitUntil(lambda: view.audit_model.rowCount() == 1)
        assert view.audit_model.index(0, 1).data() == "Recycled"
        assert "Recycle Bin" in view.detail.text()
        assert not source.exists() and destination.read_bytes() == b"temporary"


def test_storage_monitor_alert_and_pause(qtbot: QtBot, window: MainWindow, tmp_path: Path) -> None:
    root = tmp_path / "watched"
    root.mkdir()
    window.navigate(4)
    view = window.monitor
    view.path.setText(str(root))
    view.threshold.setValue(1)
    qtbot.waitUntil(view.add.isEnabled)
    view.add.click()
    qtbot.waitUntil(lambda: view.status.text() == "Watching")
    assert view.snapshots.rowCount() == 1
    (root / "growth").write_bytes(b"x" * 1024**2)
    qtbot.waitUntil(view.sample.isEnabled)
    view.sample.click()
    qtbot.waitUntil(lambda: window.statusBar().currentMessage().startswith("Storage grew"))
    assert view.snapshots.rowCount() == 2
    assert "Growth alert" in str(view.snapshots.index(0, 4).data())
    view.toggle.click()
    qtbot.waitUntil(lambda: view.status.text() == "Paused")
    assert not view.sample.isEnabled()


def test_recommendations_dismiss_and_restore(
    qtbot: QtBot, window: MainWindow, tmp_path: Path
) -> None:
    root = tmp_path / "sample"
    root.mkdir()
    old = time() - 40 * 86400
    for index in range(10):
        path = root / f"{index}.tmp"
        path.write_bytes(b"temporary")
        os.utime(path, (old, old))
    window.navigate(1)
    window.scan_view.root.setText(str(root))
    qtbot.waitUntil(window.scan_view.start.isEnabled)
    window.scan_view.start.click()
    qtbot.waitUntil(lambda: window.scan_view.status.text() == "Scan completed")
    window.navigate(5)
    view = window.recommendations
    qtbot.waitUntil(lambda: view.model.rowCount() == 1)
    assert "10 temporary files" in view.details.text()
    assert view.model.index(0, 1).data() == "Medium"
    view.dismiss.click()
    qtbot.waitUntil(lambda: view.model.rowCount() == 0)
    view.show_dismissed.setChecked(True)
    assert view.dismiss.text() == "Restore recommendation"
    view.dismiss.click()
    qtbot.waitUntil(lambda: view.dismiss.text() == "Dismiss")
    assert len(list(root.iterdir())) == 10
