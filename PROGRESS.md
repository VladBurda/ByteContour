# ByteContour progress

## Phase 1 · Foundation

- [x] Inspected the empty workspace.
- [x] Defined module contracts, storage schema, architectural decisions and risks.
- [x] Defined acceptance criteria for all eight phases.
- [x] Implemented the Qt shell, dark theme, keyboard navigation and persistent sidebar preference.
- [x] Added the uv project, structured console logging, and CI workflow.
- [x] Verified the locked environment and ran Ruff lint and format checks, strict mypy, and pytest on Windows with Python 3.13.18 and Qt 6.12.0.
- [x] Passed all three tests covering structured logging, all eight sidebar destinations and keyboard shortcuts, and sidebar persistence.
- [x] Smoke-tested startup and a clean event-loop shutdown with the offscreen Qt backend.

## Phase 2 · Scanner and persistence

- [x] Implemented depth-first inventory, directory totals, streamed write batches, and file metadata.
- [x] Implemented SQLite schema version 1, recovery, retained history, and incremental metadata comparisons.
- [x] Added a background Qt worker, scan controls, live progress, and a paged Model/View history.
- [x] Verified Windows hard links, junction cycles, and long Unicode paths using temporary fixtures.
- [x] Measured 100,000 and 1,000,000 files on Windows 11. The longest Qt timer gaps were 38.71 ms and 53.20 ms, respectively. See [scanner benchmarks](docs/BENCHMARKS.md).
- [x] Passed locked dependency sync, Ruff lint and formatting, strict mypy with Windows and Linux platform settings, and the offscreen startup and shutdown smoke test.
- [x] Passed 16 tests in the sandbox. Verified two of the three sandbox-skipped cases, hard links and junctions, separately in native Windows.

## Phase 3 · Explorer and analytics

- [x] Added a paged Qt inventory table and folder tree backed by saved scan snapshots.
- [x] Added path search, extension, size and modification filters, sorting, and file-manager navigation.
- [x] Added live volume capacity, a treemap, file type bars, largest files and folders, and chart drill-down.
- [x] Added tests for scoped aggregates, filters, pagination ties, history navigation, and chart interaction.
- [x] Passed Ruff, strict mypy with Windows and Linux platform settings, and 19 local tests. Three Windows sandbox cases skipped link fixture creation.
- [x] Reviewed Dashboard and Inventory renders offscreen. The million-row benchmark delivered 200 entries per page and recorded a 20.87 ms longest Qt timer gap.

## Phase 4 · Duplicate detection

- [x] Added size grouping, sampled xxHash, streamed SHA-256, and version-checked hash caching.
- [x] Added SQLite schema version 2, cancellation recovery, and bounded group and member pages.
- [x] Added saved-inventory selection, configurable exclusions, file previews, keeper selection, live byte comparison, and hard-link-aware estimates.
- [x] Verified hashing, cache invalidation, database migration, and the Qt review workflow. Real hard-link grouping and estimates passed in a separate Windows run.
- [x] Passed locked dependency sync, Ruff, strict mypy with Windows and Linux platform settings, and 26 sandbox tests. Four link-fixture tests were skipped in the sandbox.
- [x] Benchmarked cold and warm detection with 1 MiB and 32 MiB files. See [benchmark results](docs/BENCHMARKS.md).

## Phase 5 · Safe cleanup

- [x] Added configurable temporary-file, old large-file, empty-folder, and duplicate candidate rules.
- [x] Added a confirmed, single-use preview with scope, eligibility, estimated savings, and keeper details.
- [x] Added protected-path and live metadata checks, keeper byte verification, and native Windows recycling for eligible files.
- [x] Added schema version 3 and a per-item audit with cancellation and interrupted-run recovery.
- [x] Verified the confirmation and audit flow offscreen. A separate Windows test verified a locked file stayed in place, then recycled and restored the same disposable fixture. The native junction test passed separately.
- [x] Passed Ruff, strict mypy with Windows and Linux platform settings, and 31 tests in the sandbox. Five link-fixture tests were skipped in the sandbox.

## Phase 6 · Monitoring

- [x] Added watchdog-backed folder watches, watch settings, pause and resume, and removal.
- [x] Added bounded event delivery, modification debouncing, stale tracking, and periodic reconciliation.
- [x] Added SQLite schema version 4 for events and snapshots, a growth chart, and in-app alerts with a persistent cooldown.
- [x] Verified real create, modify, move and delete events, quiet-backend reconciliation, overflow recovery, and observer shutdown on Windows.
- [x] Verified the offscreen monitoring UI from adding a folder through a growth alert and pause.

## Phase 7 · Recommendations

- [x] Added five configurable rules covering old large files, growth, duplicates, Downloads installers, and old temporary file clusters.
- [x] Added evidence explanations, risk levels, duplicate savings estimates, editable thresholds, and dismissal and restore controls.
- [x] Added SQLite schema version 5 for evidence-specific dismissals and verified persistence across restart.
- [x] Tested threshold boundaries, a custom rule, incomplete monitoring samples, and the offscreen dismissal flow.

## Phase 8 · ByteContour release preparation

- [x] Renamed the desktop app, Python package, launch command, icons, and executable metadata to ByteContour.
- [x] Preserved access to existing inventory databases and verified one-time import of previous preferences.
- [x] Added installation instructions, screenshots, and Windows x64 package generation with a checksum and CI artifact workflow.
- [x] Profiled duplicate detection and recorded the measured costs in [benchmarks](docs/BENCHMARKS.md).
- [x] Built the wheel and portable Windows x64 archive. The packaged smoke test passed on Windows 11 with a system-only PATH and disposable storage.
- [x] Passed Ruff, strict mypy, and 42 tests. Five existing Windows sandbox link fixtures were skipped.

## Pending

The portable build is unsigned. Testing on a separate clean Windows machine and publication of a GitHub release remain pending.

## Known limitations

The scanner inventories live metadata, so concurrent filesystem changes can affect totals. Windows allocated size is unavailable. The local Windows account lacks permission to create symbolic-link fixtures. The junction-cycle test passed on Windows, and the portable symbolic-link test remains enabled in CI. GUI verification used the offscreen backend. Hosted GitHub Actions results and Windows packaging remain pending.
