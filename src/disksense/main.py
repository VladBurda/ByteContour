"""Compose the Qt application and start its event loop."""

import logging
import sys
from importlib.metadata import version

from PySide6.QtWidgets import QApplication

from disksense.logging_config import configure_logging
from disksense.ui.theme import apply_theme
from disksense.ui.window import MainWindow


def main() -> int:
    configure_logging()
    app = QApplication(sys.argv)
    app.setApplicationName("DiskSense")
    app.setOrganizationName("DiskSense")
    app.setApplicationVersion(version("disksense"))
    apply_theme(app)

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
