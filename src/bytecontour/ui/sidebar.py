"""Compact navigation with native Qt icons and accessible labels."""

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QStyle,
    QToolButton,
    QVBoxLayout,
)

from bytecontour.ui.pages import PAGES

_STANDARD = QStyle.StandardPixmap
_FALLBACK_ICONS = (
    _STANDARD.SP_ComputerIcon,
    _STANDARD.SP_DirOpenIcon,
    _STANDARD.SP_FileDialogDetailedView,
    _STANDARD.SP_TrashIcon,
    _STANDARD.SP_DriveHDIcon,
    _STANDARD.SP_MessageBoxInformation,
    _STANDARD.SP_FileDialogListView,
    _STANDARD.SP_FileDialogContentsView,
)


class Sidebar(QFrame):
    page_selected = Signal(int)
    toggle_requested = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("sidebar")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 18, 10, 12)
        layout.setSpacing(4)

        header = QHBoxLayout()
        self._toggle = QToolButton()
        self._toggle.setObjectName("sidebarToggle")
        self._toggle.setFixedSize(44, 36)
        self._toggle.clicked.connect(self.toggle_requested.emit)
        header.addWidget(self._toggle)

        self._brand = QLabel("ByteContour")
        self._brand.setObjectName("brand")
        self._brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self._brand, 1)
        layout.addLayout(header)
        layout.addSpacing(14)

        self._group = QButtonGroup(self)
        self._buttons: list[QToolButton] = []
        self._sections: list[QLabel] = []
        for index, (page, fallback) in enumerate(zip(PAGES, _FALLBACK_ICONS, strict=True)):
            if index in (0, 2, 4, 6):
                section = QLabel(
                    {0: "OVERVIEW", 2: "RECOVER SPACE", 4: "INSIGHTS", 6: "WORKSPACE"}[index]
                )
                section.setObjectName("sectionLabel")
                self._sections.append(section)
                layout.addWidget(section)
            button = QToolButton()
            button.setText(page.title)
            button.setToolTip(page.title)
            button.setAccessibleName(page.title)
            button.setIcon(QIcon.fromTheme(page.icon, self.style().standardIcon(fallback)))
            button.setIconSize(QSize(18, 18))
            button.setMinimumHeight(36)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button.setCheckable(True)
            self._group.addButton(button, index)
            self._buttons.append(button)
            layout.addWidget(button)

        layout.addStretch()
        self._group.idClicked.connect(self.page_selected.emit)
        self.set_collapsed(False)

    def select(self, index: int) -> None:
        self._buttons[index].setChecked(True)

    def set_collapsed(self, collapsed: bool) -> None:
        style = (
            Qt.ToolButtonStyle.ToolButtonIconOnly
            if collapsed
            else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        self._brand.setVisible(not collapsed)
        for section in self._sections:
            section.setVisible(not collapsed)
        self._toggle.setArrowType(Qt.ArrowType.RightArrow if collapsed else Qt.ArrowType.LeftArrow)
        label = "Expand sidebar" if collapsed else "Collapse sidebar"
        self._toggle.setAccessibleName(label)
        self._toggle.setToolTip(f"{label} (Ctrl+B)")
        self.setFixedWidth(64 if collapsed else 218)
        for button in self._buttons:
            button.setToolButtonStyle(style)
