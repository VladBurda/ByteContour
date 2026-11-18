"""Select the app's persistent profile and keep existing inventories accessible."""

from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths

APP_NAME = "ByteContour"
_LEGACY_NAME = "DiskSense"


def inventory_path(current: Path, local_data: Path) -> Path:
    current_db = current / "inventory.sqlite3"
    legacy_db = local_data / _LEGACY_NAME / _LEGACY_NAME / current_db.name
    # Keep the existing database and its companion lock/WAL together.
    if not current_db.exists() and legacy_db.is_file():
        return legacy_db
    return current_db


def default_inventory_path() -> Path:
    locations = QStandardPaths.StandardLocation
    return inventory_path(
        Path(QStandardPaths.writableLocation(locations.AppLocalDataLocation)),
        Path(QStandardPaths.writableLocation(locations.GenericDataLocation)),
    )


def app_settings() -> QSettings:
    settings = QSettings(APP_NAME, APP_NAME)
    if settings.value("profile/legacyImported", False, type=bool) is not True:
        legacy = QSettings(_LEGACY_NAME, _LEGACY_NAME)
        for key in legacy.allKeys():
            if not settings.contains(key):
                settings.setValue(key, legacy.value(key))
        settings.setValue("profile/legacyImported", True)
    return settings
