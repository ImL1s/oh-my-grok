"""Pure Stop-gate decision predicate for autopilot (read-only)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

from omg_cli.state import load_active_run

_DRIFT_GUARD_ENV = "OMG_STOP_DRIFT_GUARD"
_GRACEFUL_CAP_ENV = "OMG_STOP_GRACEFUL_CAP"
_SESSION_ID_ENV = "GROK_SESSION_ID"
_CHATTY_RE = re.compile(
    r"\b(should i|shall i|would you like|do you want me|want me to|may i)\b.*\?",
    re.IGNORECASE,
)
_DRIFT_BLOCK_REASON = (
    "Do not ask the user mid-turn with freeform yes/no questions; "
    "keep working and record uncertainty under .omg/artifacts/."
)

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


def _truthy_env(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "yes", "on")


def _drift_guard_enabled(env: Mapping[str, str]) -> bool:
    return _truthy_env(env.get(_DRIFT_GUARD_ENV))


def _graceful_cap(env: Mapping[str, str]) -> int | None:
    raw = (env.get(_GRACEFUL_CAP_ENV) or "").strip()
    if not raw:
        return None
    try:
        cap = int(raw)
    except ValueError:
        return None
    return cap if cap > 0 else None


def _stop_gate_session_id(env: Mapping[str, str]) -> str:
    return (env.get(_SESSION_ID_ENV) or "").strip() or "default"


def _stop_gate_counter_path(root: Path, session: str) -> Path:
    return root / ".omg" / "state" / "stop_gate" / f"{session}.json"


def _read_stop_counter(path: Path) -> int:
    if not path.is_file():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return 0
    count = data.get("count", 0)
    try:
        return max(0, int(count))
    except (TypeError, ValueError):
        return 0


def _write_stop_counter(path: Path, count: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"count": count}, indent=2) + "\n",
        encoding="utf-8",
    )


def _graceful_cap_decision(
    root: Path,
    *,
    env: Mapping[str, str],
    stop_hook_active: bool,
    run_id: str,
) -> dict[str, Any] | None:
    """Increment diagnostic counter; at cap return graceful force-stop shape."""
    cap = _graceful_cap(env)
    if cap is None:
        return None
    session = _stop_gate_session_id(env)
    counter_path = _stop_gate_counter_path(root, session)
    try:
        if not stop_hook_active:
            count = 0
        else:
            count = _read_stop_counter(counter_path)
        count += 1
        _write_stop_counter(counter_path, count)
        if count > cap:
            return {
                "continue": False,
                "stopReason": (
                    f"Stop-pin reached {cap} continuations this turn; "
                    f"continue cross-turn: omg autopilot run --resume {run_id} "
                    "(or /loop …)"
                ),
            }
        return None
    except Exception:
        return None


def _is_chatty_question(message: str) -> bool:
    tail = message[-200:] if message else ""
    return bool(_CHATTY_RE.search(tail))


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
    try:
        root_path = Path(root)
        if _event_get(event, "reason") != "end_turn":
            return None

        env_map = env if env is not None else os.environ
        stop_hook_active = bool(
            _event_get(event, "stopHookActive", "stop_hook_active", default=False)
        )
        last_msg = str(
            _event_get(
                event,
                "lastAssistantMessage",
                "last_assistant_message",
                default="",
            )
            or ""
        )

        active = load_active_run(root_path)
        if active and active.get("mode") == "autopilot":
            phase = str(active.get("autopilot_phase") or "")
            if phase not in TERMINAL_PHASES:
                # Yield predicates first — drift must not trap human pause / bg work.
                if active.get("autopilot_awaiting"):
                    return None

                if phase == "interview":
                    run_id = str(active.get("run_id") or "")
                    if (
                        run_id
                        and _read_interview_status(root_path, run_id) == "waiting_input"
                    ):
                        return None

                bg_tasks = _event_get(
                    event, "backgroundTasks", "background_tasks", default=[]
                )
                if bg_tasks:
                    return None

                goal = str(active.get("goal") or "")
                run_id = str(active.get("run_id") or "")
                graceful = _graceful_cap_decision(
                    root_path,
                    env=env_map,
                    stop_hook_active=stop_hook_active,
                    run_id=run_id,
                )
                if graceful is not None:
                    return graceful
                return {
                    "decision": "block",
                    "reason": continuation_reason(
                        phase,
                        goal=goal,
                        run_id=run_id,
                        stop_hook_active=stop_hook_active,
                    ),
                }

        # No incomplete autopilot: optional chatty drift guard (never overrides yields).
        if (
            not stop_hook_active
            and _drift_guard_enabled(env_map)
            and _is_chatty_question(last_msg)
        ):
            return {"decision": "block", "reason": _DRIFT_BLOCK_REASON}
        return None
    except Exception:
        return None
