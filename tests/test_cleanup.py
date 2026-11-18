import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from disksense.cleanup import CleanupService, Rules
from disksense.database import ScanRepository
from disksense.duplicate_store import DuplicateRequest, DuplicateStore
from disksense.duplicates import DuplicateService
from disksense.recycle import RecycleError
from disksense.scan_service import ScanService
from disksense.scanner import ScanControl

IMMEDIATE = Rules(temporary_days=0, large_days=0, large_mib=1)


@pytest.fixture
def isolated(tmp_path: Path) -> Iterator[tuple[Path, ScanRepository]]:
    with TemporaryDirectory(prefix="disksense-cleanup-test-", dir=Path.cwd()) as folder:
        repo = ScanRepository(tmp_path / "inventory.sqlite3")
        repo.initialize()
        try:
            yield Path(folder), repo
        finally:
            repo.close()


def _scan(root: Path, repo: ScanRepository) -> int:
    return ScanService(repo).scan(root, False, ScanControl(), lambda _: None).id


def test_selection_and_confirmed_plan_revalidate_changed_files(
    isolated: tuple[Path, ScanRepository],
) -> None:
    root, repo = isolated
    source, destination = root / "files", root / "fake-bin"
    source.mkdir()
    destination.mkdir()
    (source / "a.tmp").write_bytes(b"temporary")
    (source / "b.tmp").write_bytes(b"other tmp")
    (source / "large.txt").write_bytes(b"x" * 1024**2)
    (source / "empty").mkdir()
    (source / "nonempty").mkdir()
    (source / "nonempty" / "small.txt").touch()
    (source / ".ssh").mkdir()
    (source / ".ssh" / "private.tmp").touch()

    def recycle(path: Path, revalidate: Callable[[], None]) -> None:
        revalidate()
        path.rename(destination / path.name)

    service = CleanupService(repo, recycle)
    scan = _scan(source, repo)
    assert {row.path: row.reason for row in service.discover(scan, IMMEDIATE).rows} == {
        ".ssh/private.tmp": "Old temporary file",
        "a.tmp": "Old temporary file",
        "b.tmp": "Old temporary file",
        "empty": "Empty folder",
        "large.txt": "Old large file",
    }
    with pytest.raises(ValueError, match="Candidates changed"):
        service.preview(scan, IMMEDIATE, ("../outside.tmp",))
    with pytest.raises(ValueError, match="Candidates changed"):
        service.preview(scan, IMMEDIATE, ("nonempty",))
    (source / "empty" / "new.txt").touch()
    plan = service.preview(scan, IMMEDIATE, ("a.tmp", "b.tmp", "empty", ".ssh/private.tmp"))
    assert {row.candidate.path for row in plan.eligible} == {"a.tmp", "b.tmp"}
    assert any("contains entries" in item.detail for item in plan.items)
    assert not list(destination.iterdir()) and not service.audit().rows
    (source / "a.tmp").write_bytes(b"changed contents")
    result = service.execute(plan.token, ScanControl())
    assert {row.path: row.state for row in result.rows} == {"a.tmp": "failed", "b.tmp": "recycled"}
    assert (source / "a.tmp").read_bytes() == b"changed contents"
    assert (destination / "b.tmp").read_bytes() == b"other tmp"
    with pytest.raises(ValueError, match="no longer valid"):
        service.execute(plan.token, ScanControl())


def test_keeper_cannot_be_recycled_even_by_temporary_rule(
    isolated: tuple[Path, ScanRepository],
) -> None:
    root, repo = isolated
    for name in ("a.tmp", "b.tmp"):
        (root / name).write_bytes(b"same")
    scan = _scan(root, repo)
    DuplicateService(DuplicateStore(repo)).detect(
        DuplicateRequest(scan), ScanControl(), lambda _: None
    )

    def must_not_recycle(_: Path, __: Callable[[], None]) -> None:
        pytest.fail("Missing keeper must stop the recycle operation.")

    service = CleanupService(repo, must_not_recycle)
    plan = service.preview(scan, IMMEDIATE, ("a.tmp", "b.tmp"))
    assert [row.candidate.path for row in plan.eligible] == ["b.tmp"]
    assert "keeper" in plan.items[0].detail
    (root / "a.tmp").unlink()
    outcome = service.execute(plan.token, ScanControl())
    assert outcome.rows[0].state == "failed"
    assert (root / "b.tmp").read_bytes() == b"same"


def test_locked_unsupported_and_interrupted_outcomes(
    isolated: tuple[Path, ScanRepository],
) -> None:
    root, repo = isolated
    for name in ("a.tmp", "b.tmp"):
        (root / name).write_bytes(b"temp")
    scan = _scan(root, repo)

    def locked(_: Path, revalidate: Callable[[], None]) -> None:
        revalidate()
        raise PermissionError("Locked file")

    locked_service = CleanupService(repo, locked)
    plan = locked_service.preview(scan, IMMEDIATE, ("a.tmp",))
    outcome = locked_service.execute(plan.token, ScanControl())
    assert outcome.rows[0].state == "failed" and "Locked file" in outcome.rows[0].detail

    def unsupported(_: Path, __: Callable[[], None]) -> None:
        raise RecycleError("Recycling unsupported on this volume.")

    service = CleanupService(repo, unsupported)
    plan = service.preview(scan, IMMEDIATE, ("a.tmp",))
    outcome = service.execute(plan.token, ScanControl())
    assert outcome.rows[0].state == "failed" and (root / "a.tmp").exists()

    def crash(_: Path, __: Callable[[], None]) -> None:
        raise RuntimeError("simulated interrupted operation")

    service = CleanupService(repo, crash)
    plan = service.preview(scan, IMMEDIATE, ("a.tmp", "b.tmp"))
    with pytest.raises(RuntimeError, match="interrupted"):
        service.execute(plan.token, ScanControl())
    repo.initialize()
    audit = service.audit(operation=plan.token)
    assert {row.path: row.state for row in audit.rows} == {
        "a.tmp": "indeterminate",
        "b.tmp": "skipped",
    }
    assert (root / "a.tmp").exists() and (root / "b.tmp").exists()


def test_redirected_parent_blocks_recycling(isolated: tuple[Path, ScanRepository]) -> None:
    root, repo = isolated
    source, outside = root / "scope", root / "outside"
    nested = source / "nested"
    nested.mkdir(parents=True)
    outside.mkdir()
    (nested / "file.tmp").touch()
    (outside / "file.tmp").write_bytes(b"keep")
    scan = _scan(source, repo)
    nested.rename(source / "moved")
    try:
        if sys.platform == "win32":
            from _winapi import CreateJunction

            CreateJunction(str(outside), str(nested))
        else:
            os.symlink(outside, nested, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"Directory link unavailable: {error}")
    try:
        service = CleanupService(repo, lambda *_: pytest.fail("Unsafe path reached the recycler."))
        plan = service.preview(scan, IMMEDIATE, ("nested/file.tmp",))
        assert not plan.eligible
        assert (outside / "file.tmp").read_bytes() == b"keep"
    finally:
        if sys.platform == "win32":
            nested.rmdir()
        else:
            nested.unlink()


def test_cancelled_plan_audits_all_confirmed_files(
    isolated: tuple[Path, ScanRepository],
) -> None:
    root, repo = isolated
    files = ("a.tmp", "b.tmp")
    for path in files:
        (root / path).touch()
    service = CleanupService(repo, lambda *_: pytest.fail("Cancelled plan reached the recycler."))
    plan = service.preview(_scan(root, repo), IMMEDIATE, files)
    assert len(plan.eligible) == len(files)
    control = ScanControl()
    control.cancel()
    result = service.execute(plan.token, control)
    assert {row.path: row.state for row in result.rows} == {path: "skipped" for path in files}
    assert all((root / path).exists() for path in files)
