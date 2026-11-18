"""Clickable Qt charts with bounded data from an inventory overview."""

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QTabWidget, QVBoxLayout, QWidget

from bytecontour.inventory import Overview
from bytecontour.ui.scan_views import _size

_COLORS = ("#295654", "#28465b", "#39476b", "#65486b", "#625347", "#395461")


@dataclass(frozen=True, slots=True)
class ChartEntry:
    label: str
    size: int
    target: str | None


def _partition(entries: tuple[ChartEntry, ...], area: QRectF) -> list[tuple[ChartEntry, QRectF]]:
    if len(entries) <= 1:
        return [(entries[0], area)] if entries else []
    total = sum(entry.size for entry in entries)
    split = min(
        range(1, len(entries)),
        key=lambda i: abs(total - 2 * sum(entry.size for entry in entries[:i])),
    )
    fraction = sum(entry.size for entry in entries[:split]) / total
    x, y, width, height = area.x(), area.y(), area.width(), area.height()
    if width >= height:
        cut = width * fraction
        first, second = QRectF(x, y, cut, height), QRectF(x + cut, y, width - cut, height)
    else:
        cut = height * fraction
        first, second = QRectF(x, y, width, cut), QRectF(x, y + cut, width, height - cut)
    return _partition(entries[:split], first) + _partition(entries[split:], second)


class StorageChart(QWidget):
    activated = Signal(str)

    def __init__(self, *, bars: bool = False) -> None:
        super().__init__()
        self._bars = bars
        self._entries: tuple[ChartEntry, ...] = ()
        self._selected = 0
        self.setMinimumHeight(150)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName("File type usage" if bars else "Storage treemap")

    def set_entries(self, entries: tuple[ChartEntry, ...]) -> None:
        # A zero-byte treemap region has no area. Bar rows still show zero-byte file types.
        self._entries = entries if self._bars else tuple(entry for entry in entries if entry.size)
        self._selected = 0
        self.update()

    def _regions(self) -> list[tuple[ChartEntry, QRectF]]:
        area = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        if not self._bars:
            return _partition(self._entries, area)
        if not self._entries:
            return []
        height = area.height() / len(self._entries)
        return [
            (entry, QRectF(area.x(), area.y() + index * height, area.width(), height))
            for index, entry in enumerate(self._entries)
        ]

    def paintEvent(self, event: QPaintEvent, /) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#11151d"))
        painter.setPen(QColor("#eef2f7"))
        if not self._entries:
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "No usage to chart")
        maximum = max((item.size for item in self._entries), default=0) or 1
        for index, (entry, raw) in enumerate(self._regions()):
            area = raw.adjusted(1, 1, -1, -1)
            if area.isEmpty():
                continue
            color = QColor(_COLORS[index % len(_COLORS)])
            if self._bars:
                painter.fillRect(area, QColor("#1a222e"))
                bar = QRectF(area)
                bar.setWidth(area.width() * entry.size / maximum)
                painter.fillRect(bar, color)
            else:
                painter.fillRect(area, color)
            if self.hasFocus() and index == self._selected:
                painter.setPen(QPen(QColor("#8ce0cf"), 2))
                painter.drawRect(area)
            if area.width() > 24 and area.height() >= painter.fontMetrics().height():
                painter.setPen(QColor("#eef2f7"))
                label = painter.fontMetrics().elidedText(
                    f"{entry.label}  {_size(entry.size)}",
                    Qt.TextElideMode.ElideRight,
                    int(area.width()) - 10,
                )
                painter.drawText(area.adjusted(5, 0, -5, 0), Qt.AlignmentFlag.AlignVCenter, label)
        painter.end()

    def _hit(self, position: QPointF) -> int | None:
        return next(
            (index for index, (_, area) in enumerate(self._regions()) if area.contains(position)),
            None,
        )

    def mouseMoveEvent(self, event: QMouseEvent, /) -> None:
        index = self._hit(event.position())
        entry = self._entries[index] if index is not None else None
        self.setToolTip(f"{entry.label} · {_size(entry.size)}" if entry else "")

    def mouseReleaseEvent(self, event: QMouseEvent, /) -> None:
        index = self._hit(event.position())
        if event.button() == Qt.MouseButton.LeftButton and index is not None:
            self._selected = index
            self._activate()

    def keyPressEvent(self, event: QKeyEvent, /) -> None:
        if self._entries and event.key() in (Qt.Key.Key_Left, Qt.Key.Key_Up):
            self._selected = (self._selected - 1) % len(self._entries)
            self.update()
        elif self._entries and event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Down):
            self._selected = (self._selected + 1) % len(self._entries)
            self.update()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self._activate()
        else:
            super().keyPressEvent(event)

    def _activate(self) -> None:
        if self._entries and (target := self._entries[self._selected].target) is not None:
            self.activated.emit(target)


class UsagePanel(QTabWidget):
    entry_selected = Signal(str, bool)
    extension_selected = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._map = StorageChart()
        self._types = StorageChart(bars=True)
        self._largest = QListWidget()
        self._folders = QListWidget()
        self._tile_folders: set[str] = set()
        self.addTab(
            self._chart_page(self._map, "Largest items in this folder by logical size"),
            "Treemap",
        )
        self.addTab(
            self._chart_page(self._types, "Largest file types in this folder and its subfolders"),
            "File types",
        )
        self.addTab(self._largest, "Largest files")
        self.addTab(self._folders, "Largest folders")
        self._map.activated.connect(self._open_tile)
        self._types.activated.connect(self.extension_selected.emit)
        self._largest.itemActivated.connect(self._open_largest)
        self._folders.itemActivated.connect(
            lambda item: self.entry_selected.emit(str(item.data(Qt.ItemDataRole.UserRole)), True)
        )

    @staticmethod
    def _chart_page(chart: StorageChart, description: str) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel(description)
        label.setWordWrap(True)
        layout.addWidget(label)
        layout.addWidget(chart, 1)
        return page

    def _open_tile(self, path: str) -> None:
        self.entry_selected.emit(path, path in self._tile_folders)

    def _open_largest(self, item: QListWidgetItem) -> None:
        self.entry_selected.emit(str(item.data(Qt.ItemDataRole.UserRole)), False)

    def load(self, overview: Overview | None) -> None:
        self._largest.clear()
        self._folders.clear()
        if overview is None:
            self._tile_folders.clear()
            self._map.set_entries(())
            self._types.set_entries(())
            return
        self._tile_folders = {item.path for item in overview.tiles if item.kind == "directory"}
        tiles = tuple(ChartEntry(item.name, item.size, item.path) for item in overview.tiles)
        remainder = overview.folder.size - sum(tile.size for tile in tiles)
        if remainder > 0:
            tiles += (ChartEntry("Other items", remainder, None),)
        self._map.set_entries(tiles)
        self._types.set_entries(
            tuple(
                ChartEntry(item.extension or "No extension", item.size, item.extension)
                for item in overview.types
            )
        )
        for file in overview.largest_files:
            row = QListWidgetItem(f"{_size(file.size)}   {file.path}")
            row.setData(Qt.ItemDataRole.UserRole, file.path)
            self._largest.addItem(row)
        for folder in overview.largest_folders:
            row = QListWidgetItem(f"{_size(folder.size)}   {folder.name}")
            row.setData(Qt.ItemDataRole.UserRole, folder.path)
            self._folders.addItem(row)
