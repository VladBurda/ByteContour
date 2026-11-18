import hashlib
import os
import sqlite3
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from disksense.database import DuplicateRun, ScanRepository, ScanSummary
from disksense.duplicate_store import (
    PAGE,
    DuplicateProgress,
    DuplicateReport,
    DuplicateRequest,
    DuplicateStore,
)
from disksense.duplicates import DuplicateService
from disksense.file_hashes import (
    CHUNK_BYTES,
    SAMPLE_BYTES,
    StaleFile,
    candidate_path,
    checked_file,
    full_hash,
    sampled_hash,
)
from disksense.scan_service import ScanService
from disksense.scanner import ScanControl


@pytest.fixture
def setup(tmp_path: Path) -> Iterator[tuple[Path, ScanRepository, DuplicateService]]:
    root = tmp_path / "fixture"
    root.mkdir()
    repo = ScanRepository(tmp_path / "inventory.sqlite3")
    repo.initialize()
    try:
        yield root, repo, DuplicateService(DuplicateStore(repo))
    finally:
        repo.close()


def _scan(root: Path, repo: ScanRepository) -> ScanSummary:
    return ScanService(repo).scan(root, False, ScanControl(), lambda _: None)


def _detect(service: DuplicateService, request: DuplicateRequest | None = None) -> DuplicateReport:
    return service.detect(request or DuplicateRequest(), ScanControl(), lambda _: None)


def test_sampling_collision_full_hash_and_keeper(
    setup: tuple[Path, ScanRepository, DuplicateService],
) -> None:
    root, repo, service = setup
    payload = b"A" * CHUNK_BYTES
    other = bytearray(payload)
    other[2 * SAMPLE_BYTES] = ord("B")
    (root / "nested").mkdir()
    (root / "large.bin").write_bytes(payload)
    (root / "nested" / "copy.bin").write_bytes(payload)
    (root / "collision.bin").write_bytes(other)
    (root / "unique.bin").write_bytes(b"unique size")
    assert sampled_hash(BytesIO(payload), len(payload), ScanControl()) == sampled_hash(
        BytesIO(other), len(other), ScanControl()
    )
    assert full_hash(BytesIO(payload), len(payload), ScanControl()) == (
        hashlib.sha256(payload).hexdigest(),
        len(payload),
    )
    saved = _scan(root, repo)
    result = _detect(service, DuplicateRequest(saved.id))
    assert result.run_id is not None
    assert result.group_count == 1 and result.logical == CHUNK_BYTES
    assert result.bytes_read == 3 * (CHUNK_BYTES + 3 * SAMPLE_BYTES)
    group = result.groups[0]
    members = service.store.members(result.run_id, group.id, None)
    assert [item.version.path for item in members.rows] == ["large.bin", "nested/copy.bin"]
    assert service.verify(result.run_id, group.id, "nested/copy.bin", ScanControl()).verified
    preview = service.preview(result.run_id, group.id, "nested/copy.bin", ScanControl())
    assert preview.text == "A" * 4096
    with pytest.raises(ValueError, match="keeper must"):
        service.store.members(result.run_id, group.id, None, "collision.bin")
    service.store.members(result.run_id, group.id, None, "nested/copy.bin")
    assert service.store.report(saved.id).groups[0].keeper == "nested/copy.bin"


def test_warm_cache_and_same_size_same_mtime_rewrite(
    setup: tuple[Path, ScanRepository, DuplicateService],
) -> None:
    root, repo, service = setup
    first, second = root / "first", root / "second"
    for file in (first, second):
        file.write_bytes(b"match")
    old = _scan(root, repo)
    cold = _detect(service)
    warm = _detect(service)
    assert cold.bytes_read == 20
    assert warm.bytes_read == 0 and warm.cache_hits == 4
    assert warm.run_id is not None
    timing = second.stat()
    second.write_bytes(b"other")
    os.utime(second, ns=(timing.st_atime_ns, timing.st_mtime_ns))
    with pytest.raises(StaleFile):
        service.verify(warm.run_id, warm.groups[0].id, "second", ScanControl())
    new = _scan(root, repo)
    assert _detect(service, DuplicateRequest(new.id)).group_count == 0
    assert service.store.report(old.id).group_count == 1


def test_exclusions_empty_files_stale_entries_and_cancellation(
    setup: tuple[Path, ScanRepository, DuplicateService],
) -> None:
    root, repo, service = setup
    (root / "ignore").mkdir()
    (root / "ignore" / "hidden").write_bytes(b"same")
    (root / "same").write_bytes(b"same")
    (root / "stale").write_bytes(b"same")
    (root / "empty-a").touch()
    (root / "empty-b").touch()
    saved = _scan(root, repo)
    (root / "stale").unlink()
    filtered = _detect(service, DuplicateRequest(saved.id, exclusions=("ignore/*",)))
    assert filtered.group_count == 0 and filtered.skipped == 1
    empty = _detect(service, DuplicateRequest(saved.id, minimum=0, exclusions=("ignore/*",)))
    assert empty.group_count == 1 and empty.groups[0].size == 0
    control = ScanControl()

    def cancel(value: DuplicateProgress) -> None:
        if value.phase == "Hashing full files":
            assert control.cancel()

    interrupted = service.detect(DuplicateRequest(saved.id), control, cancel)
    assert interrupted.cancelled and interrupted.run_id == empty.run_id
    with Session(repo.engine) as session:
        assert list(session.scalars(select(DuplicateRun.id))) == [empty.run_id]


def test_hard_links_are_one_physical_copy(
    setup: tuple[Path, ScanRepository, DuplicateService],
) -> None:
    root, repo, service = setup
    one = root / "original"
    one.write_bytes(b"data")
    try:
        os.link(one, root / "alias")
    except OSError as error:
        pytest.skip(f"Hard links unavailable: {error}")
    _scan(root, repo)
    assert _detect(service).group_count == 0
    (root / "copy").write_bytes(b"data")
    _scan(root, repo)
    report = _detect(service)
    assert report.run_id is not None
    group = report.groups[0]
    assert (group.paths, group.copies, group.logical) == (3, 2, 4)
    assert group.keeper in ("alias", "original")
    page = service.store.members(report.run_id, group.id, None, "copy")
    assert page.group.logical == 0 and page.group.allocated == 0


def test_file_changed_during_read_and_unsafe_path_rejected(
    setup: tuple[Path, ScanRepository, DuplicateService],
) -> None:
    root, repo, service = setup
    for name in ("a", "b"):
        (root / name).write_bytes(b"copy")
    _scan(root, repo)
    report = _detect(service)
    assert report.run_id is not None
    group = report.groups[0]
    _, member, _ = service.store.review(report.run_id, group.id, "b")
    with pytest.raises(StaleFile, match="changed during"):
        with checked_file(root, member.version, member.stamp) as (stream, _):
            assert stream.read() == b"copy"
            (root / "b").write_bytes(b"changed length")
    with pytest.raises(StaleFile):
        candidate_path(root, "inside/../../outside")


def test_v1_migration_preserves_published_inventory(tmp_path: Path) -> None:
    root = tmp_path / "fixture"
    root.mkdir()
    database = tmp_path / "inventory.sqlite3"
    repo = ScanRepository(database)
    try:
        repo.initialize()
        saved = _scan(root, repo)
        repo.close()
        with sqlite3.connect(database) as raw:
            for table in ("duplicate_members", "duplicate_groups", "duplicate_runs", "hash_cache"):
                raw.execute(f"DROP TABLE {table}")
            raw.execute("PRAGMA user_version = 1")
        repo.initialize()
        service = DuplicateService(DuplicateStore(repo))
        assert repo.history()[0] == [saved]
        assert _detect(service).scan_id == saved.id
    finally:
        repo.close()


def test_duplicate_groups_and_members_are_paged(
    setup: tuple[Path, ScanRepository, DuplicateService],
) -> None:
    root, repo, service = setup
    for index in range(PAGE):
        for suffix in ("a", "b"):
            (root / f"{index:03}-{suffix}").write_bytes(index.to_bytes(2))
    for index in range(PAGE + 1):
        (root / f"wide-{index:03}").write_bytes(b"wide")
    saved = _scan(root, repo)
    first = _detect(service)
    assert first.run_id is not None
    assert first.group_count == PAGE + 1
    assert len(first.groups) == PAGE and first.next_page is not None
    tail = service.store.report(saved.id, first.next_page)
    assert len(tail.groups) == 1 and tail.next_page is None
    assert tail.groups[0].id not in {group.id for group in first.groups}
    wide = first.groups[0]
    assert wide.paths == PAGE + 1
    members = service.store.members(first.run_id, wide.id, None)
    assert len(members.rows) == PAGE and members.next_page is not None
    last = service.store.members(first.run_id, wide.id, members.next_page)
    assert [row.version.path for row in last.rows] == [f"wide-{PAGE:03}"]
    assert last.next_page is None
