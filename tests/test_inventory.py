from dataclasses import replace
from pathlib import Path

from disksense.database import ScanRepository
from disksense.inventory import PAGE_SIZE, InventoryQuery, InventoryReader
from disksense.scan_service import ScanService
from disksense.scanner import ScanControl


def test_inventory_scope_summary_and_filters(tmp_path: Path) -> None:
    root = tmp_path / "inventory"
    (root / "docs").mkdir(parents=True)
    (root / "docs2").mkdir()
    (root / "docs/a%real.txt").write_bytes(b"abc")
    (root / "docs/b.txt").write_bytes(b"12345")
    (root / "docs2/other.txt").write_bytes(b"1234567")
    (root / "plain").write_bytes(b"x")
    repo = ScanRepository(tmp_path / "db.sqlite3")
    try:
        repo.initialize()
        saved = ScanService(repo).scan(root, False, ScanControl(), lambda _: None)
        reader = InventoryReader(repo)
        overview = reader.read(InventoryQuery())
        assert overview.scan == saved
        assert overview.overview is not None
        assert overview.overview.folder.size == 16
        assert overview.overview.folder.file_count == 4
        assert overview.overview.capacity is not None
        assert [item.path for item in overview.overview.largest_folders] == ["docs", "docs2"]
        assert [(item.extension, item.size, item.files) for item in overview.overview.types] == [
            (".txt", 15, 3),
            ("", 1, 1),
        ]
        query = InventoryQuery(folder="docs", extension=".txt", minimum=3, maximum=4)
        result = reader.read(query)
        assert [item.path for item in result.entries] == ["docs/a%real.txt"]
        assert result.overview is not None
        assert [(item.extension, item.size) for item in result.overview.types] == [(".txt", 8)]
        modified = result.entries[0].modified
        assert not reader.read(replace(query, modified_from=modified + 1)).entries
        assert not reader.read(replace(query, modified_before=modified)).entries
        assert len(reader.read(replace(query, modified_from=modified)).entries) == 1
        literal = reader.read(InventoryQuery(search="%", overview=False))
        assert [item.path for item in literal.entries] == ["docs/a%real.txt"]
        focused = reader.read(InventoryQuery(exact_path="plain", overview=False))
        assert [item.path for item in focused.entries] == ["plain"]
        assert reader.read(InventoryQuery(scan_id=saved.id + 1)).scan is None
    finally:
        repo.close()


def test_keyset_pages_keep_size_ties(tmp_path: Path) -> None:
    root = tmp_path / "inventory"
    root.mkdir()
    for index in range(PAGE_SIZE + 3):
        (root / f"sample_{index:03}.bin").write_bytes(b"same")
    repo = ScanRepository(tmp_path / "db.sqlite3")
    try:
        repo.initialize()
        saved = ScanService(repo).scan(root, False, ScanControl(), lambda _: None)
        reader = InventoryReader(repo)
        initial = InventoryQuery(
            scan_id=saved.id, files_only=True, sort="size", descending=True, overview=False
        )
        first = reader.read(initial)
        assert len(first.entries) == PAGE_SIZE and first.next_page is not None
        second = reader.read(replace(initial, after=first.next_page))
        expected = [f"sample_{i:03}.bin" for i in reversed(range(PAGE_SIZE + 3))]
        assert [item.path for item in (*first.entries, *second.entries)] == expected
        assert second.next_page is None
    finally:
        repo.close()
