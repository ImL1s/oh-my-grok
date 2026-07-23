"""Cross-session resume routing + RESUME.md workspace side-effect (research R2).

Grok Stop and SessionStart are passive — they cannot write workspace state or
veto chat end. Continuity uses:
1. ``omg resume`` smart routing from active / named run
2. explicit CLI writing of ``.omg/state/RESUME.md`` (one-shot pack)
3. louder context pack text for agents (status + next command)

CLI owns write/clear of RESUME.md; agents only read it.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes, ensure_managed_dir
from omg_cli.contracts.resume_contract import select_resume_selector
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_integer,
    require_sha256,
)
from omg_cli.state import load_active_run, load_run, load_run_view

TERMINAL_STATUSES = frozenset({"cancelled", "completed", "failed", "verified"})

RESUME_REL = Path(".omg") / "state" / "RESUME.md"


class ResumeError(ValueError):
    """No resumable run or invalid resume request."""


def resume_md_path(root: Path) -> Path:
    return Path(root) / RESUME_REL


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_terminal(status: dict[str, Any]) -> bool:
    st = str(status.get("status") or "").lower()
    if st in TERMINAL_STATUSES:
        return True
    if status.get("verified") is True:
        return True
    return False


def resolve_run(root: Path, run_id: str | None = None) -> dict[str, Any] | None:
    """Load active or named run status; None if missing."""
    root = Path(root)
    if run_id:
        return load_run(root, run_id)
    return load_active_run(root)


def recommend_commands(status: dict[str, Any]) -> list[str]:
    """Return ordered shell commands to continue this run (print-only safe)."""
    rid = str(status.get("run_id") or "")
    mode = str(status.get("mode") or "").lower()
    stage = str(status.get("stage") or status.get("phase") or "")
    cmds: list[str] = []

    if mode == "pipeline":
        cmds.append(f"omg pipeline --resume {rid}")
    elif mode == "ralph":
        cmds.append(f"omg ralph --resume {rid}")
        sid = status.get("grok_session_id")
        if isinstance(sid, str) and sid.strip():
            cmds.append(f"grok --resume {sid.strip()}")
    elif mode == "autopilot":
        cmds.append(f"omg autopilot status --run {rid}")
        if stage:
            cmds.append(
                f"# continue playbook phase={stage!r}; "
                f"omg autopilot transition --run {rid} --phase <next>"
            )
        cmds.append(f"omg state --run {rid} --human")
    elif mode == "ulw":
        cmds.append(f"omg state --run {rid} --human")
        cmds.append(f"# re-run workers or: omg integrate --run {rid}")
    elif mode == "ralplan":
        cmds.append(f"omg ralplan --resume {rid}" if False else f"omg state --run {rid}")
        cmds.append("# ralplan: re-invoke omg ralplan with same goal if unfinished")
    elif mode in {"interview", "deep-interview"}:
        rc = status.get("resume_command")
        if isinstance(rc, str) and rc.strip():
            cmds.append(rc.strip())
        else:
            cmds.append(f"omg interview status --run {rid}")
    else:
        cmds.append(f"omg state --run {rid} --human")
        if mode:
            cmds.append(f"# mode={mode!r} stage={stage!r} — re-invoke matching skill")

    cmds.append("omg resume --clear  # after successfully continuing")
    return cmds


def build_resume_pack(root: Path, run_id: str | None = None) -> dict[str, Any]:
    """Build louder context pack for RESUME.md / agent bootstrap."""
    root = Path(root)
    status = resolve_run(root, run_id)
    if status is None:
        return {
            "ok": False,
            "reason": "no_active_run" if not run_id else "run_not_found",
            "run_id": run_id,
            "generated_at": _utc_now(),
        }

    rid = str(status.get("run_id") or run_id or "")
    view = load_run_view(root, rid) or status
    terminal = _is_terminal(status)
    pack: dict[str, Any] = {
        "ok": True,
        "generated_at": _utc_now(),
        "run_id": rid,
        "mode": status.get("mode"),
        "status": status.get("status"),
        "stage": status.get("stage") or status.get("phase"),
        "goal": status.get("goal") or status.get("task"),
        "verified": bool(status.get("verified")),
        "terminal": terminal,
        "resumable": not terminal,
        "grok_session_id": status.get("grok_session_id"),
        "commands": [] if terminal else recommend_commands(status),
        "view_keys": sorted(k for k in view.keys() if k not in {"goal", "task"}),
    }
    if terminal:
        pack["reason"] = "run_terminal"
        pack["hint"] = "Run is terminal; no omg resume re-entry. Start a new run if needed."
    return pack


def render_resume_md(pack: dict[str, Any]) -> str:
    """Markdown one-shot pack for agents (keep short — compaction safety)."""
    lines = [
        "# OMG RESUME (one-shot)",
        "",
        "> Generated by `omg resume`. **Read once**, then continue via CLI.",
        "> After you successfully resume work, run `omg resume --clear` (or let CLI clear).",
        "",
        f"- generated_at: `{pack.get('generated_at')}`",
    ]
    if not pack.get("ok"):
        lines.extend(
            [
                "- ok: false",
                f"- reason: `{pack.get('reason')}`",
                "",
                "No active non-terminal run. Do not invent resume state.",
                "",
            ]
        )
        return "\n".join(lines)

    lines.extend(
        [
            f"- run_id: `{pack.get('run_id')}`",
            f"- mode: `{pack.get('mode')}`",
            f"- status: `{pack.get('status')}`",
            f"- stage: `{pack.get('stage')}`",
            f"- verified: `{pack.get('verified')}`",
            f"- terminal: `{pack.get('terminal')}`",
            f"- resumable: `{pack.get('resumable')}`",
        ]
    )
    goal = pack.get("goal")
    if goal:
        g = str(goal).strip().replace("\n", " ")
        if len(g) > 240:
            g = g[:237] + "..."
        lines.append(f"- goal: {g}")
    sid = pack.get("grok_session_id")
    if sid:
        lines.append(f"- grok_session_id: `{sid}`")

    lines.extend(["", "## Next commands", ""])
    cmds = pack.get("commands") or []
    if not cmds:
        lines.append(pack.get("hint") or "_none_")
    else:
        lines.append("```bash")
        for c in cmds:
            lines.append(str(c))
        lines.append("```")

    lines.extend(
        [
            "",
            "## Agent rules",
            "",
            "1. If `resumable: true`, **do not** start a conflicting new run; route via commands above.",
            "2. Prefer `omg resume` / mode CLI over free-form re-implementation.",
            "3. Never write `passes`/`verified` yourself.",
            "4. After continuing successfully: `omg resume --clear`.",
            "",
        ]
    )
    return "\n".join(lines)


def write_resume_md(root: Path, run_id: str | None = None) -> Path | None:
    """Write RESUME.md when a run exists (including terminal with explanation).

    Returns path written, or None if no run at all.
    """
    root = Path(root)
    pack = build_resume_pack(root, run_id)
    if not pack.get("ok") and pack.get("reason") in {"no_active_run", "run_not_found"}:
        # Still write a short "no active" pack so agents stop guessing.
        path = resume_md_path(root)
        ensure_managed_dir(path.parent)
        atomic_write_bytes(
            path,
            render_resume_md(pack).encode("utf-8"),
            mode=DATA_FILE_MODE,
            replace=True,
        )
        return path
    path = resume_md_path(root)
    ensure_managed_dir(path.parent)
    atomic_write_bytes(
        path,
        render_resume_md(pack).encode("utf-8"),
        mode=DATA_FILE_MODE,
        replace=True,
    )
    return path


def clear_resume_md(root: Path) -> bool:
    """Delete RESUME.md if present. Returns True if removed."""
    path = resume_md_path(root)
    if path.is_file():
        path.unlink()
        return True
    return False


def route_resume(
    root: Path,
    *,
    run_id: str | None = None,
    write_md: bool = True,
    as_json: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Primary `omg resume` entry: pack + optional RESUME.md write.

    Exit codes: 0 ok/resumable or terminal explained; 1 missing run; 2 terminal
    when strict? We use 0 for terminal with message (idempotent), 1 for missing.
    """
    pack = build_resume_pack(root, run_id)
    if write_md:
        write_resume_md(root, run_id)
        pack["resume_md"] = str(resume_md_path(root))

    if not pack.get("ok"):
        return 1, pack
    if pack.get("terminal"):
        return 0, pack
    return 0, pack


def format_pack_human(pack: dict[str, Any]) -> str:
    return render_resume_md(pack)


def format_pack_json(pack: dict[str, Any]) -> str:
    return json.dumps(pack, indent=2, ensure_ascii=False) + "\n"


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except (TypeError, ValueError, AttributeError) as exc:
        raise ContractValidationError("E_RESUME_NOT_FOUND: malformed handoff expiry") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def resolve_resume_selection(
    selectors: dict[str, Any],
    candidates: list[dict[str, Any]],
    *,
    expected_repository_id: str,
    expected_host: str,
    expected_cwd_hash: str,
    current_generation: int,
    best_effort: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply W0's exact six-rank selector without lower-rank fallback."""

    selector = select_resume_selector(selectors, best_effort=best_effort)
    require_sha256(expected_cwd_hash, label="expected_cwd_hash")
    require_integer(current_generation, label="current_generation", minimum=0)
    eligible = [
        dict(row)
        for row in candidates
        if row.get("repository_id") == expected_repository_id
        and row.get("host") == expected_host
        and row.get("cwd_hash") == expected_cwd_hash
    ]

    if selector == "recovery_manifest":
        request = selectors.get(selector)
        if not isinstance(request, dict):
            raise ContractValidationError("E_RESUME_NOT_FOUND")
        digest = request.get("sha256")
        require_sha256(digest, label="recovery manifest sha256")
        matches = [
            row
            for row in eligible
            if row.get("recovery_manifest_sha256") == digest
            and row.get("parent_valid") is True
        ]
    elif selector == "run_id":
        run_id = selectors.get("run_id")
        matches = [
            row
            for row in eligible
            if row.get("run_id") == run_id and row.get("generation") == current_generation
        ]
        native = selectors.get("native_session_id")
        if native not in (None, ""):
            matches = [row for row in matches if row.get("native_session_id") == native]
    elif selector == "native_session_id":
        native = selectors.get("native_session_id")
        matches = [row for row in eligible if row.get("native_session_id") == native]
    elif selector == "current_process_run":
        run_id = selectors.get("current_process_run")
        matches = [
            row
            for row in eligible
            if row.get("run_id") == run_id
            and row.get("live_lease") is True
            and row.get("generation") == current_generation
        ]
    elif selector == "signed_handoff":
        request = selectors.get(selector)
        if not isinstance(request, dict):
            raise ContractValidationError("E_RESUME_NOT_FOUND")
        digest = request.get("sha256")
        require_sha256(digest, label="signed handoff sha256")
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        matches = []
        for row in eligible:
            if row.get("signed_handoff_sha256") != digest or row.get("parent_valid") is not True:
                continue
            expires_at = row.get("expires_at")
            if not isinstance(expires_at, str) or _parse_timestamp(expires_at) <= current_time:
                continue
            matches.append(row)
    else:
        if not best_effort or selectors.get("best_effort_cwd") is not True:
            raise ContractValidationError("E_RESUME_NOT_FOUND")
        if not eligible or any(row.get("parent_valid") is not True for row in eligible):
            raise ContractValidationError("E_RESUME_AMBIGUOUS")
        generations: list[int] = []
        for row in eligible:
            generation = row.get("generation")
            generations.append(
                require_integer(generation, label="candidate generation", minimum=0)
            )
        highest = max(generations)
        matches = [row for row in eligible if row.get("generation") == highest]

    if not matches:
        raise ContractValidationError("E_RESUME_NOT_FOUND")
    if len(matches) != 1:
        raise ContractValidationError("E_RESUME_AMBIGUOUS")
    return {**matches[0], "selector": selector, "verified": selector != "best_effort_cwd"}


__all__ = [
    "ResumeError",
    "TERMINAL_STATUSES",
    "build_resume_pack",
    "clear_resume_md",
    "format_pack_human",
    "format_pack_json",
    "recommend_commands",
    "resolve_resume_selection",
    "render_resume_md",
    "resolve_run",
    "resume_md_path",
    "route_resume",
    "write_resume_md",
]
