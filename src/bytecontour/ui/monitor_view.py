"""Folder watch controls, storage history, and recent filesystem events."""

from datetime import datetime

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen, QPolygonF
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from bytecontour.monitor import MonitorData, Root, Snapshot
from bytecontour.ui.duplicate_view import _fill, _table
from bytecontour.ui.scan_controller import ScanController
from bytecontour.ui.scan_views import _layout, _note, _size

_MIB = 1024**2


def _when(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%d %b %H:%M:%S")


class GrowthChart(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.samples: tuple[Snapshot, ...] = ()
        self.setMinimumHeight(150)
        self.setAccessibleName("Logical storage usage over time")

    def paintEvent(self, event: QPaintEvent, /) -> None:
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#151c27"))
        painter.setPen(QColor("#e5edf5"))
        chart = QRectF(self.rect()).adjusted(80, 14, -20, -30)
        samples = tuple(sample for sample in reversed(self.samples) if sample.error is None)
        if not samples or chart.width() <= 0:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No complete samples yet")
            painter.end()
            return
        elapsed = max(1.0, samples[-1].observed - samples[0].observed)
        high = max(1, max(sample.logical for sample in samples))
        for tick in (0, 0.5, 1):
            y = chart.bottom() - chart.height() * tick
            painter.setPen(QColor("#334254"))
            painter.drawLine(QPointF(chart.left(), y), QPointF(chart.right(), y))
            painter.setPen(QColor("#e5edf5"))
            painter.drawText(QPointF(10, y + 4), _size(int(high * tick)))
        points = [
            QPointF(
                chart.left() + chart.width() * (sample.observed - samples[0].observed) / elapsed,
                chart.bottom() - chart.height() * sample.logical / high,
            )
            for sample in samples
        ]
        painter.setPen(QPen(QColor("#53d9bf"), 2))
        painter.drawPolyline(QPolygonF(points))
        painter.setBrush(QColor("#53d9bf"))
        for point, sample in zip(points, samples, strict=True):
            painter.setBrush(QColor("#ffc277" if sample.growth_alert else "#53d9bf"))
            painter.drawEllipse(point, 3, 3)
        painter.setPen(QColor("#e5edf5"))
        painter.drawText(QPointF(chart.left(), chart.bottom() + 20), _when(samples[0].observed))
        if len(samples) > 1:
            last = _when(samples[-1].observed)
            width = painter.fontMetrics().horizontalAdvance(last)
            if chart.width() > 2 * width + 16:
                painter.drawText(QPointF(chart.right() - width, chart.bottom() + 20), last)
        painter.end()


class MonitorView(QWidget):
    def __init__(self, controller: ScanController) -> None:
        super().__init__()
        self._controller = controller
        self._data = MonitorData((), None, (), ())
        self._busy = self._request = self._wanted = False
        layout = _layout(self, "Storage Monitor")
        add_row = QHBoxLayout()
        self.path = QLineEdit()
        self.path.setPlaceholderText("Folder to watch")
        self.path.setAccessibleName("Folder to watch")
        self.path.textChanged.connect(self._buttons)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.add = QPushButton("Watch folder")
        self.add.clicked.connect(self._add)
        add_row.addWidget(self.path, 1)
        add_row.addWidget(browse)
        add_row.addWidget(self.add)
        layout.addLayout(add_row)
        fields = QGridLayout()
        self.roots = QComboBox()
        self.roots.setAccessibleName("Watched folder")
        self.roots.activated.connect(self._select)
        fields.addWidget(self.roots, 0, 0, 1, 3)
        self.interval = self._number("Sample interval (s)", 5, 86400, 60)
        self.threshold = self._number("Growth alert (MiB, 0 off)", 0, 2**30, 128)
        self.cooldown = self._number("Alert cooldown (s)", 1, 86400, 300)
        for index, value in enumerate((self.interval, self.threshold, self.cooldown)):
            fields.addWidget(QLabel(value.accessibleName()), 1, index)
            fields.addWidget(value, 2, index)
        layout.addLayout(fields)
        actions = QHBoxLayout()
        self.save = QPushButton("Save settings")
        self.save.clicked.connect(self._save)
        self.toggle = QPushButton("Pause watch")
        self.toggle.clicked.connect(self._toggle)
        self.sample = QPushButton("Reconcile now")
        self.sample.clicked.connect(lambda: self._poll(reconcile=True))
        self.remove = QPushButton("Remove watch")
        self.remove.clicked.connect(self._remove)
        for button in (self.save, self.toggle, self.sample, self.remove):
            actions.addWidget(button)
        actions.addStretch()
        self.cancel = QPushButton("Cancel sample")
        self.cancel.hide()
        self.cancel.clicked.connect(self._cancel)
        actions.addWidget(self.cancel)
        layout.addLayout(actions)
        self.status = _note("Add a folder to start collecting storage history.")
        layout.addWidget(self.status)
        tabs = QTabWidget()
        history = QWidget()
        history_layout = QVBoxLayout(history)
        self.chart = GrowthChart()
        history_layout.addWidget(self.chart, 1)
        view, self.snapshots = _table(
            self, ("Sample time", "Files", "Logical", "Allocated", "State")
        )
        history_layout.addWidget(view, 1)
        tabs.addTab(history, "Growth history")
        view, self.events = _table(self, ("Path", "Change", "Destination", "Observed"))
        tabs.addTab(view, "Recent events")
        layout.addWidget(tabs, 1)
        controller.monitor_ready.connect(self._ready)
        controller.monitor_busy.connect(self.cancel.setVisible)
        controller.busy_changed.connect(self._busy_changed)
        controller.failed.connect(self._failed)
        controller.idle.connect(self._pump)
        self._buttons()

    @staticmethod
    def _number(label: str, minimum: int, maximum: int, initial: int) -> QSpinBox:
        widget = QSpinBox()
        widget.setAccessibleName(label)
        widget.setRange(minimum, maximum)
        widget.setValue(initial)
        return widget

    def activate(self) -> None:
        self._wanted = True
        self._pump()

    def _pump(self) -> None:
        if self._wanted and self.isVisible():
            self._poll()

    def _root(self) -> Root | None:
        identifier = self.roots.currentData()
        return next((root for root in self._data.roots if root.id == identifier), None)

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Choose a folder to monitor")
        if folder:
            self.path.setText(folder)

    def _fields(self, root: Root) -> None:
        self.interval.setValue(root.interval)
        self.threshold.setValue(root.threshold // _MIB)
        self.cooldown.setValue(root.cooldown)

    def _configure(self, root: Root | None, enabled: bool, *, preserve: bool = False) -> None:
        if preserve and root is not None:
            self._fields(root)
        if self._controller.configure_monitor(
            root.id if root else None,
            self.path.text(),
            enabled,
            self.interval.value(),
            self.threshold.value() * _MIB,
            self.cooldown.value(),
        ):
            self._request = True
            self.status.setText("Updating folder watch…")

    def _add(self) -> None:
        self._configure(None, True)

    def _save(self) -> None:
        if root := self._root():
            self._configure(root, root.enabled)

    def _toggle(self) -> None:
        if root := self._root():
            self._configure(root, not root.enabled, preserve=True)

    def _select(self) -> None:
        if root := self._root():
            self._fields(root)
        self._poll()

    def _remove(self) -> None:
        root = self._root()
        if root is None:
            return
        question = QMessageBox(self)
        question.setWindowTitle("Remove folder watch")
        question.setTextFormat(Qt.TextFormat.PlainText)
        question.setText(f"Remove this watch and its monitoring history?\n{root.path}")
        question.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        question.setDefaultButton(QMessageBox.StandardButton.No)
        if question.exec() == QMessageBox.StandardButton.Yes:
            self._request = self._controller.remove_monitor(root.id)
            if not self._request:
                self.status.setText("Storage is busy. Try removing the watch again.")

    def _poll(self, *, reconcile: bool = False) -> None:
        root = self._root()
        if self._controller.monitor_poll(root.id if root else None, reconcile=reconcile):
            self._request = True
            self._wanted = False
            if reconcile:
                self.status.setText("Reconciling folder size…")

    def _ready(self, data: MonitorData) -> None:
        self._request = False
        old = self._data
        self._data = data
        if (
            tuple((r.id, r.path) for r in data.roots) != tuple((r.id, r.path) for r in old.roots)
            or self.roots.currentData() != data.selected
        ):
            self.roots.clear()
            for option in data.roots:
                self.roots.addItem(option.path, option.id)
            self.roots.setCurrentIndex(self.roots.findData(data.selected))
        root = self._root()
        previous = next((r for r in old.roots if r.id == data.selected), None)
        if root and (old.selected != data.selected or previous is None):
            self._fields(root)
            self.path.clear()
        self.status.setText(root.status if root else "Add a folder to start collecting history.")
        if data.selected != old.selected or data.snapshots != old.snapshots:
            self.chart.samples = data.snapshots
            self.chart.update()
            _fill(
                self.snapshots,
                [
                    (
                        _when(s.observed),
                        str(s.files),
                        _size(s.logical),
                        _size(s.allocated),
                        f"Growth alert +{_size(s.growth_alert)}"
                        if s.growth_alert
                        else "Partial / unavailable"
                        if s.error
                        else "Complete",
                    )
                    for s in data.snapshots
                ],
            )
        if data.selected != old.selected or data.events != old.events:
            _fill(
                self.events,
                [
                    (e.source, e.kind.capitalize(), e.destination or "", _when(e.observed))
                    for e in data.events
                ],
            )
        self._buttons()

    def _busy_changed(self, busy: bool) -> None:
        self._busy = busy
        self._buttons()

    def _cancel(self) -> None:
        if self._controller.cancel():
            self.status.setText("Cancelling sample…")

    def _failed(self, message: str) -> None:
        if self._request:
            self._request = False
            self.status.setText(f"Storage Monitor · {message}")

    def _buttons(self, *_: object) -> None:
        root = self._root()
        self.add.setEnabled(not self._busy and bool(self.path.text()))
        self.roots.setEnabled(not self._busy and bool(self._data.roots))
        self.save.setEnabled(not self._busy and root is not None)
        self.toggle.setEnabled(not self._busy and root is not None)
        self.toggle.setText("Resume watch" if root and not root.enabled else "Pause watch")
        self.sample.setEnabled(not self._busy and bool(root and root.enabled))
        self.remove.setEnabled(not self._busy and root is not None)
