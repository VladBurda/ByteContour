"""Shared dark Qt palette and widget stylesheet."""

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication


def apply_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = app.palette()
    roles = QPalette.ColorRole
    for role, color in {
        roles.Window: "#11151d",
        roles.WindowText: "#eef2f7",
        roles.Base: "#1a222e",
        roles.AlternateBase: "#1d2633",
        roles.Text: "#eef2f7",
        roles.Button: "#232d3b",
        roles.ButtonText: "#eef2f7",
        roles.Highlight: "#29494a",
        roles.HighlightedText: "#ffffff",
        roles.PlaceholderText: "#aeb9c9",
    }.items():
        palette.setColor(role, QColor(color))
    for role in (roles.Text, roles.WindowText, roles.ButtonText):
        palette.setColor(QPalette.ColorGroup.Disabled, role, QColor("#8793a4"))
    app.setPalette(palette)
    app.setStyleSheet(DARK_THEME)


# Teal identifies actions and selection; neutral surfaces keep dense data readable.
DARK_THEME = """
QMainWindow, QWidget#workspace { background: #11151d; color: #eef2f7; }
QWidget { color: #eef2f7; font-family: "Segoe UI"; font-size: 13px; }
QFrame#sidebar { background: #151b25; border-right: 1px solid #293342; }
QFrame#emptyCard, QFrame#metricCard {
    background: #1a222e; border: 1px solid #30394a; border-radius: 10px;
}
QLabel#brand { color: #eef2f7; font-size: 19px; font-weight: 600; }
QLabel#pageTitle { font-size: 28px; font-weight: 600; }
QLabel#cardTitle { font-size: 18px; font-weight: 600; }
QLabel#metricValue { font-size: 26px; font-weight: 600; }
QLabel#metricLabel { color: #aeb9c9; font-size: 11px; }
QLabel#secondary { color: #aeb9c9; }
QLabel#sectionLabel { color: #8e9caf; font-size: 11px; font-weight: 600; padding: 12px 8px 4px; }
QToolButton {
    background: transparent; border: 1px solid transparent;
    border-radius: 6px; padding: 8px; text-align: left;
}
QToolButton:hover { background: #242e3b; }
QToolButton:checked { background: #233c3e; color: #a2eddd; border-left: 3px solid #8ce0cf; }
QToolButton:focus { border: 1px solid #8ce0cf; }
QToolButton#sidebarToggle { color: #aeb9c9; }
QMenu, QStatusBar { background: #181e29; }
QMenu::item { padding: 7px 20px; }
QMenu::item:selected { background: #233c3e; }
QToolTip { background: #242e3b; color: #eef2f7; border: 1px solid #617080; padding: 5px; }
QLineEdit, QPlainTextEdit, QPushButton, QTableView, QTreeView, QListWidget,
QProgressBar, QComboBox, QSpinBox {
    background: #1a222e; border: 1px solid #30394a; border-radius: 6px;
}
QLineEdit, QPushButton, QComboBox, QSpinBox { padding: 7px 10px; min-height: 18px; }
QPushButton { background: #232d3b; font-weight: 500; }
QPushButton:enabled:hover { background: #303e50; border-color: #617080; }
QPushButton:enabled:pressed { background: #18222f; }
QPushButton[role="primary"]:enabled {
    background: #8ce0cf;
    color: #102522;
    border-color: #8ce0cf;
    font-weight: 600;

}
QPushButton[role="primary"]:enabled:hover { background: #b0f0e3; border-color: #b0f0e3; }
QPushButton[role="primary"]:enabled:pressed { background: #63bbaa; }
QPushButton[role="danger"]:enabled { color: #ffb4ab; border-color: #85504e; }
QPushButton[role="danger"]:enabled:hover { background: #462c31; }
QPushButton:disabled, QLineEdit:disabled {
    color: #8793a4;
    background: #181e29;
    border-color: #27303e;

}
QLineEdit:focus, QPushButton:focus, QComboBox:focus, QSpinBox:focus,
QTableView:focus, QTreeView:focus, QListWidget:focus { border: 1px solid #8ce0cf; }
QPushButton[role="primary"]:focus { border: 2px solid #ffffff; padding: 6px 9px; }
QCheckBox { spacing: 8px; }
QCheckBox:focus { color: #a2eddd; }
QProgressBar { min-height: 8px; max-height: 16px; text-align: center; }
QProgressBar::chunk { background: #8ce0cf; border-radius: 3px; }
QTableView { gridline-color: #293342; selection-background-color: #29494a; }
QTreeView::item, QListWidget::item { min-height: 28px; padding: 2px 4px; }
QTableView::item:selected, QTreeView::item:selected, QListWidget::item:selected {
    background: #29494a; color: #ffffff;
}
QHeaderView::section {
    background: #202a38;
    color: #aeb9c9;
    padding: 9px 8px;
    border: 0;
    border-bottom: 1px solid #30394a;
    font-weight: 600;

}
QTableCornerButton::section { background: #202a38; border: 0; }
QTabWidget::pane { background: #11151d; border: 1px solid #30394a; border-radius: 6px; }
QTabBar::tab {
    background: transparent;
    color: #aeb9c9;
    border-bottom: 2px solid transparent;
    padding: 10px 14px;

}
QTabBar::tab:selected { border-bottom-color: #8ce0cf; color: #a2eddd; }
QTabBar::tab:hover:!selected { background: #242e3b; color: #eef2f7; }
QSplitter::handle { background: #293342; }
QScrollBar:vertical { background: #151b25; width: 12px; margin: 0; }
QScrollBar:horizontal { background: #151b25; height: 12px; margin: 0; }
QScrollBar::handle { background: #455366; border-radius: 5px; min-height: 24px; min-width: 24px; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: transparent; }
"""
