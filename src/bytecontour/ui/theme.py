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


DARK_THEME = """
QMainWindow, QWidget#workspace { background: #11151d; color: #eef2f7; }
QWidget { color: #eef2f7; }
QFrame#sidebar { background: #181e29; border-right: 1px solid #30394a; }
QFrame#emptyCard { background: #1a222e; border: 1px solid #30394a; border-radius: 12px; }
QLabel#brand { color: #8ce0cf; font-size: 19px; font-weight: bold; }
QLabel#pageTitle { font-size: 32px; font-weight: bold; }
QLabel#cardTitle { font-size: 18px; font-weight: bold; }
QLabel#secondary { color: #aeb9c9; }
QToolButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 8px;
}
QToolButton:hover { background: #242e3b; }
QToolButton:checked { background: #233c3e; color: #a2eddd; }
QToolButton:focus { border-color: #8ce0cf; }
QMenuBar, QMenu, QStatusBar { background: #181e29; }
QMenuBar::item:selected, QMenu::item:selected { background: #233c3e; }
QToolTip { background: #242e3b; color: #eef2f7; border: 1px solid #617080; }
QLineEdit, QPlainTextEdit, QPushButton, QTableView, QTreeView, QListWidget,
QProgressBar, QComboBox, QSpinBox {
    background: #1a222e;
    border: 1px solid #30394a;
    border-radius: 6px;
}
QLineEdit, QPushButton, QComboBox, QSpinBox { padding: 6px 8px; }
QLineEdit:focus, QPushButton:focus, QComboBox:focus, QSpinBox:focus {
    border: 1px solid #8ce0cf;
}
QPushButton:enabled:hover { background: #263d43; }
QPushButton:disabled, QLineEdit:disabled { color: #8793a4; }
QProgressBar { min-height: 8px; }
QProgressBar::chunk { background: #8ce0cf; }
QTableView { gridline-color: #30394a; }
QTreeView::item, QListWidget::item { min-height: 23px; }
QHeaderView::section { background: #232d3b; padding: 6px; border: 0; }
QTabWidget::pane { background: #11151d; border: 1px solid #30394a; }
QTabBar::tab {
    background: #181e29;
    border: 1px solid #30394a;
    border-bottom: 2px solid #30394a;
    padding: 7px 11px;
}
QTabBar::tab:selected { background: #233c3e; border-bottom-color: #8ce0cf; color: #a2eddd; }
QTabBar::tab:hover:!selected { background: #242e3b; }
QSplitter::handle { background: #30394a; }
"""
