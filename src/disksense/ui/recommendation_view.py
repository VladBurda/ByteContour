"""Review evidence, tune thresholds, and dismiss recommendations."""

from PySide6.QtCore import QModelIndex, QSettings
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from disksense.recommendations import Recommendation, RecommendationPage, Thresholds
from disksense.ui.duplicate_view import _fill, _table
from disksense.ui.scan_controller import ScanController
from disksense.ui.scan_views import _layout, _note, _size

_FIELDS = (
    ("old_days", "Large file age (days)", 36500),
    ("large_mib", "Large file size (MiB)", 2**30),
    ("growth_mib", "Growth threshold (MiB)", 2**30),
    ("growth_days", "Growth window (days)", 365),
    ("duplicate_mib", "Duplicate potential (MiB)", 2**30),
    ("installer_count", "Installers per folder", 1_000_000),
    ("temp_days", "Temporary file age (days)", 36500),
    ("temp_count", "Temporary files per folder", 1_000_000),
)


def _setting(settings: QSettings, name: str, default: int, maximum: int) -> int:
    value = settings.value(f"recommendations/{name}", default, type=int)
    return max(1, min(maximum, value)) if isinstance(value, int) else default


class RecommendationView(QWidget):
    def __init__(self, controller: ScanController, settings: QSettings) -> None:
        super().__init__()
        self._controller = controller
        self._settings = settings
        self._page = RecommendationPage((), frozenset(), None, False)
        self._visible: tuple[Recommendation, ...] = ()
        self._busy = self._wanted = self._loading = False
        defaults = Thresholds()
        self._thresholds = Thresholds(
            **{
                name: _setting(settings, name, getattr(defaults, name), maximum)
                for name, _, maximum in _FIELDS
            }
        )
        layout = _layout(self, "Recommendations")
        self.scope = _note()
        layout.addWidget(self.scope)
        bar = QHBoxLayout()
        self.refresh = QPushButton("Refresh")
        self.refresh.clicked.connect(self.activate)
        self.thresholds = QPushButton("Edit thresholds")
        self.thresholds.clicked.connect(self._edit)
        self.show_dismissed = QCheckBox("Show dismissed")
        self.show_dismissed.toggled.connect(self._render)
        bar.addWidget(self.refresh)
        bar.addWidget(self.thresholds)
        bar.addWidget(self.show_dismissed)
        bar.addStretch()
        layout.addLayout(bar)
        self.status = _note()
        layout.addWidget(self.status)
        self.table, self.model = _table(self, ("Recommendation", "Risk", "Logical potential"))
        self.table.setAccessibleName("Storage recommendations")
        self.table.selectionModel().currentChanged.connect(self._select)
        layout.addWidget(self.table, 1)
        self.details = _note()
        layout.addWidget(self.details)
        self.dismiss = QPushButton("Dismiss")
        self.dismiss.clicked.connect(self._dismiss)
        layout.addWidget(self.dismiss)
        controller.recommendations_ready.connect(self._ready)
        controller.busy_changed.connect(self._set_busy)
        controller.idle.connect(self._pump)
        controller.failed.connect(self._failed)
        self._buttons()

    def activate(self) -> None:
        self._wanted = True
        self._pump()

    def _pump(self) -> None:
        if self._wanted and self.isVisible() and not self._busy:
            if self._controller.load_recommendations(self._thresholds):
                self._wanted = False
                self._loading = True
                self.status.setText("Evaluating saved storage evidence…")

    def _ready(self, page: RecommendationPage) -> None:
        self._loading = False
        self._page = page
        scan = page.scan_scope
        scope = f"Latest saved inventory · {scan}" if scan else "No saved inventory"
        if page.partial:
            scope += " · Partial"
        self.scope.setText(f"{scope}\nGrowth recommendations use complete monitor samples.")
        self._render()

    def _render(self) -> None:
        self._visible = tuple(
            item
            for item in self._page.rows
            if self.show_dismissed.isChecked() or item.identity not in self._page.dismissed
        )
        self.details.clear()
        _fill(
            self.model,
            [
                (
                    f"{item.title} (dismissed)"
                    if item.identity in self._page.dismissed
                    else item.title,
                    item.risk,
                    _size(item.logical_potential) if item.logical_potential is not None else "—",
                )
                for item in self._visible
            ],
        )
        self.status.setText(
            f"{len(self._visible)} recommendations"
            if self._visible
            else "No recommendations match the current evidence and thresholds."
        )
        if self._visible:
            self.table.setCurrentIndex(self.model.index(0, 0))
        self._buttons()

    def _selected(self) -> Recommendation | None:
        row = self.table.currentIndex().row()
        return self._visible[row] if 0 <= row < len(self._visible) else None

    def _select(self, _: QModelIndex) -> None:
        item = self._selected()
        if item is not None:
            allocation = (
                f"\nAllocated estimate  {_size(item.allocated_potential)}"
                if item.logical_potential is not None
                else ""
            )
            self.details.setText(f"{item.scope}\n{item.reason}{allocation}\n{item.next_step}")
        else:
            self.details.clear()
        self._buttons()

    def _dismiss(self) -> None:
        item = self._selected()
        if item is not None and self._controller.load_recommendations(
            self._thresholds, item, item.identity not in self._page.dismissed
        ):
            self._loading = True

    def _edit(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Recommendation thresholds")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        fields: dict[str, QSpinBox] = {}
        for name, label, maximum in _FIELDS:
            field = QSpinBox()
            field.setRange(1, maximum)
            field.setValue(getattr(self._thresholds, name))
            form.addRow(label, field)
            fields[name] = field
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            values = {name: field.value() for name, field in fields.items()}
            self._thresholds = Thresholds(**values)
            for name, value in values.items():
                self._settings.setValue(f"recommendations/{name}", value)
            self.activate()
        dialog.deleteLater()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._buttons()

    def _failed(self, message: str) -> None:
        if self._loading:
            self._loading = False
            self.status.setText(f"Recommendations · {message}")

    def _buttons(self) -> None:
        item = self._selected()
        self.dismiss.setEnabled(not self._busy and item is not None)
        self.dismiss.setText(
            "Restore recommendation"
            if item and item.identity in self._page.dismissed
            else "Dismiss"
        )
