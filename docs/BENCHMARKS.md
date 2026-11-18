# Scanner benchmarks

The harness creates its own temporary tree of empty files, with up to 1,000 files per directory. It runs a full scan through the production Qt controller and Scan View. A 10 ms Qt timer measures main-thread responsiveness. The harness deletes its fixtures when it exits.

```powershell
uv run --locked python benchmarks/scan.py --files 100000
uv run --locked python benchmarks/scan.py --files 1000000
```

Run from a workspace with space for the temporary filesystem tree and SQLite inventory. Use `--workspace` to select an existing directory on another volume. The harness uses the offscreen Qt backend by default.

## Windows results

Windows 11 build 26300, Python 3.13.18, Qt 6.12.0, SQLAlchemy 2.0.58, and a local SK hynix BC501 NVMe SSD. Each row is one run through the Windows development sandbox. Fixture creation and cleanup are outside the timed interval.

| Files | Scan time | Files/s | SQLite insertion time | Rows/s during insertion | Peak process RSS | Longest Qt timer gap | Final database |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 100,000 | 15.121 s | 6,613 | 4.902 s | 20,422 | 101.0 MiB | 38.71 ms | 24.2 MiB |
| 1,000,000 | 161.287 s | 6,200 | 55.082 s | 18,173 | 101.4 MiB | 53.20 ms | 243.2 MiB |

The 100,000-file run met the 250 ms timer-gap target. Peak RSS before scanning was 95.2 MiB for the 100,000-file run and 95.1 MiB for the million-file run. Both runs capped write batches at 512 rows. Insert timing includes mapping and batch transaction commits. Rows include both files and directories. RSS is the operating system's process high-water mark and includes Qt and Python overhead. Timer gaps measure event-loop scheduling under the offscreen backend. Zero-byte fixtures exercise directory traversal and metadata persistence without file-content reads.

## Explorer benchmark

```powershell
uv run --locked python benchmarks/browse.py --files 1000000
```

The explorer harness creates a disposable SQLite inventory with one directory and one million synthetic file rows. It times loading that inventory and its overview, paging forward, and applying a literal substring search through the Qt controls. It excludes fixture creation and runs the dark theme on the offscreen Qt backend.

| Metric | Windows 11, one million rows |
| --- | ---: |
| Overview and first page | 2,056.84 ms |
| Next page | 18.77 ms |
| Substring search | 427.56 ms |
| Longest 10 ms Qt timer gap | 20.87 ms |
| Rows delivered | 200, 200, 1 |

This run used the same Windows 11 environment as the scanner benchmark. The aggregate query increased the first-load time while the main thread continued processing timer events. The longest observed gap met the 250 ms target.

## Duplicate detection benchmark

```powershell
uv run --locked python benchmarks/duplicates.py
uv run --locked python benchmarks/duplicates.py --pairs 2 --decoys 1 --mib-per-file 32
```

The harness creates same-size file pairs with identical bytes and unique same-size decoys. It inventories the fixture before timing. The cold run starts with an empty DiskSense hash cache. The warm run uses the same inventory and cache with live metadata validation. The operating system's filesystem cache stays enabled.

| Windows 11 fixture | Groups | Cold time | Cold file bytes read | Warm time | Warm file bytes read |
| --- | ---: | ---: | ---: | ---: | ---: |
| 144 files, 1 MiB each | 64 | 2.120 s | 162,529,280 | 0.786 s | 0 |
| 5 files, 32 MiB each | 2 | 0.441 s | 135,200,768 | 0.156 s | 0 |

Cold reads include both sampling and full hashing. The larger-file run used repeated 1 MiB reads. The warm runs recorded 272 and 9 cache hits respectively, counting sample and full stages separately. Both fixtures ran on the local NVMe drive with Python 3.13.18.
