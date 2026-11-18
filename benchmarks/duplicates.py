"""Measure cold and warm duplicate detection over disposable local files."""

import argparse
import json
import platform
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from bytecontour.database import ScanRepository
from bytecontour.duplicate_store import DuplicateRequest, DuplicateStore
from bytecontour.duplicates import DuplicateService
from bytecontour.file_hashes import CHUNK_BYTES
from bytecontour.scan_service import ScanService
from bytecontour.scanner import ScanControl


def measure(workspace: Path, pairs: int, decoys: int, mib_per_file: int) -> dict[str, object]:
    root = workspace / "fixture"
    root.mkdir()
    for index in range(pairs + decoys):
        content = index.to_bytes(4) * (CHUNK_BYTES // 4)
        for suffix in ("a", "b") if index < pairs else ("a",):
            with (root / f"{index:04}-{suffix}.dat").open("wb") as stream:
                for _ in range(mib_per_file):
                    stream.write(content)
    repo = ScanRepository(workspace / "inventory.sqlite3")
    try:
        repo.initialize()
        scan = ScanService(repo).scan(root, False, ScanControl(), lambda _: None)
        service = DuplicateService(DuplicateStore(repo))
        output: dict[str, object] = {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "files": 2 * pairs + decoys,
            "size_per_file": CHUNK_BYTES * mib_per_file,
            "pairs": pairs,
        }
        for stage in ("cold", "warm"):
            started = perf_counter()
            result = service.detect(DuplicateRequest(scan.id), ScanControl(), lambda _: None)
            elapsed = perf_counter() - started
            if result.group_count != pairs or result.skipped:
                raise RuntimeError("Benchmark did not find the expected duplicate pairs.")
            output[stage] = {
                "wall_seconds": round(elapsed, 3),
                "bytes_read": result.bytes_read,
                "cache_hits": result.cache_hits,
                "groups": result.group_count,
            }
        return output
    finally:
        repo.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pairs", type=int, default=64)
    parser.add_argument("--decoys", type=int, default=16)
    parser.add_argument("--mib-per-file", type=int, default=1)
    args = parser.parse_args()
    if args.pairs < 1 or args.decoys < 0 or args.pairs + args.decoys >= 2**32:
        parser.error("Choose at least one pair and a non-negative decoy count.")
    if args.mib_per_file < 1:
        parser.error("--mib-per-file must be positive.")
    with TemporaryDirectory(prefix="bytecontour-benchmark-", dir=Path.cwd()) as directory:
        result = measure(Path(directory), args.pairs, args.decoys, args.mib_per_file)
        print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
