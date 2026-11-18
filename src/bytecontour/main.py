"""Compose the Qt application and start its event loop."""

import logging
import sys
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from bytecontour.app_paths import APP_NAME
from bytecontour.database import ScanSummary
from bytecontour.logging_config import configure_logging
from bytecontour.monitor import MonitorData
from bytecontour.ui.theme import apply_theme
from bytecontour.ui.window import MainWindow


def main() -> int:
    configure_logging()
    smoke = sys.argv[1:] == ["--smoke-test"]
    app = QApplication([sys.argv[0], "-platform", "offscreen"] if smoke else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_NAME)
    app.setApplicationVersion(version("bytecontour"))
    app.setWindowIcon(QIcon(str(Path(__file__).parent / "assets" / "bytecontour.png")))
    apply_theme(app)
    if smoke:
        return _smoke_test(app)

    window = MainWindow()
    window.show()
    logging.getLogger(__name__).info("application_started")
    try:
        exit_code = app.exec()
    finally:
        window.scan_controller.shutdown()
    logging.getLogger(__name__).info(
        "application_stopped", extra={"context": {"exit_code": exit_code}}
    )
    return exit_code


def _smoke_test(app: QApplication) -> int:
    if app.windowIcon().pixmap(32, 32).isNull():
        return 2
    with TemporaryDirectory(prefix="bytecontour-smoke-") as directory:
        base = Path(directory)
        root = base / "sample"
        root.mkdir()
        (root / "probe.txt").write_bytes(b"bytecontour")
        settings = QSettings(str(base / "settings.ini"), QSettings.Format.IniFormat)
        window = MainWindow(settings, base / "inventory.sqlite3")
        window.navigate(1)
        window.show()

        def finished(summary: ScanSummary) -> None:
            if summary.state != "completed" or summary.files != 1 or summary.logical_bytes != 11:
                app.exit(2)
            elif not window.scan_controller.configure_monitor(None, str(root), True, 60, 0, 300):
                app.exit(2)

        def monitored(data: MonitorData) -> None:
            ok = (
                len(data.roots) == 1
                and data.roots[0].status == "Watching"
                and len(data.snapshots) == 1
                and data.snapshots[0].logical == 11
            )
            app.exit(0 if ok else 2)

        window.scan_controller.scan_finished.connect(finished)
        window.scan_controller.monitor_ready.connect(monitored)
        window.scan_controller.failed.connect(lambda _: app.exit(2))
        QTimer.singleShot(0, lambda: window.scan_controller.start(root, False))
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(lambda: app.exit(3))
        deadline.start(20_000)
        try:
            return app.exec()
        finally:
            deadline.stop()
            window.scan_controller.shutdown()
            window.close()
