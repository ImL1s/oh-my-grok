# omg_cli/qa.py
"""Bounded adversarial UltraQA repair FSM (CLI-authoritative).

QA clean is distinct from verified. Max five repair cycles. Unchanged product
hash cannot mark a repair complete unless classified as test-harness correction.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from omg_cli.evidence import CLI_WRITER, sha256_bytes, validate_identifier


MAX_CYCLES = 5


class QAError(ValueError):
    """Invalid QA state or operation."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def qa_state_path(root: Path | str, run_id: str) -> Path:
    run_id = validate_identifier(run_id, label="run_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / run_id
        / "stages"
        / "ultraqa.json"
    )


def product_hash(root: Path | str, paths: Sequence[str] | None = None) -> str:
    """Hash selected product files (default: omg_cli/**/*.py if present)."""
    root = Path(root).resolve()
    files: list[Path] = []
    if paths:
        for p in paths:
            fp = root / p
            if fp.is_file():
                files.append(fp)
    else:
        cli = root / "omg_cli"
        if cli.is_dir():
            files = sorted(cli.rglob("*.py"))
        else:
            # temp fixtures: hash all *.py under root
            files = sorted(root.rglob("*.py"))
    h = hashlib.sha256()
    for fp in files:
        try:
            rel = str(fp.relative_to(root))
        except ValueError:
            rel = str(fp)
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(fp.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def fingerprint_failure(scenario_id: str, output: str) -> str:
    body = f"{scenario_id}\n{output}".encode("utf-8")
    return sha256_bytes(body)


def freeze_scenarios(
    root: Path | str,
    run_id: str,
    scenarios: list[dict[str, Any]],
    *,
    plan_hash: str | None = None,
    spec_hash: str | None = None,
    allow_always_pass: bool = False,
) -> dict[str, Any]:
    root = Path(root).resolve()
    run_id = validate_identifier(run_id, label="run_id")
    if not scenarios:
        raise QAError("at least one scenario is required")
    for s in scenarios:
        if not isinstance(s, Mapping) or not str(s.get("id") or "").strip():
            raise QAError("each scenario needs id")
        check = s.get("check")
        if check == "always_pass" and not allow_always_pass:
            raise QAError(
                f"scenario {s.get('id')!r}: always_pass is test-only "
                "(pass allow_always_pass=True for hermetic unit tests)"
            )
        if not str(s.get("command") or "").strip() and check is None:
            raise QAError(f"scenario {s.get('id')!r} needs command or check")
    state = {
        "writer": CLI_WRITER,
        "schema_version": 2,
        "run_id": run_id,
        "status": "frozen",
        "plan_hash": plan_hash,
        "spec_hash": spec_hash,
        "product_hash_at_freeze": product_hash(root),
        "scenarios": [
            {
                "id": str(s["id"]),
                "command": s.get("command"),
                "check": s.get("check"),
                "required": bool(s.get("required", True)),
            }
            for s in scenarios
        ],
        "cycles": [],
        "cycle_count": 0,
        "max_cycles": MAX_CYCLES,
        "clean": False,
        "verified": False,  # always false — QA never sets verified
        "blocker": None,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }
    _save(root, run_id, state)
    return state


def _save(root: Path, run_id: str, state: dict[str, Any]) -> None:
    path = qa_state_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    state = dict(state)
    state["writer"] = CLI_WRITER
    state["updated_at"] = _utc_now()
    path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def load_qa(root: Path | str, run_id: str) -> dict[str, Any]:
    path = qa_state_path(root, run_id)
    if not path.is_file():
        raise QAError(f"ultraqa state missing for {run_id}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("writer") != CLI_WRITER:
        raise QAError("ultraqa state lacks CLI writer")
    return data


def _run_command(root: Path, command: str | list[str]) -> tuple[int, str]:
    """Run a QA command as argv only (no shell); enforce acceptance command policy."""
    import shlex

    from omg_cli.command_policy import CommandPolicyError, check_command_policy

    if isinstance(command, list):
        argv = [str(x) for x in command]
    else:
        text = (command or "").strip()
        if not text:
            return 2, "empty command"
        try:
            argv = shlex.split(text)
        except ValueError as exc:
            return 2, f"command parse error: {exc}"
    if not argv:
        return 2, "empty command"
    try:
        check_command_policy(argv, project_root=root)
    except CommandPolicyError as exc:
        return 2, f"command_policy: {exc}"

    from omg_cli.acceptance import sanitized_env

    env = sanitized_env(os.environ)
    # Controlled project path only — after hijack scrub.
    env["PYTHONPATH"] = str(root)
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            cwd=str(root),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
        return int(proc.returncode), out
    except subprocess.TimeoutExpired as exc:
        return 124, f"timeout: {exc}"
    except OSError as exc:
        return 127, f"environment: {exc}"


def run_qa_cycle(
    root: Path | str,
    run_id: str,
    *,
    repair_classification: str | None = None,
    product_paths: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Execute frozen scenarios once; on failure record diagnose fingerprint.

    repair_classification:
      - None: normal run
      - "product_change": allowed when product hash changed since last fail
      - "test_harness_correction": allowed without product hash change
    """
    root = Path(root).resolve()
    run_id = validate_identifier(run_id, label="run_id")
    state = load_qa(root, run_id)
    if state.get("status") == "blocked":
        raise QAError(f"ultraqa blocked: {state.get('blocker')}")
    if int(state.get("cycle_count") or 0) >= int(state.get("max_cycles") or MAX_CYCLES):
        state["status"] = "blocked"
        state["blocker"] = {
            "kind": "max_cycles",
            "message": f"exceeded max_cycles={state.get('max_cycles')}",
        }
        _save(root, run_id, state)
        raise QAError(state["blocker"]["message"])

    current_hash = product_hash(root, product_paths)
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for sc in state.get("scenarios") or []:
        sid = sc["id"]
        if sc.get("check") == "always_pass":
            results.append({"id": sid, "rc": 0, "ok": True, "output": "always_pass"})
            continue
        cmd = sc.get("command")
        if not cmd:
            results.append(
                {"id": sid, "rc": 2, "ok": False, "output": "missing command"}
            )
            failures.append(
                {
                    "id": sid,
                    "fingerprint": fingerprint_failure(sid, "missing command"),
                    "kind": "harness",
                }
            )
            continue
        rc, out = _run_command(root, cmd)
        ok = rc == 0
        results.append({"id": sid, "rc": rc, "ok": ok, "output": out[-4000:]})
        if not ok and sc.get("required", True):
            kind = "timeout" if rc == 124 else ("environment" if rc == 127 else "product")
            failures.append(
                {
                    "id": sid,
                    "fingerprint": fingerprint_failure(sid, out[-2000:]),
                    "kind": kind,
                    "rc": rc,
                }
            )

    cycle_n = int(state.get("cycle_count") or 0) + 1
    cycle = {
        "n": cycle_n,
        "product_hash": current_hash,
        "results": results,
        "failures": failures,
        "at": _utc_now(),
        "repair_classification": repair_classification,
    }
    cycles = list(state.get("cycles") or [])
    cycles.append(cycle)
    state["cycles"] = cycles
    state["cycle_count"] = cycle_n

    if not failures:
        state["status"] = "clean"
        state["clean"] = True
        state["verified"] = False
        state["blocker"] = None
        # Successful retest clears rework/replan invalidation so stage_qa_is_clean
        # can re-open acceptance without a full re-freeze.
        state.pop("invalidated", None)
        state.pop("invalidated_reason", None)
        state.pop("invalidated_at", None)
        _save(root, run_id, state)
        return state

    # Failure path: check repeated fingerprint
    fps = [f["fingerprint"] for f in failures]
    prior = [
        f.get("fingerprint")
        for c in cycles[:-1]
        for f in (c.get("failures") or [])
    ]
    repeats = sum(1 for fp in fps if prior.count(fp) >= 1)
    if any(prior.count(fp) >= 2 for fp in fps):
        state["status"] = "blocked"
        state["clean"] = False
        state["blocker"] = {
            "kind": "repeat_fingerprint",
            "message": "same failure fingerprint repeated at threshold",
            "fingerprints": fps,
        }
        _save(root, run_id, state)
        return state

    # Repair attempt accounting: if this is a retest after claimed repair
    if repair_classification == "product_change":
        prev_hash = None
        for c in reversed(cycles[:-1]):
            if c.get("failures"):
                prev_hash = c.get("product_hash")
                break
        if prev_hash is not None and prev_hash == current_hash:
            state["status"] = "blocked"
            state["clean"] = False
            state["blocker"] = {
                "kind": "unchanged_hash",
                "message": "repair claimed product_change but product hash unchanged",
            }
            _save(root, run_id, state)
            return state
    elif repair_classification not in (None, "test_harness_correction"):
        if repair_classification is not None:
            raise QAError(f"unknown repair_classification={repair_classification!r}")

    state["status"] = "failed"
    state["clean"] = False
    state["blocker"] = {
        "kind": "scenario_failed",
        "failures": failures,
        "next": "diagnose then minimal repair then retest",
    }
    _save(root, run_id, state)
    return state


def qa_status(root: Path | str, run_id: str) -> dict[str, Any]:
    state = load_qa(root, run_id)
    return {
        "run_id": run_id,
        "status": state.get("status"),
        "clean": state.get("clean"),
        "verified": False,
        "cycle_count": state.get("cycle_count"),
        "max_cycles": state.get("max_cycles"),
        "blocker": state.get("blocker"),
        "product_hash_at_freeze": state.get("product_hash_at_freeze"),
    }


__all__ = [
    "MAX_CYCLES",
    "QAError",
    "fingerprint_failure",
    "freeze_scenarios",
    "load_qa",
    "product_hash",
    "qa_state_path",
    "qa_status",
    "run_qa_cycle",
]
