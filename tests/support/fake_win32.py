"""POSIX-backed CreateFileW/NtCreateFile stand-in that does not follow."""

from __future__ import annotations

import os
import stat
from typing import Callable

from omg_cli.win32_nofollow import (
    FILE_APPEND_DATA,
    FILE_CREATE,
    FILE_DIRECTORY_FILE,
    FILE_FLAG_OPEN_REPARSE_POINT,
    FILE_NON_DIRECTORY_FILE,
    FILE_OPEN,
    FILE_OPEN_IF,
    FILE_OPEN_REPARSE_POINT,
    FileInfo,
    GENERIC_WRITE,
    IO_REPARSE_TAG_SYMLINK,
    OBJ_CASE_INSENSITIVE,
    OBJ_DONT_REPARSE,
    OPEN_EXISTING,
    Win32NofollowError,
)


class _Handle:
    __slots__ = ("path", "fd", "is_dir", "reparse_tag", "writable", "append")

    def __init__(
        self,
        path: str,
        *,
        fd: int | None,
        is_dir: bool,
        reparse_tag: int,
        writable: bool,
        append: bool = False,
    ) -> None:
        self.path = path
        self.fd = fd
        self.is_dir = is_dir
        self.reparse_tag = reparse_tag
        self.writable = writable
        self.append = append


class FakeWin32API:
    """POSIX-backed CreateFileW/NtCreateFile stand-in that does not follow."""

    def __init__(self) -> None:
        self._next = 0x1000
        self.handles: dict[int, _Handle] = {}
        self.create_file_calls: list[tuple[str, int]] = []
        self.nt_create_calls: list[tuple[str, str, int, int, int]] = []
        self.forced_reparse: dict[str, tuple[int, bool]] = {}
        self.before_nt_create: Callable[[str, str], None] | None = None

    def _alloc(
        self,
        path: str,
        *,
        fd: int | None,
        is_dir: bool,
        reparse_tag: int,
        writable: bool,
        append: bool = False,
    ) -> int:
        handle = self._next
        self._next += 1
        self.handles[handle] = _Handle(
            path,
            fd=fd,
            is_dir=is_dir,
            reparse_tag=reparse_tag,
            writable=writable,
            append=append,
        )
        return handle

    def _require(self, handle: int) -> _Handle:
        try:
            return self.handles[handle]
        except KeyError as exc:
            raise Win32NofollowError("unavailable", "invalid handle") from exc

    def _follow(self, *, flags: int = 0, options: int = 0, oa_attributes: int = 0) -> bool:
        if flags & FILE_FLAG_OPEN_REPARSE_POINT:
            return False
        if options & FILE_OPEN_REPARSE_POINT:
            return False
        if oa_attributes & OBJ_DONT_REPARSE:
            return False
        return True

    def _classify(self, path: str) -> tuple[str, int, bool]:
        path = os.path.abspath(path)
        planted = self.forced_reparse.get(path) or self.forced_reparse.get(
            os.path.realpath(path)
        )
        if planted is not None:
            return "reparse", planted[0], planted[1]
        try:
            st = os.lstat(path)
        except FileNotFoundError as exc:
            raise Win32NofollowError("missing", f"target does not exist: {path}") from exc
        if stat.S_ISLNK(st.st_mode):
            return "reparse", IO_REPARSE_TAG_SYMLINK, False
        if stat.S_ISDIR(st.st_mode):
            return "dir", 0, True
        if not stat.S_ISREG(st.st_mode):
            raise Win32NofollowError("not_regular", "target must be a regular file")
        return "file", 0, False

    def _try_classify(self, path: str) -> tuple[str, int, bool] | None:
        try:
            return self._classify(path)
        except Win32NofollowError as exc:
            if exc.kind == "missing":
                return None
            raise

    def _open_file(
        self, path: str, *, writable: bool, create: bool, append: bool = False
    ) -> int:
        if append:
            flags = os.O_WRONLY | os.O_APPEND
        elif writable:
            flags = os.O_RDWR
        else:
            flags = os.O_RDONLY
        if create:
            flags |= os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(path, flags, 0o644)
        except FileExistsError as exc:
            raise Win32NofollowError("write", f"cannot create {path}") from exc
        except FileNotFoundError as exc:
            raise Win32NofollowError("missing", f"target does not exist: {path}") from exc
        except OSError as exc:
            raise Win32NofollowError("unavailable", str(exc)) from exc
        return fd

    def create_file(
        self,
        path: str,
        *,
        access: int,
        share: int,
        disposition: int,
        flags: int,
    ) -> int:
        del share
        self.create_file_calls.append((path, flags))
        path = os.path.abspath(path)
        if self._follow(flags=flags):
            path = os.path.realpath(path)
        writable = bool(access & GENERIC_WRITE)
        create = disposition != OPEN_EXISTING and disposition != FILE_OPEN
        kind, tag, is_dir = self._classify(path) if not create else ("file", 0, False)
        if create:
            fd = self._open_file(path, writable=True, create=True)
            return self._alloc(path, fd=fd, is_dir=False, reparse_tag=0, writable=True)
        if kind == "reparse":
            return self._alloc(
                path, fd=None, is_dir=is_dir, reparse_tag=tag, writable=False
            )
        if kind == "dir":
            return self._alloc(path, fd=None, is_dir=True, reparse_tag=0, writable=False)
        fd = self._open_file(path, writable=writable, create=False)
        return self._alloc(path, fd=fd, is_dir=False, reparse_tag=0, writable=writable)

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
        del share
        parent = self._require(root_handle)
        if self.before_nt_create is not None:
            self.before_nt_create(parent.path, name)
        child = os.path.abspath(os.path.join(parent.path, name))
        self.nt_create_calls.append(
            (parent.path, name, options, oa_attributes, disposition)
        )
        if self._follow(options=options, oa_attributes=oa_attributes):
            child = os.path.realpath(child)
        want_dir = bool(options & FILE_DIRECTORY_FILE)
        append = bool(access & FILE_APPEND_DATA) and not (access & GENERIC_WRITE)
        writable = bool(access & (GENERIC_WRITE | FILE_APPEND_DATA))
        classified = self._try_classify(child)
        creating = disposition == FILE_CREATE or (
            disposition == FILE_OPEN_IF and classified is None
        )
        if creating:
            if classified is not None and disposition == FILE_CREATE:
                raise Win32NofollowError("write", f"cannot create {child}")
            if want_dir:
                try:
                    os.mkdir(child, 0o700)
                except FileExistsError as exc:
                    raise Win32NofollowError("write", f"cannot create {child}") from exc
                except FileNotFoundError as exc:
                    raise Win32NofollowError(
                        "missing", f"target does not exist: {child}"
                    ) from exc
                except OSError as exc:
                    raise Win32NofollowError("write", str(exc)) from exc
                return self._alloc(
                    child, fd=None, is_dir=True, reparse_tag=0, writable=False
                )
            fd = self._open_file(child, writable=True, create=True, append=append)
            return self._alloc(
                child,
                fd=fd,
                is_dir=False,
                reparse_tag=0,
                writable=True,
                append=append,
            )
        if classified is None:
            raise Win32NofollowError("missing", f"target does not exist: {child}")
        kind, tag, is_dir = classified
        if kind == "reparse":
            return self._alloc(
                child, fd=None, is_dir=is_dir, reparse_tag=tag, writable=False
            )
        if want_dir and not is_dir:
            raise Win32NofollowError("not_regular", f"{name} is not a directory")
        if options & FILE_NON_DIRECTORY_FILE and is_dir:
            raise Win32NofollowError("not_regular", f"{name} must be a regular file")
        if kind == "dir":
            return self._alloc(child, fd=None, is_dir=True, reparse_tag=0, writable=False)
        fd = self._open_file(child, writable=writable, create=False, append=append)
        return self._alloc(
            child,
            fd=fd,
            is_dir=False,
            reparse_tag=0,
            writable=writable,
            append=append,
        )

    def close(self, handle: int) -> None:
        item = self.handles.pop(handle, None)
        if item is not None and item.fd is not None:
            os.close(item.fd)

    def get_info(self, handle: int) -> FileInfo:
        item = self._require(handle)
        if item.reparse_tag:
            return FileInfo(
                attributes=0x400 | (0x10 if item.is_dir else 0),
                size=0,
                links=1,
                volume=1,
                index=handle,
                mode=0o666,
                is_directory=item.is_dir,
                is_reparse=True,
                reparse_tag=item.reparse_tag,
            )
        if item.fd is not None:
            st = os.fstat(item.fd)
        else:
            st = os.lstat(item.path)
        is_dir = stat.S_ISDIR(st.st_mode)
        return FileInfo(
            attributes=0x10 if is_dir else 0x80,
            size=int(st.st_size),
            links=int(st.st_nlink),
            volume=int(st.st_dev),
            index=int(st.st_ino),
            mode=stat.S_IMODE(st.st_mode),
            is_directory=is_dir,
            is_reparse=False,
            reparse_tag=0,
        )

    def read(self, handle: int, n: int) -> bytes:
        item = self._require(handle)
        if item.fd is None:
            raise Win32NofollowError("symlink", "cannot read a reparse point")
        if n <= 0:
            return b""
        return os.read(item.fd, n)

    def write(self, handle: int, data: bytes) -> int:
        item = self._require(handle)
        if item.fd is None:
            raise Win32NofollowError("write", "cannot write a reparse point")
        return os.write(item.fd, data)

    def flush(self, handle: int) -> None:
        item = self._require(handle)
        if item.fd is not None:
            os.fsync(item.fd)

    def rename_replace(
        self,
        handle: int,
        dest_name: str,
        *,
        root_handle: int,
        replace: bool = True,
    ) -> None:
        src = self._require(handle)
        parent = self._require(root_handle)
        dest = os.path.join(parent.path, dest_name)
        if os.path.lexists(dest):
            if os.path.islink(dest):
                raise Win32NofollowError(
                    "symlink", f"target may not be a symlink: {dest_name}"
                )
            if not replace:
                raise Win32NofollowError("exists", f"destination exists: {dest_name}")
        os.replace(src.path, dest)
        if src.fd is not None:
            os.close(src.fd)
            src.fd = None
        self.handles.pop(handle, None)

    def unlink(self, root_handle: int, name: str) -> None:
        parent = self._require(root_handle)
        path = os.path.join(parent.path, name)
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def lock_ex(self, handle: int) -> None:
        import fcntl

        item = self._require(handle)
        if item.fd is None:
            raise Win32NofollowError("unavailable", "cannot lock a reparse or directory")
        fcntl.flock(item.fd, fcntl.LOCK_EX)

    def unlock(self, handle: int) -> None:
        import fcntl

        item = self._require(handle)
        if item.fd is None:
            return
        fcntl.flock(item.fd, fcntl.LOCK_UN)


def assert_nofollow_flags(api: FakeWin32API) -> None:
    assert api.create_file_calls, "CreateFileW must open the workspace root"
    for _path, flags in api.create_file_calls:
        assert flags & FILE_FLAG_OPEN_REPARSE_POINT
    assert api.nt_create_calls, "NtCreateFile must walk relative components"
    for row in api.nt_create_calls:
        options, oa = row[2], row[3]
        assert options & FILE_OPEN_REPARSE_POINT
        assert oa & OBJ_DONT_REPARSE
