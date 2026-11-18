# DiskSense

DiskSense inventories folders and drives, compares rescans, visualizes saved storage usage, finds duplicate files, and previews cleanup. Scan metadata, hash digests and cleanup audit records stay in a local SQLite database.

## Run locally

Install uv, then run the following in the project directory. uv uses Python 3.13 by default for development. The project supports Python 3.12 and later where PySide6 wheels are available.

```powershell
uv sync --locked
uv run python -m disksense
```

Use **Alt+1** through **Alt+8** to switch sections and **Ctrl+B** to collapse or expand the sidebar. DiskSense saves the sidebar preference with Qt's native settings.

## Scan a folder or drive

1. Open **Disk Explorer**. Use **Browse** or enter an absolute path such as `C:\`.
2. Start the scan. You can pause, resume, or cancel while traversal is running. Progress shows the number of files found and an average rate. The activity bar is indeterminate because the total number of entries is unknown.
3. Open **Scan History** to inspect saved totals and scan outcomes. Leave **Compare with the last complete scan** checked on a rescan to see file additions, metadata changes, and removals.

Scans record live metadata as files are visited. A partial result identifies an observed access or filesystem error and reports only the data collected. Logical size counts each path, including hard links. Allocated size is reported when the OS exposes block counts and can count shared allocation more than once. Windows currently shows allocated size as unavailable.

On Windows the inventory database lives in DiskSense's per-user local app data directory. DiskSense keeps the five latest finished attempts for each scanned root and preserves the latest complete inventory for comparisons.

## Explore storage

- Open **Dashboard** for the latest inventory, current volume capacity, a treemap, file type bars, and the largest files and folders. Select a chart item to browse it in Disk Explorer.
- In **Disk Explorer → Inventory**, select a folder in the tree or open a folder from the entries table. Apply path, extension, size, or modification date filters. A search or file filter includes descendants of the selected folder. Usage charts summarize the whole selected folder independently of the table filters.
- In **Scan History**, select a completed or partial scan and choose **Browse inventory** to inspect its saved state. Use **Reveal in file manager** on a table entry to locate it on the current filesystem.

The file table shows 200 entries per page. The tree expands the active branch and shows its first 100 immediate folders, with a shortcut to browse the rest in the entries table. Volume capacity reflects the live filesystem and can be unavailable if the saved location is disconnected.

## Review duplicates

1. Open **Duplicate Finder** for the latest inventory, or choose **Find duplicates** on a completed or partial scan in **Scan History**.
2. Set a minimum byte count and optional relative-path exclusion patterns, one glob per line. For example, `downloads/*.tmp` excludes matching paths. Set the minimum to zero to include empty files.
3. Start detection and select a group. **Preview** shows a short text or hexadecimal excerpt. **Verify bytes** compares a selected candidate against the current keeper. **Keep this copy** records the file you want to retain.

Duplicate review is read-only for your files. Rerunning detection replaces the snapshot's previous results and default keeper choices. Files that changed since inventory are skipped when their recorded metadata differs. Rescan to include their current versions. Logical potential excludes paths with unknown identity or multiple hard links. The finder reports an allocated estimate only when the required allocation measurements are available.

## Review and recycle files

1. Open **Cleanup Center** for the latest inventory, or choose **Review cleanup candidates** on a saved scan in **Scan History**.
2. Set the age and size thresholds and refresh the list. Select files and choose **Preview selected** to review the complete selection, file sizes and eligibility.
3. Choose **Send eligible files to Recycle Bin** to confirm. Use **Audit** to see the outcome for each approved file.

DiskSense revalidates files before sending them to the Windows Recycle Bin on supported fixed local drives. For duplicate candidates, it also compares the file with its keeper. Empty folders are review-only. Restore successfully recycled files from the Windows Recycle Bin while Windows retains them. The space estimate describes eventual savings after Windows removes the recycled files.

## Monitor storage growth

Open **Storage Monitor**, browse to a folder, and choose **Watch folder**. DiskSense records recent changes and samples the folder's size at a configurable interval. Set a growth threshold and cooldown under the selected folder. Growth alerts appear in the app status bar and in **Growth history**. **Reconcile now** refreshes the totals when the storage worker is free.

Watches run while DiskSense is open. Pausing a watch retains its history. Enabled watches resume when the app reopens. Reconciliation measures the current folder even if the watch missed events while DiskSense was closed.

## Development

```powershell
uv run ruff check .
uv run ruff format --check .
uv run mypy
$env:QT_QPA_PLATFORM = "offscreen"
uv run pytest
```

- [Architecture and module interfaces](docs/ARCHITECTURE.md)
- [Implementation plan and acceptance criteria](docs/IMPLEMENTATION_PLAN.md)
- [Scanner benchmarks](docs/BENCHMARKS.md)
- [Progress and verification](PROGRESS.md)
