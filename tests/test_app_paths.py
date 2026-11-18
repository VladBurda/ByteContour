from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from bytecontour.app_paths import app_settings, inventory_path


def test_existing_inventory_stays_with_its_sidecars(tmp_path: Path) -> None:
    fresh = tmp_path / "ByteContour" / "ByteContour"
    legacy = tmp_path / "DiskSense" / "DiskSense"
    assert inventory_path(fresh, tmp_path) == fresh / "inventory.sqlite3"
    legacy.mkdir(parents=True)
    original = legacy / "inventory.sqlite3"
    original.touch()
    assert inventory_path(fresh, tmp_path) == original
    fresh.mkdir(parents=True)
    (fresh / original.name).touch()
    assert inventory_path(fresh, tmp_path) == fresh / original.name


def test_legacy_preferences_import_once_without_overwriting_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profiles = {
        name: QSettings(str(tmp_path / f"{name}.ini"), QSettings.Format.IniFormat)
        for name in ("ByteContour", "DiskSense")
    }
    old = profiles["DiskSense"]
    new = profiles["ByteContour"]
    old.setValue("ui/sidebarCollapsed", True)
    old.setValue("cleanup/largeDays", 200)
    new.setValue("cleanup/largeDays", 300)
    monkeypatch.setattr("bytecontour.app_paths.QSettings", lambda name, _: profiles[name])
    assert app_settings().value("ui/sidebarCollapsed", type=bool) is True
    assert new.value("cleanup/largeDays", type=int) == 300
    new.remove("ui/sidebarCollapsed")
    assert not app_settings().contains("ui/sidebarCollapsed")
