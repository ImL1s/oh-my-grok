"""Manifest-driven ``omg setup import`` / ``omg setup migrate`` (#77 leftover).

Copy-safe ingest and in-place legacy classification. File copy is not live
Grok or Antigravity discovery. Never sets ``verified`` / ``observed`` /
``healthy`` / ``passes``.

Source walks and regular-file reads use POSIX ``O_NOFOLLOW`` open+fstat when
available; otherwise Windows ``CreateFileW`` / ``NtCreateFile``
``FILE_FLAG_OPEN_REPARSE_POINT`` via ``win32_nofollow`` / ``path_keys``.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from omg_cli.contracts.path_keys import (
    MAX_MANAGED_READ_BYTES,
    ContractPathError,
    atomic_write_bytes,
    ensure_managed_dir,
)
from omg_cli.win32_nofollow import (
    Win32NofollowError,
    read_relative_regular,
    volume_root_and_parts,
    walk_managed_directories,
    windows_nofollow_ready,
)
from omg_cli.install_manifest import (
    SCHEMA,
    classify_path,
    load_manifest,
    path_is_under,
    persist_manifest,
    project_manifest_path,
    upsert_manifest_artifacts,
    user_manifest_path,
    user_store,
)
from omg_cli.project_root import path_is_under as resolved_path_is_under

# Same credential needles as medley_inspect / redaction.
_SK_TOKEN_RE = re.compile(r"(?i)(?:^|[^a-z0-9])sk-[a-z0-9_-]{4,}")
_BEARER_RE = re.compile(r"(?i)(?:^|[^a-z0-9])bearer\s+(?:sk-|eyj|[a-z0-9._\-+/=]{20,})")
_X_API_KEY_RE = re.compile(r"(?i)x-api-key\s*[:=]\s*\S+")
_PEM_NEEDLE = "-----begin "
_TEXT_NEEDLES = ("api_key", "private_key")

MAX_IMPORT_BYTES = min(MAX_MANAGED_READ_BYTES, 1_048_576)
MAX_IMPORT_FILES = 256
_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        "node_modules",
        "state",
        "tx",
    }
)
_KIND_SLUG = {
    "rules": "rules",
    "skill": "skill",
    "agent": "agent",
    "hook": "hook",
    "MCP config": "mcp",
}
_RELATIVE_MANAGED = {
    "AGENTS.md": "project.agents",
    ".gitignore": "project.gitignore",
    "rules/omg.md": "user.grok.rules",
    "hooks/omg-pretool-deny.json": "user.grok.hook",
    ".omg/projections/antigravity/README.md": "project.ag.projection",
}

_HONESTY_NOTE = (
    "File copy is not live Grok/Antigravity discovery. Doctor observed/healthy/verified stay false."
)


class InstallMigrateError(ValueError):
    """Fail-closed import/migrate error. Message must not include secrets."""

    def __init__(self, code: str, message: str, *, details: Any | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _posix(path: Path) -> str:
    return path.absolute().as_posix()


def _posix_nofollow_ready() -> bool:
    return os.name == "posix" and hasattr(os, "O_NOFOLLOW")


def _require_nofollow() -> None:
    if _posix_nofollow_ready():
        return
    if windows_nofollow_ready():
        return
    raise InstallMigrateError(
        "E_PATH",
        "import/migrate requires POSIX O_NOFOLLOW or Windows no-follow",
    )


def _win32_source_error(exc: Win32NofollowError) -> InstallMigrateError:
    kind = exc.kind
    if kind == "symlink":
        return InstallMigrateError("E_SYMLINK", "refusing symlink source")
    if kind == "size":
        return InstallMigrateError("E_SOURCE", "source file exceeds import size limit")
    if kind == "changed":
        return InstallMigrateError("E_SOURCE", "source file changed while reading")
    if kind in {"not_regular", "links"}:
        return InstallMigrateError("E_SOURCE", "source is not a regular file")
    if kind == "missing":
        return InstallMigrateError("E_SOURCE", "source file is unreadable")
    return InstallMigrateError("E_SOURCE", "source file is unreadable")


def _windows_parts(path: Path) -> tuple[Path, list[str]]:
    try:
        root, parts = volume_root_and_parts(path)
    except Win32NofollowError as exc:
        raise _win32_source_error(exc) from exc
    if not parts:
        raise InstallMigrateError("E_PATH", "unsafe source path")
    return root, parts


def _windows_leaf_kind(path: Path) -> str:
    """Return ``file`` or ``dir``. Fail closed on reparse/symlink."""
    base, parts = _windows_parts(path)
    try:
        api, handle = walk_managed_directories(base, parts, create=False)
    except Win32NofollowError as exc:
        if exc.kind == "symlink":
            raise InstallMigrateError("E_SYMLINK", "refusing symlink source") from exc
        if exc.kind == "not_regular":
            return "file"
        raise _win32_source_error(exc) from exc
    api.close(handle)
    return "dir"


def _read_regular_windows(path: Path, *, max_bytes: int) -> bytes:
    root, parts = _windows_parts(path)
    try:
        body, _mode = read_relative_regular(root, parts, max_bytes=max_bytes)
    except Win32NofollowError as exc:
        raise _win32_source_error(exc) from exc
    return body


def _iter_source_files_windows(source: Path) -> list[Path]:
    """List regular files under *source* without following a reparse/symlink."""
    source = Path(source).absolute()
    kind = _windows_leaf_kind(source)
    if kind == "file":
        return [source]
    found: list[Path] = []

    def walk(directory: Path) -> None:
        if _windows_leaf_kind(directory) != "dir":
            raise InstallMigrateError("E_SOURCE", "source path is not a file or directory")
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise InstallMigrateError("E_SOURCE", "source directory is unreadable") from exc
        for entry in entries:
            path = Path(entry.path)
            child_kind = _windows_leaf_kind(path)
            if child_kind == "dir":
                if entry.name in _SKIP_DIR_NAMES:
                    continue
                walk(path)
                continue
            if child_kind == "file":
                found.append(path)
                if len(found) > MAX_IMPORT_FILES:
                    raise InstallMigrateError(
                        "E_SOURCE", "source tree exceeds import file count limit"
                    )

    walk(source)
    return found


def credential_shaped(data: bytes) -> bool:
    """True when bytes match medley_inspect / redaction credential needles."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    lower = text.lower()
    if (
        _SK_TOKEN_RE.search(lower)
        or _BEARER_RE.search(lower)
        or _X_API_KEY_RE.search(lower)
        or _PEM_NEEDLE in lower
    ):
        return True
    return any(needle in lower for needle in _TEXT_NEEDLES)


def read_regular_nofollow(path: Path, *, max_bytes: int = MAX_IMPORT_BYTES) -> bytes:
    """Read a regular file without following a symlink. Fail-closed on TOCTOU."""
    _require_nofollow()
    if not _posix_nofollow_ready():
        return _read_regular_windows(path, max_bytes=max_bytes)
    if path.is_symlink():
        raise InstallMigrateError("E_SYMLINK", "refusing symlink source")
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {getattr(os, "ELOOP", -1), getattr(os, "EMLINK", -1)}:
            raise InstallMigrateError("E_SYMLINK", "refusing symlink source") from exc
        raise InstallMigrateError("E_SOURCE", "source file is unreadable") from exc
    try:
        info = os.fstat(fd)
        if stat.S_ISLNK(info.st_mode):
            raise InstallMigrateError("E_SYMLINK", "refusing symlink source")
        if not stat.S_ISREG(info.st_mode):
            raise InstallMigrateError("E_SOURCE", "source is not a regular file")
        if info.st_size > max_bytes:
            raise InstallMigrateError("E_SOURCE", "source file exceeds import size limit")
        body = os.read(fd, max_bytes + 1)
        if len(body) > max_bytes:
            raise InstallMigrateError("E_SOURCE", "source file exceeds import size limit")
        after = os.fstat(fd)
        if (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise InstallMigrateError("E_SOURCE", "source file changed while reading")
        return body
    finally:
        os.close(fd)


def _safe_name(name: str) -> str:
    base = Path(name).name
    if not base or base in {".", ".."} or "/" in base or "\\" in base or "\x00" in base:
        raise InstallMigrateError("E_PATH", "unsafe source file name")
    return base


def infer_artifact_type(path: Path, *, single_file: bool) -> str | None:
    """Classify a user artifact. None → skip (directory scan only)."""
    name = path.name.lower()
    parts = [part.lower() for part in path.parts]
    if name == "skill.md" or "skills" in parts:
        return "skill"
    if name in {"agents.md", "omg.md"} or "rules" in parts:
        return "rules"
    if "agents" in parts and name.endswith(".md"):
        return "agent"
    if name.endswith((".json", ".jsonc")):
        if "hook" in name or "hooks" in parts:
            return "hook"
        if "mcp" in name or name in {"claude_desktop_config.json", "mcp.json"}:
            return "MCP config"
        if single_file:
            return "MCP config"
    if single_file and name.endswith((".md", ".markdown", ".txt")):
        return "skill"
    if single_file:
        return "skill"
    return None


def _kind_slug(kind: str) -> str:
    return _KIND_SLUG.get(kind, "artifact")


def _imported_id(kind: str, name: str, digest: str) -> str:
    stem = Path(_safe_name(name)).stem or "artifact"
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip("-._") or "artifact"
    return f"imported.{_kind_slug(kind)}.{slug}.{digest[:8]}"


def _imported_target(
    *,
    scope: str,
    project_root: Path | None,
    kind: str,
    name: str,
) -> Path:
    slug = _kind_slug(kind)
    safe = _safe_name(name)
    if scope == "user":
        return user_store() / "install" / "imported" / slug / safe
    if project_root is None:
        raise InstallMigrateError("E_SCOPE", "project scope requires a project root")
    return Path(project_root) / ".omg" / "install" / "imported" / slug / safe


def _honesty_fields() -> dict[str, Any]:
    return {
        "verified": False,
        "observed": False,
        "healthy": False,
        "note": _HONESTY_NOTE,
    }


def _containment_roots(
    *,
    scope: str,
    project_root: Path | None,
    grok_home: Path | None,
) -> tuple[Path, ...]:
    roots: list[Path] = []
    if scope == "user":
        roots.append(user_store())
    elif project_root is not None:
        roots.append(Path(project_root))
    if grok_home is not None:
        roots.append(Path(grok_home))
    return tuple(roots)


def _contained(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path_is_under(path, root) for root in roots)


def _path_has_dotdot(path: Path) -> bool:
    """True when any lexical component is ``..`` (``relative_to`` allows these)."""
    return any(part == ".." for part in Path(path).parts)


def _uninstall_target_contained(path: Path, roots: tuple[Path, ...]) -> bool:
    """Admit an uninstall target only after rejecting ``..`` and resolving under *roots*."""
    if not roots or _path_has_dotdot(path):
        return False
    return any(resolved_path_is_under(path, root) for root in roots)


def _resolved_under_omg_state(path: Path) -> bool:
    """True when *path* resolves under a ``.omg/state`` directory."""
    try:
        parts = Path(path).resolve().parts
    except OSError:
        return True
    for index, part in enumerate(parts[:-1]):
        if part == ".omg" and parts[index + 1] == "state":
            return True
    return False


def _symlink_parent(path: Path, roots: tuple[Path, ...]) -> bool:
    abs_path = path.absolute()
    current = abs_path.parent
    while True:
        if current.is_symlink():
            return True
        if any(current == root.absolute() for root in roots):
            return False
        parent = current.parent
        if parent == current:
            return True
        current = parent


def _iter_source_files(source: Path) -> list[Path]:
    """List regular files under *source*. Any symlink in the tree fails closed."""
    _require_nofollow()
    if not _posix_nofollow_ready():
        return _iter_source_files_windows(source)
    if source.is_symlink():
        raise InstallMigrateError("E_SYMLINK", "refusing symlink source")
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise InstallMigrateError("E_SOURCE", "source path is not a file or directory")
    found: list[Path] = []

    def walk(directory: Path) -> None:
        if directory.is_symlink():
            raise InstallMigrateError("E_SYMLINK", "refusing symlink source")
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise InstallMigrateError("E_SOURCE", "source directory is unreadable") from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                is_link = entry.is_symlink()
            except OSError as exc:
                raise InstallMigrateError("E_SOURCE", "source entry is unreadable") from exc
            if is_link:
                raise InstallMigrateError("E_SYMLINK", "refusing symlink source")
            if entry.is_dir(follow_symlinks=False):
                if entry.name in _SKIP_DIR_NAMES:
                    continue
                walk(path)
                continue
            if entry.is_file(follow_symlinks=False):
                found.append(path)
                if len(found) > MAX_IMPORT_FILES:
                    raise InstallMigrateError(
                        "E_SOURCE", "source tree exceeds import file count limit"
                    )

    walk(source)
    return found


def _provenance(source: Path, data: bytes, *, imported_at: str) -> dict[str, Any]:
    return {
        "source": _posix(source),
        "sha256": _sha256_bytes(data),
        "byte_size": len(data),
        "imported_at": imported_at,
    }


def _row_base(
    *,
    ident: str,
    kind: str,
    target: Path,
    ownership: str,
    classification: str,
    digest: str,
    runtime: str,
    scope: str,
    provenance: Mapping[str, Any],
    enabled: bool,
) -> dict[str, Any]:
    return {
        "id": ident,
        "runtime": runtime,
        "scope": scope,
        "type": kind,
        "target": str(target),
        "ownership": ownership,
        "classification": classification,
        "content_hash": digest,
        "enabled": enabled,
        "mergeable": False,
        "provenance": dict(provenance),
        "note": _HONESTY_NOTE,
    }


def _empty_document(
    *,
    runtime: str,
    scope: str,
    source_version: str | None = None,
) -> dict[str, Any]:
    payload = {
        "schema": SCHEMA,
        "kind": "omg_install_manifest",
        "runtime": runtime,
        "scope": scope,
        "source_version": source_version,
        "source_commit": None,
        "artifacts": [],
        **_honesty_fields(),
    }
    return payload


def _target_classification(target: Path, desired: bytes | None) -> str:
    return classify_path(target, desired=desired)


def plan_import(
    source: Path,
    *,
    project_root: Path | None,
    scope: str = "project",
    runtime: str = "grok",
    imported_at: str | None = None,
) -> dict[str, Any]:
    """Build import rows. Reads sources (no dest writes). Refuses secrets/symlinks."""
    source = Path(source)
    if not source.exists() and not source.is_symlink():
        raise InstallMigrateError("E_SOURCE", "source path does not exist")
    files = _iter_source_files(source)
    if not files:
        raise InstallMigrateError("E_SOURCE", "no importable artifacts in source")
    stamp = imported_at or _utc_now()
    rows: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    used_targets: set[str] = set()
    for path in files:
        kind = infer_artifact_type(path, single_file=source.is_file())
        if kind is None:
            continue
        data = read_regular_nofollow(path)
        if credential_shaped(data):
            raise InstallMigrateError(
                "E_SECRET",
                "refusing credential-shaped bytes",
            )
        digest = _sha256_bytes(data)
        ident = _imported_id(kind, path.name, digest)
        if ident in used_ids:
            ident = f"{ident}.{len(used_ids):02d}"
        used_ids.add(ident)
        target = _imported_target(
            scope=scope,
            project_root=project_root,
            kind=kind,
            name=path.name,
        )
        if str(target) in used_targets:
            stem = Path(path.name).stem
            suffix = Path(path.name).suffix
            target = target.with_name(f"{stem}.{digest[:8]}{suffix}")
        used_targets.add(str(target))
        klass = _target_classification(target, data)
        rows.append(
            _row_base(
                ident=ident,
                kind=kind,
                target=target,
                ownership="imported",
                classification=klass,
                digest=digest,
                runtime=runtime,
                scope=scope,
                provenance=_provenance(path, data, imported_at=stamp),
                enabled=True,
            )
        )
        rows[-1]["_bytes"] = data
    if not rows:
        raise InstallMigrateError("E_SOURCE", "no importable artifacts in source")
    return {
        "ok": True,
        "operation": "import",
        "dry_run": True,
        "runtime": runtime,
        "scope": scope,
        "rows": rows,
        **_honesty_fields(),
    }


def _strip_private(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        clean = {key: value for key, value in row.items() if not str(key).startswith("_")}
        out.append(clean)
    return out


def _refuse_existing_clobber(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        klass = str(row.get("classification") or "")
        if klass in {"user_owned", "user_owned_conflict", "foreign", "malformed"}:
            raise InstallMigrateError(
                "E_FOREIGN" if klass == "foreign" else "E_MALFORMED",
                "refusing to overwrite existing target",
            )
        if klass == "stale":
            raise InstallMigrateError(
                "E_MALFORMED",
                "refusing to overwrite existing target with different bytes",
            )


def _write_imported_file(target: Path, data: bytes) -> None:
    try:
        ensure_managed_dir(target.parent)
        atomic_write_bytes(target, data)
    except ContractPathError as exc:
        raise InstallMigrateError("E_PATH", "refusing unsafe import target") from exc


def run_import(
    source: Path,
    *,
    project_root: Path | None,
    scope: str = "project",
    runtime: str = "grok",
    dry_run: bool = False,
    source_version: str | None = None,
) -> dict[str, Any]:
    """Import user artifacts into the install manifest. Dry-run writes nothing."""
    planned = plan_import(
        Path(source),
        project_root=project_root,
        scope=scope,
        runtime=runtime,
    )
    public_rows = _strip_private(planned["rows"])
    result = {
        **planned,
        "dry_run": bool(dry_run),
        "rows": public_rows,
        "written": [],
        "manifest": None,
    }
    if dry_run:
        return result
    _refuse_existing_clobber(public_rows)
    existing = load_manifest(project_root=project_root, scope=scope, strict=True)
    document = existing or _empty_document(
        runtime=runtime,
        scope=scope,
        source_version=source_version,
    )
    if existing is None:
        document["runtime"] = runtime
    written: list[str] = []
    try:
        for row, full in zip(public_rows, planned["rows"]):
            data = full.get("_bytes")
            if not isinstance(data, bytes):
                raise InstallMigrateError("E_SOURCE", "import plan missing source bytes")
            target = Path(str(row["target"]))
            if target.is_symlink():
                raise InstallMigrateError("E_SYMLINK", "refusing symlink import target")
            if target.is_file():
                current = read_regular_nofollow(target)
                if current != data:
                    raise InstallMigrateError(
                        "E_MALFORMED",
                        "refusing to overwrite existing target with different bytes",
                    )
            else:
                _write_imported_file(target, data)
                written.append(str(target))
            row["classification"] = "exact"
        document = upsert_manifest_artifacts(document, public_rows)
        dest = persist_manifest(document, project_root=project_root, scope=scope)
    except Exception:
        for path_str in written:
            path = Path(path_str)
            if path.is_symlink():
                continue
            if path.is_file():
                path.unlink(missing_ok=True)
        raise
    result["written"] = written
    result["manifest"] = str(dest)
    result["ok"] = True
    result.update(_honesty_fields())
    return result


def _relative_posix(path: Path, root: Path) -> str:
    try:
        return path.absolute().relative_to(root.absolute()).as_posix()
    except ValueError:
        return path.name


def _managed_id_for(rel: str) -> str | None:
    return _RELATIVE_MANAGED.get(rel)


def plan_migrate(
    source: Path,
    *,
    project_root: Path | None,
    scope: str = "project",
    runtime: str = "grok",
    grok_home: Path | None = None,
    imported_at: str | None = None,
) -> dict[str, Any]:
    """Classify a legacy layout in place. Does not copy or overwrite files."""
    source = Path(source)
    if source.is_symlink():
        raise InstallMigrateError("E_SYMLINK", "refusing symlink source")
    if not source.exists():
        raise InstallMigrateError("E_SOURCE", "source path does not exist")
    stamp = imported_at or _utc_now()
    roots = _containment_roots(scope=scope, project_root=project_root, grok_home=grok_home)
    files = _iter_source_files(source)
    rows: list[dict[str, Any]] = []
    scan_root = source if source.is_dir() else source.parent
    for path in files:
        rel = _relative_posix(path, scan_root)
        parts = Path(rel).parts
        if ".omg" in parts and ("state" in parts or "install" in parts):
            continue
        data = read_regular_nofollow(path)
        if credential_shaped(data):
            raise InstallMigrateError("E_SECRET", "refusing credential-shaped bytes")
        digest = _sha256_bytes(data)
        managed_id = _managed_id_for(rel)
        kind = infer_artifact_type(path, single_file=source.is_file())
        if managed_id is None and kind is None:
            continue
        contained = _contained(path, roots) if roots else False
        if not contained:
            klass = "foreign"
            ownership = "foreign"
            ident = managed_id or _imported_id(kind or "skill", path.name, digest)
            enabled = False
        else:
            klass = classify_path(path, desired=None)
            if klass == "foreign":
                ownership = "foreign"
                ident = managed_id or _imported_id(kind or "skill", path.name, digest)
                enabled = False
            elif klass == "malformed":
                ownership = "foreign"
                ident = managed_id or _imported_id(kind or "skill", path.name, digest)
                enabled = False
            elif managed_id is not None:
                ident = managed_id
                if klass in {"user_owned", "user_owned_conflict"}:
                    ownership = "user-owned"
                    enabled = False
                else:
                    ownership = "OMG-managed"
                    enabled = klass in {"exact", "stale"}
            else:
                ident = _imported_id(kind or "skill", path.name, digest)
                ownership = "imported"
                enabled = klass not in {"malformed", "foreign"}
        rows.append(
            _row_base(
                ident=ident,
                kind=kind or "rules",
                target=path,
                ownership=ownership,
                classification=klass,
                digest=digest,
                runtime=runtime,
                scope="user" if str(ident).startswith("user.") else scope,
                provenance=_provenance(path, data, imported_at=stamp),
                enabled=enabled,
            )
        )
    if not rows:
        raise InstallMigrateError("E_SOURCE", "no migratable artifacts in source")
    return {
        "ok": True,
        "operation": "migrate",
        "dry_run": True,
        "runtime": runtime,
        "scope": scope,
        "rows": rows,
        **_honesty_fields(),
    }


def _fail_closed_classes(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        klass = str(row.get("classification") or "")
        ownership = str(row.get("ownership") or "")
        if klass in {"foreign", "malformed"} or ownership == "foreign":
            raise InstallMigrateError(
                "E_FOREIGN" if klass == "foreign" or ownership == "foreign" else "E_MALFORMED",
                "refusing foreign or malformed classification (re-run after moving those paths)",
            )


def run_migrate(
    source: Path,
    *,
    project_root: Path | None,
    scope: str = "project",
    runtime: str = "grok",
    dry_run: bool = False,
    grok_home: Path | None = None,
    source_version: str | None = None,
) -> dict[str, Any]:
    """Classify a legacy layout and record rows. Never overwrites user-owned files."""
    planned = plan_migrate(
        Path(source),
        project_root=project_root,
        scope=scope,
        runtime=runtime,
        grok_home=grok_home,
    )
    rows = _strip_private(planned["rows"])
    result = {
        **planned,
        "dry_run": bool(dry_run),
        "rows": rows,
        "written": [],
        "manifest": None,
    }
    if dry_run:
        return result
    _fail_closed_classes(rows)
    existing = load_manifest(project_root=project_root, scope=scope, strict=True)
    document = existing or _empty_document(
        runtime=runtime,
        scope=scope,
        source_version=source_version,
    )
    document = upsert_manifest_artifacts(document, rows)
    dest = persist_manifest(document, project_root=project_root, scope=scope)
    result["manifest"] = str(dest)
    result["ok"] = True
    result.update(_honesty_fields())
    return result


def _is_state_path(path: Path, project_root: Path | None) -> bool:
    if project_root is None:
        return False
    state = Path(project_root) / ".omg" / "state"
    return path_is_under(path, state) or resolved_path_is_under(path, state)


def plan_owned_uninstall(
    *,
    project_root: Path | None,
    include_user_manifest: bool = False,
    grok_home: Path | None = None,
) -> dict[str, Any]:
    """Plan uninstall of manifest-owned unchanged regular files only."""
    documents: list[tuple[str, dict[str, Any], Path | None]] = []
    if project_root is not None:
        doc = load_manifest(project_root=project_root, scope="project", strict=False)
        if doc is not None:
            documents.append(("project", doc, Path(project_root)))
    if include_user_manifest:
        doc = load_manifest(project_root=None, scope="user", strict=False)
        if doc is not None:
            documents.append(("user", doc, user_store()))
    roots: list[Path] = []
    if project_root is not None:
        roots.append(Path(project_root))
    if grok_home is not None:
        roots.append(Path(grok_home))
    if include_user_manifest:
        roots.append(user_store())
    from omg_cli.antigravity_install import config_root as antigravity_config_root

    ag_config = antigravity_config_root()
    roots.append(ag_config)
    root_tuple = tuple(roots)
    uninstall_roots: list[Path] = []
    if project_root is not None:
        uninstall_roots.append(Path(project_root) / ".omg" / "install")
        uninstall_roots.append(Path(project_root) / ".omg" / "projections")
    if grok_home is not None:
        from omg_cli.guidance import rules_file_path
        from omg_cli.hook_install import managed_hook_paths

        # Exact machine-scoped files only — never the whole GROK_HOME tree.
        uninstall_roots.extend(managed_hook_paths(home=Path(grok_home)))
        uninstall_roots.append(rules_file_path(home=Path(grok_home)))
    if include_user_manifest:
        uninstall_roots.append(user_store())
    # Exact official Agy plugin target only; never authorize the config tree.
    from omg_cli.antigravity_install import installed_plugin_path

    ag_plugin = installed_plugin_path()
    uninstall_roots.append(ag_plugin)
    uninstall_tuple = tuple(uninstall_roots)
    allowed_global_ids = frozenset({"user.grok.hook", "user.grok.rules"})
    planned_plugin_references: set[str] = set()
    for _scope, doc, _root in documents:
        if not any(
            isinstance(row, dict) and str(row.get("id") or "") == "user.ag.plugin"
            for row in (doc.get("artifacts") or [])
        ):
            continue
        if _scope == "user":
            planned_plugin_references.add(str(user_manifest_path().absolute()))
        elif _root is not None:
            planned_plugin_references.add(
                str(project_manifest_path(Path(_root)).absolute())
            )
    remove: list[dict[str, Any]] = []
    remove_external: list[dict[str, Any]] = []
    release_external_references: list[dict[str, Any]] = []
    preserve: list[dict[str, Any]] = []
    for _scope, doc, _root in documents:
        for row in doc.get("artifacts") or []:
            if not isinstance(row, dict):
                continue
            ownership = str(row.get("ownership") or "")
            target = Path(str(row.get("target") or ""))
            claimed = row.get("content_hash")
            ident = str(row.get("id") or "")
            if not ident or not str(row.get("target") or ""):
                continue
            if ownership not in {"OMG-managed", "imported"}:
                preserve.append(
                    {
                        "id": ident,
                        "path": str(target),
                        "reason": "not-owned",
                    }
                )
                continue
            if grok_home is not None and ident not in allowed_global_ids:
                try:
                    under_home = resolved_path_is_under(target, Path(grok_home))
                except (OSError, ValueError):
                    under_home = False
                if under_home:
                    preserve.append(
                        {
                            "id": ident,
                            "path": str(target),
                            "reason": "out-of-scope",
                        }
                    )
                    continue
            if _is_state_path(target, project_root):
                preserve.append({"id": ident, "path": str(target), "reason": "state"})
                continue
            if _path_has_dotdot(target):
                preserve.append({"id": ident, "path": str(target), "reason": "escape"})
                continue
            if not uninstall_tuple or not _uninstall_target_contained(target, uninstall_tuple):
                preserve.append({"id": ident, "path": str(target), "reason": "out-of-scope"})
                continue
            if target.name == "manifest.json":
                preserve.append({"id": ident, "path": str(target), "reason": "manifest"})
                continue
            if target.name == "omg.md" and target.parent.name == "rules":
                preserve.append(
                    {
                        "id": ident,
                        "path": str(target),
                        "reason": "surgical-guidance",
                    }
                )
                continue
            if ident == "user.ag.plugin":
                reference_path = (
                    user_manifest_path()
                    if _scope == "user"
                    else project_manifest_path(Path(_root))  # type: ignore[arg-type]
                ).absolute()
                if ownership not in {"OMG-managed", "imported"}:
                    preserve.append({"id": ident, "path": str(target), "reason": "machine-global"})
                    continue
                if target.absolute() != ag_plugin.absolute() or target.is_symlink():
                    preserve.append({"id": ident, "path": str(target), "reason": "escape"})
                    continue
                if not isinstance(claimed, str) or not claimed:
                    preserve.append({"id": ident, "path": str(target), "reason": "not-owned"})
                    continue
                from omg_cli.antigravity_install import (
                    committed_owned_uninstall_matches,
                    load_ownership_receipt,
                    package_digest,
                    resumable_owned_uninstall_matches,
                )

                actual_digest = package_digest(target) or ""
                ownership_receipt = load_ownership_receipt()
                references = (
                    ownership_receipt.get("references", [])
                    if isinstance(ownership_receipt, dict)
                    else []
                )
                registry_identity = row.get("registry_identity")
                mcp_registry_identity = row.get("mcp_registry_identity")
                committed_removal = bool(
                    isinstance(registry_identity, str)
                    and isinstance(mcp_registry_identity, str)
                    and committed_owned_uninstall_matches(
                        expected_digest=claimed,
                        expected_registry_identity=registry_identity,
                        expected_mcp_registry_identity=mcp_registry_identity,
                    )
                )
                resumable_removal = bool(
                    isinstance(registry_identity, str)
                    and isinstance(mcp_registry_identity, str)
                    and resumable_owned_uninstall_matches(
                        expected_digest=claimed,
                        expected_registry_identity=registry_identity,
                        expected_mcp_registry_identity=mcp_registry_identity,
                    )
                )
                receipt_refs = {
                    str(item) for item in references if isinstance(item, str)
                }
                covers_all_owners = bool(receipt_refs) and (
                    receipt_refs <= planned_plugin_references
                )
                central_reference_authorizes = bool(
                    str(reference_path) in references
                    and (len(set(references)) == 1 or covers_all_owners)
                )
                if (
                    str(reference_path) in references
                    and len(set(references)) > 1
                    and not covers_all_owners
                    and isinstance(registry_identity, str)
                    and isinstance(mcp_registry_identity, str)
                    and actual_digest == claimed
                ):
                    release_external_references.append(
                        {
                            "reference": str(reference_path),
                            "content_hash": claimed,
                            "registry_identity": registry_identity,
                            "mcp_registry_identity": mcp_registry_identity,
                        }
                    )
                    preserve.append({"id": ident, "path": str(target), "reason": "shared-global"})
                    continue
                if (
                    not committed_removal
                    and not resumable_removal
                    and (
                        (_scope == "project" and not central_reference_authorizes)
                        or (_scope == "user" and ownership != "OMG-managed")
                        or
                        not target.is_dir()
                        or actual_digest != claimed
                        or ownership_receipt is None
                        or ownership_receipt.get("plugin_digest") != claimed
                        or ownership_receipt.get("registry_identity") != registry_identity
                        or ownership_receipt.get("mcp_registry_identity")
                        != mcp_registry_identity
                        or not isinstance(registry_identity, str)
                        or not isinstance(mcp_registry_identity, str)
                    )
                ):
                    preserve.append({"id": ident, "path": str(target), "reason": "hash-drift"})
                    continue
                if any(
                    str(existing.get("path")) == str(target) for existing in remove_external
                ):
                    continue
                remove_external.append(
                    {
                        "id": ident,
                        "path": str(target),
                        "content_hash": claimed,
                        "registry_identity": registry_identity,
                        "mcp_registry_identity": mcp_registry_identity,
                        "action": "agy-plugin-uninstall",
                    }
                )
                continue
            if (
                not root_tuple
                or not _contained(target, root_tuple)
                or _symlink_parent(target, root_tuple)
            ):
                preserve.append({"id": ident, "path": str(target), "reason": "escape"})
                continue
            if target.is_symlink() or (target.exists() and not target.is_file()):
                preserve.append({"id": ident, "path": str(target), "reason": "not-regular"})
                continue
            if not isinstance(claimed, str) or not claimed:
                preserve.append({"id": ident, "path": str(target), "reason": "no-hash"})
                continue
            if not target.is_file():
                continue
            try:
                actual = read_regular_nofollow(target)
            except InstallMigrateError:
                preserve.append({"id": ident, "path": str(target), "reason": "unreadable"})
                continue
            digest = _sha256_bytes(actual)
            if digest != claimed:
                preserve.append(
                    {
                        "id": ident,
                        "path": str(target),
                        "reason": "hash-drift",
                    }
                )
                continue
            remove.append(
                {
                    "id": ident,
                    "path": str(target),
                    "content_hash": claimed,
                }
            )
    return {
        "ok": True,
        "has_manifest": bool(documents),
        "remove": remove,
        "remove_external": remove_external,
        "release_external_references": (
            [] if remove_external else release_external_references
        ),
        "preserve": preserve,
        **_honesty_fields(),
    }


def apply_owned_uninstall(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Unlink regular files whose on-disk hash still matches the plan."""
    removed: list[str] = []
    preserved: list[str] = [str(row.get("path")) for row in plan.get("preserve") or []]
    from omg_cli.antigravity_install import release_ownership_reference

    removing_plugin = bool(plan.get("remove_external"))
    for row in plan.get("release_external_references") or []:
        if removing_plugin:
            continue
        if not isinstance(row, dict) or not release_ownership_reference(
            reference=str(row.get("reference") or ""),
            expected_digest=str(row.get("content_hash") or ""),
            expected_registry_identity=str(row.get("registry_identity") or ""),
            expected_mcp_registry_identity=str(row.get("mcp_registry_identity") or ""),
        ):
            return {
                "ok": False,
                "removed": removed,
                "preserved": preserved,
                **_honesty_fields(),
            }
    for row in plan.get("remove") or []:
        if not isinstance(row, dict):
            continue
        target = Path(str(row.get("path") or ""))
        expected = str(row.get("content_hash") or "")
        if (
            _path_has_dotdot(target)
            or _resolved_under_omg_state(target)
            or target.is_symlink()
            or not target.is_file()
        ):
            preserved.append(str(target))
            continue
        try:
            actual = read_regular_nofollow(target)
        except InstallMigrateError:
            preserved.append(str(target))
            continue
        if _sha256_bytes(actual) != expected:
            preserved.append(str(target))
            continue
        target.unlink()
        removed.append(str(target))
    return {
        "ok": True,
        "removed": removed,
        "preserved": preserved,
        **_honesty_fields(),
    }


__all__ = [
    "InstallMigrateError",
    "apply_owned_uninstall",
    "credential_shaped",
    "plan_import",
    "plan_migrate",
    "plan_owned_uninstall",
    "read_regular_nofollow",
    "run_import",
    "run_migrate",
]
