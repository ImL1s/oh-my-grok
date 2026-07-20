# omg_cli/workers.py
"""No-shell worker prepare/seal bridge for ULW result envelopes.

Read-write workers lack Execute/shell, so they cannot ``git commit`` themselves.
The leader (or operator) runs ``omg worker prepare`` / ``omg worker seal`` to:

1. create a worktree under ``.omg/worktrees/<run_id>/<task_id>``
2. stage+commit changes in that worktree and write a run-scoped ULW envelope

Only the omg CLI owns envelopes under
``.omg/artifacts/ulw-results/<run_id>/``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from omg_cli.evidence import EvidenceError, validate_identifier
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
    try:
        safe_run_id = validate_identifier(run_id, label="run_id")
    except EvidenceError as exc:
        raise WorkerError(str(exc)) from exc
    return root / ".omg" / "worktrees" / safe_run_id / validate_task_id(task_id)


def envelope_path(
    root: Path | str,
    task_id: str,
    *,
    run_id: str | None = None,
) -> Path:
    """Return an envelope path.

    New writes must supply ``run_id``.  Omitting it preserves the legacy
    inspection helper only; ``seal_task`` and integration never use that root.
    """

    return default_envelopes_dir(Path(root), run_id) / (
        f"{validate_task_id(task_id)}.json"
    )


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
    try:
        run_id = validate_identifier(run_id, label="run_id")
    except EvidenceError as exc:
        raise WorkerError(str(exc)) from exc

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
    - Writes ``.omg/artifacts/ulw-results/<run_id>/<task_id>.json``

    Returns the envelope dict.
    """
    root = Path(root).resolve()
    task_id = validate_task_id(task_id)
    try:
        run_id = validate_identifier(run_id, label="run_id")
    except EvidenceError as exc:
        raise WorkerError(str(exc)) from exc
    if status not in ("ok", "failed"):
        raise WorkerError(f"status must be ok|failed, got {status!r}")

    wt = worktree_dir(root, run_id, task_id)
    if not wt.is_dir():
        raise WorkerError(
            f"worktree missing: {wt}; run `omg worker prepare --task {task_id}` first"
        )

    # Validate schema before any envelope write.  Classification is read-only
    # and never upgrades legacy status in place.
    from omg_cli.state import classify_run_schema, load_run

    run = load_run(root, run_id)
    if run is None:
        raise WorkerError(f"no status.json for run_id={run_id!r}")
    try:
        classify_run_schema(run)
    except (TypeError, ValueError) as exc:
        raise WorkerError(f"unsupported run schema for seal: {exc}") from exc

    # Resolve base_sha from run if not provided.
    if base_sha is None:
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

    out = envelope_path(root, task_id, run_id=run_id)
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


def ownership_manifest_path(root: Path | str, run_id: str) -> Path:
    try:
        run_id = validate_identifier(run_id, label="run_id")
    except EvidenceError as exc:
        raise WorkerError(str(exc)) from exc
    return Path(root).resolve() / ".omg" / "state" / "runs" / run_id / "task_ownership.json"


def build_ownership_manifest(
    root: Path | str,
    run_id: str,
    tasks: list[dict[str, Any]],
    *,
    required_capability_mode: str = "read-write",
) -> dict[str, Any]:
    """Persist task ownership for a ULW run (CLI-authoritative).

    Each task needs: task_id, owned_files (list[str]), optional role.
    Shared-file collisions require an explicit coordination boundary.
    """
    root = Path(root).resolve()
    try:
        run_id = validate_identifier(run_id, label="run_id")
    except EvidenceError as exc:
        raise WorkerError(str(exc)) from exc
    if required_capability_mode not in ("read-write", "read-only"):
        raise WorkerError(
            f"invalid required_capability_mode={required_capability_mode!r}"
        )
    if not tasks:
        raise WorkerError("at least one task is required")

    entries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    file_owners: dict[str, str] = {}
    for raw in tasks:
        if not isinstance(raw, Mapping):
            raise WorkerError("each task must be an object")
        tid = validate_task_id(str(raw.get("task_id") or raw.get("id") or ""))
        if tid in seen_ids:
            raise WorkerError(f"duplicate task_id: {tid}")
        seen_ids.add(tid)
        owned = raw.get("owned_files") or raw.get("files") or []
        if not isinstance(owned, list) or not all(isinstance(f, str) for f in owned):
            raise WorkerError(f"task {tid}: owned_files must be a string list")
        owned_norm = [f.strip().lstrip("./") for f in owned if f.strip()]
        if not owned_norm:
            raise WorkerError(f"task {tid}: owned_files must be non-empty")
        coord = str(raw.get("coordination") or "").strip()
        for f in owned_norm:
            if f in file_owners and file_owners[f] != tid:
                if not coord:
                    raise WorkerError(
                        f"shared-file collision on {f!r} between "
                        f"{file_owners[f]!r} and {tid!r}; set coordination "
                        "boundary or serialize tasks"
                    )
            file_owners[f] = tid
        role = str(raw.get("role") or "omg-executor").strip()
        cap = str(
            raw.get("capability_mode") or required_capability_mode
        ).strip()
        if cap not in ("read-write", "read-only"):
            raise WorkerError(f"task {tid}: bad capability_mode {cap!r}")
        wt = worktree_dir(root, run_id, tid)
        entries.append(
            {
                "task_id": tid,
                "role": role,
                "capability_mode": cap,
                "owned_files": owned_norm,
                "worktree_path": str(wt),
                "coordination": coord or None,
                "status": "planned",
            }
        )

    manifest = {
        "writer": CLI_WRITER,
        "schema_version": 1,
        "run_id": run_id,
        "required_capability_mode": required_capability_mode,
        "tasks": entries,
        "created_at": _utc_now(),
        "status": "open",
    }
    path = ownership_manifest_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return manifest


def load_ownership_manifest(root: Path | str, run_id: str) -> dict[str, Any]:
    path = ownership_manifest_path(root, run_id)
    if not path.is_file():
        raise WorkerError(f"ownership manifest missing for run {run_id}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkerError(f"ownership manifest unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("writer") != CLI_WRITER:
        raise WorkerError("ownership manifest lacks CLI writer authority")
    return data


def join_worker_results(
    root: Path | str,
    run_id: str,
    *,
    require_all_ok: bool = True,
) -> dict[str, Any]:
    """Join sealed envelopes against ownership manifest.

    Missing task result or failed envelope blocks completion.
    Envelopes without matching ownership entries are ignored for join but
    cannot alone satisfy the manifest.
    """
    root = Path(root).resolve()
    try:
        run_id = validate_identifier(run_id, label="run_id")
    except EvidenceError as exc:
        raise WorkerError(str(exc)) from exc
    manifest = load_ownership_manifest(root, run_id)
    tasks = list(manifest.get("tasks") or [])
    if len(tasks) < 1:
        raise WorkerError("ownership manifest has no tasks")

    results: list[dict[str, Any]] = []
    missing: list[str] = []
    failed: list[str] = []
    for task in tasks:
        tid = task["task_id"]
        epath = envelope_path(root, tid, run_id=run_id)
        if not epath.is_file():
            missing.append(tid)
            results.append(
                {
                    "task_id": tid,
                    "present": False,
                    "status": "missing",
                    "capability_mode": task.get("capability_mode"),
                }
            )
            continue
        try:
            env = json.loads(epath.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failed.append(tid)
            results.append(
                {
                    "task_id": tid,
                    "present": True,
                    "status": "corrupt",
                    "capability_mode": task.get("capability_mode"),
                }
            )
            continue
        # Envelope writer alone is not enough; must be CLI seal for ok path
        st = env.get("status")
        writer = env.get("writer")
        if writer != CLI_WRITER:
            failed.append(tid)
            results.append(
                {
                    "task_id": tid,
                    "present": True,
                    "status": "untrusted_writer",
                    "capability_mode": task.get("capability_mode"),
                    "envelope_status": st,
                }
            )
            continue
        if st != "ok":
            failed.append(tid)
        results.append(
            {
                "task_id": tid,
                "present": True,
                "status": st,
                "capability_mode": task.get("capability_mode"),
                "head_sha": env.get("head_sha"),
                "base_sha": env.get("base_sha"),
                "worktree_path": env.get("worktree_path"),
                "writer": writer,
            }
        )

    complete = not missing and (not failed if require_all_ok else True)
    out = {
        "writer": CLI_WRITER,
        "run_id": run_id,
        "task_count": len(tasks),
        "results": results,
        "missing": missing,
        "failed": failed,
        "complete": complete,
        "blocked_reason": None
        if complete
        else (
            f"missing={missing} failed={failed}"
            if missing or failed
            else "join incomplete"
        ),
        "joined_at": _utc_now(),
    }
    # Persist join report under run stages (CLI write)
    report = (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "stages"
        / "ulw_join.json"
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(out, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    # Update manifest status
    manifest = dict(manifest)
    manifest["status"] = "complete" if complete else "blocked"
    manifest["join"] = {
        "complete": complete,
        "missing": missing,
        "failed": failed,
        "joined_at": out["joined_at"],
    }
    path = ownership_manifest_path(root, run_id)
    path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return out


def prepare_owned_tasks(root: Path | str, run_id: str) -> list[Path]:
    """Prepare worktrees for every task in the ownership manifest."""
    manifest = load_ownership_manifest(root, run_id)
    paths: list[Path] = []
    for task in manifest.get("tasks") or []:
        paths.append(prepare_task(root, run_id, task["task_id"]))
    return paths


__all__ = [
    "WorkerError",
    "build_ownership_manifest",
    "envelope_path",
    "join_worker_results",
    "load_ownership_manifest",
    "ownership_manifest_path",
    "prepare_owned_tasks",
    "prepare_task",
    "seal_task",
    "validate_task_id",
    "worktree_dir",
]
