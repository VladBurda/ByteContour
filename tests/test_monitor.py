from collections.abc import Callable, Iterator
from pathlib import Path
from time import monotonic, sleep, time

import pytest

from disksense.database import ScanRepository
from disksense.monitor import Change, EventBuffer, MonitorData, MonitorService, _ObserverQueue
from disksense.scanner import Inventory, ScanControl


@pytest.fixture
def service(tmp_path: Path) -> Iterator[MonitorService]:
    database = tmp_path / "inventory.sqlite3"
    repo = ScanRepository(database)
    repo.initialize()
    monitor = MonitorService(repo, database)
    try:
        yield monitor
    finally:
        monitor.close()
        repo.close()


def _wait_events(
    service: MonitorService, root_id: int, matches: Callable[[MonitorData], bool]
) -> MonitorData:
    deadline = monotonic() + 5
    while True:
        data = service.poll(root_id, ScanControl())
        if matches(data):
            return data
        if monotonic() >= deadline:
            pytest.fail(f"Timed out waiting for filesystem events: {data.events}")
        sleep(0.05)


def test_observer_reports_real_changes_and_stops(
    tmp_path: Path,
    service: MonitorService,
) -> None:
    root = tmp_path / "watched"
    root.mkdir()
    identifier = service.configure(None, str(root), True, 60, 0, 60)
    first = service.poll(identifier, ScanControl())
    assert first.roots[0].status == "Watching"
    original = root / "source.txt"
    original.touch()
    _wait_events(
        service,
        identifier,
        lambda d: any(e.kind == "created" and e.source == "source.txt" for e in d.events),
    )
    original.write_bytes(b"changed")
    _wait_events(
        service,
        identifier,
        lambda d: any(e.kind == "modified" and e.source == "source.txt" for e in d.events),
    )
    renamed = original.rename(root / "renamed.txt")
    _wait_events(
        service,
        identifier,
        lambda d: any(
            e.kind == "moved" and e.source == "source.txt" and e.destination == "renamed.txt"
            for e in d.events
        ),
    )
    renamed.unlink()
    _wait_events(
        service,
        identifier,
        lambda d: any(e.kind == "deleted" and e.source == "renamed.txt" for e in d.events),
    )
    observer = service._observer
    assert observer is not None
    service.configure(identifier, "", False, 60, 0, 60)
    assert not observer.is_alive()
    assert service.read(identifier).roots[0].status == "Paused"


def test_overflow_and_reconciliation_match_inventory(tmp_path: Path) -> None:
    database = tmp_path / "inventory.sqlite3"
    root = tmp_path / "watched"
    root.mkdir()
    repo = ScanRepository(database)
    repo.initialize()
    monitor = MonitorService(repo, database, capacity=1)
    try:
        identifier = monitor.configure(None, str(root), True, 60, 0, 60)
        monitor.poll(identifier, ScanControl())
        (root / "sample.dat").write_bytes(b"hello")
        for kind in ("created", "modified", "modified"):
            monitor.buffer.push(Change(identifier, time(), kind, "sample.dat"))
        stale = monitor.poll(identifier, ScanControl())
        assert "overflow" in stale.roots[0].status
        assert stale.snapshots[0].logical == 0
        result = monitor.poll(identifier, ScanControl(), force=identifier)
        inventory = Inventory(root)
        inventory.run(ScanControl(), lambda _: None, lambda _: None)
        assert result.roots[0].status == "Watching"
        assert result.snapshots[0].logical == inventory.logical
        assert result.snapshots[0].files == inventory.files
    finally:
        monitor.close()
        repo.close()


def test_alert_cooldown_and_periodic_reconciliation(
    tmp_path: Path, service: MonitorService, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "watched"
    root.mkdir()
    clock = [1000.0]
    monkeypatch.setattr("disksense.monitor.time", lambda: clock[0])
    monkeypatch.setattr("disksense.monitor.monotonic", lambda: clock[0])
    monkeypatch.setattr(service, "_attach", lambda _: None)  # simulate a quiet or lossy backend
    identifier = service.configure(None, str(root), True, 5, 4, 30)
    assert service.poll(identifier, ScanControl()).alert is None
    file = root / "sample.dat"
    file.write_bytes(b"12345")
    clock[0] += 5
    result = service.poll(identifier, ScanControl())
    assert result.alert == (str(root), 5)
    file.write_bytes(b"1234567890")
    clock[0] += 5
    assert service.poll(identifier, ScanControl()).alert is None
    restarted = MonitorService(
        ScanRepository(tmp_path / "inventory.sqlite3"), tmp_path / "inventory.sqlite3"
    )
    try:
        monkeypatch.setattr(restarted, "_attach", lambda _: None)
        assert restarted.poll(identifier, ScanControl()).alert is None
        clock[0] += 30
        data = restarted.poll(identifier, ScanControl())
        assert data.alert == (str(root), 5)
        assert [sample.growth_alert for sample in data.snapshots].count(5) == 2
        clock[0] += 30
        assert restarted.poll(identifier, ScanControl()).alert is None
    finally:
        restarted.close()
        restarted.engine.dispose()


def test_debounces_modifications_and_excludes_database(
    tmp_path: Path, service: MonitorService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service, "_attach", lambda _: None)
    identifier = service.configure(None, str(tmp_path), True, 60, 0, 60)
    first = service.poll(identifier, ScanControl())
    assert first.snapshots[0].files == 0
    for _ in range(3):
        service.buffer.push(Change(identifier, time(), "modified", "sample.txt"))
    assert len(service.poll(identifier, ScanControl()).events) == 1
    removed = service.remove(identifier)
    assert not removed.roots and not service.read(identifier).events


def test_watchdog_dispatch_queue_has_a_nonblocking_overflow_signal() -> None:
    buffer = EventBuffer()
    dispatcher = _ObserverQueue(buffer.backend_overflow)
    for _ in range(dispatcher.maxsize + 1):
        dispatcher.put(object())
    assert dispatcher.qsize() == dispatcher.maxsize
    assert buffer.drain() == ([], set(), True)
    assert buffer.drain() == ([], set(), False)
