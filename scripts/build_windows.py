"""Build, smoke-test, and archive ByteContour for Windows x64."""

import hashlib
import os
import platform
import shutil
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

import pefile
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def _resolve_icu_collision(internal: Path, system32: Path) -> None:
    bundled = internal / "icuuc.dll"
    if not bundled.is_file():
        return
    with pefile.PE(str(internal / "PySide6" / "Qt6Core.dll")) as qt:
        required = {
            symbol.name
            for entry in qt.DIRECTORY_ENTRY_IMPORT
            if entry.dll.lower() == b"icuuc.dll"
            for symbol in entry.imports
            if symbol.name
        }
    with pefile.PE(str(bundled)) as library:
        provided = {symbol.name for symbol in library.DIRECTORY_ENTRY_EXPORT.symbols}
        data_files = [
            entry.dll.decode("ascii")
            for entry in library.DIRECTORY_ENTRY_IMPORT
            if entry.dll.lower().startswith(b"icudt") and entry.dll.lower().endswith(b".dll")
        ]
    if required <= provided:
        return
    # A toolchain ICU can share the OS DLL's name but export versioned symbols.
    # Remove it only after confirming the OS supplies the symbols Qt actually imports.
    with pefile.PE(str(system32 / "icuuc.dll")) as library:
        system_symbols = {symbol.name for symbol in library.DIRECTORY_ENTRY_EXPORT.symbols}
    if not required <= system_symbols:
        raise RuntimeError("The bundled and Windows ICU libraries both lack required Qt symbols.")
    bundled.unlink()
    for name in data_files:
        if Path(name).name == name:
            (internal / name).unlink(missing_ok=True)
    print("Removed incompatible ICU DLL collected from the build environment.", flush=True)


def main() -> None:
    if sys.platform != "win32" or sys.version_info[:2] != (3, 13):
        raise SystemExit("Use Windows with 64-bit Python 3.13 to build ByteContour.")
    if platform.machine().upper() not in {"AMD64", "X86_64"} or sys.maxsize <= 2**32:
        raise SystemExit("The release target requires Windows x64.")
    output = ROOT / "dist"
    bundle = output / "ByteContour"
    assets = ROOT / "src" / "bytecontour" / "assets"
    release = version("bytecontour")
    numbers = (*Version(release).release[:4], 0, 0, 0, 0)[:4]
    build = ROOT / "build"
    if any(path.resolve() != path for path in (build, output, bundle)):
        raise SystemExit("Build and distribution paths must stay inside the project.")
    build.mkdir(exist_ok=True)
    os.environ.setdefault("PYINSTALLER_CONFIG_DIR", str(build / "pyinstaller-cache"))
    resource = build / "windows-version.txt"
    resource.write_text(
        f"VSVersionInfo(ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, "
        "mask=0x3f, flags=0, OS=0x40004, fileType=1, subtype=0, date=(0, 0)), kids=["
        "StringFileInfo([StringTable('040904B0', ["
        "StringStruct('FileDescription', 'ByteContour'), "
        "StringStruct('ProductName', 'ByteContour'), "
        "StringStruct('OriginalFilename', 'ByteContour.exe'), "
        f"StringStruct('FileVersion', {release!r}), StringStruct('ProductVersion', {release!r})"
        "])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])])",
        encoding="utf-8",
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onedir",
            "--windowed",
            "--name",
            "ByteContour",
            "--noupx",
            "--icon",
            str(assets / "bytecontour.ico"),
            "--version-file",
            str(resource),
            "--specpath",
            "build",
            "--recursive-copy-metadata",
            "bytecontour",
            "--add-data",
            f"{assets / 'bytecontour.png'}:bytecontour/assets",
            "--hidden-import",
            "watchdog.observers.read_directory_changes",
            "src/bytecontour/__main__.py",
        ],
        cwd=ROOT,
        check=True,
    )
    shutil.copyfile(ROOT / "docs" / "WINDOWS.md", bundle / "README.txt")
    system_root = Path(os.environ["SYSTEMROOT"])
    _resolve_icu_collision(bundle / "_internal", system_root / "System32")
    with TemporaryDirectory(prefix="bytecontour-package-") as scratch:
        env = {
            key: value for key, value in os.environ.items() if not key.upper().startswith("PYTHON")
        }
        env["PATH"] = os.pathsep.join((str(system_root / "System32"), str(system_root)))
        subprocess.run(
            [str(bundle / "ByteContour.exe"), "--smoke-test"],
            cwd=scratch,
            env=env,
            timeout=45,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    name = f"ByteContour-{release}-windows-x64"
    archive = Path(shutil.make_archive(str(output / name), "zip", output, bundle.name))
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    (output / f"{archive.name}.sha256").write_text(
        f"{checksum}  {archive.name}\n", encoding="ascii"
    )
    print(f"Built and smoke-tested {archive}")


if __name__ == "__main__":
    main()
