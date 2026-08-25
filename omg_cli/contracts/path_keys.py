"""Safe path keys and durable local-store primitives.

Raw host/run identifiers never become path components.  Callers use a SHA-256
key, then confine the resulting path beneath a managed root.

On POSIX, managed directories and files are created and mutated through
descriptor-relative (``dir_fd``) operations with ``O_NOFOLLOW`` so
intermediate or destination symlinks cannot redirect authoritative state
outside the trusted store.  Path strings are used only to locate a
pre-existing base directory; every managed component under that base is
opened or created without following symlinks.

On Windows, the same high-level primitives walk each component with
``CreateFileW`` / ``NtCreateFile`` ``FILE_FLAG_OPEN_REPARSE_POINT`` and
reject reparse points. Descriptor-relative ``*_at`` / ``dir_fd`` APIs
remain POSIX-only and fail closed.

On hosts without POSIX ``dir_fd`` / ``O_NOFOLLOW`` or Windows no-follow
support the primitives fail closed with :class:`ContractPathError` rather
than falling back to weaker path-based writes.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, NoReturn

from omg_cli.win32_nofollow import (
    Win32NofollowError,
    append_locked_relative_jsonl,
    atomic_write_relative,
    confined_relative_walk,
    ensure_relative_directories,
    exclusive_lock_relative,
    read_relative_regular,
    windows_nofollow_ready,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - OMG is supported on POSIX hosts
    fcntl = None  # type: ignore[assignment]


MANAGED_DIR_MODE = 0o700
DATA_FILE_MODE = 0o600
IMMUTABLE_SOURCE_MODE = 0o400
EXECUTABLE_MODE = 0o700
MAX_MANAGED_READ_BYTES = 16 * 1024 * 1024
SAFE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")
MANAGED_ROOT_MARKER = ".omg"

# errno.ELOOP is 40 on Linux and 62 on macOS; also accept EINVAL for odd platforms.
_NOFOLLOW_ERRNOS = {errno.ELOOP, getattr(errno, "EMLINK", -1), errno.EINVAL}


class ContractPathError(ValueError):
    """A raw identifier or candidate path violates the store boundary."""


def _reject_unsafe_text(value: str, *, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractPathError(f"{label} must be a non-empty string")
    for char in value:
        codepoint = ord(char)
        if codepoint == 0 or codepoint < 0x20 or 0xD800 <= codepoint <= 0xDFFF:
            raise ContractPathError(f"{label} contains a control or surrogate")
    return value


def safe_path_key(raw_id: str, *, namespace: str = "omg") -> str:
    """Return a namespace-bound lowercase SHA-256 key for an opaque ID."""

    raw_id = _reject_unsafe_text(raw_id, label="raw_id")
    namespace = _reject_unsafe_text(namespace, label="namespace")
    return hashlib.sha256(
        namespace.encode("utf-8") + b"\0" + raw_id.encode("utf-8")
    ).hexdigest()


def validate_safe_key(value: str) -> str:
    if not isinstance(value, str) or not SAFE_KEY_RE.fullmatch(value):
        raise ContractPathError("path key must be 64 lowercase hexadecimal characters")
    return value


def _posix_confinement_ready() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )


def _require_confinement_platform() -> None:
    """Fail closed unless POSIX ``dir_fd`` / ``O_NOFOLLOW`` is available.

    Descriptor-relative ``*_at`` APIs stay POSIX-only. High-level helpers
    dispatch to Windows no-follow when this returns via ``_managed_store_backend``.
    """

    if not _posix_confinement_ready():
        raise ContractPathError(
            "fd-level managed-store APIs require POSIX dir_fd/O_NOFOLLOW"
        )


def _managed_store_backend() -> str:
    if _posix_confinement_ready():
        return "posix"
    if windows_nofollow_ready():
        return "windows"
    raise ContractPathError(
        "managed-store confinement requires POSIX dir_fd/O_NOFOLLOW or Windows no-follow"
    )


def _raise_win32(exc: Win32NofollowError, *, label: str, name: str) -> NoReturn:
    kind = exc.kind
    if kind == "symlink":
        raise ContractPathError(f"{label} may not be a symlink: {name}") from exc
    if kind == "exists":
        raise FileExistsError(name) from exc
    if kind == "missing":
        raise FileNotFoundError(name) from exc
    if kind == "links":
        raise ContractPathError(
            f"{label} must be a single-link regular file: {name}"
        ) from exc
    if kind == "not_regular":
        raise ContractPathError(f"{label} must be a regular file: {name}") from exc
    if kind == "size":
        raise ContractPathError(str(exc)) from exc
    if kind == "changed":
        raise ContractPathError(f"{label} changed while reading: {name}") from exc
    raise ContractPathError(str(exc)) from exc


def _win32_file_parts(path: Path) -> tuple[Path, list[str]]:
    path = path.absolute()
    name = _validate_component(path.name)
    base, components = _split_base_and_components(path.parent)
    return base, components + [name]


def _validate_component(part: str) -> str:
    _reject_unsafe_text(part, label="path component")
    if part in {".", ".."} or Path(part).name != part or "/" in part or "\\" in part:
        raise ContractPathError(f"unsafe path component: {part!r}")
    return part


def _is_nofollow_error(exc: OSError) -> bool:
    return exc.errno in _NOFOLLOW_ERRNOS or exc.errno == errno.ELOOP


def _open_dir_at(dir_fd: int | None, name: str) -> int:
    """Open an existing directory relative to *dir_fd* without following symlinks."""

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        if dir_fd is None:
            return os.open(name, flags)
        return os.open(name, flags, dir_fd=dir_fd)
    except OSError as exc:
        if _is_nofollow_error(exc) or exc.errno in {errno.ENOTDIR, errno.EEXIST}:
            raise ContractPathError(
                f"managed directory may not be a symlink or non-directory: {name}"
            ) from exc
        raise


def _mkdir_open_at(dir_fd: int, name: str, *, mode: int = MANAGED_DIR_MODE) -> int:
    """Create *name* under *dir_fd* if missing, then open it with O_NOFOLLOW.

    Mode is enforced only when this call creates the directory so existing
    intermediate components keep their prior permissions.
    """

    created = False
    try:
        os.mkdir(name, mode, dir_fd=dir_fd)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        if _is_nofollow_error(exc):
            raise ContractPathError(
                f"managed directory may not be a symlink: {name}"
            ) from exc
        raise
    child = _open_dir_at(dir_fd, name)
    try:
        st = os.fstat(child)
        if not stat.S_ISDIR(st.st_mode):
            raise ContractPathError(f"managed path is not a directory: {name}")
        if created:
            os.fchmod(child, mode)
    except Exception:
        os.close(child)
        raise
    return child


def _split_base_and_components(target: Path) -> tuple[Path, list[str]]:
    """Return (base_path, managed_components) for *target*.

    When the path contains ``.omg``, the base is everything above that marker
    (system path prefixes may contain symlinks such as ``/tmp`` → ``/private/tmp``).
    Components from ``.omg`` downward are traversed with no-follow semantics.

    Otherwise the deepest existing non-symlink ancestor is the base and the
    remaining missing names are managed components.
    """

    target = target.absolute()
    parts = target.parts
    if not parts:
        raise ContractPathError("empty managed path")

    if MANAGED_ROOT_MARKER in parts:
        # First ``.omg`` establishes the managed boundary so earlier managed
        # components (including nested markers under it) stay no-follow.
        # Existing intermediate directories are not re-chmod'd (see _mkdir_open_at).
        idx = parts.index(MANAGED_ROOT_MARKER)
        if idx == 0:
            raise ContractPathError("managed root marker cannot be filesystem root")
        base = Path(*parts[:idx])
        components = [_validate_component(p) for p in parts[idx:]]
        return base, components

    missing: list[str] = []
    current = target
    while True:
        try:
            exists = os.path.lexists(current)
        except OSError:
            exists = False
        if exists or current.parent == current:
            break
        missing.append(current.name)
        current = current.parent
    missing.reverse()
    if os.path.lexists(current) and current.is_symlink():
        raise ContractPathError(f"managed base may not be a symlink: {current}")
    if not os.path.lexists(current) and current.parent == current:
        # filesystem root as base
        return current, [_validate_component(p) for p in missing]
    if not missing:
        # entire path already exists — re-open leaf under parent with no-follow
        if current.parent == current:
            return current, []
        return current.parent, [_validate_component(current.name)]
    return current, [_validate_component(p) for p in missing]


def _open_base_dir(base: Path) -> int:
    """Open an existing base directory (system path; intermediate symlinks OK).

    The final component is opened with ``O_NOFOLLOW`` so a swap of the base
    directory itself to a symlink cannot redirect managed creation outside.
    """

    base = base.absolute()
    if not base.exists():
        # Create base path without no-follow on system prefixes (mkdir parents).
        # Only the final base directory is required to be a real directory.
        base.mkdir(parents=True, exist_ok=True)
    if base.is_symlink():
        raise ContractPathError(f"managed base may not be a symlink: {base}")
    if not base.is_dir():
        raise ContractPathError(f"managed base is not a directory: {base}")
    try:
        return os.open(
            base, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError as exc:
        if _is_nofollow_error(exc):
            raise ContractPathError(
                f"managed base may not be a symlink: {base}"
            ) from exc
        raise ContractPathError(f"cannot open managed base: {base}") from exc


def _walk_managed_dirs(base_fd: int, components: list[str], *, create: bool) -> int:
    """Walk *components* under *base_fd* with O_NOFOLLOW; return final dir fd.

    The returned descriptor is owned by the caller. Intermediate descriptors are
    closed. *base_fd* is not closed.
    """

    current = base_fd
    owns_current = False
    try:
        for name in components:
            if create:
                nxt = _mkdir_open_at(current, name)
            else:
                nxt = _open_dir_at(current, name)
            if owns_current:
                os.close(current)
            current = nxt
            owns_current = True
        if not owns_current:
            # No components: duplicate base fd so caller can always close.
            return os.dup(base_fd)
        owns_current = False
        return current
    finally:
        if owns_current:
            os.close(current)


def ensure_managed_dir(path: Path | str) -> Path:
    """Create *path* as a ``0700`` directory with no-follow managed components."""

    if _managed_store_backend() == "windows":
        target = Path(path).absolute()
        base, components = _split_base_and_components(target)
        try:
            ensure_relative_directories(base, components)
        except Win32NofollowError as exc:
            _raise_win32(exc, label="managed directory", name=target.name)
        return target
    target = Path(path).absolute()
    base, components = _split_base_and_components(target)
    base_fd = _open_base_dir(base)
    try:
        final_fd = _walk_managed_dirs(base_fd, components, create=True)
        try:
            os.fchmod(final_fd, MANAGED_DIR_MODE)
        finally:
            os.close(final_fd)
    finally:
        os.close(base_fd)
    return target


def _ensure_parent_dir_fd(path: Path) -> tuple[int, str]:
    """Ensure parent directory of *path*; return ``(parent_fd, basename)``."""

    path = path.absolute()
    name = _validate_component(path.name)
    parent = path.parent
    base, components = _split_base_and_components(parent)
    base_fd = _open_base_dir(base)
    try:
        parent_fd = _walk_managed_dirs(base_fd, components, create=True)
    finally:
        os.close(base_fd)
    return parent_fd, name


def _fsync_fd(descriptor: int) -> None:
    if os.name != "posix":  # pragma: no cover
        return
    os.fsync(descriptor)


def _reject_symlink_at(dir_fd: int, name: str, *, label: str) -> None:
    try:
        st = os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise ContractPathError(f"{label} may not be a symlink: {name}")


def confined_path(root: Path | str, *parts: str) -> Path:
    """Build a path below *root* while rejecting traversal and symlink parents."""

    root_path = Path(root).absolute()
    clean_parts = [_validate_component(part) for part in parts]
    if _managed_store_backend() == "windows":
        if root_path.is_symlink():
            raise ContractPathError(f"managed root may not be a symlink: {root_path}")
        candidate = root_path.joinpath(*clean_parts)
        try:
            candidate.relative_to(root_path)
        except ValueError as exc:  # pragma: no cover - guarded by component checks
            raise ContractPathError("candidate escapes managed root") from exc
        try:
            confined_relative_walk(root_path, clean_parts)
        except Win32NofollowError as exc:
            _raise_win32(
                exc,
                label="managed path",
                name=clean_parts[-1] if clean_parts else str(root_path),
            )
        return candidate
    if root_path.is_symlink():
        raise ContractPathError(f"managed root may not be a symlink: {root_path}")
    candidate = root_path.joinpath(*clean_parts)
    try:
        candidate.relative_to(root_path)
    except ValueError as exc:  # pragma: no cover - guarded by component checks
        raise ContractPathError("candidate escapes managed root") from exc
    # Descriptor walk from root for every existing component.
    base_fd = _open_base_dir(root_path)
    try:
        current = base_fd
        owns = False
        for name in clean_parts:
            child_path = (
                root_path.joinpath(name) if current is base_fd and not owns else None
            )
            # Only open components that already exist; confined_path does not create.
            try:
                st = os.lstat(name, dir_fd=current)
            except FileNotFoundError:
                break
            if stat.S_ISLNK(st.st_mode):
                raise ContractPathError(f"managed path contains symlink: {name}")
            if not stat.S_ISDIR(st.st_mode):
                break
            nxt = _open_dir_at(current, name)
            if owns:
                os.close(current)
            current = nxt
            owns = True
            _ = child_path
        if owns:
            os.close(current)
    finally:
        os.close(base_fd)
    return candidate


def atomic_write_bytes_at(
    parent_fd: int,
    name: str,
    body: bytes,
    *,
    mode: int = DATA_FILE_MODE,
    replace: bool = True,
) -> None:
    """Publish *name* under an already-open parent directory descriptor.

    Does not close *parent_fd*. Callers that also hold a lock under the same
    directory must use this so publication cannot re-resolve a swapped path.
    """

    _require_confinement_platform()
    name = _validate_component(name)
    temporary = f".{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    _reject_symlink_at(parent_fd, name, label="destination")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    try:
        descriptor = os.open(temporary, flags, mode, dir_fd=parent_fd)
    except OSError as exc:
        if _is_nofollow_error(exc):
            raise ContractPathError(
                f"temporary path may not be a symlink: {temporary}"
            ) from exc
        raise
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        tfd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            os.fchmod(tfd, mode)
        finally:
            os.close(tfd)
        if replace:
            _reject_symlink_at(parent_fd, name, label="destination")
            os.rename(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            dfd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                st = os.fstat(dfd)
                if not stat.S_ISREG(st.st_mode):
                    raise ContractPathError(
                        f"destination must be a regular file: {name}"
                    )
                os.fchmod(dfd, mode)
            finally:
                os.close(dfd)
        else:
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise
            except OSError as exc:
                if _is_nofollow_error(exc):
                    raise ContractPathError(
                        f"destination may not be a symlink: {name}"
                    ) from exc
                raise
            os.unlink(temporary, dir_fd=parent_fd)
        _fsync_fd(parent_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass


def atomic_write_bytes(
    path: Path | str,
    body: bytes,
    *,
    mode: int = DATA_FILE_MODE,
    replace: bool = True,
) -> Path:
    """Write bytes durably with an exact mode under a no-follow parent descriptor.

    ``replace=False`` publishes with a same-directory hard-link operation on
    POSIX (one atomic no-clobber decision in the kernel) and a no-replace
    rename on Windows.  Either backend refuses an already-present symlink or
    reparse instead of replacing or following it.
    """

    if _managed_store_backend() == "windows":
        destination = Path(path).absolute()
        base, parts = _win32_file_parts(destination)
        try:
            atomic_write_relative(base, parts, body, replace=replace)
        except Win32NofollowError as exc:
            _raise_win32(exc, label="destination", name=destination.name)
        return destination
    destination = Path(path).absolute()
    parent_fd, name = _ensure_parent_dir_fd(destination)
    try:
        atomic_write_bytes_at(parent_fd, name, body, mode=mode, replace=replace)
    finally:
        os.close(parent_fd)
    return destination


def open_managed_dir_fd(path: Path | str) -> int:
    """Open *path* as a managed directory and return an owned dir fd.

    Creates missing managed components with no-follow semantics. Caller must
    ``os.close`` the returned descriptor.
    """

    _require_confinement_platform()
    target = Path(path).absolute()
    ensure_managed_dir(target)
    base, components = _split_base_and_components(target)
    base_fd = _open_base_dir(base)
    try:
        return _walk_managed_dirs(base_fd, components, create=True)
    finally:
        os.close(base_fd)


def open_existing_managed_dir_fd(path: Path | str) -> int:
    """Open an existing managed directory without creating missing components."""
    _require_confinement_platform()
    target = Path(path).absolute()
    base, components = _split_base_and_components(target)
    base_fd = _open_base_dir(base)
    try:
        return _walk_managed_dirs(base_fd, components, create=False)
    finally:
        os.close(base_fd)


def fsync_existing_managed_dir(path: Path | str) -> None:
    """Retry durability for an existing managed directory through a pinned fd."""
    descriptor = open_existing_managed_dir_fd(path)
    try:
        _fsync_fd(descriptor)
    finally:
        os.close(descriptor)


def read_managed_regular_bytes(
    path: Path | str,
    *,
    max_bytes: int = MAX_MANAGED_READ_BYTES,
    required_mode: int | None = None,
) -> bytes:
    """Read one bounded regular, single-link file through a pinned parent fd.

    When ``required_mode`` is set, the same opened inode must have that
    exact permission mode both before and after the read. Callers must
    not reopen the path to learn mode or digest.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative integer")
    if required_mode is not None and (
        isinstance(required_mode, bool) or not isinstance(required_mode, int)
    ):
        raise ValueError("required_mode must be an integer or None")
    if _managed_store_backend() == "windows":
        source = Path(path).absolute()
        name = _validate_component(source.name)
        base, parts = _win32_file_parts(source)
        try:
            body, mode = read_relative_regular(base, parts, max_bytes=max_bytes)
        except Win32NofollowError as exc:
            _raise_win32(exc, label="managed file", name=name)
        if required_mode is not None and mode != required_mode:
            raise ContractPathError(
                f"managed file mode must be {required_mode:04o}, got {mode:04o}"
            )
        return body
    source = Path(path).absolute()
    name = _validate_component(source.name)
    parent_fd = open_existing_managed_dir_fd(source.parent)
    try:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            if _is_nofollow_error(exc):
                raise ContractPathError(
                    f"managed file may not be a symlink: {name}"
                ) from exc
            raise
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise ContractPathError(
                    f"managed file must be a single-link regular file: {name}"
                )
            if (
                required_mode is not None
                and stat.S_IMODE(before.st_mode) != required_mode
            ):
                raise ContractPathError(
                    f"managed file mode must be {required_mode:04o}, "
                    f"got {stat.S_IMODE(before.st_mode):04o}"
                )
            if before.st_size > max_bytes:
                raise ContractPathError(
                    f"managed file exceeds {max_bytes} byte read limit: {name}"
                )
            body = handle.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise ContractPathError(
                    f"managed file exceeds {max_bytes} byte read limit: {name}"
                )
            after = os.fstat(handle.fileno())
            if (
                before.st_dev,
                before.st_ino,
                before.st_nlink,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            ) != (
                after.st_dev,
                after.st_ino,
                after.st_nlink,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or after.st_size != len(body):
                raise ContractPathError(f"managed file changed while reading: {name}")
            if (
                required_mode is not None
                and stat.S_IMODE(after.st_mode) != required_mode
            ):
                raise ContractPathError(
                    f"managed file mode must be {required_mode:04o}, "
                    f"got {stat.S_IMODE(after.st_mode):04o}"
                )
        return body
    finally:
        os.close(parent_fd)


def _open_lock_descriptor_at(parent_fd: int, name: str) -> int:
    """Open/create a regular lock file under *parent_fd* with O_NOFOLLOW only.

    Never falls back to absolute path resolution (intermediate symlink escape).
    Retries a few times on descriptor-relative races (ENOENT after unlink).
    """

    name = _validate_component(name)
    _reject_symlink_at(parent_fd, name, label="lock file")
    last_exc: OSError | None = None
    for _ in range(4):
        try:
            return os.open(
                name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                DATA_FILE_MODE,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            try:
                return os.open(name, os.O_RDWR | os.O_NOFOLLOW, dir_fd=parent_fd)
            except OSError as exc:
                if _is_nofollow_error(exc):
                    raise ContractPathError(
                        f"lock file may not be a symlink: {name}"
                    ) from exc
                if exc.errno == errno.ENOENT:
                    last_exc = exc
                    continue
                raise
        except OSError as exc:
            if _is_nofollow_error(exc):
                raise ContractPathError(
                    f"lock file may not be a symlink: {name}"
                ) from exc
            if exc.errno == errno.ENOENT:
                # Parent gone or race — fail closed after retries; never path-open.
                last_exc = exc
                continue
            raise
    raise ContractPathError(
        f"unable to open lock under managed parent descriptor: {name}"
    ) from last_exc


@contextmanager
def exclusive_lock_at(parent_fd: int, name: str) -> Iterator[None]:
    """Hold a lock on *name* relative to an already-open parent directory fd.

    Does not close *parent_fd*. Callers that also write journal/data files must
    use this so lock and mutation share one pinned directory inode.
    """

    _require_confinement_platform()
    if fcntl is None:  # pragma: no cover
        raise RuntimeError("reliable POSIX advisory locking is unavailable")
    descriptor = _open_lock_descriptor_at(parent_fd, name)
    try:
        st = os.fstat(descriptor)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise ContractPathError(
                f"lock file must be a unique regular file: {name} "
                f"(nlink={getattr(st, 'st_nlink', '?')})"
            )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        st = os.fstat(descriptor)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise ContractPathError(
                f"lock file must be a unique regular file under flock: {name} (nlink={st.st_nlink})"
            )
        os.fchmod(descriptor, DATA_FILE_MODE)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


@contextmanager
def exclusive_lock(path: Path | str) -> Iterator[None]:
    """Hold an advisory lock without following a lock-file symlink."""

    if _managed_store_backend() == "windows":
        lock_path = Path(path).absolute()
        base, parts = _win32_file_parts(lock_path)
        try:
            with exclusive_lock_relative(base, parts):
                yield
        except Win32NofollowError as exc:
            _raise_win32(exc, label="lock file", name=lock_path.name)
        return
    lock_path = Path(path).absolute()
    parent_fd, name = _ensure_parent_dir_fd(lock_path)
    try:
        with exclusive_lock_at(parent_fd, name):
            yield
    finally:
        os.close(parent_fd)


def append_locked_jsonl(path: Path | str, canonical_record: bytes) -> None:
    """Append one complete canonical record with one ``O_APPEND`` write."""

    if b"\n" in canonical_record or not canonical_record:
        raise ValueError("canonical JSONL record must be one non-empty physical line")
    if _managed_store_backend() == "windows":
        destination = Path(path).absolute()
        base, parts = _win32_file_parts(destination)
        try:
            append_locked_relative_jsonl(base, parts, canonical_record)
        except Win32NofollowError as exc:
            _raise_win32(exc, label="journal", name=destination.name)
        return
    destination = Path(path).absolute()
    parent_fd, name = _ensure_parent_dir_fd(destination)
    lock_name = name + ".lock"
    try:
        _reject_symlink_at(parent_fd, name, label="journal")
        with exclusive_lock_at(parent_fd, lock_name):
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                    DATA_FILE_MODE,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                if _is_nofollow_error(exc):
                    raise ContractPathError(
                        f"journal may not be a symlink: {name}"
                    ) from exc
                raise
            try:
                st = os.fstat(descriptor)
                if not stat.S_ISREG(st.st_mode):
                    raise ContractPathError(f"journal must be a regular file: {name}")
                if st.st_nlink != 1:
                    raise ContractPathError(
                        f"journal must not be hard-linked: {name} (nlink={st.st_nlink})"
                    )
                os.fchmod(descriptor, DATA_FILE_MODE)
                payload = canonical_record + b"\n"
                written = os.write(descriptor, payload)
                if written != len(payload):  # pragma: no cover
                    raise OSError("short O_APPEND journal write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_fd(parent_fd)
    finally:
        os.close(parent_fd)


def append_locked_jsonl_once(
    path: Path | str,
    canonical_record: bytes,
    *,
    identity: str,
    identity_from_record: Callable[[bytes], str],
) -> bool:
    """Append once by identity under the journal lock.

    An exact byte replay is idempotent.  Reusing an identity for different
    canonical bytes is a collision and fails without mutating the journal.
    """

    _require_confinement_platform()
    if b"\n" in canonical_record or not canonical_record:
        raise ValueError("canonical JSONL record must be one non-empty physical line")
    destination = Path(path).absolute()
    parent_fd, name = _ensure_parent_dir_fd(destination)
    lock_name = name + ".lock"
    try:
        _reject_symlink_at(parent_fd, name, label="journal")
        with exclusive_lock_at(parent_fd, lock_name):
            # Read existing lines via no-follow open when present.
            try:
                existing_fd = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd
                )
            except FileNotFoundError:
                existing_fd = None
            except OSError as exc:
                if _is_nofollow_error(exc):
                    raise ContractPathError(
                        f"journal may not be a symlink: {name}"
                    ) from exc
                raise
            if existing_fd is not None:
                try:
                    with os.fdopen(existing_fd, "rb", closefd=True) as handle:
                        for raw_line in handle:
                            if not raw_line.endswith(b"\n"):
                                raise ValueError(
                                    "journal has an incomplete physical line"
                                )
                            existing = raw_line[:-1]
                            if identity_from_record(existing) != identity:
                                continue
                            if existing == canonical_record:
                                return False
                            raise ValueError("journal identity collision")
                except Exception:
                    # existing_fd closed by fdopen on success; on error before fdopen
                    # we already transferred ownership — if fdopen failed, close.
                    raise
            try:
                descriptor = os.open(
                    name,
                    os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                    DATA_FILE_MODE,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                if _is_nofollow_error(exc):
                    raise ContractPathError(
                        f"journal may not be a symlink: {name}"
                    ) from exc
                raise
            try:
                st = os.fstat(descriptor)
                if not stat.S_ISREG(st.st_mode):
                    raise ContractPathError(f"journal must be a regular file: {name}")
                if st.st_nlink != 1:
                    raise ContractPathError(
                        f"journal must not be hard-linked: {name} (nlink={st.st_nlink})"
                    )
                os.fchmod(descriptor, DATA_FILE_MODE)
                payload = canonical_record + b"\n"
                written = os.write(descriptor, payload)
                if written != len(payload):  # pragma: no cover
                    raise OSError("short O_APPEND journal write")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_fd(parent_fd)
    finally:
        os.close(parent_fd)
    return True


def mode_bits(path: Path | str) -> int:
    return stat.S_IMODE(Path(path).stat().st_mode)
