"""Safe path keys and durable local-store primitives.

Raw host/run identifiers never become path components.  Callers use a SHA-256
key, then confine the resulting path beneath a non-symlink managed root.
Canonical JSON persistence is intentionally implemented here rather than by a
third-party dependency so the byte and permission contract stays reviewable.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - OMG is supported on POSIX hosts
    fcntl = None  # type: ignore[assignment]


MANAGED_DIR_MODE = 0o700
DATA_FILE_MODE = 0o600
IMMUTABLE_SOURCE_MODE = 0o400
EXECUTABLE_MODE = 0o700
SAFE_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


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


def _assert_no_symlink_components(root: Path, candidate: Path) -> None:
    current = root
    if current.is_symlink():
        raise ContractPathError(f"managed root may not be a symlink: {current}")
    relative = candidate.relative_to(root)
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ContractPathError(f"managed path contains symlink: {current}")


def confined_path(root: Path | str, *parts: str) -> Path:
    """Build a path below *root* while rejecting traversal and symlink parents."""

    root_path = Path(root).absolute()
    clean_parts: list[str] = []
    for part in parts:
        _reject_unsafe_text(part, label="path component")
        if part in {".", ".."} or Path(part).name != part or "/" in part or "\\" in part:
            raise ContractPathError(f"unsafe path component: {part!r}")
        clean_parts.append(part)
    candidate = root_path.joinpath(*clean_parts)
    try:
        candidate.relative_to(root_path)
    except ValueError as exc:  # pragma: no cover - guarded by component checks
        raise ContractPathError("candidate escapes managed root") from exc
    _assert_no_symlink_components(root_path, candidate)
    return candidate


def ensure_managed_dir(path: Path | str) -> Path:
    directory = Path(path)
    if directory.exists() and directory.is_symlink():
        raise ContractPathError(f"managed directory may not be a symlink: {directory}")
    directory.mkdir(parents=True, exist_ok=True, mode=MANAGED_DIR_MODE)
    os.chmod(directory, MANAGED_DIR_MODE)
    return directory


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":  # pragma: no cover
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(
    path: Path | str,
    body: bytes,
    *,
    mode: int = DATA_FILE_MODE,
    replace: bool = True,
) -> Path:
    """Write bytes durably with an exact mode.

    ``replace=False`` publishes with a same-filesystem hard-link operation.
    Unlike a preflight ``exists()`` check followed by ``os.replace()``, link
    creation is one atomic no-clobber decision in the kernel.  It also refuses
    an already-present symlink instead of replacing or following it.
    """

    destination = Path(path)
    parent = ensure_managed_dir(destination.parent)
    if destination.is_symlink():
        raise ContractPathError(f"destination may not be a symlink: {destination}")
    temporary = parent / f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        if replace:
            if destination.is_symlink():
                raise ContractPathError(
                    f"destination may not be a symlink: {destination}"
                )
            os.replace(temporary, destination)
            os.chmod(destination, mode)
        else:
            # POSIX link(2) is atomic and never replaces an existing directory
            # entry.  ``follow_symlinks=False`` documents and enforces that the
            # source is the temporary regular file itself.
            os.link(temporary, destination, follow_symlinks=False)
            temporary.unlink()
        _fsync_directory(parent)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
    return destination


@contextmanager
def exclusive_lock(path: Path | str) -> Iterator[None]:
    """Hold a POSIX advisory lock without exposing lock-file contents."""

    if fcntl is None:  # pragma: no cover
        raise RuntimeError("reliable POSIX advisory locking is unavailable")
    lock_path = Path(path)
    ensure_managed_dir(lock_path.parent)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, DATA_FILE_MODE)
    try:
        os.fchmod(descriptor, DATA_FILE_MODE)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def append_locked_jsonl(path: Path | str, canonical_record: bytes) -> None:
    """Append one complete canonical record with one ``O_APPEND`` write."""

    if b"\n" in canonical_record or not canonical_record:
        raise ValueError("canonical JSONL record must be one non-empty physical line")
    destination = Path(path)
    ensure_managed_dir(destination.parent)
    if destination.is_symlink():
        raise ContractPathError(f"journal may not be a symlink: {destination}")
    lock_path = destination.with_name(destination.name + ".lock")
    with exclusive_lock(lock_path):
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            DATA_FILE_MODE,
        )
        try:
            os.fchmod(descriptor, DATA_FILE_MODE)
            payload = canonical_record + b"\n"
            written = os.write(descriptor, payload)
            if written != len(payload):  # pragma: no cover - regular files are atomic here
                raise OSError("short O_APPEND journal write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(destination.parent)


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

    if b"\n" in canonical_record or not canonical_record:
        raise ValueError("canonical JSONL record must be one non-empty physical line")
    destination = Path(path)
    ensure_managed_dir(destination.parent)
    if destination.is_symlink():
        raise ContractPathError(f"journal may not be a symlink: {destination}")
    lock_path = destination.with_name(destination.name + ".lock")
    with exclusive_lock(lock_path):
        if destination.exists():
            with destination.open("rb") as handle:
                for raw_line in handle:
                    if not raw_line.endswith(b"\n"):
                        raise ValueError("journal has an incomplete physical line")
                    existing = raw_line[:-1]
                    if identity_from_record(existing) != identity:
                        continue
                    if existing == canonical_record:
                        return False
                    raise ValueError("journal identity collision")
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            DATA_FILE_MODE,
        )
        try:
            os.fchmod(descriptor, DATA_FILE_MODE)
            payload = canonical_record + b"\n"
            written = os.write(descriptor, payload)
            if written != len(payload):  # pragma: no cover - regular files are atomic here
                raise OSError("short O_APPEND journal write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(destination.parent)
    return True


def mode_bits(path: Path | str) -> int:
    return stat.S_IMODE(Path(path).stat().st_mode)
