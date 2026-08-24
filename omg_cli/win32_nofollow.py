"""Windows confined no-follow I/O (CreateFileW + NtCreateFile).

Each path component is opened with ``FILE_FLAG_OPEN_REPARSE_POINT`` /
``FILE_OPEN_REPARSE_POINT`` so a swapped symlink or mount-point junction is
not pathname-followed. Any reparse tag is rejected. This is not POSIX
``dir_fd``; it is the fail-closed Windows substitute.

Callers on POSIX must keep ``O_NOFOLLOW`` / ``dir_fd``. Tests inject ``_API``.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# kernel32 CreateFileW
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
CREATE_NEW = 1
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
INVALID_HANDLE_VALUE = -1

# ntdll NtCreateFile
FILE_OPEN = 1
FILE_CREATE = 2
FILE_DIRECTORY_FILE = 0x00000001
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_OPEN_FOR_BACKUP_INTENT = 0x00004000
FILE_OPEN_REPARSE_POINT = 0x00200000
OBJ_CASE_INSENSITIVE = 0x00000040
OBJ_DONT_REPARSE = 0x00001000

IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C

_NT_MISSING = {0xC0000033, 0xC0000034, 0xC000003A}
_NT_COLLISION = {0xC0000035}
_NT_SHARING = {0xC0000043}
_WIN_MISSING = {2, 3}
_WIN_SHARING = {32}

_DEFAULT_MODE = 0o666
_MAX_PATH_CHARS = 32767


class Win32NofollowError(OSError):
    """Fail-closed Windows no-follow error. ``kind`` is a stable token."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class FileInfo:
    attributes: int
    size: int
    links: int
    volume: int
    index: int
    mode: int
    is_directory: bool
    is_reparse: bool
    reparse_tag: int


class Win32NofollowAPI(Protocol):
    def create_file(
        self,
        path: str,
        *,
        access: int,
        share: int,
        disposition: int,
        flags: int,
    ) -> int: ...

    def nt_create(
        self,
        root_handle: int,
        name: str,
        *,
        access: int,
        share: int,
        disposition: int,
        options: int,
        oa_attributes: int = OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE,
    ) -> int: ...

    def close(self, handle: int) -> None: ...

    def get_info(self, handle: int) -> FileInfo: ...

    def read(self, handle: int, n: int) -> bytes: ...

    def write(self, handle: int, data: bytes) -> int: ...

    def flush(self, handle: int) -> None: ...

    def rename_replace(self, handle: int, dest_name: str, *, root_handle: int) -> None: ...

    def unlink(self, root_handle: int, name: str) -> None: ...


_API: Win32NofollowAPI | None = None


def windows_nofollow_ready() -> bool:
    """True when this process can use the Windows no-follow backend.

    An injected ``_API`` (hermetic tests) counts even on POSIX so Darwin can
    drive the real Windows walk without constructing ``WindowsPath``.
    """

    if _API is not None:
        return True
    if os.name != "nt":
        return False
    try:
        _get_api()
    except Exception:
        return False
    return True


def _get_api() -> Win32NofollowAPI:
    global _API
    if _API is not None:
        return _API
    if os.name != "nt":
        raise Win32NofollowError(
            "unavailable",
            "Windows no-follow API requires os.name=='nt'",
        )
    loaded = CtypesWin32API()
    _API = loaded
    return loaded


def _validate_component(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise Win32NofollowError("not_regular", "empty path component")
    if name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise Win32NofollowError("not_regular", f"unsafe path component: {name!r}")
    return name


def _reject_reparse(api: Win32NofollowAPI, handle: int, *, label: str, directory: bool) -> FileInfo:
    info = api.get_info(handle)
    if info.is_reparse or info.reparse_tag:
        raise Win32NofollowError(
            "symlink",
            f"{label} may not be a symlink or reparse point",
        )
    if directory and not info.is_directory:
        raise Win32NofollowError("not_regular", f"{label} is not a directory")
    if not directory and info.is_directory:
        raise Win32NofollowError("not_regular", f"{label} must be a regular file")
    return info


def _close(api: Win32NofollowAPI, handle: int | None) -> None:
    if handle is None or handle in {INVALID_HANDLE_VALUE, 0}:
        return
    try:
        api.close(handle)
    except Exception:
        pass


def _read_until(api: Win32NofollowAPI, handle: int, *, limit: int) -> bytes:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise Win32NofollowError("size", "read limit must be a non-negative integer")
    chunks: list[bytes] = []
    remaining = limit
    while remaining:
        chunk = api.read(handle, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _open_root_dir(api: Win32NofollowAPI, root: Path) -> int:
    path = str(Path(root).absolute())
    handle = api.create_file(
        path,
        access=GENERIC_READ | SYNCHRONIZE,
        share=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        disposition=OPEN_EXISTING,
        flags=FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
    )
    try:
        _reject_reparse(api, handle, label="workspace root", directory=True)
    except Exception:
        _close(api, handle)
        raise
    return handle


def _walk_parent(root: Path, parts: list[str]) -> tuple[Win32NofollowAPI, int, str]:
    if not parts:
        raise Win32NofollowError("not_regular", "path is empty")
    clean = [_validate_component(part) for part in parts]
    api = _get_api()
    current = _open_root_dir(api, root)
    try:
        for component in clean[:-1]:
            nxt = api.nt_create(
                current,
                component,
                access=GENERIC_READ | SYNCHRONIZE,
                share=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                disposition=FILE_OPEN,
                options=(
                    FILE_DIRECTORY_FILE
                    | FILE_OPEN_REPARSE_POINT
                    | FILE_SYNCHRONOUS_IO_NONALERT
                    | FILE_OPEN_FOR_BACKUP_INTENT
                ),
            )
            _close(api, current)
            current = nxt
            _reject_reparse(api, current, label=component, directory=True)
        return api, current, clean[-1]
    except Exception:
        _close(api, current)
        raise


def read_relative_regular(
    root: Path | str,
    parts: list[str],
    *,
    max_bytes: int,
) -> tuple[bytes, int]:
    """Read ``/``.join(*parts) under *root* without following a reparse point."""

    api, parent, name = _walk_parent(Path(root), parts)
    leaf: int | None = None
    try:
        leaf = api.nt_create(
            parent,
            name,
            access=GENERIC_READ | SYNCHRONIZE,
            share=FILE_SHARE_READ | FILE_SHARE_DELETE,
            disposition=FILE_OPEN,
            options=(
                FILE_NON_DIRECTORY_FILE
                | FILE_OPEN_REPARSE_POINT
                | FILE_SYNCHRONOUS_IO_NONALERT
            ),
        )
        before = _reject_reparse(api, leaf, label=name, directory=False)
        if before.links != 1:
            raise Win32NofollowError(
                "links",
                f"target must be a single-link regular file: {name}",
            )
        if before.size > max_bytes:
            raise Win32NofollowError("size", f"current bytes exceed {max_bytes} byte limit")
        body = _read_until(
            api,
            leaf,
            limit=min(int(before.size) + 1, max_bytes + 1),
        )
        if len(body) > max_bytes:
            raise Win32NofollowError("size", f"current bytes exceed {max_bytes} byte limit")
        after = api.get_info(leaf)
        if (
            before.size,
            before.index,
            before.volume,
            before.links,
        ) != (
            after.size,
            after.index,
            after.volume,
            after.links,
        ) or after.size != len(body):
            raise Win32NofollowError("changed", f"target changed while reading: {name}")
        mode = before.mode if before.mode else _DEFAULT_MODE
        return body, mode
    finally:
        _close(api, leaf)
        _close(api, parent)


def write_relative_regular(
    root: Path | str,
    parts: list[str],
    body: bytes,
    *,
    expected: bytes | None = None,
    mode: int | None = None,
    max_bytes: int,
) -> int:
    """Replace ``/``.join(*parts) under *root* without following a reparse point."""

    if not isinstance(body, (bytes, bytearray)):
        raise Win32NofollowError("write", "body must be bytes")
    payload = bytes(body)
    if len(payload) > max_bytes:
        raise Win32NofollowError("size", f"current bytes exceed {max_bytes} byte limit")
    api, parent, name = _walk_parent(Path(root), parts)
    leaf: int | None = None
    tmp: int | None = None
    tmp_name = f".{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        leaf = api.nt_create(
            parent,
            name,
            access=GENERIC_READ | SYNCHRONIZE,
            share=0,
            disposition=FILE_OPEN,
            options=(
                FILE_NON_DIRECTORY_FILE
                | FILE_OPEN_REPARSE_POINT
                | FILE_SYNCHRONOUS_IO_NONALERT
            ),
        )
        before = _reject_reparse(api, leaf, label=name, directory=False)
        if before.links != 1:
            raise Win32NofollowError(
                "links",
                f"target must be a single-link regular file: {name}",
            )
        current = _read_until(
            api,
            leaf,
            limit=min(int(before.size) + 1, max_bytes + 1),
        )
        if len(current) > max_bytes:
            raise Win32NofollowError("size", f"current bytes exceed {max_bytes} byte limit")
        applied_mode = mode if mode is not None else (before.mode or _DEFAULT_MODE)
        if current == payload:
            return applied_mode
        if expected is not None and current != expected:
            raise Win32NofollowError("expected", "current file bytes do not match expected")
        _close(api, leaf)
        leaf = None
        tmp = api.nt_create(
            parent,
            tmp_name,
            access=GENERIC_WRITE | GENERIC_READ | SYNCHRONIZE | DELETE,
            share=0,
            disposition=FILE_CREATE,
            options=(
                FILE_NON_DIRECTORY_FILE
                | FILE_OPEN_REPARSE_POINT
                | FILE_SYNCHRONOUS_IO_NONALERT
            ),
        )
        _reject_reparse(api, tmp, label=tmp_name, directory=False)
        written = 0
        while written < len(payload):
            written += api.write(tmp, payload[written:])
        api.flush(tmp)
        probe: int | None = None
        try:
            probe = api.nt_create(
                parent,
                name,
                access=GENERIC_READ | SYNCHRONIZE,
                share=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                disposition=FILE_OPEN,
                options=(
                    FILE_NON_DIRECTORY_FILE
                    | FILE_OPEN_REPARSE_POINT
                    | FILE_SYNCHRONOUS_IO_NONALERT
                ),
            )
            _reject_reparse(api, probe, label=name, directory=False)
        finally:
            _close(api, probe)
        api.rename_replace(tmp, name, root_handle=parent)
        tmp = None
        published, _published_mode = read_relative_regular(
            root, parts, max_bytes=max_bytes
        )
        if published != payload:
            raise Win32NofollowError("write", "post-replace readback mismatch")
        return applied_mode
    except Win32NofollowError:
        raise
    except OSError as exc:
        raise Win32NofollowError("write", "atomic replace failed") from exc
    finally:
        if tmp is not None:
            _close(api, tmp)
            try:
                api.unlink(parent, tmp_name)
            except Exception:
                pass
        _close(api, leaf)
        _close(api, parent)


def write_path_regular(path: Path | str, body: bytes) -> None:
    """Replace *path*'s leaf without following a dest reparse point."""

    dest = Path(path).absolute()
    parent = dest.parent
    api = _get_api()
    parent_h = api.create_file(
        str(parent),
        access=GENERIC_READ | SYNCHRONIZE,
        share=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        disposition=OPEN_EXISTING,
        flags=FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
    )
    tmp: int | None = None
    tmp_name = f".{dest.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        _reject_reparse(api, parent_h, label=str(parent), directory=True)
        _validate_component(dest.name)
        try:
            existing = api.nt_create(
                parent_h,
                dest.name,
                access=GENERIC_READ | SYNCHRONIZE,
                share=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                disposition=FILE_OPEN,
                options=(
                    FILE_NON_DIRECTORY_FILE
                    | FILE_OPEN_REPARSE_POINT
                    | FILE_SYNCHRONOUS_IO_NONALERT
                ),
            )
        except Win32NofollowError as exc:
            if exc.kind != "missing":
                raise
        else:
            try:
                _reject_reparse(api, existing, label=dest.name, directory=False)
            finally:
                _close(api, existing)
        payload = bytes(body)
        tmp = api.nt_create(
            parent_h,
            tmp_name,
            access=GENERIC_WRITE | GENERIC_READ | SYNCHRONIZE | DELETE,
            share=0,
            disposition=FILE_CREATE,
            options=(
                FILE_NON_DIRECTORY_FILE
                | FILE_OPEN_REPARSE_POINT
                | FILE_SYNCHRONOUS_IO_NONALERT
            ),
        )
        written = 0
        while written < len(payload):
            written += api.write(tmp, payload[written:])
        api.flush(tmp)
        api.rename_replace(tmp, dest.name, root_handle=parent_h)
        tmp = None
    finally:
        if tmp is not None:
            _close(api, tmp)
            try:
                api.unlink(parent_h, tmp_name)
            except Exception:
                pass
        _close(api, parent_h)


def reject_existing_reparse(path: Path | str) -> None:
    """Open *path* without following; raise if it is a reparse point."""

    target = Path(path)
    if not target.exists() and not target.is_symlink():
        return
    api = _get_api()
    flags = FILE_FLAG_OPEN_REPARSE_POINT
    try:
        if target.is_dir() and not target.is_symlink():
            flags |= FILE_FLAG_BACKUP_SEMANTICS
    except OSError:
        flags |= FILE_FLAG_BACKUP_SEMANTICS
    handle = api.create_file(
        str(target),
        access=GENERIC_READ | SYNCHRONIZE,
        share=FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        disposition=OPEN_EXISTING,
        flags=flags,
    )
    try:
        info = api.get_info(handle)
        if info.is_reparse or info.reparse_tag:
            raise Win32NofollowError(
                "symlink",
                f"refusing symlink dest: {Path(path).as_posix()}",
            )
    finally:
        _close(api, handle)


class CtypesWin32API:
    """Production backend: kernel32 + ntdll. Instantiated only on Windows."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        ntdll = ctypes.WinDLL("ntdll")
        self._kernel32 = kernel32
        self._ntdll = ntdll
        self._HANDLE = ctypes.c_void_p
        self._INVALID = ctypes.c_void_p(-1).value

        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            self._HANDLE,
        ]
        kernel32.CreateFileW.restype = self._HANDLE
        kernel32.CloseHandle.argtypes = [self._HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.ReadFile.argtypes = [
            self._HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.WriteFile.argtypes = [
            self._HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        kernel32.WriteFile.restype = wintypes.BOOL
        kernel32.FlushFileBuffers.argtypes = [self._HANDLE]
        kernel32.FlushFileBuffers.restype = wintypes.BOOL
        kernel32.GetFileInformationByHandle.argtypes = [self._HANDLE, ctypes.c_void_p]
        kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
        kernel32.DeviceIoControl.argtypes = [
            self._HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        kernel32.DeviceIoControl.restype = wintypes.BOOL
        kernel32.GetFinalPathNameByHandleW.argtypes = [
            self._HANDLE,
            wintypes.LPWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
        kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
        kernel32.MoveFileExW.restype = wintypes.BOOL
        kernel32.DeleteFileW.argtypes = [wintypes.LPCWSTR]
        kernel32.DeleteFileW.restype = wintypes.BOOL
        kernel32.GetFileType.argtypes = [self._HANDLE]
        kernel32.GetFileType.restype = wintypes.DWORD

        class _IO_STATUS_BLOCK(ctypes.Structure):
            _fields_ = [("Status", ctypes.c_long), ("Information", ctypes.c_size_t)]

        class _UNICODE_STRING(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.USHORT),
                ("MaximumLength", wintypes.USHORT),
                ("Buffer", ctypes.POINTER(wintypes.WCHAR)),
            ]

        class _OBJECT_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("Length", wintypes.ULONG),
                ("RootDirectory", self._HANDLE),
                ("ObjectName", ctypes.POINTER(_UNICODE_STRING)),
                ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", ctypes.c_void_p),
                ("SecurityQualityOfService", ctypes.c_void_p),
            ]

        class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        self._IO_STATUS_BLOCK = _IO_STATUS_BLOCK
        self._UNICODE_STRING = _UNICODE_STRING
        self._OBJECT_ATTRIBUTES = _OBJECT_ATTRIBUTES
        self._BY_HANDLE_FILE_INFORMATION = _BY_HANDLE_FILE_INFORMATION

        ntdll.NtCreateFile.argtypes = [
            ctypes.POINTER(self._HANDLE),
            wintypes.DWORD,
            ctypes.POINTER(_OBJECT_ATTRIBUTES),
            ctypes.POINTER(_IO_STATUS_BLOCK),
            ctypes.c_void_p,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
        ]
        ntdll.NtCreateFile.restype = ctypes.c_long

    def _handle(self, value: Any) -> int:
        if value is None:
            return INVALID_HANDLE_VALUE
        raw = int(value) if isinstance(value, int) else int(value or 0)
        if raw in {INVALID_HANDLE_VALUE, self._INVALID, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF}:
            return INVALID_HANDLE_VALUE
        return raw

    def _raise_last(self, kind: str, message: str) -> None:
        err = int(self._ctypes.get_last_error() or 0)
        if err in _WIN_MISSING:
            raise Win32NofollowError("missing", message)
        if err in _WIN_SHARING:
            raise Win32NofollowError("changed", message)
        raise Win32NofollowError(kind, message)

    def _raise_nt(self, status: int, message: str) -> None:
        code = status & 0xFFFFFFFF
        if code in _NT_MISSING:
            raise Win32NofollowError("missing", message)
        if code in _NT_COLLISION:
            raise Win32NofollowError("write", message)
        if code in _NT_SHARING:
            raise Win32NofollowError("changed", message)
        raise Win32NofollowError("write", f"{message} (ntstatus=0x{code:08x})")

    def create_file(
        self,
        path: str,
        *,
        access: int,
        share: int,
        disposition: int,
        flags: int,
    ) -> int:
        handle = self._kernel32.CreateFileW(
            path,
            access,
            share,
            None,
            disposition,
            flags,
            None,
        )
        parsed = self._handle(handle)
        if parsed == INVALID_HANDLE_VALUE:
            self._raise_last("unavailable", f"cannot open {path}")
        return parsed

    def nt_create(
        self,
        root_handle: int,
        name: str,
        *,
        access: int,
        share: int,
        disposition: int,
        options: int,
        oa_attributes: int = OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE,
    ) -> int:
        ctypes = self._ctypes
        buf = ctypes.create_unicode_buffer(name)
        uni = self._UNICODE_STRING()
        uni.Length = len(name) * 2
        uni.MaximumLength = (len(name) + 1) * 2
        uni.Buffer = ctypes.cast(buf, ctypes.POINTER(self._wintypes.WCHAR))
        oa = self._OBJECT_ATTRIBUTES()
        oa.Length = ctypes.sizeof(self._OBJECT_ATTRIBUTES)
        oa.RootDirectory = self._HANDLE(root_handle)
        oa.ObjectName = ctypes.pointer(uni)
        oa.Attributes = oa_attributes
        oa.SecurityDescriptor = None
        oa.SecurityQualityOfService = None
        iosb = self._IO_STATUS_BLOCK()
        out = self._HANDLE()
        status = int(
            self._ntdll.NtCreateFile(
                ctypes.byref(out),
                access,
                ctypes.byref(oa),
                ctypes.byref(iosb),
                None,
                FILE_ATTRIBUTE_NORMAL,
                share,
                disposition,
                options,
                None,
                0,
            )
        )
        if status < 0:
            self._raise_nt(status, f"cannot open {name}")
        parsed = self._handle(out.value)
        if parsed == INVALID_HANDLE_VALUE:
            raise Win32NofollowError("unavailable", f"cannot open {name}")
        return parsed

    def close(self, handle: int) -> None:
        self._kernel32.CloseHandle(self._HANDLE(handle))

    def get_info(self, handle: int) -> FileInfo:
        ctypes = self._ctypes
        info = self._BY_HANDLE_FILE_INFORMATION()
        ok = self._kernel32.GetFileInformationByHandle(
            self._HANDLE(handle), ctypes.byref(info)
        )
        if not ok:
            self._raise_last("unavailable", "GetFileInformationByHandle failed")
        attributes = int(info.dwFileAttributes)
        size = (int(info.nFileSizeHigh) << 32) | int(info.nFileSizeLow)
        index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
        is_dir = bool(attributes & FILE_ATTRIBUTE_DIRECTORY)
        is_reparse = bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT)
        tag = 0
        if is_reparse:
            tag = self._reparse_tag(handle)
        mode = 0o444 if attributes & 0x1 else _DEFAULT_MODE
        file_type = int(self._kernel32.GetFileType(self._HANDLE(handle)) or 0)
        # FILE_TYPE_DISK=1; anything else is not a confined regular file.
        if not is_dir and file_type != 1:
            raise Win32NofollowError("not_regular", "target must be a regular file")
        return FileInfo(
            attributes=attributes,
            size=size,
            links=int(info.nNumberOfLinks),
            volume=int(info.dwVolumeSerialNumber),
            index=index,
            mode=mode,
            is_directory=is_dir,
            is_reparse=is_reparse,
            reparse_tag=tag,
        )

    def _reparse_tag(self, handle: int) -> int:
        ctypes = self._ctypes
        FSCTL_GET_REPARSE_POINT = 0x000900A8
        buf = ctypes.create_string_buffer(16 * 1024)
        returned = self._wintypes.DWORD(0)
        ok = self._kernel32.DeviceIoControl(
            self._HANDLE(handle),
            FSCTL_GET_REPARSE_POINT,
            None,
            0,
            buf,
            len(buf),
            ctypes.byref(returned),
            None,
        )
        if not ok or returned.value < 4:
            return IO_REPARSE_TAG_SYMLINK
        return int.from_bytes(buf.raw[:4], "little")

    def read(self, handle: int, n: int) -> bytes:
        ctypes = self._ctypes
        if n <= 0:
            return b""
        buf = ctypes.create_string_buffer(n)
        read = self._wintypes.DWORD(0)
        ok = self._kernel32.ReadFile(
            self._HANDLE(handle),
            buf,
            n,
            ctypes.byref(read),
            None,
        )
        if not ok:
            self._raise_last("unavailable", "ReadFile failed")
        return buf.raw[: int(read.value)]

    def write(self, handle: int, data: bytes) -> int:
        ctypes = self._ctypes
        if not data:
            return 0
        written = self._wintypes.DWORD(0)
        ok = self._kernel32.WriteFile(
            self._HANDLE(handle),
            data,
            len(data),
            ctypes.byref(written),
            None,
        )
        if not ok:
            self._raise_last("write", "WriteFile failed")
        return int(written.value)

    def flush(self, handle: int) -> None:
        if not self._kernel32.FlushFileBuffers(self._HANDLE(handle)):
            self._raise_last("write", "FlushFileBuffers failed")

    def _final_path(self, handle: int) -> str:
        ctypes = self._ctypes
        buf = ctypes.create_unicode_buffer(_MAX_PATH_CHARS)
        n = int(
            self._kernel32.GetFinalPathNameByHandleW(
                self._HANDLE(handle), buf, _MAX_PATH_CHARS, 0
            )
        )
        if n == 0 or n >= _MAX_PATH_CHARS:
            self._raise_last("unavailable", "GetFinalPathNameByHandleW failed")
        return buf.value

    def rename_replace(self, handle: int, dest_name: str, *, root_handle: int) -> None:
        parent = self._final_path(root_handle)
        src = self._final_path(handle)
        dest = parent + ("\\" if not parent.endswith("\\") else "") + dest_name
        MOVEFILE_REPLACE_EXISTING = 0x1
        MOVEFILE_WRITE_THROUGH = 0x8
        # Close is required on some Windows versions before MoveFileEx replace.
        self.close(handle)
        ok = self._kernel32.MoveFileExW(
            src, dest, MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH
        )
        if not ok:
            self._raise_last("write", f"cannot replace {dest_name}")

    def unlink(self, root_handle: int, name: str) -> None:
        parent = self._final_path(root_handle)
        path = parent + ("\\" if not parent.endswith("\\") else "") + name
        if not self._kernel32.DeleteFileW(path):
            err = int(self._ctypes.get_last_error() or 0)
            if err not in _WIN_MISSING:
                self._raise_last("write", f"cannot unlink {name}")
