"""Canonical project-root discovery for all project-scoped CLI commands (#22).

Precedence (highest first):

1. explicit ``--project-root PATH`` (or API ``explicit=``)
2. ``OMG_PROJECT_ROOT`` environment variable
3. owning project of ``<root>/.omg/worktrees/…`` when cwd is inside that tree
4. nearest ancestor containing a real ``.omg/`` control-plane directory
   that is **inside** the git toplevel (when git exists); unrelated ancestors
   (especially shared temp like ``/tmp/.omg``) are ignored
5. ``git rev-parse --show-toplevel`` for the starting directory
6. the starting directory (normally ``cwd``)

``here=True`` (``omg setup --here``) forces the starting directory and skips
discovery. Install/global hooks are not project-scoped and must not call this
for their install target.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


ENV_PROJECT_ROOT = "OMG_PROJECT_ROOT"


class ProjectRootError(ValueError):
    """Invalid override or unusable root. CLI maps to exit code 2."""

    exit_code = 2


@dataclass(frozen=True)
class ProjectRootResolution:
    """Resolved root plus diagnostics for doctor / status."""

    root: Path
    source: str  # explicit | env | omg | git | cwd | here
    cwd: Path
    shadowed_omg_ancestors: tuple[Path, ...] = ()
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_root": str(self.root),
            "project_root_source": self.source,
            "cwd": str(self.cwd),
            "shadowed_omg_ancestors": [str(p) for p in self.shadowed_omg_ancestors],
            "note": self.note,
        }


def _validate_root_dir(path: Path, *, label: str) -> Path:
    try:
        resolved = path.expanduser().resolve()
    except OSError as exc:
        raise ProjectRootError(f"{label} is not a usable path: {path}: {exc}") from exc
    if not resolved.exists():
        raise ProjectRootError(f"{label} does not exist: {resolved}")
    if not resolved.is_dir():
        raise ProjectRootError(f"{label} is not a directory: {resolved}")
    if resolved.is_symlink():
        # resolve() already followed; re-check final for clarity
        pass
    return resolved


def _omg_control_plane_dir(candidate: Path) -> bool:
    """True when ``candidate/.omg`` is a real directory (control-plane root parent)."""
    omg = candidate / ".omg"
    try:
        return omg.is_dir() and not omg.is_symlink()
    except OSError:
        return False


def path_is_under(path: Path, parent: Path) -> bool:
    """True when *path* is *parent* or a descendant (after resolve)."""
    try:
        return path.resolve() == parent.resolve() or path.resolve().is_relative_to(
            parent.resolve()
        )
    except (OSError, ValueError):
        return False


def _inside_omg_tree(path: Path) -> bool:
    """True when *path* lives inside a ``.omg/`` directory (nested worktree, prompts)."""
    return any(part == ".omg" for part in path.parts)


def owning_project_from_omg_worktree(start: Path) -> Path | None:
    """If *start* is under ``<project>/.omg/worktrees/…``, return ``<project>``."""
    cur = start.resolve()
    while True:
        if cur.name == "worktrees" and cur.parent.name == ".omg":
            owner = cur.parent.parent
            if owner != cur.parent:
                return owner
            return None
        parent = cur.parent
        if parent == cur:
            return None
        cur = parent


def is_shared_temp_root(path: Path) -> bool:
    """True for well-known shared temp directories (never implicit project roots)."""
    try:
        resolved = path.resolve()
    except OSError:
        return False
    known: list[Path] = []
    for raw in ("/tmp", "/var/tmp", "/private/tmp"):
        try:
            known.append(Path(raw).resolve())
        except OSError:
            known.append(Path(raw))
    for key in ("TMPDIR", "TMP", "TEMP"):
        val = (os.environ.get(key) or "").strip()
        if not val:
            continue
        try:
            known.append(Path(val).expanduser().resolve())
        except OSError:
            continue
    return resolved in known


def list_omg_ancestors(start: Path) -> list[Path]:
    """Return project roots (parents of ``.omg``) from nearest to farthest.

    Directories *inside* an existing ``.omg/`` tree (team worktree prompts,
    ``.omg/worktrees/…``) are not control-plane roots.
    """
    found: list[Path] = []
    cur = start.resolve()
    while True:
        if _omg_control_plane_dir(cur) and not _inside_omg_tree(cur):
            found.append(cur)
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return found


def git_toplevel(start: Path, *, timeout_s: float = 5.0) -> Path | None:
    """Return git worktree top-level for *start*, or None if not a git worktree."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return None
    try:
        return Path(line[0]).resolve()
    except OSError:
        return None


def resolve_project_root(
    *,
    cwd: Path | str | None = None,
    explicit: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    here: bool = False,
    env_var: str = ENV_PROJECT_ROOT,
) -> ProjectRootResolution:
    """Resolve the canonical project root with documented precedence."""
    start = Path(cwd) if cwd is not None else Path.cwd()
    try:
        start = start.resolve()
    except OSError as exc:
        raise ProjectRootError(f"cannot resolve cwd: {exc}") from exc

    if here:
        return ProjectRootResolution(
            root=start,
            source="here",
            cwd=start,
            note="setup --here forced cwd; discovery skipped",
        )

    if explicit is not None and str(explicit).strip() != "":
        root = _validate_root_dir(Path(str(explicit)), label="--project-root")
        return ProjectRootResolution(
            root=root,
            source="explicit",
            cwd=start,
        )

    env_map = env if env is not None else os.environ
    raw_env = (env_map.get(env_var) or "").strip()
    if raw_env:
        root = _validate_root_dir(Path(raw_env), label=env_var)
        return ProjectRootResolution(
            root=root,
            source="env",
            cwd=start,
            note=f"from {env_var}",
        )

    worktree_owner = owning_project_from_omg_worktree(start)
    if worktree_owner is not None and _omg_control_plane_dir(worktree_owner):
        omg_roots = list_omg_ancestors(start)
        shadowed = tuple(p for p in omg_roots if p != worktree_owner)
        return ProjectRootResolution(
            root=worktree_owner,
            source="omg",
            cwd=start,
            shadowed_omg_ancestors=shadowed,
            note="resolved owning project from .omg/worktrees path",
        )

    omg_roots = list_omg_ancestors(start)
    git_root = git_toplevel(start)

    def _omg_resolution(
        nearest: Path, *, ignored: tuple[Path, ...] = ()
    ) -> ProjectRootResolution:
        shadowed = tuple(p for p in omg_roots if p != nearest)
        note = None
        bits: list[str] = []
        if shadowed:
            bits.append(
                f"nearest .omg at {nearest} shadows ancestor control plane(s): "
                + ", ".join(str(p) for p in shadowed)
                + "; not auto-merged (see docs/project-root.md)"
            )
        if ignored:
            bits.append(
                "ignored unrelated ancestor .omg: "
                + ", ".join(str(p) for p in ignored)
            )
        if bits:
            note = "; ".join(bits)
        return ProjectRootResolution(
            root=nearest,
            source="omg",
            cwd=start,
            shadowed_omg_ancestors=shadowed,
            note=note,
        )

    if omg_roots and git_root is not None:
        inside = [p for p in omg_roots if path_is_under(p, git_root)]
        ignored = tuple(p for p in omg_roots if p not in inside)
        if inside:
            return _omg_resolution(inside[0], ignored=ignored)
        return ProjectRootResolution(
            root=git_root,
            source="git",
            cwd=start,
            note=(
                "ignored unrelated ancestor .omg outside git toplevel: "
                + ", ".join(str(p) for p in ignored)
            ),
        )

    if omg_roots:
        usable: list[Path] = []
        ignored_temp: list[Path] = []
        for candidate in omg_roots:
            if is_shared_temp_root(candidate) and candidate != start:
                ignored_temp.append(candidate)
                continue
            usable.append(candidate)
        if usable:
            return _omg_resolution(usable[0], ignored=tuple(ignored_temp))

    if git_root is not None:
        return ProjectRootResolution(
            root=git_root,
            source="git",
            cwd=start,
        )

    return ProjectRootResolution(
        root=start,
        source="cwd",
        cwd=start,
        note="no .omg ancestor and not a git worktree; using cwd",
    )


# Process-local resolved root (set once per CLI invocation after argv parse).
_RESOLVED: ProjectRootResolution | None = None


def clear_resolved_project_root() -> None:
    global _RESOLVED
    _RESOLVED = None


def set_resolved_project_root(resolution: ProjectRootResolution) -> None:
    global _RESOLVED
    _RESOLVED = resolution


def get_resolved_project_root() -> ProjectRootResolution | None:
    return _RESOLVED


def project_root(
    *,
    cwd: Path | str | None = None,
    explicit: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    here: bool = False,
) -> Path:
    """Return only the Path; uses process resolution when already set."""
    if (
        _RESOLVED is not None
        and explicit is None
        and not here
        and cwd is None
        and env is None
    ):
        return _RESOLVED.root
    return resolve_project_root(
        cwd=cwd, explicit=explicit, env=env, here=here
    ).root
