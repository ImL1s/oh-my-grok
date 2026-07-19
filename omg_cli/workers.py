# omg_cli/workers.py
"""No-shell worker prepare/seal bridge for ULW result envelopes.

Read-write workers lack Execute/shell, so they cannot ``git commit`` themselves.
The leader (or operator) runs ``omg worker prepare`` / ``omg worker seal`` to:

1. create a worktree under ``.omg/worktrees/<run_id>/<task_id>``
2. stage+commit changes in that worktree and write a ULW envelope JSON

Only the omg CLI owns envelopes under ``.omg/artifacts/ulw-results/``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.integrate import (
    _run_git,
    default_envelopes_dir,
    git_available,
    git_rev_parse_head,
)

CLI_WRITER = "omg-cli"
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Capture real subprocess for git helpers (immune to grok-isolation patches).
_REAL_RUN = subprocess.run


class WorkerError(RuntimeError):
    """prepare/seal failures callers may handle."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_task_id(task_id: str) -> str:
    tid = (task_id or "").strip()
    if not tid or not _TASK_ID_RE.match(tid):
        raise WorkerError(
            f"invalid task_id {task_id!r}; expected [A-Za-z0-9][A-Za-z0-9._-]{{0,63}}"
        )
    return tid


def worktree_dir(root: Path | str, run_id: str, task_id: str) -> Path:
    root = Path(root).resolve()
    return root / ".omg" / "worktrees" / run_id / validate_task_id(task_id)


def envelope_path(root: Path | str, task_id: str) -> Path:
    return default_envelopes_dir(Path(root)) / f"{validate_task_id(task_id)}.json"


def _branch_name(run_id: str, task_id: str) -> str:
    # Keep branch short and filesystem-safe
    rid = re.sub(r"[^A-Za-z0-9._-]+", "-", run_id)[:40]
    return f"omg/{rid}/{task_id}"


def prepare_task(root: Path | str, run_id: str, task_id: str) -> Path:
    """Create ``.omg/worktrees/<run_id>/<task_id>`` via ``git worktree add`` if possible.

    Falls back to mkdir-only (documented clone path) when not a git work tree
    or ``git worktree add`` fails.

    Returns the worktree path.
    """
    root = Path(root).resolve()
    task_id = validate_task_id(task_id)
    run_id = (run_id or "").strip()
    if not run_id:
        raise WorkerError("run_id required")

    wt = worktree_dir(root, run_id, task_id)
    if wt.is_dir() and (wt / ".git").exists():
        return wt
    if wt.is_dir() and any(wt.iterdir()):
        # Already prepared as clone/mkdir path
        return wt

    wt.parent.mkdir(parents=True, exist_ok=True)

    if not git_available(root):
        wt.mkdir(parents=True, exist_ok=True)
        # Document clone fallback for operators
        note = wt / "OMG_WORKTREE_NOTE.txt"
        note.write_text(
            "git worktree unavailable: directory created as clone path.\n"
            "Clone or copy the project here, commit worker changes, then "
            "`omg worker seal --task <id>` from the project root.\n",
            encoding="utf-8",
        )
        return wt

    branch = _branch_name(run_id, task_id)
    # Prefer linked worktree on a new branch from HEAD
    r = _run_git(
        ["worktree", "add", "-b", branch, str(wt), "HEAD"],
        cwd=root,
        timeout=120.0,
    )
    if r.returncode != 0:
        # Branch may already exist — try without -b
        r2 = _run_git(
            ["worktree", "add", str(wt), branch],
            cwd=root,
            timeout=120.0,
        )
        if r2.returncode != 0:
            # Final fallback: mkdir clone path
            wt.mkdir(parents=True, exist_ok=True)
            note = wt / "OMG_WORKTREE_NOTE.txt"
            err = (r.stderr or r2.stderr or "").strip()
            note.write_text(
                "git worktree add failed; directory created as clone path.\n"
                f"error: {err}\n"
                "Clone or copy the project here, commit worker changes, then "
                "`omg worker seal --task <id>` from the project root.\n",
                encoding="utf-8",
            )
            return wt
    return wt


def _list_changed_files(worktree: Path, base_sha: str, head_sha: str) -> list[str]:
    if base_sha.lower() == head_sha.lower():
        return []
    r = _run_git(
        ["diff", "--name-only", base_sha, head_sha],
        cwd=worktree,
    )
    if r.returncode != 0:
        return []
    return [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]


def _porcelain_has_changes(worktree: Path) -> bool | None:
    """Return True if dirty, False if clean, None if status failed (fail-closed)."""
    r = _run_git(["status", "--porcelain"], cwd=worktree)
    if r.returncode != 0:
        return None
    return bool((r.stdout or "").strip())


def seal_task(
    root: Path | str,
    run_id: str,
    task_id: str,
    *,
    message: str = "omg seal",
    base_sha: str | None = None,
    status: str = "ok",
    evidence: str = "",
) -> dict[str, Any]:
    """Commit worktree changes (if any) and write ULW envelope JSON.

    - Runs ``git add -A`` + ``git commit`` in the worktree when dirty
    - Does **not** allow empty commits when nothing changed (returns failed
      envelope if there is no new head beyond base and no prior commit)
    - Records base_sha (from run or arg), head_sha, changed_files
    - Writes ``.omg/artifacts/ulw-results/<task_id>.json``

    Returns the envelope dict.
    """
    root = Path(root).resolve()
    task_id = validate_task_id(task_id)
    run_id = (run_id or "").strip()
    if not run_id:
        raise WorkerError("run_id required")
    if status not in ("ok", "failed"):
        raise WorkerError(f"status must be ok|failed, got {status!r}")

    wt = worktree_dir(root, run_id, task_id)
    if not wt.is_dir():
        raise WorkerError(
            f"worktree missing: {wt}; run `omg worker prepare --task {task_id}` first"
        )

    # Resolve base_sha from run if not provided
    if base_sha is None:
        from omg_cli.state import load_run

        run = load_run(root, run_id)
        if run is not None:
            rb = run.get("base_sha")
            if isinstance(rb, str) and rb.strip():
                base_sha = rb.strip()
        if base_sha is None:
            base_sha = git_rev_parse_head(root)
    if not base_sha:
        raise WorkerError("base_sha unavailable (not a git repo / no run base_sha)")
    base_sha = base_sha.strip().lower()

    head_sha = base_sha
    commit_msg = (message or "omg seal").strip() or "omg seal"
    seal_note: str | None = None

    is_git = git_available(wt) or (wt / ".git").exists()
    dirty = _porcelain_has_changes(wt) if is_git else None
    if is_git and dirty is None:
        seal_note = "git status --porcelain failed; fail-closed seal"
        status = "failed"
        evidence = (evidence + "\n" if evidence else "") + seal_note
        head_now = git_rev_parse_head(wt)
        if head_now:
            head_sha = head_now.strip().lower()
    elif is_git and dirty:
        r_add = _run_git(["add", "-A"], cwd=wt)
        if r_add.returncode != 0:
            raise WorkerError(
                f"git add -A failed: {(r_add.stderr or r_add.stdout or '').strip()}"
            )
        # Commit only if index has staged changes
        r_diff = _run_git(["diff", "--cached", "--quiet"], cwd=wt)
        if r_diff.returncode != 0:
            # Non-zero means there are staged changes
            r_c = _run_git(
                ["commit", "-m", commit_msg],
                cwd=wt,
                timeout=60.0,
            )
            if r_c.returncode != 0:
                raise WorkerError(
                    f"git commit failed: {(r_c.stderr or r_c.stdout or '').strip()}"
                )
        head_now = git_rev_parse_head(wt)
        if head_now:
            head_sha = head_now.strip().lower()
        # Still dirty after seal attempt → failed
        still_dirty = _porcelain_has_changes(wt)
        if still_dirty is True:
            seal_note = "worktree still dirty after seal; refuse ok envelope"
            status = "failed"
            evidence = (evidence + "\n" if evidence else "") + seal_note
        elif head_sha == base_sha:
            # Dirty but nothing staged/committed (e.g. ignored-only noise)
            seal_note = "dirty worktree produced no new commit (head==base)"
            status = "failed"
            evidence = (evidence + "\n" if evidence else "") + seal_note
    elif is_git:
        head_now = git_rev_parse_head(wt)
        if head_now:
            head_sha = head_now.strip().lower()
        if head_sha == base_sha:
            seal_note = "no changes to commit"
            if status == "ok":
                # Nothing new — still write envelope but mark failed unless
                # worker already committed earlier (head > base).
                status = "failed"
                evidence = (evidence + "\n" if evidence else "") + seal_note
    else:
        seal_note = "worktree is not a git checkout; cannot commit"
        status = "failed"
        evidence = (evidence + "\n" if evidence else "") + seal_note

    # Final integrity: ok envelope must advance head beyond base
    if status == "ok" and head_sha == base_sha:
        seal_note = (seal_note or "") + "; refuse ok when head_sha==base_sha"
        status = "failed"
        evidence = (evidence + "\n" if evidence else "") + seal_note.strip("; ")

    changed = _list_changed_files(wt, base_sha, head_sha) if is_git else []

    envelope: dict[str, Any] = {
        "task_id": task_id,
        "base_sha": base_sha,
        "head_sha": head_sha,
        "worktree_path": str(wt),
        "status": status,
        "changed_files": changed,
        "evidence": evidence or (seal_note or ""),
        "writer": CLI_WRITER,
        "run_id": run_id,
        "sealed_at": _utc_now(),
        "message": commit_msg,
    }
    if seal_note:
        envelope["note"] = seal_note

    out = envelope_path(root, task_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(envelope, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass

    envelope["_source"] = str(out)
    return envelope


__all__ = [
    "WorkerError",
    "envelope_path",
    "prepare_task",
    "seal_task",
    "validate_task_id",
    "worktree_dir",
]
