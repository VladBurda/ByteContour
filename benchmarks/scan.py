"""Measure production scan throughput and Qt responsiveness on disposable files."""

import argparse
import ctypes
import json
import os
import platform
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication

from disksense.database import ScanRepository, ScanSummary
from disksense.scanner import Entry
from disksense.ui.window import MainWindow


def peak_rss() -> int:
    if sys.platform == "win32":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("page_faults", wintypes.DWORD),
                ("peak_working_set", ctypes.c_size_t),
                ("working_set", ctypes.c_size_t),
                *[
                    (name, ctypes.c_size_t)
                    for name in (
                        "peak_paged",
                        "paged",
                        "peak_nonpaged",
                        "nonpaged",
                        "pagefile",
                        "peak_pagefile",
                    )
                ],
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(Counters),
            wintypes.DWORD,
        )
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(counters.peak_working_set)

    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak if platform.system() == "Darwin" else peak * 1024)


def populate(root: Path, files: int) -> None:
    root.mkdir()
    for offset in range(0, files, 1000):
        folder = root / f"{offset:07}"
        folder.mkdir()
        for index in range(offset, min(offset + 1000, files)):
            os.close(os.open(folder / f"{index:07}.bin", os.O_CREAT | os.O_EXCL | os.O_WRONLY))


def measure(app: QApplication, scratch: Path, files: int) -> dict[str, object]:
    root = scratch / "fixture"
    populate(root, files)
    database = scratch / "inventory.sqlite3"
    settings = QSettings(str(scratch / "settings.ini"), QSettings.Format.IniFormat)
    window = MainWindow(settings, database)
    window.navigate(1)
    window.show()
    app.processEvents()

    samples = 0
    max_gap = 0.0
    last_tick = 0.0
    insert_seconds = 0.0
    inserted = max_batch = 0
    baseline_rss = peak_rss()
    summary: ScanSummary | None = None
    failure: str | None = None
    append = ScanRepository.append

    def timed_append(repo: ScanRepository, scan_id: int, rows: list[Entry]) -> None:
        nonlocal insert_seconds, inserted, max_batch
        start = perf_counter()
        append(repo, scan_id, rows)
        insert_seconds += perf_counter() - start
        inserted += len(rows)
        max_batch = max(max_batch, len(rows))

    def tick() -> None:
        nonlocal last_tick, max_gap, samples
        now = perf_counter()
        max_gap = max(max_gap, now - last_tick)
        last_tick = now
        samples += 1

    def finished(result: ScanSummary) -> None:
        nonlocal summary
        summary = result
        tick()
        app.quit()

    def failed(message: str) -> None:
        nonlocal failure
        failure = message
        app.quit()

    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(tick)
    window.scan_controller.scan_finished.connect(finished)
    window.scan_controller.failed.connect(failed)
    window.scan_view.root.setText(str(root))
    with patch.object(ScanRepository, "append", timed_append):
        started = last_tick = perf_counter()
        timer.start()
        window.scan_view.start.click()
        app.exec()
        wall = perf_counter() - started
    timer.stop()
    scan_peak = peak_rss()
    window.close()
    if summary is None or summary.state != "completed" or summary.files != files:
        raise RuntimeError(failure or f"Unexpected scan summary: {summary}")
    return {
        "system": platform.platform(),
        "python": platform.python_version(),
        "qt_backend": app.platformName(),
        "fixture_drive": scratch.anchor,
        "files": files,
        "directories": summary.directories,
        "wall_seconds": round(wall, 3),
        "files_per_second": round(files / wall),
        "insert_seconds": round(insert_seconds, 3),
        "insert_rows_per_second": round(inserted / insert_seconds),
        "max_batch_rows": max_batch,
        "pre_scan_peak_rss_mib": round(baseline_rss / 2**20, 1),
        "peak_rss_mib": round(scan_peak / 2**20, 1),
        "max_qt_timer_gap_ms": round(max_gap * 1000, 2),
        "qt_timer_samples": samples,
        "database_mib": round(database.stat().st_size / 2**20, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--files", type=int, required=True)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args()
    if args.files < 1:
        parser.error("--files must be positive")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication([])
    app.setApplicationName("DiskSense Benchmark")
    with TemporaryDirectory(prefix="disksense-benchmark-", dir=args.workspace.resolve()) as temp:
        print(json.dumps(measure(app, Path(temp), args.files), indent=2), flush=True)


if __name__ == "__main__":
    main()
