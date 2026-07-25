"""Pure Stop-gate decision predicate for autopilot (read-only)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from omg_cli.state import load_active_run

TERMINAL_PHASES = frozenset({"verified", "cancelled"})

_PHASE_NEXT_GATE: dict[str, str] = {
    "ralplan": "ralplan consensus",
    "implement": "omg autopilot transition --phase review",
    "review": "stages/structured_review.json clean",
    "qa": "stages/ultraqa.json status clean",
    "acceptance": "omg autopilot complete",
    "rework": "review evidence or `omg autopilot transition --phase blocked`",
    "interview": "interview completion or `omg autopilot transition --phase blocked`",
    "init": "advance autopilot phase",
    "blocked": "resolve blocker via `omg autopilot transition --phase <target>`",
}


def _event_get(event: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in event:
            return event[key]
    return default


def _read_interview_status(root: Path, run_id: str) -> str | None:
    try:
        from omg_cli.interview import interview_state_path

        path = interview_state_path(root, run_id)
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        status = data.get("status")
        return str(status) if status is not None else None
    except Exception:
        return None


def continuation_reason(
    phase: str,
    *,
    goal: str,
    run_id: str,
    stop_hook_active: bool = False,
) -> str:
    phase_key = (phase or "").strip() or "unknown"
    next_gate = _PHASE_NEXT_GATE.get(
        phase_key, f"advance autopilot phase {phase_key!r}"
    )
    parts = [
        f"Autopilot phase {phase_key!r} is incomplete for run {run_id}.",
        f"Goal: {goal or '(unspecified)'}.",
        f"Next gate: {next_gate}.",
        "Do not ask the user mid-phase; record uncertainty under .omg/artifacts/ "
        "or run `omg autopilot transition --phase blocked`.",
    ]
    if stop_hook_active:
        parts.append(
            "You already continued this turn; produce the gate stamp or "
            "`omg autopilot transition --phase blocked`."
        )
    return " ".join(parts)


def decide_stop(
    root: Path | str,
    event: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any] | None:
    _ = env  # reserved for Task 8/9 env-gated behavior
    try:
        root_path = Path(root)
        if _event_get(event, "reason") != "end_turn":
            return None

        active = load_active_run(root_path)
        if not active or active.get("mode") != "autopilot":
            return None

        phase = str(active.get("autopilot_phase") or "")
        if phase in TERMINAL_PHASES:
            return None

        if active.get("autopilot_awaiting"):
            return None

        if phase == "interview":
            run_id = str(active.get("run_id") or "")
            if run_id and _read_interview_status(root_path, run_id) == "waiting_input":
                return None

        bg_tasks = _event_get(event, "backgroundTasks", "background_tasks", default=[])
        if bg_tasks:
            return None

        stop_hook_active = bool(
            _event_get(event, "stopHookActive", "stop_hook_active", default=False)
        )
        goal = str(active.get("goal") or "")
        run_id = str(active.get("run_id") or "")
        return {
            "decision": "block",
            "reason": continuation_reason(
                phase,
                goal=goal,
                run_id=run_id,
                stop_hook_active=stop_hook_active,
            ),
        }
    except Exception:
        return None
