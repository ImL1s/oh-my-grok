"""Process fanout supervisor (no tmux) — opt-in multi-PID ``grok -p`` workers.

Default ULW path remains skill-driven ``spawn_subagent`` (``--fanout skill``).
This module implements ``omg ulw --fanout process --workers N``:

- create run
- launch N× independent ``grok -p`` (or dry_run argv skeleton)
- write ``workers/wNN.pid.json`` for multi-PID cancel
- wait for all workers
- never sets verified without acceptance

Does **not** require tmux. Nested process fanout is forbidden (workers are
single-slice; they must not re-invoke ``omg ulw --fanout process``).
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from omg_cli.modes import (
    HARD_RULES_REMINDER,
    build_grok_argv,
    plugin_root,
    resolve_launch_timeout,
)
from omg_cli.state import create_run, load_run, write_pid_metadata, write_status

DEFAULT_WORKERS = 2
DEFAULT_MAX_WORKERS = 4
HARD_CAP_WORKERS = 8
FANOUT_SKILL = "skill"
FANOUT_PROCESS = "process"


def max_workers_cap() -> int:
    """Hard cap for process workers (env OMG_MAX_WORKERS, max HARD_CAP_WORKERS)."""
    raw = (os.environ.get("OMG_MAX_WORKERS") or "").strip()
    if raw:
        try:
            n = int(raw)
            return max(1, min(HARD_CAP_WORKERS, n))
        except ValueError:
            pass
    return HARD_CAP_WORKERS


def resolve_worker_count(n: int | None) -> int:
    """Clamp worker count to [1, cap]. Default DEFAULT_WORKERS when None."""
    cap = max_workers_cap()
    if n is None:
        n = DEFAULT_WORKERS
    n = int(n)
    if n < 1:
        raise ValueError("workers must be >= 1")
    if n > cap:
        raise ValueError(f"workers={n} exceeds hard cap {cap} (OMG_MAX_WORKERS / {HARD_CAP_WORKERS})")
    return n


def _run_dir(root: Path, run_id: str) -> Path:
    return Path(root) / ".omg" / "state" / "runs" / run_id


def workers_dir(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "workers"


def worker_id_label(index: int) -> str:
    """1-based worker label: w01, w02, …"""
    return f"w{index:02d}"


def build_worker_prompt(
    goal: str,
    *,
    run_id: str,
    worker_id: str,
    worker_index: int,
    workers: int,
) -> str:
    """Prompt for a process-fanout worker (single slice; no nested fanout)."""
    from omg_cli.modes import load_skill_body

    skill = load_skill_body("ulw", root=plugin_root())
    lines = [
        skill,
        "",
        HARD_RULES_REMINDER,
        "",
        "## Active mode: ulw (process fanout worker)",
        f"## Run id: {run_id}",
        f"## Worker: {worker_id} ({worker_index}/{workers})",
        "",
        "## Process-fanout contract (CLI supervisor)",
        "- You are **one** OS-level worker, not a spawn_subagent child.",
        "- Own **one** non-overlapping slice of the goal. Do not re-fanout.",
        "- Do **not** invoke `omg ulw --fanout process` or other multi-PID supervisors.",
        "- Prefer isolation worktree if the goal implies writes; leave result envelope",
        "  under `.omg/artifacts/ulw-results/` when applicable.",
        "- Do **not** set verified / passes in `.omg/state/` — only omg CLI does.",
        "- Shell/tests: prefer omg CLI acceptance path; capability_mode read-write",
        "  (no Execute) when acting as a pure implementer.",
        "",
        "## Goal (shared; claim one slice)",
        goal.strip() or "(no goal provided)",
        "",
        f"Worker index {worker_index} of {workers}. Coordinate via artifacts only.",
    ]
    return "\n".join(lines)


def fanout_meta_path(root: Path, run_id: str) -> Path:
    return workers_dir(root, run_id) / "fanout.json"


def _write_worker_argv(wdir: Path, worker_id: str, argv: list[str]) -> Path:
    path = wdir / f"{worker_id}.argv.json"
    path.write_text(
        json.dumps(argv, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return path


def _spawn_worker_process(
    argv: list[str],
    *,
    cwd: Path,
    pid_path: Path,
    timeout: float | None,
    dry_run: bool,
) -> tuple[subprocess.Popen[Any] | None, int | None]:
    """Launch one worker. Returns (proc, exit_code).

    dry_run: write argv only; no process; exit_code 0.
    Non-dry: Popen with start_new_session; write pid.json; return (proc, None)
    until waited.
    """
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    if dry_run:
        # Skeleton only — never invent a live pid that cancel could signal.
        meta = {
            "pid": None,
            "starttime": None,
            "pgid": None,
            "dry_run": True,
            "status": "dry_run",
        }
        pid_path.write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return None, 0

    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": os.environ.copy(),
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except OSError as exc:
        err = {
            "pid": None,
            "error": str(exc),
            "status": "launch_error",
        }
        pid_path.write_text(
            json.dumps(err, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return None, 127

    pgid: int | None = proc.pid
    if os.name == "posix":
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError, OSError):
            pgid = proc.pid
    write_pid_metadata(pid_path, pid=proc.pid, pgid=pgid)
    # Note: timeout is applied at wait time by caller
    return proc, None


def _wait_proc(proc: subprocess.Popen[Any], timeout: float | None) -> int:
    try:
        return int(proc.wait(timeout=timeout))
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, OSError):
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return 124


def run_process_fanout(
    goal: str,
    *,
    workers: int | None = None,
    root: Path | str | None = None,
    yolo: bool = False,
    safe: bool = False,
    dry_run: bool = False,
    timeout: float | None = None,
    force: bool = False,
    extra: Sequence[str] | None = None,
    existing_run_id: str | None = None,
    require_acceptance: bool = False,
) -> int:
    """Supervise N× grok -p workers under one run. Returns aggregate exit code.

    dry_run: create run + per-worker argv/pid skeleton; no exec.
    Never sets verified without CLI acceptance (this path does not run accept).
    """
    root_path = Path(root) if root is not None else Path.cwd().resolve()
    goal = (goal or "").strip() or "(no goal)"
    try:
        n = resolve_worker_count(workers)
    except ValueError as exc:
        print(f"omg ulw --fanout process: {exc}", file=sys.stderr)
        return 2

    launch_timeout = resolve_launch_timeout(timeout, dry_run=dry_run)

    if existing_run_id:
        run_id = existing_run_id
        if load_run(root_path, run_id) is None:
            print(
                f"omg ulw fanout: no run found for existing_run_id={run_id!r}",
                file=sys.stderr,
            )
            return 1
    else:
        try:
            from omg_cli.integrate import git_rev_parse_head

            create_extra: dict[str, Any] = {
                "fanout": FANOUT_PROCESS,
                "workers": n,
                "yolo": bool(yolo),
                "safe": bool(safe),
                "note": "process fanout (no tmux); multi-PID under workers/",
            }
            try:
                base_sha = git_rev_parse_head(root_path)
                if base_sha:
                    create_extra["base_sha"] = base_sha
            except Exception:
                pass
            run = create_run(
                root_path,
                mode="ulw",
                goal=goal,
                extra=create_extra,
                force=force,
            )
        except RuntimeError as exc:
            print(f"omg ulw fanout: {exc}", file=sys.stderr)
            return 1
        run_id = run["run_id"]

    run_dir = _run_dir(root_path, run_id)
    wdir = workers_dir(root_path, run_id)
    wdir.mkdir(parents=True, exist_ok=True)

    write_status(
        root_path,
        run_id,
        "running",
        extra={"fanout": FANOUT_PROCESS, "workers": n, "stage": "process_fanout"},
    )

    worker_records: list[dict[str, Any]] = []
    procs: list[tuple[str, subprocess.Popen[Any]]] = []
    exit_codes: dict[str, int] = {}

    for i in range(1, n + 1):
        wid = worker_id_label(i)
        prompt = build_worker_prompt(
            goal,
            run_id=run_id,
            worker_id=wid,
            worker_index=i,
            workers=n,
        )
        # Leaders/workers in process fanout: do NOT disallow_shell by default;
        # implementers may need shell. capability_mode is prompt-level.
        argv = build_grok_argv(
            mode="ulw",
            goal=goal,
            yolo=yolo,
            cwd=root_path,
            safe=safe,
            extra=extra,
            run_id=run_id,
            skill_root=plugin_root(),
            prompt=prompt,
            disallow_shell=False,
        )
        argv_path = _write_worker_argv(wdir, wid, argv)
        pid_path = wdir / f"{wid}.pid.json"

        # Also stash prompt for debug
        (wdir / f"{wid}.prompt.md").write_text(prompt, encoding="utf-8")

        proc, early_rc = _spawn_worker_process(
            argv,
            cwd=root_path,
            pid_path=pid_path,
            timeout=launch_timeout,
            dry_run=dry_run,
        )
        rec: dict[str, Any] = {
            "worker_id": wid,
            "index": i,
            "argv_path": str(argv_path.relative_to(run_dir)),
            "pid_path": str(pid_path.relative_to(run_dir)),
        }
        if early_rc is not None:
            exit_codes[wid] = early_rc
            rec["exit_code"] = early_rc
            rec["status"] = "dry_run" if dry_run else "launch_error"
        elif proc is not None:
            procs.append((wid, proc))
            rec["status"] = "running"
            rec["pid"] = proc.pid
        worker_records.append(rec)

    # Wait remaining live processes
    for wid, proc in procs:
        rc = _wait_proc(proc, launch_timeout)
        exit_codes[wid] = rc
        for rec in worker_records:
            if rec["worker_id"] == wid:
                rec["exit_code"] = rc
                rec["status"] = "ok" if rc == 0 else "failed"
                break

    # Mirror last argv as first worker for debugging (leader slot unused)
    if worker_records:
        first_argv = wdir / f"{worker_records[0]['worker_id']}.argv.json"
        if first_argv.is_file():
            (run_dir / "last_argv.json").write_text(
                first_argv.read_text(encoding="utf-8"), encoding="utf-8"
            )

    meta = {
        "version": 1,
        "run_id": run_id,
        "fanout": FANOUT_PROCESS,
        "workers": n,
        "dry_run": bool(dry_run),
        "records": worker_records,
        "exit_codes": exit_codes,
        "note": "process fanout supervisor; no tmux; cancel via workers/*.pid.json",
    }
    fanout_meta_path(root_path, run_id).write_text(
        json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # Aggregate: any non-zero fails the run (except all dry_run zeros)
    bad = [c for c in exit_codes.values() if c != 0]
    last_rc = bad[0] if bad else 0

    current = load_run(root_path, run_id) or {}
    if last_rc != 0 and not dry_run:
        write_status(
            root_path,
            run_id,
            "failed",
            extra={
                "fanout": FANOUT_PROCESS,
                "workers": n,
                "exit_code": last_rc,
                "exit_codes": exit_codes,
            },
        )
        print(
            f"omg ulw fanout: failed run {run_id} workers={n} exit_codes={exit_codes}",
            file=sys.stderr,
        )
        return int(last_rc)

    write_status(
        root_path,
        run_id,
        "completed",
        extra={
            "fanout": FANOUT_PROCESS,
            "workers": n,
            "exit_code": 0,
            "exit_codes": exit_codes,
            "note": "process fanout completed; verified remains false without acceptance",
            "require_acceptance": bool(require_acceptance),
        },
    )
    # Never auto-verify process fanout without acceptance path
    if current.get("verified") is True:
        pass  # only set_verified could have done this

    print(f"omg ulw fanout: run={run_id} workers={n} dry_run={bool(dry_run)}")
    if require_acceptance and not (load_run(root_path, run_id) or {}).get("verified"):
        print(
            "omg ulw fanout: not verified (require_acceptance); "
            "use `omg accept` after envelopes/integrate",
            file=sys.stderr,
        )
        return 1
    return 0


__all__ = [
    "DEFAULT_WORKERS",
    "DEFAULT_MAX_WORKERS",
    "FANOUT_PROCESS",
    "FANOUT_SKILL",
    "HARD_CAP_WORKERS",
    "build_worker_prompt",
    "fanout_meta_path",
    "max_workers_cap",
    "resolve_worker_count",
    "run_process_fanout",
    "worker_id_label",
    "workers_dir",
]
