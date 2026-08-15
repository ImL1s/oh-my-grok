"""Windows-safe workspace-relative file reads (no POSIX dir_fd required)."""

from __future__ import annotations

from pathlib import Path

from omg_cli.hash_edit.descriptor import HashEditDescriptorError, require_workspace_relpath

MAX_HYGIENE_FILE_BYTES = 1_048_576


class WorkspacePathError(ValueError):
    """Relative path escaped the workspace or is not a regular file."""

    code = "E_EDIT_PATH"


def posix_relpath(raw: str) -> str:
    """Normalize a caller path to a canonical workspace-relative POSIX path."""

    text = str(raw).strip().replace("\\", "/")
    if not text:
        raise WorkspacePathError("path must be a non-empty workspace-relative path")
    try:
        return require_workspace_relpath(text)
    except HashEditDescriptorError as exc:
        raise WorkspacePathError(str(exc)) from exc


def relativize_to_root(root: Path, raw: str) -> str:
    """Map an absolute-or-relative path to a workspace-relative POSIX path."""

    root_res = Path(root).resolve()
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(root_res)
        except ValueError as exc:
            raise WorkspacePathError("path escapes the workspace root") from exc
        return posix_relpath(rel.as_posix())
    return posix_relpath(str(raw))


def resolve_workspace_file(root: Path, relative: str) -> Path:
    """Resolve *relative* under *root* without treating ``..`` as in-tree."""

    rel = posix_relpath(relative)
    root_res = Path(root).resolve()
    raw = root_res.joinpath(*rel.split("/"))
    if raw.is_symlink():
        raise WorkspacePathError("workspace file must not be a symlink")
    try:
        resolved = raw.resolve()
        resolved.relative_to(root_res)
    except ValueError as exc:
        raise WorkspacePathError("path escapes the workspace root") from exc
    return raw


def read_workspace_text(root: Path, relative: str, *, max_bytes: int = MAX_HYGIENE_FILE_BYTES) -> str:
    path = resolve_workspace_file(root, relative)
    if not path.is_file() or path.is_symlink():
        raise WorkspacePathError("workspace path is not a regular file")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise WorkspacePathError("cannot stat workspace file") from exc
    if size > max_bytes:
        raise WorkspacePathError(f"file exceeds {max_bytes} bytes")
    try:
        body = path.read_bytes()
    except OSError as exc:
        raise WorkspacePathError("cannot read workspace file") from exc
    if len(body) > max_bytes:
        raise WorkspacePathError(f"file exceeds {max_bytes} bytes")
    if b"\x00" in body:
        raise WorkspacePathError("workspace file contains NUL bytes")
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspacePathError("workspace file is not UTF-8") from exc


def write_workspace_text(root: Path, relative: str, text: str) -> None:
    path = resolve_workspace_file(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
