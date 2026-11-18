"""Windows Shell recycling with no permanent-delete fallback."""

import ctypes
import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID


class RecycleError(OSError):
    """The platform refused to recycle the file."""


class RecycleUncertain(RecycleError):
    """The Shell call ended without a conclusive outcome."""


def _guid(value: str) -> Any:
    return (ctypes.c_ubyte * 16).from_buffer_copy(UUID(value).bytes_le)


if sys.platform == "win32":
    from ctypes import wintypes

    _ole = ctypes.WinDLL("ole32")
    _shell = ctypes.WinDLL("shell32")
    _kernel = ctypes.WinDLL("kernel32")
    _ptr = ctypes.c_void_p
    _hr = ctypes.c_long
    _ole.CoInitializeEx.argtypes = (_ptr, wintypes.DWORD)
    _ole.CoInitializeEx.restype = _hr
    _ole.CoCreateInstance.argtypes = (_ptr, _ptr, wintypes.DWORD, _ptr, ctypes.POINTER(_ptr))
    _ole.CoCreateInstance.restype = _hr
    _shell.SHCreateItemFromParsingName.argtypes = (
        wintypes.LPCWSTR,
        _ptr,
        _ptr,
        ctypes.POINTER(_ptr),
    )
    _shell.SHCreateItemFromParsingName.restype = _hr
    _kernel.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
    _kernel.GetDriveTypeW.restype = wintypes.UINT
    _kernel.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        _ptr,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    _kernel.CreateFileW.restype = wintypes.HANDLE
    _kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    _kernel.CloseHandle.restype = wintypes.BOOL

    def _method(obj: ctypes.c_void_p, index: int, result: Any, *args: Any) -> Any:
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(_ptr)))[0]
        return ctypes.WINFUNCTYPE(result, _ptr, *args)(vtable[index])

    def _check(result: int, action: str) -> None:
        if result != 0:
            raise RecycleError(f"{action} failed (Windows status 0x{result & 0xFFFFFFFF:08X}).")

    @contextmanager
    def _pin_parents(path: Path) -> Iterator[None]:
        """Hold directory handles without delete sharing during the Shell operation."""
        with ExitStack() as handles:
            for parent in reversed(path.parents):
                # READ_ATTRIBUTES, share READ|WRITE, OPEN_EXISTING.
                # BACKUP_SEMANTICS and OPEN_REPARSE_POINT permit a directory handle.
                handle = _kernel.CreateFileW(str(parent), 0x80, 0x3, None, 3, 0x02200000, None)
                if handle in (None, _ptr(-1).value):
                    raise RecycleError(f"Could not hold the parent directory in place: {parent}")
                handles.callback(_kernel.CloseHandle, handle)
            yield

    def _source_present(path: Path) -> bool:
        try:
            path.lstat()
        except FileNotFoundError:
            return False
        except OSError as error:
            raise RecycleUncertain(
                "Could not confirm the source location. Check Recycle Bin."
            ) from error
        return True

    def recycle_file(path: Path, revalidate: Callable[[], None]) -> None:
        if len(path.drive) != 2 or _kernel.GetDriveTypeW(path.anchor) != 3:
            raise RecycleError("Recycling requires a local fixed Windows drive.")
        initialized = _ole.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
        if initialized not in (0, 1):
            _check(initialized, "COM initialization")
        operation, item = _ptr(), _ptr()
        try:
            clsid = _guid("3ad05575-8857-4850-9277-11b85bdb8e09")
            iid = _guid("947aab5f-0a5c-4c13-b4d6-4bf7836fc9f8")
            shell_item = _guid("43826d1e-e718-42ee-bc55-a1e261c37bfe")
            _check(
                _ole.CoCreateInstance(clsid, None, 1, iid, ctypes.byref(operation)),
                "Creating the Shell operation",
            )
            # RECYCLEONDELETE, ADDUNDORECORD, EARLYFAILURE.
            # NOERRORUI, NO_CONNECTED_ELEMENTS, SILENT.
            flags = 0x00080000 | 0x20000000 | 0x00100000 | 0x0400 | 0x2000 | 0x0004
            _check(
                _method(operation, 5, _hr, wintypes.DWORD)(operation, flags),
                "Setting recycle flags",
            )
            _check(
                _shell.SHCreateItemFromParsingName(str(path), None, shell_item, ctypes.byref(item)),
                "Opening the Shell item",
            )
            _check(
                _method(operation, 18, _hr, _ptr, _ptr)(operation, item, None), "Queueing the item"
            )
            with _pin_parents(path):
                revalidate()
                result = _method(operation, 21, _hr)(operation)
                aborted = wintypes.BOOL()
                status = _method(operation, 22, _hr, ctypes.POINTER(wintypes.BOOL))(
                    operation, ctypes.byref(aborted)
                )
                present = _source_present(path)
            if result != 0 or status != 0 or aborted.value:
                if not present:
                    raise RecycleUncertain("The Shell outcome is uncertain. Check the Recycle Bin.")
                _check(result or status, "Recycling")
                raise RecycleError("Windows aborted the recycle operation.")
            if present:
                raise RecycleError("Windows left the source in place.")
        finally:
            for interface in (item, operation):
                if interface:
                    _method(interface, 2, wintypes.ULONG)(interface)
            _ole.CoUninitialize()

else:

    def recycle_file(path: Path, revalidate: Callable[[], None]) -> None:
        raise RecycleError("Native recycling is currently available only on Windows.")
