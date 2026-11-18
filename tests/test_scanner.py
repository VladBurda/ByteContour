import os
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from bytecontour.database import SCHEMA_VERSION, InventoryEntry, ScanRepository, ScanSummary
from bytecontour.scan_service import ScanService
from bytecontour.scanner import BATCH_SIZE, DirectoryEntries, Entry, ScanControl


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[ScanRepository]:
    repo = ScanRepository(tmp_path / "inventory.sqlite3", retain_per_root=2)
    repo.initialize()
    try:
        yield repo
    finally:
        repo.close()


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "root"
    folder.mkdir()
    return folder


def scan(root: Path, repository: ScanRepository, *, compare: bool = False) -> ScanSummary:
    return ScanService(repository).scan(root, compare, ScanControl(), lambda _: None)


def entries(repository: ScanRepository, scan_id: int) -> dict[str, InventoryEntry]:
    with Session(repository.engine) as session:
        rows = session.scalars(select(InventoryEntry).where(InventoryEntry.scan_id == scan_id))
        return {row.path: row for row in rows}


def test_inventory_metadata_and_totals(root: Path, repository: ScanRepository) -> None:
    folder = root / "nested"
    folder.mkdir()
    (root / "empty").mkdir()
    file = folder / "café.TXT"
    file.write_bytes(b"abcdef")
    (root / "empty.dat").touch()

    summary = scan(root, repository)
    assert (summary.state, summary.files, summary.directories) == ("completed", 2, 3)
    assert summary.logical_bytes == 6
    rows = entries(repository, summary.id)
    assert rows[""].logical_bytes == 6
    assert rows["nested"].file_count == 1
    assert rows["empty"].file_count == 0
    assert rows["nested/café.TXT"].extension == ".txt"
    assert rows["nested/café.TXT"].modified_ns == file.stat().st_mtime_ns
    if hasattr(file.stat(), "st_blocks"):
        expected = sum(row.allocated_bytes or 0 for row in rows.values())
        assert summary.allocated_bytes == expected
    else:
        assert summary.allocated_bytes is None


def test_hard_links_share_identity(root: Path, repository: ScanRepository) -> None:
    file = root / "original"
    file.write_bytes(b"abcdef")
    try:
        os.link(file, root / "alias")
    except OSError as error:
        pytest.skip(f"Hard links unavailable: {error}")
    summary = scan(root, repository)
    rows = entries(repository, summary.id)
    assert summary.logical_bytes == 12
    assert rows["original"].identity is not None
    assert rows["original"].identity == rows["alias"].identity
    assert rows["alias"].links == 2


def test_rescan_reports_changes_and_persists_history(
    tmp_path: Path, root: Path, repository: ScanRepository
) -> None:
    (root / "changed").write_bytes(b"x")
    (root / "removed").touch()
    (root / "unchanged").touch()
    first = scan(root, repository, compare=True)
    assert first.baseline_id is None
    (root / "changed").write_bytes(b"longer")
    (root / "removed").unlink()
    (root / "added").touch()
    second = scan(root, repository, compare=True)
    assert second.baseline_id == first.id
    assert (second.added, second.changed, second.removed) == (1, 1, 1)
    assert set(entries(repository, second.id)) == {"", "changed", "unchanged", "added"}
    repository.close()
    reopened = ScanRepository(tmp_path / "inventory.sqlite3")
    try:
        reopened.initialize()
        assert reopened.history() == ([second, first], False)
        assert reopened.history(before=second.id) == ([first], False)
    finally:
        reopened.close()


def test_skips_links_and_cycles(root: Path, repository: ScanRepository) -> None:
    (root / "normal").touch()
    try:
        (root / "cycle").symlink_to(root, target_is_directory=True)
        (root / "broken").symlink_to(root / "missing")
    except OSError as error:
        pytest.skip(f"Symbolic links unavailable: {error}")
    summary = scan(root, repository)
    assert summary.state == "completed"
    assert summary.skipped == 2
    assert set(entries(repository, summary.id)) == {"", "normal"}
    assert scan(root / "cycle", repository).state == "failed"


def test_skips_windows_junction_cycle(root: Path, repository: ScanRepository) -> None:
    (root / "visible").touch()
    cycle = root / "cycle"
    try:
        if sys.platform == "win32":
            from _winapi import CreateJunction

            CreateJunction(str(root), str(cycle))
        else:
            pytest.skip("Windows junctions")
    except OSError as error:
        pytest.skip(f"Junctions unavailable: {error}")
    try:
        summary = scan(root, repository)
        assert summary.state == "completed"
        assert summary.skipped == 1
        assert set(entries(repository, summary.id)) == {"", "visible"}
        assert scan(cycle, repository).state == "failed"
    finally:
        cycle.rmdir()


def test_denied_directory_keeps_partial_inventory(
    root: Path, repository: ScanRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    denied = root / "denied"
    denied.mkdir()
    (denied / "hidden").touch()
    (root / "visible").touch()
    first = scan(root, repository)
    original = os.scandir

    def scandir(path: str | Path) -> DirectoryEntries:
        if Path(path) == denied:
            raise PermissionError("Access denied by test")
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", scandir)
        summary = scan(root, repository, compare=True)
    assert summary.baseline_id == first.id
    assert (summary.state, summary.files, summary.errors) == ("partial", 1, 1)
    assert summary.removed is None
    assert summary.detail is not None and "denied" in summary.detail
    assert entries(repository, summary.id)["denied"].incomplete
    assert entries(repository, summary.id)[""].incomplete


def test_cancel_discards_already_committed_batches(
    root: Path, repository: ScanRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(BATCH_SIZE + 1):
        (root / str(index)).touch()
    control = ScanControl()
    original = repository.append

    def append(scan_id: int, batch: list[Entry]) -> None:
        assert len(batch) == BATCH_SIZE
        original(scan_id, batch)
        assert control.cancel()

    monkeypatch.setattr(repository, "append", append)
    summary = ScanService(repository).scan(root, False, control, lambda _: None)
    assert summary.state == "cancelled"
    assert summary.files == BATCH_SIZE
    assert entries(repository, summary.id) == {}


def test_root_disappears_before_snapshot_publication(
    root: Path, repository: ScanRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(BATCH_SIZE):
        (root / str(index)).touch()
    original_stat = Path.lstat
    original_append = repository.append
    disconnected = False

    def lstat(path: Path) -> os.stat_result:
        if disconnected and path == root:
            raise FileNotFoundError("Test volume disconnected")
        return original_stat(path)

    def append(scan_id: int, batch: list[Entry]) -> None:
        nonlocal disconnected
        original_append(scan_id, batch)
        disconnected = True

    monkeypatch.setattr(repository, "append", append)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", lstat)
        summary = scan(root, repository)
    assert summary.state == "failed"
    assert summary.detail == "Test volume disconnected"
    assert entries(repository, summary.id) == {}


def test_vanished_file_marked_incomplete(
    root: Path, repository: ScanRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    (root / "stays").touch()
    (root / "vanishes").touch()
    original = os.scandir

    class ChangingDirectory:
        def __init__(self, children: DirectoryEntries) -> None:
            self.children = children

        def __next__(self) -> os.DirEntry[str]:
            child = next(self.children)
            if child.name == "vanishes":
                Path(child.path).unlink()
            return child

        def close(self) -> None:
            self.children.close()

    def scandir(path: str | Path) -> DirectoryEntries:
        children = original(path)
        return ChangingDirectory(children) if Path(path) == root else children

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", scandir)
        summary = scan(root, repository)
    assert (summary.state, summary.files, summary.errors) == ("partial", 1, 1)
    rows = entries(repository, summary.id)
    assert set(rows) == {"", "stays"}
    assert rows[""].incomplete


def test_long_unicode_path(root: Path, repository: ScanRepository) -> None:
    parts = ["目录" * 32, "résumé" * 12, "例" * 68, "данные.txt"]
    file = root.joinpath(*parts)
    assert len(str(file)) > 260
    native_root = Path("\\\\?\\" + str(root)) if os.name == "nt" else root
    native_file = native_root.joinpath(*parts)
    try:
        native_file.parent.mkdir(parents=True)
        native_file.write_bytes(b"long path")
        summary = scan(root, repository)
        assert summary.state == "completed", summary.detail
        assert entries(repository, summary.id)["/".join(parts)].logical_bytes == 9
        direct = scan(file.parent, repository)
        assert direct.state == "completed", direct.detail
        assert direct.files == 1
    finally:
        native_file.unlink(missing_ok=True)
        for parent in native_file.parents:
            if parent == native_root:
                break
            if parent.exists():
                parent.rmdir()


def test_recovery_rolls_back_failed_batch_and_prunes_history(
    tmp_path: Path, root: Path, repository: ScanRepository
) -> None:
    (root / "file").touch()
    completed = scan(root, repository)
    running, _ = repository.begin(str(root), os.path.normcase(str(root)), False)
    entry = Entry("a", "", "file", "", 0, None, 0, 0, None, 1, 1)
    with pytest.raises(IntegrityError):
        repository.append(running, [entry, entry])
    assert entries(repository, running) == {}
    repository.append(running, [entry])
    assert [row.id for row in repository.history()[0]] == [completed.id]
    repository.initialize()
    recovered = repository.history()[0][0]
    assert recovered.id == running and recovered.state == "interrupted"
    assert entries(repository, running) == {}
    with sqlite3.connect(tmp_path / "inventory.sqlite3") as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)

    third = scan(root, repository)
    fourth = scan(root, repository)
    assert repository.history()[0] == [fourth, third]
    assert entries(repository, completed.id) == {}
