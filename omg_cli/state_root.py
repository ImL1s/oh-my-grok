"""Canonical state-root contract (#74 PR1).

Pure, versioned, side-effect-free resolver.  Resolved paths confer **no**
state authority: only existing OMG CLI state APIs may mutate passes/verified.
This module does not mkdir, write, spawn, or relocate current ``.omg`` trees.

Precedence for the physical state directory (highest first):

1. explicit centralized ``OMG_STATE_DIR`` / API ``explicit_state_dir``
2. nearest ``.omg-workspace`` marker when explicitly enabled and not killed
3. derived from the canonical project-root identity (per-worktree ``<root>/.omg``)

Project identity still follows ``omg_cli.project_root`` precedence
(``--project-root`` / ``OMG_PROJECT_ROOT`` / ``.omg/worktrees`` owner /
in-repo ``.omg`` / git worktree / cwd) but git discovery here is
filesystem-only (no subprocess). Unrelated ancestor ``.omg`` outside the
git worktree is ignored.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

from omg_cli.contracts.path_keys import (
    ContractPathError,
    safe_path_key,
    validate_safe_key,
)
from omg_cli.project_root import (
    ENV_PROJECT_ROOT,
    is_shared_temp_root,
    list_omg_ancestors,
    owning_project_from_omg_worktree,
    path_is_under,
)

STATE_ROOT_SCHEMA_VERSION = 1
ENV_STATE_DIR = "OMG_STATE_DIR"
ENV_WORKSPACE_MARKER = "OMG_WORKSPACE_MARKER"
ENV_DISABLE_WORKSPACE_MARKER = "OMG_DISABLE_WORKSPACE_MARKER"
WORKSPACE_MARKER_NAME = ".omg-workspace"
PROJECT_KEY_NAMESPACE = "omg-state-root-v1"
MARKER_MAX_BYTES = 4096
SCOPE_PER_WORKTREE: Literal["per_worktree"] = "per_worktree"
SCOPE_WORKSPACE: Literal["workspace_shared"] = "workspace_shared"
SCOPE_CENTRALIZED: Literal["centralized"] = "centralized"
StateScope = Literal["per_worktree", "workspace_shared", "centralized"]
_TRUTHY = frozenset({"1", "true", "yes", "on"})
_MARKER_KEYS = frozenset({"version"})
_NOFOLLOW_ERRNOS = {errno.ELOOP, getattr(errno, "EMLINK", -1), errno.EINVAL}


class StateRootError(ValueError):
    """Invalid state-root inputs or a fail-closed safety rejection."""

    exit_code = 2


@dataclass(frozen=True)
class StateRootResolution:
    """Immutable state-root contract. Public dumps must stay secret-free."""

    project_root: Path
    state_dir: Path
    source: str
    scope: StateScope
    project_key: str
    diagnostics: Mapping[str, str]

    @property
    def schema_version(self) -> int:
        return STATE_ROOT_SCHEMA_VERSION

    def to_public_dict(self) -> dict[str, Any]:
        """Machine-readable view with no raw host paths or env values."""
        return {
            "diagnostics": dict(self.diagnostics),
            "project_key": self.project_key,
            "schema_version": STATE_ROOT_SCHEMA_VERSION,
            "scope": self.scope,
            "source": self.source,
        }

    def serialize(self) -> bytes:
        return json.dumps(
            self.to_public_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")


def _flag(env: Mapping[str, str], name: str) -> bool:
    return (env.get(name) or "").strip().lower() in _TRUTHY


def _resolve_existing_dir(path: Path, *, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve()
    except OSError as exc:
        raise StateRootError(f"{label} is not a usable path") from exc
    if not resolved.exists() or not resolved.is_dir():
        raise StateRootError(f"{label} is not an existing directory")
    return resolved


def _is_filesystem_root(path: Path) -> bool:
    return path.parent == path


def _is_broad_scope(path: Path, home: Path) -> bool:
    return path == home or _is_filesystem_root(path)


def _opaque_key(kind: str, identity: str) -> str:
    try:
        key = safe_path_key(f"{kind}:{identity}", namespace=PROJECT_KEY_NAMESPACE)
        return validate_safe_key(key)
    except ContractPathError as exc:
        raise StateRootError("cannot derive opaque project_key") from exc


def _diagnostics(
    *,
    project_root_source: str,
    identity_kind: str,
    marker: str,
    home_scope: str,
) -> Mapping[str, str]:
    payload = {
        "authority": "none",
        "home_scope": home_scope,
        "identity_kind": identity_kind,
        "marker": marker,
        "project_root_source": project_root_source,
        "schema_version": str(STATE_ROOT_SCHEMA_VERSION),
    }
    return MappingProxyType(payload)


def _read_nofollow_regular(path: Path, *, max_bytes: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in _NOFOLLOW_ERRNOS:
            raise StateRootError(f"{label} may not be a symlink") from exc
        raise StateRootError(f"{label} is unreadable") from exc
    try:
        st = os.fstat(fd)
        reject_marker_stat(st, label=label)
        if st.st_size > max_bytes:
            raise StateRootError(f"{label} exceeds size bound")
        body = os.read(fd, max_bytes + 1)
        if len(body) > max_bytes:
            raise StateRootError(f"{label} exceeds size bound")
        return body
    finally:
        os.close(fd)


def reject_marker_stat(st: os.stat_result, *, label: str = "workspace marker") -> None:
    """Fail closed on non-regular / multi-link / special marker inodes."""
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        raise StateRootError(f"{label} may not be a symlink")
    if stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        raise StateRootError(f"{label} may not be a device")
    if stat.S_ISFIFO(mode):
        raise StateRootError(f"{label} may not be a FIFO")
    if stat.S_ISSOCK(mode):
        raise StateRootError(f"{label} may not be a socket")
    if not stat.S_ISREG(mode):
        raise StateRootError(f"{label} must be a regular file")
    if st.st_nlink != 1:
        raise StateRootError(f"{label} may not be a hardlink")


def _parse_workspace_marker(body: bytes) -> None:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StateRootError("workspace marker is not UTF-8") from exc
    text = text.strip()
    if not text:
        raise StateRootError("workspace marker is empty")

    def _no_dup(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in pairs:
            if key in out:
                raise StateRootError("workspace marker has a duplicate key")
            out[key] = value
        return out

    decoder = json.JSONDecoder(object_pairs_hook=_no_dup)
    try:
        obj, idx = decoder.raw_decode(text)
    except json.JSONDecodeError as exc:
        raise StateRootError("workspace marker is malformed") from exc
    if text[idx:].strip():
        raise StateRootError("workspace marker has trailing data")
    if not isinstance(obj, dict):
        raise StateRootError("workspace marker must be a JSON object")
    unknown = set(obj) - _MARKER_KEYS
    if unknown:
        raise StateRootError("workspace marker has an unknown key")
    if "version" not in obj:
        raise StateRootError("workspace marker is missing version")
    version = obj["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise StateRootError("workspace marker version must be an integer")
    if version != STATE_ROOT_SCHEMA_VERSION:
        raise StateRootError("workspace marker version is unsupported")


def _load_workspace_marker(path: Path, workspace_root: Path, home: Path) -> Path:
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise StateRootError("workspace marker is unreadable") from exc
    reject_marker_stat(st)
    _parse_workspace_marker(
        _read_nofollow_regular(path, max_bytes=MARKER_MAX_BYTES, label="workspace marker")
    )
    root = workspace_root.resolve()
    if _is_broad_scope(root, home):
        raise StateRootError("workspace marker must not select HOME or filesystem root")
    return root


def _find_workspace_marker(start: Path, home: Path) -> Path | None:
    cur = start
    while True:
        candidate = cur / WORKSPACE_MARKER_NAME
        if os.path.lexists(candidate):
            return _load_workspace_marker(candidate, cur, home)
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


def _planned_physical_dir(path: Path, *, cwd: Path, label: str) -> Path:
    """Canonicalize a planned directory without creating it or following a leaf link."""
    try:
        raw = Path(os.path.expanduser(str(path)))
    except OSError as exc:
        raise StateRootError(f"{label} is not a usable path") from exc
    if not raw.is_absolute():
        raw = cwd / raw
    normalized = Path(os.path.normpath(raw))
    if os.path.lexists(normalized) and os.path.islink(normalized):
        raise StateRootError(f"{label} leaf may not be a symlink")
    if os.path.lexists(normalized) and not os.path.isdir(normalized):
        raise StateRootError(f"{label} is not a directory")

    current = normalized
    missing: list[str] = []
    while True:
        if os.path.lexists(current):
            break
        if current.parent == current:
            break
        missing.append(current.name)
        current = current.parent
    missing.reverse()

    if os.path.lexists(current) and os.path.islink(current):
        raise StateRootError(f"{label} ancestor may not be a symlink")
    if os.path.lexists(current):
        if not os.path.isdir(current):
            raise StateRootError(f"{label} ancestor is not a directory")
        try:
            base = current.resolve()
        except OSError as exc:
            raise StateRootError(f"{label} ancestor is not a usable path") from exc
    else:
        base = current
    for name in missing:
        if name in {".", "..", ""} or "/" in name or "\\" in name:
            raise StateRootError(f"{label} has an unsafe path component")
        base = base / name
    if _is_filesystem_root(base):
        raise StateRootError(f"{label} must not be the filesystem root")
    return base


def _git_worktree_and_common(start: Path) -> tuple[Path, Path] | None:
    """Filesystem-only git identity: (worktree root, common dir)."""
    cur = start
    while True:
        git = cur / ".git"
        try:
            st = os.lstat(git)
        except OSError:
            st = None
        if st is not None:
            if stat.S_ISLNK(st.st_mode):
                return None
            if stat.S_ISDIR(st.st_mode):
                try:
                    return cur.resolve(), git.resolve()
                except OSError:
                    return None
            if stat.S_ISREG(st.st_mode) and st.st_nlink == 1:
                parsed = _parse_gitdir_file(git, cur)
                if parsed is not None:
                    return parsed
            return None
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


def _parse_gitdir_file(git_file: Path, worktree: Path) -> tuple[Path, Path] | None:
    try:
        body = _read_nofollow_regular(git_file, max_bytes=4096, label="gitdir file")
        line = body.decode("utf-8").splitlines()[0].strip()
    except (StateRootError, UnicodeDecodeError, IndexError):
        return None
    prefix = "gitdir:"
    if not line.lower().startswith(prefix):
        return None
    raw = line[len(prefix) :].strip()
    if not raw:
        return None
    gitdir = Path(raw)
    if not gitdir.is_absolute():
        gitdir = worktree / gitdir
    try:
        gitdir = Path(os.path.normpath(gitdir))
    except OSError:
        return None
    if os.path.lexists(gitdir) and os.path.islink(gitdir):
        return None
    common = _read_commondir(gitdir)
    try:
        return worktree.resolve(), common.resolve()
    except OSError:
        return None


def _read_commondir(gitdir: Path) -> Path:
    commondir = gitdir / "commondir"
    if os.path.lexists(commondir) and not os.path.islink(commondir):
        try:
            rel = _read_nofollow_regular(
                commondir, max_bytes=4096, label="git commondir"
            ).decode("utf-8").strip()
        except (StateRootError, UnicodeDecodeError):
            rel = ""
        if rel:
            candidate = Path(rel)
            if not candidate.is_absolute():
                candidate = gitdir / candidate
            return Path(os.path.normpath(candidate))
    if gitdir.name and gitdir.parent.name == "worktrees":
        return gitdir.parent.parent
    return gitdir


def _discover_project(
    *,
    start: Path,
    explicit: Path | str | None,
    env: Mapping[str, str],
    here: bool,
) -> tuple[Path, str, tuple[Path, Path] | None]:
    git_ids = _git_worktree_and_common(start)
    if here:
        return start, "here", git_ids
    if explicit is not None and str(explicit).strip() != "":
        return _resolve_existing_dir(Path(str(explicit)), label="project-root"), "explicit", git_ids
    raw_env = (env.get(ENV_PROJECT_ROOT) or "").strip()
    if raw_env:
        return _resolve_existing_dir(Path(raw_env), label=ENV_PROJECT_ROOT), "env", git_ids
    worktree_owner = owning_project_from_omg_worktree(start)
    if worktree_owner is not None and (worktree_owner / ".omg").is_dir():
        return worktree_owner.resolve(), "omg", git_ids
    omg_roots = list_omg_ancestors(start)
    if omg_roots and git_ids is not None:
        git_root = git_ids[0]
        inside = [p for p in omg_roots if path_is_under(p, git_root)]
        if inside:
            return inside[0].resolve(), "omg", git_ids
        return git_root, "git", git_ids
    if omg_roots:
        usable = [
            p
            for p in omg_roots
            if not (is_shared_temp_root(p, env=env) and p.resolve() != start)
        ]
        if usable:
            return usable[0].resolve(), "omg", git_ids
    if git_ids is not None:
        return git_ids[0], "git", git_ids
    return start, "cwd", git_ids


def _git_ids_for_root(project_root: Path, discovered: tuple[Path, Path] | None) -> tuple[Path, Path] | None:
    if discovered is not None and discovered[0] == project_root:
        return discovered
    probed = _git_worktree_and_common(project_root)
    if probed is not None and probed[0] == project_root:
        return probed
    return None


def _central_state_dir(central: Path, project_key: str) -> Path:
    planned = central / project_key
    if os.path.lexists(planned) and os.path.islink(planned):
        raise StateRootError("centralized project state leaf may not be a symlink")
    return planned


def resolve_state_root(
    *,
    cwd: Path | str | None = None,
    explicit_project_root: Path | str | None = None,
    explicit_state_dir: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    here: bool = False,
    enable_workspace_marker: bool | None = None,
    home: Path | str | None = None,
) -> StateRootResolution:
    """Resolve the state-root contract from injected inputs only."""
    env_map: Mapping[str, str] = os.environ if env is None else env
    if cwd is not None:
        start = Path(cwd)
    else:
        start = Path.cwd()
    try:
        start = start.expanduser().resolve()
    except OSError as exc:
        raise StateRootError("cannot resolve cwd") from exc
    if not start.is_dir():
        raise StateRootError("cwd is not a directory")

    if home is not None:
        home_path = Path(home)
    else:
        home_raw = (env_map.get("HOME") or "").strip()
        home_path = Path(home_raw) if home_raw else Path.home()
    try:
        home_path = home_path.expanduser().resolve()
    except OSError as exc:
        raise StateRootError("cannot resolve HOME") from exc

    project_root, project_source, discovered_git = _discover_project(
        start=start,
        explicit=explicit_project_root,
        env=env_map,
        here=here,
    )
    git_ids = _git_ids_for_root(project_root, discovered_git)

    implicit = project_source not in {"explicit", "env", "here"}
    if implicit and _is_broad_scope(project_root, home_path):
        raise StateRootError("refusing implicit HOME or filesystem-root state scope")
    home_scope = "explicit_override" if _is_broad_scope(project_root, home_path) else "ok"

    killed = _flag(env_map, ENV_DISABLE_WORKSPACE_MARKER)
    if enable_workspace_marker is None:
        marker_enabled = _flag(env_map, ENV_WORKSPACE_MARKER)
    else:
        marker_enabled = bool(enable_workspace_marker)
    if killed:
        marker_state = "killed"
        marker_enabled = False
    elif marker_enabled:
        marker_state = "absent"
    else:
        marker_state = "disabled"

    raw_central = explicit_state_dir
    if raw_central is None or str(raw_central).strip() == "":
        raw_central = (env_map.get(ENV_STATE_DIR) or "").strip() or None

    if raw_central is not None and str(raw_central).strip() != "":
        central = _planned_physical_dir(
            Path(str(raw_central)), cwd=start, label="centralized state directory"
        )
        if git_ids is not None:
            identity_kind = "git_common"
            identity = str(git_ids[1])
        else:
            identity_kind = "project_root"
            identity = str(project_root)
        project_key = _opaque_key("centralized", identity)
        state_dir = _central_state_dir(central, project_key)
        return StateRootResolution(
            project_root=project_root,
            state_dir=state_dir,
            source="centralized_env",
            scope=SCOPE_CENTRALIZED,
            project_key=project_key,
            diagnostics=_diagnostics(
                project_root_source=project_source,
                identity_kind=identity_kind,
                marker=marker_state,
                home_scope=home_scope,
            ),
        )

    if marker_enabled:
        workspace = _find_workspace_marker(start, home_path)
        if workspace is not None:
            project_key = _opaque_key("workspace", str(workspace))
            state_dir = workspace / ".omg"
            if os.path.lexists(state_dir) and os.path.islink(state_dir):
                raise StateRootError("workspace state directory may not be a symlink")
            return StateRootResolution(
                project_root=project_root,
                state_dir=state_dir,
                source="workspace_marker",
                scope=SCOPE_WORKSPACE,
                project_key=project_key,
                diagnostics=_diagnostics(
                    project_root_source=project_source,
                    identity_kind="workspace",
                    marker="used",
                    home_scope=home_scope,
                ),
            )

    planned = project_root / ".omg"
    if os.path.lexists(planned) and os.path.islink(planned):
        raise StateRootError("per-worktree state directory may not be a symlink")
    project_key = _opaque_key("per_worktree", str(project_root))
    return StateRootResolution(
        project_root=project_root,
        state_dir=planned,
        source="project_derived",
        scope=SCOPE_PER_WORKTREE,
        project_key=project_key,
        diagnostics=_diagnostics(
            project_root_source=project_source,
            identity_kind="project_root",
            marker=marker_state,
            home_scope=home_scope,
        ),
    )
