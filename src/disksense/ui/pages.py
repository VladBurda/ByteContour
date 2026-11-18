"""Navigation metadata and feature empty states."""

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget


@dataclass(frozen=True, slots=True)
class Page:
    title: str
    icon: str
    summary: str
    empty_title: str
    empty_detail: str


PAGES = (
    Page(
        "Dashboard",
        "view-dashboard",
        "Disk usage and storage trends",
        "Scan a folder or drive",
        "Start in Disk Explorer or open Scan History to review saved totals.",
    ),
    Page(
        "Disk Explorer",
        "folder-open",
        "Browse storage by folder and file",
        "Ready to scan",
        "Scan a folder or drive to build a disk usage inventory.",
    ),
    Page(
        "Duplicate Finder",
        "edit-copy",
        "Find matching files",
        "No duplicate groups",
        "Duplicate detection is planned for Phase 4.",
    ),
    Page(
        "Cleanup Center",
        "user-trash",
        "Review files before sending them to the Recycle Bin",
        "No cleanup candidates",
        "Cleanup previews are planned for Phase 5.",
    ),
    Page(
        "Storage Monitor",
        "utilities-system-monitor",
        "Track changes in selected folders",
        "No monitored folders",
        "Folder monitoring is planned for Phase 6.",
    ),
    Page(
        "Recommendations",
        "dialog-information",
        "Review suggestions for recovering storage",
        "No recommendations",
        "Storage recommendations are planned for Phase 7.",
    ),
    Page(
        "Scan History",
        "document-open-recent",
        "Reopen saved scans",
        "No saved scans",
        "Completed scans and interrupted attempts appear here.",
    ),
    Page(
        "Settings",
        "preferences-system",
        "DiskSense preferences",
        "More settings will appear here",
        "Use the View menu to change sidebar visibility.",
    ),
)


def build_page(page: Page) -> QWidget:
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(36, 30, 36, 30)
    layout.setSpacing(10)

    title = QLabel(page.title)
    title.setObjectName("pageTitle")
    layout.addWidget(title)

    subtitle = QLabel(page.summary)
    subtitle.setObjectName("secondary")
    subtitle.setWordWrap(True)
    layout.addWidget(subtitle)
    layout.addSpacing(16)

    card = QFrame()
    card.setObjectName("emptyCard")
    card_layout = QVBoxLayout(card)
    card_layout.setContentsMargins(24, 28, 24, 28)
    heading = QLabel(page.empty_title)
    heading.setObjectName("cardTitle")
    heading.setWordWrap(True)
    card_layout.addWidget(heading)

    detail = QLabel(page.empty_detail)
    detail.setObjectName("secondary")
    detail.setWordWrap(True)
    card_layout.addWidget(detail)
    layout.addWidget(card, alignment=Qt.AlignmentFlag.AlignTop)
    layout.addStretch()
    return widget
