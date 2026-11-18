"""Time explorer interactions over a disposable million-row SQLite inventory."""

import argparse
import json
import os
import platform
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication

from bytecontour.database import ScanRepository
from bytecontour.inventory import PAGE_SIZE, InventoryResult
from bytecontour.scanner import Entry, ScanProgress
from bytecontour.ui.theme import apply_theme
from bytecontour.ui.window import MainWindow


def seed(database: Path, root: Path, files: int) -> int:
    repo = ScanRepository(database)
    try:
        repo.initialize()
        scan, _ = repo.begin(str(root), os.path.normcase(str(root)), False)
        repo.append(scan, [Entry("", None, "directory", "", files, None, 0, 0, None, 1, files)])
        with repo.engine.begin() as conn:
            conn.exec_driver_sql(
                """
                WITH RECURSIVE numbers(i) AS (
                    VALUES(1) UNION ALL SELECT i + 1 FROM numbers WHERE i < ?
                )
                INSERT INTO entries (
                    scan_id, path, parent, kind, extension, logical_bytes, allocated_bytes,
                    modified_ns, change_ns, identity, links, file_count, incomplete
                )
                SELECT ?, printf('sample-%07d.bin', i), '', 'file', '.bin', 1, NULL,
                       0, 0, NULL, 1, 1, 0
                FROM numbers
                """,
                (files, scan),
            )
        repo.finish(scan, "completed", ScanProgress(files, 1, files, None, 0, 0, 0, "", None), None)
        return scan
    finally:
        repo.close()


def measure(app: QApplication, root: Path, files: int) -> dict[str, object]:
    database = root / "inventory.sqlite3"
    scan_id = seed(database, root, files)
    window = MainWindow(QSettings(str(root / "ui.ini"), QSettings.Format.IniFormat), database)
    window.navigate(1)
    window.show()
    max_gap = 0.0
    last_tick = started = perf_counter()
    times: list[float] = []
    page_rows: list[int] = []
    failures: list[str] = []

    def tick() -> None:
        nonlocal last_tick, max_gap
        now = perf_counter()
        max_gap = max(max_gap, now - last_tick)
        last_tick = now

    def ready(result: InventoryResult) -> None:
        nonlocal started
        tick()
        times.append(perf_counter() - started)
        page_rows.append(len(result.entries))
        started = perf_counter()
        if len(times) == 1:
            QTimer.singleShot(0, window.explorer.next.click)
        elif len(times) == 2:

            def search() -> None:
                window.explorer.search.setText("0000010")
                window.explorer.search.returnPressed.emit()

            QTimer.singleShot(0, search)
        else:
            app.quit()

    def failed(message: str) -> None:
        failures.append(message)
        app.quit()

    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(tick)
    deadline = QTimer()
    deadline.setSingleShot(True)
    deadline.timeout.connect(lambda: failed("Explorer timed out"))
    window.scan_controller.inventory_ready.connect(ready)
    window.scan_controller.failed.connect(failed)
    timer.start()
    deadline.start(60_000)
    started = last_tick = perf_counter()
    window.history_view.inventory_requested.emit(scan_id)
    try:
        app.exec()
    finally:
        timer.stop()
        deadline.stop()
        window.scan_controller.shutdown()
        window.close()
    if failures or page_rows != [PAGE_SIZE, PAGE_SIZE, 1]:
        raise RuntimeError(f"Unexpected benchmark result. {failures} Rows {page_rows}")
    return {
        "system": platform.platform(),
        "python": platform.python_version(),
        "qt_backend": app.platformName(),
        "files": files,
        "page_rows": page_rows,
        "overview_and_first_page_ms": round(times[0] * 1000, 2),
        "next_page_ms": round(times[1] * 1000, 2),
        "substring_search_ms": round(times[2] * 1000, 2),
        "max_qt_timer_gap_ms": round(max_gap * 1000, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, default=1_000_000)
    args = parser.parse_args()
    if not PAGE_SIZE * 2 <= args.files <= 9_999_999:
        parser.error(f"--files must be between {PAGE_SIZE * 2} and 9999999")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication([])
    app.setApplicationName("ByteContour Browse Benchmark")
    apply_theme(app)
    with TemporaryDirectory(prefix="bytecontour-benchmark-", dir=Path.cwd()) as path:
        print(json.dumps(measure(app, Path(path), args.files), indent=2), flush=True)


if __name__ == "__main__":
    main()
