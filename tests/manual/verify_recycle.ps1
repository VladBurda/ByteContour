$ErrorActionPreference = 'Stop'
$workspace = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '../..')).Path
$python = Join-Path $workspace '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Run uv sync --locked first.' }
$tag = [guid]::NewGuid().ToString('N')
$fixture = Join-Path $workspace "disksense-native-test-$tag"
$null = New-Item -ItemType Directory -Path $fixture
$target = Join-Path $fixture "recycle-check-$tag.tmp"
$env:DISKSENSE_NATIVE_TEST = $target
try {
@'
import os
from pathlib import Path

from disksense.cleanup import CleanupService, Rules
from disksense.database import ScanRepository
from disksense.scan_service import ScanService
from disksense.scanner import ScanControl

path = Path(os.environ["DISKSENSE_NATIVE_TEST"])
path.write_bytes(b"DiskSense disposable recycle test")
database = path.parent / "audit.sqlite3"
repo = ScanRepository(database)
try:
    repo.initialize()
    scan = ScanService(repo).scan(path.parent, False, ScanControl(), lambda _: None)
    service = CleanupService(repo)
    rules = Rules(temporary_days=0)
    with path.open("rb"):
        preview = service.preview(scan.id, rules, (path.name,))
        assert preview.eligible, preview.items
        locked = service.execute(preview.token, ScanControl())
        assert locked.rows[0].state == "failed" and path.is_file(), locked
    print("Locked fixture remained in place.")
    preview = service.preview(scan.id, rules, (path.name,))
    assert preview.eligible, preview.items
    outcome = service.execute(preview.token, ScanControl())
    assert outcome.rows[0].state == "recycled" and not path.exists(), outcome
finally:
    repo.close()
    database.unlink(missing_ok=True)
'@ | & $python -
    if ($LASTEXITCODE) { throw 'Native cleanup test failed.' }
    $shell = New-Object -ComObject Shell.Application
    $bin = $shell.Namespace(10)
    $fixtureItem = $null
    foreach ($entry in $bin.Items()) {
        if ($entry.ExtendedProperty('System.FileName') -eq (Split-Path -Leaf $target) -and
            $entry.ExtendedProperty('System.Recycle.DeletedFrom') -eq $fixture) {
            $fixtureItem = $entry
            break
        }
    }
    if ($null -eq $fixtureItem) { throw "Disposable fixture missing from Recycle Bin: $target" }
    Write-Output 'Verified the disposable fixture in the Windows Recycle Bin.'
    $fixtureItem.InvokeVerb('undelete')
    for ($i = 0; $i -lt 30 -and -not (Test-Path -LiteralPath $target); $i++) {
        Start-Sleep -Milliseconds 100
    }
    if ((Get-Content -Raw -LiteralPath $target) -ne 'DiskSense disposable recycle test') {
        throw 'Restored fixture contents differ.'
    }
    Write-Output 'Restored the disposable fixture with its original bytes.'
} finally {
    Remove-Item Env:DISKSENSE_NATIVE_TEST -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $target -PathType Leaf) { Remove-Item -LiteralPath $target }
    if (-not (Get-ChildItem -Force -LiteralPath $fixture)) { Remove-Item -LiteralPath $fixture }
}
