"""Confined atomic apply for a hash-edit plan.

Re-reads the target under existing managed-path primitives and a same-directory
lock. Failures leave the file bytes unchanged. The result is copy-safe: digests
and relative path only — never raw source, replacement, or local absolute paths.
"""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

try:
    import fcntl
except ImportError:  # pragma: no cover - OMG is POSIX
    fcntl = None  # type: ignore[assignment]

from omg_cli.contracts.path_keys import (
    ContractPathError,
    atomic_write_bytes_at,
    confined_path,
)
from omg_cli.contracts.state_schemas import require_integer, require_safe_id, require_sha256

from .descriptor import (
    HASH_EDIT_SCHEMA_VERSION,
    HashEditDescriptorV1,
    parse_hash_edit_descriptor,
    require_workspace_relpath,
)
from .errors import (
    HashEditApplyError,
    HashEditConcurrencyError,
    HashEditDescriptorError,
    HashEditInputError,
    HashEditPathError,
)
from .planner import (
    MAX_PLAN_FILE_BYTES,
    HashEditCurrentFact,
    HashEditPlanV1,
    plan_hash_edit,
)

APPLY_RESULT_KIND: Final[str] = "omg.hash_edit.apply_result.v1"
VERIFY_RESULT_KIND: Final[str] = "omg.hash_edit.verify.v1"
_NOFOLLOW_ERRNOS = {errno.ELOOP, getattr(errno, "EMLINK", -1), errno.EINVAL}


def _sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _require_plan(plan: HashEditPlanV1) -> HashEditPlanV1:
    if not isinstance(plan, HashEditPlanV1):
        raise HashEditApplyError("plan must be a HashEditPlanV1")
    # Re-run dataclass checks via a new instance so a mutated object cannot pass.
    return HashEditPlanV1(
        descriptor_digest=plan.descriptor_digest,
        path=plan.path,
        before_sha256=plan.before_sha256,
        after_sha256=plan.after_sha256,
        start_offset=plan.start_offset,
        end_offset=plan.end_offset,
        start_line=plan.start_line,
        end_line=plan.end_line,
        rebased=plan.rebased,
        unified_diff=plan.unified_diff,
        unified_diff_sha256=plan.unified_diff_sha256,
    )


def _require_descriptor(
    descriptor: HashEditDescriptorV1 | Mapping[str, Any] | bytes | str,
) -> HashEditDescriptorV1:
    if isinstance(descriptor, HashEditDescriptorV1):
        return parse_hash_edit_descriptor(descriptor.to_canonical_mapping())
    try:
        return parse_hash_edit_descriptor(descriptor)
    except HashEditDescriptorError:
        raise
    except Exception as exc:
        raise HashEditApplyError(f"descriptor is not usable: {exc}") from exc


def _workspace_root(root: Path | str) -> Path:
    if not isinstance(root, (Path, str)) or (isinstance(root, str) and not root):
        raise HashEditPathError("workspace root must be a non-empty path")
    path = Path(root)
    try:
        path = path.absolute()
    except OSError as exc:
        raise HashEditPathError("workspace root is not usable") from exc
    if path.is_symlink():
        raise HashEditPathError("workspace root may not be a symlink")
    if not path.is_dir():
        raise HashEditPathError("workspace root must be a directory")
    return path


def _confined_target(root: Path, relative: str) -> Path:
    try:
        rel = require_workspace_relpath(relative, label="apply path")
        parts = rel.split("/")
        return confined_path(root, *parts)
    except (HashEditDescriptorError, ContractPathError) as exc:
        raise HashEditPathError(str(exc)) from exc


def _open_workspace_root_fd(root: Path) -> int:
    try:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        raise HashEditPathError("cannot open workspace root") from exc


def _open_parent_fd(root_fd: int, parts: list[str]) -> tuple[int, str]:
    """Walk *parts* from a pinned root fd; return ``(parent_fd, basename)``."""

    if not parts:
        raise HashEditPathError("apply path is empty")
    name = parts[-1]
    current = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            try:
                nxt = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
            except OSError as exc:
                if exc.errno in _NOFOLLOW_ERRNOS or exc.errno == errno.ELOOP:
                    raise HashEditPathError("path contains a symlink") from exc
                raise HashEditPathError("cannot walk confined parent") from exc
            os.close(current)
            current = nxt
        return current, name
    except Exception:
        os.close(current)
        raise


def _posix_nofollow_ready() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and hasattr(os, "O_DIRECTORY")
    )


def read_confined_regular_file(workspace_root: Path | str, relative: str) -> bytes:
    """Read a workspace-relative regular file without following a swapped symlink.

    POSIX: pinned ``O_NOFOLLOW`` directory descriptors (same walk as apply).
    Other hosts fail closed — there is no no-follow ``dir_fd`` walk to pin the
    target, so a pathname reopen would race. Size is inspected on the pinned
    descriptor before allocating the full contents.
    """

    rel = require_workspace_relpath(relative, label="edit path")
    if not _posix_nofollow_ready():
        raise HashEditPathError(
            "confined target read requires POSIX O_NOFOLLOW/dir_fd"
        )
    root = _workspace_root(workspace_root)
    parts = rel.split("/")
    _confined_target(root, rel)
    root_fd = _open_workspace_root_fd(root)
    try:
        parent_fd, name = _open_parent_fd(root_fd, parts)
        try:
            body, _mode = _read_regular_at(parent_fd, name)
            return body
        finally:
            os.close(parent_fd)
    finally:
        os.close(root_fd)


def _read_fd_until(descriptor: int, *, limit: int) -> bytes:
    """Accumulate ``os.read`` from a pinned fd until *limit* bytes or EOF.

    A single ``os.read`` may return short of the request; comparing that
    partial length to ``st_size`` would false-fire concurrency errors.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise HashEditApplyError("read limit must be a non-negative integer")
    chunks: list[bytes] = []
    remaining = limit
    while remaining:
        chunk = os.read(descriptor, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_regular_at(parent_fd: int, name: str) -> tuple[bytes, int]:
    try:
        probed = os.lstat(name, dir_fd=parent_fd)
    except FileNotFoundError as exc:
        raise HashEditPathError(f"target does not exist: {name}") from exc
    except OSError as exc:
        raise HashEditPathError(f"cannot lstat target: {name}") from exc
    if stat.S_ISLNK(probed.st_mode):
        raise HashEditPathError(f"target may not be a symlink: {name}")
    if (
        stat.S_ISFIFO(probed.st_mode)
        or stat.S_ISCHR(probed.st_mode)
        or stat.S_ISBLK(probed.st_mode)
        or stat.S_ISSOCK(probed.st_mode)
    ):
        raise HashEditPathError(
            f"target must be a regular file (not fifo/device/socket): {name}"
        )
    if not stat.S_ISREG(probed.st_mode):
        raise HashEditPathError(f"target must be a regular file: {name}")
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in _NOFOLLOW_ERRNOS or exc.errno == errno.ELOOP:
            raise HashEditPathError(f"target may not be a symlink: {name}") from exc
        if exc.errno == errno.ENOENT:
            raise HashEditPathError(f"target does not exist: {name}") from exc
        raise HashEditPathError(f"cannot open target: {name}") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise HashEditPathError(f"target must be a regular file: {name}")
        if before.st_nlink != 1:
            raise HashEditPathError(
                f"target must be a single-link regular file: {name} (nlink={before.st_nlink})"
            )
        if before.st_size > MAX_PLAN_FILE_BYTES:
            raise HashEditInputError(
                f"current bytes exceed {MAX_PLAN_FILE_BYTES} byte limit"
            )
        body = _read_fd_until(
            descriptor,
            limit=min(int(before.st_size) + 1, MAX_PLAN_FILE_BYTES + 1),
        )
        if len(body) > MAX_PLAN_FILE_BYTES:
            raise HashEditInputError(
                f"current bytes exceed {MAX_PLAN_FILE_BYTES} byte limit"
            )
        after = os.fstat(descriptor)
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
            raise HashEditConcurrencyError(f"target changed while reading: {name}")
        return body, stat.S_IMODE(before.st_mode)
    finally:
        os.close(descriptor)


@dataclass(frozen=True, slots=True)
class HashEditApplyResultV1:
    """Copy-safe apply evidence. Constructed only after a successful apply."""

    path: str
    descriptor_digest: str
    before_sha256: str
    after_sha256: str
    start_offset: int
    end_offset: int
    start_line: int
    end_line: int
    rebased: bool
    unified_diff_sha256: str
    preserved_mode: int

    def __post_init__(self) -> None:
        try:
            require_workspace_relpath(self.path, label="result path")
            require_sha256(self.descriptor_digest, label="descriptor_digest")
            require_sha256(self.before_sha256, label="before_sha256")
            require_sha256(self.after_sha256, label="after_sha256")
            require_sha256(self.unified_diff_sha256, label="unified_diff_sha256")
            require_integer(self.start_offset, label="start_offset", minimum=0)
            require_integer(self.end_offset, label="end_offset", minimum=0)
            require_integer(self.start_line, label="start_line", minimum=1)
            require_integer(self.end_line, label="end_line", minimum=1)
            require_integer(self.preserved_mode, label="preserved_mode", minimum=0)
        except Exception as exc:
            raise HashEditApplyError(str(exc)) from exc
        if self.end_offset < self.start_offset:
            raise HashEditApplyError("byte offsets must satisfy start <= end")
        if self.end_line < self.start_line:
            raise HashEditApplyError("line range must be ordered")
        if not isinstance(self.rebased, bool):
            raise HashEditApplyError("rebased must be a bool")
        if self.preserved_mode > 0o7777:
            raise HashEditApplyError("preserved_mode is not a permission mask")

    @property
    def kind(self) -> str:
        return APPLY_RESULT_KIND

    @property
    def schema_version(self) -> int:
        return HASH_EDIT_SCHEMA_VERSION

    @property
    def ok(self) -> bool:
        return True


def apply_hash_edit(
    workspace_root: Path | str,
    descriptor: HashEditDescriptorV1 | Mapping[str, Any] | bytes | str,
    plan: HashEditPlanV1,
) -> HashEditApplyResultV1:
    """Apply *plan* under *workspace_root* or raise without mutating the file.

    Re-reads and re-plans immediately under a same-directory lock. Concurrent
    digest mismatch is not rewritten as a new unique_shift apply. The result
    never includes raw source, replacement, unified-diff text, or absolute paths.

    Mode contract: the existing file's ``stat.S_IMODE`` permission bits
    (including execute bits) are passed through to the atomic replace.
    """

    desc = _require_descriptor(descriptor)
    locked_plan = _require_plan(plan)
    if locked_plan.path != desc.path or locked_plan.descriptor_digest != desc.digest():
        raise HashEditApplyError("plan is not bound to this descriptor")
    root = _workspace_root(workspace_root)
    _confined_target(root, desc.path)
    parts = desc.path.split("/")
    if fcntl is None:  # pragma: no cover
        raise HashEditApplyError("advisory locking is unavailable")
    root_fd = _open_workspace_root_fd(root)
    try:
        parent_fd, name = _open_parent_fd(root_fd, parts)
    except Exception:
        os.close(root_fd)
        raise
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        try:
            current, preserved_mode = _read_regular_at(parent_fd, name)
            fresh_digest = _sha256_bytes(current)
            if fresh_digest != locked_plan.before_sha256:
                raise HashEditConcurrencyError(
                    "current file digest does not match plan.before_sha256"
                )

            fresh_plan = plan_hash_edit(
                desc,
                HashEditCurrentFact(path=desc.path, current_bytes=current),
            )
            if (
                fresh_plan.before_sha256 != locked_plan.before_sha256
                or fresh_plan.after_sha256 != locked_plan.after_sha256
                or fresh_plan.start_offset != locked_plan.start_offset
                or fresh_plan.end_offset != locked_plan.end_offset
                or fresh_plan.descriptor_digest != locked_plan.descriptor_digest
            ):
                raise HashEditConcurrencyError("re-plan does not match the supplied plan")

            spliced = (
                current[: fresh_plan.start_offset]
                + desc.replacement.encode("utf-8")
                + current[fresh_plan.end_offset :]
            )
            if len(spliced) > MAX_PLAN_FILE_BYTES:
                raise HashEditApplyError(
                    f"planned bytes exceed {MAX_PLAN_FILE_BYTES} byte limit"
                )
            if _sha256_bytes(spliced) != locked_plan.after_sha256:
                raise HashEditApplyError("spliced bytes do not match plan.after_sha256")

            result = HashEditApplyResultV1(
                path=desc.path,
                descriptor_digest=desc.digest(),
                before_sha256=fresh_digest,
                after_sha256=_sha256_bytes(spliced),
                start_offset=fresh_plan.start_offset,
                end_offset=fresh_plan.end_offset,
                start_line=fresh_plan.start_line,
                end_line=fresh_plan.end_line,
                rebased=fresh_plan.rebased,
                unified_diff_sha256=fresh_plan.unified_diff_sha256,
                preserved_mode=preserved_mode,
            )
            if spliced == current:
                return result

            try:
                atomic_write_bytes_at(
                    parent_fd,
                    name,
                    spliced,
                    mode=preserved_mode,
                    replace=True,
                )
            except ContractPathError as exc:
                raise HashEditPathError(str(exc)) from exc
            except OSError as exc:
                raise HashEditApplyError("atomic replace failed") from exc

            published, published_mode = _read_regular_at(parent_fd, name)
            if published != spliced or published_mode != preserved_mode:
                raise HashEditApplyError("post-replace readback mismatch")
            return result
        finally:
            fcntl.flock(parent_fd, fcntl.LOCK_UN)
    finally:
        os.close(parent_fd)
        os.close(root_fd)


def write_confined_regular_file(
    workspace_root: Path | str,
    relative: str,
    body: bytes,
    *,
    mode: int | None = None,
) -> int:
    """Replace a workspace-relative regular file without following a swapped symlink.

    Same pinned ``O_NOFOLLOW`` walk + parent-dir flock as apply. Used to restore
    original bytes after a later hash-edit in the same invocation fails.
    Returns the permission mask written. Fail-closed on hosts without
    ``O_NOFOLLOW`` / ``dir_fd``.
    """

    if not isinstance(body, (bytes, bytearray)):
        raise HashEditApplyError("restore body must be bytes")
    payload = bytes(body)
    if len(payload) > MAX_PLAN_FILE_BYTES:
        raise HashEditInputError(
            f"restore bytes exceed {MAX_PLAN_FILE_BYTES} byte limit"
        )
    if mode is not None:
        if isinstance(mode, bool) or not isinstance(mode, int):
            raise HashEditApplyError("mode must be an integer")
        if mode < 0 or mode > 0o7777:
            raise HashEditApplyError("mode is not a permission mask")
    rel = require_workspace_relpath(relative, label="edit path")
    if not _posix_nofollow_ready():
        raise HashEditPathError(
            "confined target write requires POSIX O_NOFOLLOW/dir_fd"
        )
    if fcntl is None:  # pragma: no cover
        raise HashEditApplyError("advisory locking is unavailable")
    root = _workspace_root(workspace_root)
    parts = rel.split("/")
    _confined_target(root, rel)
    root_fd = _open_workspace_root_fd(root)
    try:
        parent_fd, name = _open_parent_fd(root_fd, parts)
    except Exception:
        os.close(root_fd)
        raise
    try:
        fcntl.flock(parent_fd, fcntl.LOCK_EX)
        try:
            _current, preserved_mode = _read_regular_at(parent_fd, name)
            applied_mode = preserved_mode if mode is None else mode
            if _current == payload and preserved_mode == applied_mode:
                return applied_mode
            try:
                atomic_write_bytes_at(
                    parent_fd,
                    name,
                    payload,
                    mode=applied_mode,
                    replace=True,
                )
            except ContractPathError as exc:
                raise HashEditPathError(str(exc)) from exc
            except OSError as exc:
                raise HashEditApplyError("atomic replace failed") from exc
            published, published_mode = _read_regular_at(parent_fd, name)
            if published != payload or published_mode != applied_mode:
                raise HashEditApplyError("post-replace readback mismatch")
            return applied_mode
        finally:
            fcntl.flock(parent_fd, fcntl.LOCK_UN)
    finally:
        os.close(parent_fd)
        os.close(root_fd)


@dataclass(frozen=True, slots=True)
class HashEditVerifyResultV1:
    """Copy-safe verify evidence. Never a ``verified`` / ``passes`` stamp."""

    path: str
    edit_id: str
    descriptor_digest: str
    before_sha256: str
    after_sha256: str
    start_offset: int
    end_offset: int
    start_line: int
    end_line: int
    rebased: bool
    unified_diff_sha256: str

    def __post_init__(self) -> None:
        try:
            require_workspace_relpath(self.path, label="result path")
            require_safe_id(self.edit_id, label="edit_id")
            require_sha256(self.descriptor_digest, label="descriptor_digest")
            require_sha256(self.before_sha256, label="before_sha256")
            require_sha256(self.after_sha256, label="after_sha256")
            require_sha256(self.unified_diff_sha256, label="unified_diff_sha256")
            require_integer(self.start_offset, label="start_offset", minimum=0)
            require_integer(self.end_offset, label="end_offset", minimum=0)
            require_integer(self.start_line, label="start_line", minimum=1)
            require_integer(self.end_line, label="end_line", minimum=1)
        except Exception as exc:
            raise HashEditApplyError(str(exc)) from exc
        if self.end_offset < self.start_offset:
            raise HashEditApplyError("byte offsets must satisfy start <= end")
        if self.end_line < self.start_line:
            raise HashEditApplyError("line range must be ordered")
        if not isinstance(self.rebased, bool):
            raise HashEditApplyError("rebased must be a bool")

    @property
    def kind(self) -> str:
        return VERIFY_RESULT_KIND

    @property
    def schema_version(self) -> int:
        return HASH_EDIT_SCHEMA_VERSION

    @property
    def status(self) -> str:
        return "ok"


def verify_hash_edit(
    workspace_root: Path | str,
    descriptor: HashEditDescriptorV1 | Mapping[str, Any] | bytes | str,
) -> HashEditVerifyResultV1:
    """Re-read and re-plan *descriptor*. Never writes the target file.

    Same ``O_NOFOLLOW`` confinement as apply. Stale / ambiguous / path
    failures raise the planner/apply errors; callers must not treat this
    as an OMG ``verified`` stamp.
    """

    desc = _require_descriptor(descriptor)
    current = read_confined_regular_file(workspace_root, desc.path)
    plan = plan_hash_edit(
        desc,
        HashEditCurrentFact(path=desc.path, current_bytes=current),
    )
    return HashEditVerifyResultV1(
        path=desc.path,
        edit_id=desc.edit_id,
        descriptor_digest=plan.descriptor_digest,
        before_sha256=plan.before_sha256,
        after_sha256=plan.after_sha256,
        start_offset=plan.start_offset,
        end_offset=plan.end_offset,
        start_line=plan.start_line,
        end_line=plan.end_line,
        rebased=plan.rebased,
        unified_diff_sha256=plan.unified_diff_sha256,
    )
