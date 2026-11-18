from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from sqlalchemy.orm import Session

from bytecontour.database import (
    DuplicateGroup,
    DuplicateMember,
    DuplicateRun,
    ScanRepository,
    StorageSnapshot,
    WatchRoot,
)
from bytecontour.recommendations import (
    DAY,
    MIB,
    Context,
    Recommendation,
    RecommendationService,
    Thresholds,
)
from bytecontour.scanner import Entry, ScanProgress

NOW = 2_000_000_000
CONFIG = Thresholds(
    old_days=10,
    large_mib=2,
    growth_mib=3,
    growth_days=7,
    duplicate_mib=4,
    installer_count=2,
    temp_days=5,
    temp_count=2,
)


def _entry(path: str, size: int, modified: int) -> Entry:
    return Entry(
        path,
        path.rpartition("/")[0],
        "file",
        Path(path).suffix,
        size,
        None,
        modified,
        modified,
        path,
        1,
        1,
    )


def test_builtin_rules_and_threshold_boundaries(tmp_path: Path) -> None:
    repo = ScanRepository(tmp_path / "inventory.sqlite3")
    repo.initialize()
    try:
        root = str(tmp_path)
        scan, _ = repo.begin(root, root, False)
        old = (NOW - 10 * DAY) * 10**9
        temp = (NOW - 5 * DAY) * 10**9
        repo.append(
            scan,
            [
                _entry("large.dat", 2 * MIB, old),
                _entry("too-small.dat", 2 * MIB - 1, old),
                _entry("too-recent.dat", 2 * MIB, old + 1),
                _entry("Downloads/a.msi", 2, old),
                _entry("Downloads/ProductSetup.exe", 3, old),
                _entry("DownloadsArchive/a.msi", 1, old),
                _entry("DownloadsArchive/b.msi", 1, old),
                _entry("cache/a.tmp", 4, temp),
                _entry("cache/b.temp", 5, temp),
                _entry("cache/current.tmp", 5, temp + 1),
            ],
        )
        repo.finish(scan, "completed", ScanProgress(10, 4, 6 * MIB, None, 0, 0, 0, "", None), None)
        with Session(repo.engine) as session, session.begin():
            watch = WatchRoot(
                path=root,
                key=root,
                enabled=True,
                interval=60,
                threshold=0,
                cooldown=1,
                status="Watching",
            )
            run = DuplicateRun(scan_id=scan, complete=True, exclusions="[]", minimum=1)
            session.add_all([watch, run])
            session.flush()
            session.add_all(
                [
                    StorageSnapshot(root_id=watch.id, observed=NOW - 8 * DAY, files=0, logical=0),
                    StorageSnapshot(root_id=watch.id, observed=NOW - DAY, files=2, logical=2 * MIB),
                    StorageSnapshot(root_id=watch.id, observed=NOW, files=3, logical=5 * MIB),
                    StorageSnapshot(
                        root_id=watch.id, observed=NOW, files=3, logical=99 * MIB, error="partial"
                    ),
                    DuplicateMember(
                        run_id=run.id, path="keeper", size=4 * MIB, identity="1:1", links=1
                    ),
                    DuplicateGroup(
                        run_id=run.id,
                        size=4 * MIB,
                        digest="example",
                        paths=2,
                        copies=2,
                        keeper="keeper",
                        logical=8 * MIB,
                        allocated=0,
                        unknown=2,
                    ),
                ]
            )
        service = RecommendationService(repo)
        page = service.read(CONFIG, now=NOW)
        rules = {item.rule: item for item in page.rows}
        assert len(page.rows) == len(rules) == 5
        assert rules["old-large"].scope.endswith("/large.dat")
        assert rules["old-large"].risk == "High" and "10 days" in rules["old-large"].reason
        assert rules["installers"].scope.endswith("/Downloads")
        assert "2 temporary files" in rules["temporary"].reason
        assert "3.0 MiB" in rules["growth"].reason
        assert rules["duplicates"].logical_potential == 4 * MIB
        assert rules["duplicates"].allocated_potential is None
        assert all(
            item.logical_potential is None for name, item in rules.items() if name != "duplicates"
        )
        strict = replace(
            CONFIG, large_mib=3, growth_mib=4, duplicate_mib=5, installer_count=3, temp_count=3
        )
        assert not service.read(strict, now=NOW).rows
    finally:
        repo.close()


def test_custom_rule_and_dismissal_survive_reopen(tmp_path: Path) -> None:
    class CustomRule:
        def evaluate(self, context: Context, thresholds: Thresholds) -> Iterable[Recommendation]:
            yield Recommendation(
                "custom",
                "folder",
                "Review storage",
                "Test evidence",
                "Low",
                "Review the evidence.",
                (thresholds.large_mib,),
            )

    database = tmp_path / "inventory.sqlite3"
    repo = ScanRepository(database)
    repo.initialize()
    service = RecommendationService(repo, (CustomRule(),))
    item = service.read(CONFIG).rows[0]
    service.set_dismissed(item, True)
    service.set_dismissed(item, True)
    repo.close()
    reopened = ScanRepository(database)
    reopened.initialize()
    try:
        service = RecommendationService(reopened, (CustomRule(),))
        assert service.read(CONFIG).dismissed == {item.identity}
        assert not service.read(replace(CONFIG, large_mib=3)).dismissed
        service.set_dismissed(item, False)
        assert not service.read(CONFIG).dismissed
    finally:
        reopened.close()
