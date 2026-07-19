# omg_cli/integrate.py
"""ULW clean-tree preflight + result-envelope integrator.

Child workers write envelopes under ``.omg/artifacts/ulw-results/<task_id>.json``.
The leader (or ``omg integrate``) applies them in ``task_id`` order via
``git cherry-pick`` of each envelope's ``head_sha`` onto the project root.

Only the omg CLI owns integration status under
``.omg/state/runs/<run_id>/integrate.result.json``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CLI_WRITER = "omg-cli"
ENVELOPES_REL = Path(".omg") / "artifacts" / "ulw-results"
RESULT_NAME = "integrate.result.json"

# Minimal envelope keys required by the ULW convergence protocol.
REQUIRED_ENVELOPE_KEYS = (
    "task_id",
    "base_sha",
    "head_sha",
    "worktree_path",
    "status",
    "changed_files",
)
VALID_ENVELOPE_STATUSES = frozenset({"ok", "failed"})

_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")

# Capture real subprocess entry points at import time so git helpers still work
# when tests monkeypatch ``subprocess.Popen`` / ``run`` to isolate grok launch.
_REAL_POPEN = subprocess.Popen
_REAL_RUN = subprocess.run


class IntegrateError(RuntimeError):
    """Raised for dirty trees, bad envelopes, or apply failures callers may handle."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runs_dir(root: Path) -> Path:
    return Path(root) / ".omg" / "state" / "runs"


def run_dir(root: Path, run_id: str) -> Path:
    return _runs_dir(root) / run_id


def result_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / RESULT_NAME


def default_envelopes_dir(root: Path) -> Path:
    return Path(root) / ENVELOPES_REL


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _run_git(
    args: list[str],
    *,
    cwd: Path | str,
    check: bool = False,
    timeout: float | None = 60.0,
) -> subprocess.CompletedProcess[str]:
    """Run git with the real Popen/run, immune to grok-isolation monkeypatches."""
    prev_popen = subprocess.Popen
    prev_run = subprocess.run
    subprocess.Popen = _REAL_POPEN  # type: ignore[misc, assignment]
    subprocess.run = _REAL_RUN  # type: ignore[misc, assignment]
    try:
        return _REAL_RUN(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )
    finally:
        subprocess.Popen = prev_popen  # type: ignore[misc, assignment]
        subprocess.run = prev_run  # type: ignore[misc, assignment]


def git_available(root: Path | str | None = None) -> bool:
    """True if ``git`` runs and (when root given) root is inside a work tree."""
    try:
        if root is None:
            r = subprocess.run(
                ["git", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return r.returncode == 0
        r = _run_git(["rev-parse", "--is-inside-work-tree"], cwd=root)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False


def git_rev_parse_head(root: Path | str) -> str | None:
    """Return ``HEAD`` full sha for ``root``, or None if not a git work tree."""
    root = Path(root)
    try:
        r = _run_git(["rev-parse", "HEAD"], cwd=root)
    except Exception:
        # OSError / Timeout / broken mocks in tests — best-effort only
        return None
    if r.returncode != 0:
        return None
    sha = (r.stdout or "").strip()
    return sha if _SHA_RE.match(sha) else None


def _porcelain_is_dirty(porcelain: str) -> bool:
    """True if porcelain output has real dirt, ignoring oh-my-grok ``.omg/`` state.

    Runtime state under ``.omg/`` is expected untracked/modified during runs and
    must not block ULW integrate preflight (create_run always writes there).
    """
    for line in porcelain.splitlines():
        line = line.rstrip("\n")
        if not line.strip():
            continue
        # porcelain: XY PATH or XY ORIG -> PATH (rename)
        path_part = line[3:] if len(line) > 3 else line
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[-1]
        path_part = path_part.strip().strip('"')
        # Ignore .omg runtime tree (and nested paths)
        if path_part == ".omg" or path_part.startswith(".omg/"):
            continue
        return True
    return False


def preflight_clean_tree(root: Path | str) -> None:
    """Require clean work tree (ignoring ``.omg/``). No auto-stash.

    Raises:
        IntegrateError: dirty tree, not a git repo, or git unavailable.
    """
    root = Path(root)
    if not git_available(root):
        raise IntegrateError(
            f"preflight_clean_tree: not a git work tree or git missing: {root}"
        )
    try:
        r = _run_git(["status", "--porcelain"], cwd=root)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise IntegrateError(f"preflight_clean_tree: git status failed: {exc}") from exc
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        raise IntegrateError(f"preflight_clean_tree: git status failed: {err}")
    if _porcelain_is_dirty(r.stdout or ""):
        raise IntegrateError(
            "preflight_clean_tree: working tree is dirty "
            "(git status --porcelain not empty); commit/stash first — no auto-stash"
        )


def record_base_sha(root: Path | str, run_id: str | None = None) -> str | None:
    """Capture ``git rev-parse HEAD`` and optionally persist on the run.

    When ``run_id`` is provided, writes ``base_sha`` into that run's
    ``status.json`` via ``write_status`` (extra field). Returns the sha or None
    when git is unavailable.
    """
    root = Path(root)
    sha = git_rev_parse_head(root)
    if sha is None:
        return None
    if run_id is not None:
        from omg_cli.state import write_status

        # Preserve current status value while attaching base_sha
        from omg_cli.state import load_run

        current = load_run(root, run_id)
        if current is None:
            raise FileNotFoundError(f"no status.json for run_id={run_id!r}")
        st = str(current.get("status") or "initialized")
        write_status(root, run_id, st, extra={"base_sha": sha})
    return sha


def validate_envelope(data: dict[str, Any]) -> dict[str, Any]:
    """Validate a child result envelope. Returns a normalized copy.

    Required keys: task_id, base_sha, head_sha, worktree_path, status,
    changed_files. ``status`` must be ``ok`` or ``failed``.

    Raises:
        ValueError: on missing/invalid fields.
    """
    if not isinstance(data, dict):
        raise ValueError("envelope must be a dict")

    missing = [k for k in REQUIRED_ENVELOPE_KEYS if k not in data]
    if missing:
        raise ValueError(f"envelope missing keys: {missing}")

    task_id = data["task_id"]
    if not isinstance(task_id, str) or not task_id.strip():
        raise ValueError("envelope.task_id must be a non-empty string")

    for sha_key in ("base_sha", "head_sha"):
        val = data[sha_key]
        if not isinstance(val, str) or not _SHA_RE.match(val.strip()):
            raise ValueError(
                f"envelope.{sha_key} must be a git object id (7–64 hex chars)"
            )

    worktree_path = data["worktree_path"]
    if not isinstance(worktree_path, str) or not worktree_path.strip():
        raise ValueError("envelope.worktree_path must be a non-empty string")

    status = data["status"]
    if not isinstance(status, str) or status not in VALID_ENVELOPE_STATUSES:
        raise ValueError(
            f"envelope.status must be one of {sorted(VALID_ENVELOPE_STATUSES)}"
        )

    changed = data["changed_files"]
    if not isinstance(changed, list):
        raise ValueError("envelope.changed_files must be a list")
    for i, item in enumerate(changed):
        if not isinstance(item, str):
            raise ValueError(f"envelope.changed_files[{i}] must be a string")

    evidence = data.get("evidence", "")
    if evidence is not None and not isinstance(evidence, str):
        raise ValueError("envelope.evidence must be a string when present")

    out: dict[str, Any] = {
        "task_id": task_id.strip(),
        "base_sha": data["base_sha"].strip().lower(),
        "head_sha": data["head_sha"].strip().lower(),
        "worktree_path": worktree_path.strip(),
        "status": status,
        "changed_files": list(changed),
    }
    if isinstance(evidence, str):
        out["evidence"] = evidence
    return out


def load_envelopes(
    envelopes_dir: Path | str,
) -> list[dict[str, Any]]:
    """Load and validate ``*.json`` envelopes; sort by ``task_id``."""
    d = Path(envelopes_dir)
    if not d.is_dir():
        return []

    loaded: list[dict[str, Any]] = []
    errors: list[str] = []
    for path in sorted(d.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        try:
            env = validate_envelope(raw if isinstance(raw, dict) else {})
        except ValueError as exc:
            errors.append(f"{path.name}: {exc}")
            continue
        env["_source"] = str(path)
        loaded.append(env)

    if errors and not loaded:
        raise IntegrateError(
            "no valid envelopes; parse errors:\n  " + "\n  ".join(errors)
        )
    # Sort by task_id (stable); keep parseable ones even if some files failed
    loaded.sort(key=lambda e: e["task_id"])
    return loaded


def _commit_exists(root: Path, sha: str) -> bool:
    r = _run_git(["cat-file", "-e", f"{sha}^{{commit}}"], cwd=root)
    return r.returncode == 0


def _ensure_commit_reachable(root: Path, head_sha: str, worktree_path: Path) -> None:
    """Make ``head_sha`` available in ``root``'s object store if needed."""
    if _commit_exists(root, head_sha):
        return
    if not worktree_path.is_dir():
        raise IntegrateError(
            f"worktree_path does not exist and head_sha not in repo: {worktree_path}"
        )
    # Fetch objects from the worker worktree/clone into the leader.
    # Works for linked worktrees (usually already present) and separate clones.
    r = _run_git(
        ["fetch", "--no-tags", str(worktree_path), head_sha],
        cwd=root,
        timeout=120.0,
    )
    if r.returncode != 0 or not _commit_exists(root, head_sha):
        # Fallback: fetch HEAD from that repo and hope head_sha is reachable
        r2 = _run_git(
            ["fetch", "--no-tags", str(worktree_path), "HEAD"],
            cwd=root,
            timeout=120.0,
        )
        if r2.returncode != 0 or not _commit_exists(root, head_sha):
            err = (r.stderr or r2.stderr or "").strip()
            raise IntegrateError(
                f"cannot obtain head_sha={head_sha} from worktree {worktree_path}: {err}"
            )


def _cherry_pick(root: Path, head_sha: str) -> None:
    """Cherry-pick ``head_sha`` onto leader. Abort and raise on conflict."""
    r = _run_git(
        ["cherry-pick", "--allow-empty", head_sha],
        cwd=root,
        timeout=120.0,
    )
    if r.returncode == 0:
        return
    # Conflict or other failure — leave tree resolvable but abort the pick
    _run_git(["cherry-pick", "--abort"], cwd=root, timeout=30.0)
    err = (r.stderr or r.stdout or "cherry-pick failed").strip()
    raise IntegrateError(f"cherry-pick conflict or failure for {head_sha}: {err}")


def integrate_results(
    root: Path | str,
    run_id: str,
    envelopes_dir: Path | str | None = None,
    *,
    dry_run: bool = False,
    skip_preflight: bool = False,
) -> dict[str, Any]:
    """Load ULW envelopes, apply in task_id order, write integrate.result.json.

    - ``preflight_clean_tree`` unless ``skip_preflight`` or ``dry_run`` (dry_run
      still validates envelopes / base_sha but does not require a clean tree).
    - Envelopes default path: ``.omg/artifacts/ulw-results/*.json``
    - ``status != ok`` → stop, overall failed (no apply for that task)
    - If run has ``base_sha``, each envelope ``base_sha`` must match
    - Apply: ensure ``head_sha`` reachable, then ``git cherry-pick head_sha``
    - Conflict → abort cherry-pick, mark failed, stop
    - Missing envelopes → result status ``missing`` (not an exception)
    """
    root = Path(root).resolve()
    env_dir = (
        Path(envelopes_dir)
        if envelopes_dir is not None
        else default_envelopes_dir(root)
    )

    from omg_cli.state import load_run, write_status

    run = load_run(root, run_id)
    if run is None:
        raise FileNotFoundError(f"no status.json for run_id={run_id!r}")

    run_base = run.get("base_sha")
    if isinstance(run_base, str):
        run_base = run_base.strip().lower() or None
    else:
        run_base = None

    if not dry_run and not skip_preflight:
        preflight_clean_tree(root)

    result: dict[str, Any] = {
        "writer": CLI_WRITER,
        "run_id": run_id,
        "status": "ok",
        "dry_run": bool(dry_run),
        "envelopes_dir": str(env_dir),
        "base_sha": run_base,
        "applied": [],
        "failed_task": None,
        "error": None,
        "created_at": _utc_now(),
        "note": None,
    }

    try:
        envelopes = load_envelopes(env_dir)
    except IntegrateError as exc:
        result["status"] = "failed"
        result["error"] = str(exc)
        _atomic_write_json(result_path(root, run_id), result)
        if not dry_run:
            write_status(
                root,
                run_id,
                "failed",
                extra={"integrate_status": "failed", "integrate_error": str(exc)},
            )
        return result

    if not envelopes:
        result["status"] = "missing"
        result["note"] = (
            f"no envelopes under {env_dir}; "
            "workers should write "
            ".omg/artifacts/ulw-results/<task_id>.json "
            "with task_id, base_sha, head_sha, worktree_path, "
            "changed_files, status"
        )
        _atomic_write_json(result_path(root, run_id), result)
        return result

    for env in envelopes:
        task_id = env["task_id"]
        entry: dict[str, Any] = {
            "task_id": task_id,
            "head_sha": env["head_sha"],
            "status": "pending",
        }

        if env["status"] != "ok":
            entry["status"] = "skipped_failed_envelope"
            entry["error"] = f"envelope status={env['status']!r} (expected ok)"
            result["applied"].append(entry)
            result["status"] = "failed"
            result["failed_task"] = task_id
            result["error"] = entry["error"]
            break

        if run_base and env["base_sha"] != run_base:
            entry["status"] = "base_sha_mismatch"
            entry["error"] = (
                f"envelope base_sha={env['base_sha']} != run base_sha={run_base}"
            )
            result["applied"].append(entry)
            result["status"] = "failed"
            result["failed_task"] = task_id
            result["error"] = entry["error"]
            break

        worktree = Path(env["worktree_path"])
        if not worktree.is_absolute():
            worktree = (root / worktree).resolve()

        if dry_run:
            entry["status"] = "dry_run_ok"
            entry["worktree_path"] = str(worktree)
            result["applied"].append(entry)
            continue

        try:
            _ensure_commit_reachable(root, env["head_sha"], worktree)
            _cherry_pick(root, env["head_sha"])
            entry["status"] = "applied"
            entry["worktree_path"] = str(worktree)
            result["applied"].append(entry)
        except IntegrateError as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
            result["applied"].append(entry)
            result["status"] = "failed"
            result["failed_task"] = task_id
            result["error"] = str(exc)
            break

    result["finished_at"] = _utc_now()
    _atomic_write_json(result_path(root, run_id), result)

    if not dry_run:
        if result["status"] == "ok":
            # Do not set verified — acceptance still required
            write_status(
                root,
                run_id,
                str(run.get("status") or "running"),
                extra={"integrate_status": "ok"},
            )
        elif result["status"] == "failed":
            write_status(
                root,
                run_id,
                "failed",
                extra={
                    "integrate_status": "failed",
                    "integrate_error": result.get("error"),
                },
            )

    return result
