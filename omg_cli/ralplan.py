"""RALPLAN CLI-owned finite state machine (plan consensus, no implementation).

FSM::

    draft → critic → revise → verifier → (accept | revise)* → accepted | failed

State is persisted under ``runs/<id>/ralplan.json``. Each stage writes a
prompt pack under the run dir and may launch Grok via ``modes.build_grok_argv``
/ ``modes._launch_grok``. With ``dry_run=True`` only artifacts are recorded.

Terminal ``accepted`` requires a verifier stage artifact containing the word
``APPROVE`` (case-sensitive whole word) or a JSON field with that verdict.
After ``max_rounds`` verifier attempts without accept → ``failed``.

This module **never** implements product code and never sets ``verified``.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from omg_cli.modes import (
    DEFAULT_TIMEOUT,
    _launch_grok,
    build_grok_argv,
    plugin_root,
)
from omg_cli.state import create_run, load_run, write_status

DEFAULT_MAX_ROUNDS = 3

# Stages that must run read-only (no product edits)
READ_ONLY_STAGES = frozenset({"critic", "verifier"})

STAGE_ORDER_NOTE = "draft → critic → revise → verifier → (accept | revise)*"

# Whole-word APPROVE (case-sensitive)
_APPROVE_WORD_RE = re.compile(r"(?<![A-Za-z0-9_])APPROVE(?![A-Za-z0-9_])")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_dir(root: Path, run_id: str) -> Path:
    return Path(root) / ".omg" / "state" / "runs" / run_id


def ralplan_state_path(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "ralplan.json"


def stages_dir(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "stages"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Write JSON via temp + replace (same pattern as state.py)."""
    import os
    import uuid

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def load_ralplan_state(root: Path, run_id: str) -> dict[str, Any] | None:
    path = ralplan_state_path(root, run_id)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def save_ralplan_state(root: Path, run_id: str, state: dict[str, Any]) -> Path:
    path = ralplan_state_path(root, run_id)
    state = dict(state)
    state["updated_at"] = _utc_now()
    _atomic_write_json(path, state)
    return path


def initial_ralplan_state(
    *,
    run_id: str,
    goal: str,
    max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> dict[str, Any]:
    now = _utc_now()
    return {
        "run_id": run_id,
        "goal": goal,
        "status": "draft",
        "stage": "draft",
        "round": 0,
        "max_rounds": int(max_rounds),
        "history": [],
        "accepted": False,
        "fsm": STAGE_ORDER_NOTE,
        "note": "CLI-owned RALPLAN FSM — never implements product code; never sets verified",
        "created_at": now,
        "updated_at": now,
    }


def stage_prompt_path(root: Path, run_id: str, stage: str, round_n: int) -> Path:
    return stages_dir(root, run_id) / f"{stage}-{round_n:02d}.prompt.md"


def stage_artifact_path(root: Path, run_id: str, stage: str, round_n: int) -> Path:
    """Primary text artifact for a stage (Grok output / dry_run stub)."""
    return stages_dir(root, run_id) / f"{stage}-{round_n:02d}.md"


def stage_artifact_json_path(
    root: Path, run_id: str, stage: str, round_n: int
) -> Path:
    return stages_dir(root, run_id) / f"{stage}-{round_n:02d}.json"


def build_stage_prompt(
    stage: str,
    goal: str,
    *,
    run_id: str,
    round_n: int,
    max_rounds: int,
    run_dir: Path | None = None,
) -> str:
    """Compose stage-specific prompt. Critic/verifier force read-only."""
    from omg_cli.modes import HARD_RULES_REMINDER, load_skill_body

    skill = load_skill_body("ralplan", root=plugin_root())
    read_only = stage in READ_ONLY_STAGES

    lines = [
        skill,
        "",
        HARD_RULES_REMINDER,
        "",
        f"## Active mode: ralplan",
        f"## Run id: {run_id}",
        f"## FSM stage: {stage}",
        f"## Round: {round_n}/{max_rounds}",
        f"## FSM: {STAGE_ORDER_NOTE}",
        "",
        "## Stage contract (CLI-owned)",
        f"- You are in stage **{stage}** only. Do not skip ahead to implementation.",
        "- **Never** implement product code in ralplan.",
        "- Write stage findings under the run stages/ paths designated by CLI.",
        "- Do not set verified / passes in `.omg/state/`.",
    ]

    if read_only:
        lines.extend(
            [
                "",
                "## READ-ONLY capability (mandatory for this stage)",
                "- capability_mode: **read-only** (or permissionMode plan).",
                "- Allowed: read_file, grep, list_dir; structured findings only.",
                "- Forbidden: search_replace on product source, spawn_subagent,",
                "  applying patches, running implementation agents.",
                "- Optionally note paths under `.omg/artifacts/`; prefer returning",
                "  findings so the leader/CLI records them.",
            ]
        )

    if stage == "draft":
        lines.extend(
            [
                "",
                "## Draft instructions",
                "Write a plan draft covering: problem, goals, non-goals, steps,",
                "risks, acceptance criteria. Output under the stage artifact path.",
                "No product code changes.",
            ]
        )
    elif stage == "critic":
        lines.extend(
            [
                "",
                "## Critic instructions (read-only)",
                "Attack assumptions, missing tests, scope holes, security/migration",
                "risks, test theatre, contract mismatch. Severity: blocker|major|minor|nit.",
                "Do not APPROVE a bad plan to be helpful.",
            ]
        )
    elif stage == "revise":
        lines.extend(
            [
                "",
                "## Revise instructions",
                "Merge valid critique into the plan; restate acceptance checks.",
                "Update plan artifact only — no product implementation.",
            ]
        )
    elif stage == "verifier":
        lines.extend(
            [
                "",
                "## Verifier instructions (read-only)",
                "Check plan coherence, testability, scope, risk coverage.",
                "Verdict must be explicit: **APPROVE** | **REQUEST CHANGES** | **FAILED**.",
                "Write the verdict into the stage artifact (markdown or JSON).",
                "APPROVE is case-sensitive; CLI accepts only when the artifact",
                "contains the whole word APPROVE or JSON verdict field APPROVE.",
                "APPROVE is a recommendation for the CLI FSM — not a state write.",
            ]
        )

    lines.extend(
        [
            "",
            "## Artifact paths (this stage)",
            f"- text artifact: `stages/{stage}-{round_n:02d}.md`",
            f"- optional JSON: `stages/{stage}-{round_n:02d}.json`",
            f"- stage prompt: `stages/{stage}-{round_n:02d}.prompt.md`",
        ]
    )
    if run_dir is not None:
        lines.append(f"- run dir: `{run_dir}`")

    lines.extend(
        [
            "",
            "## Goal",
            goal.strip() or "(no goal provided)",
            "",
            "Follow the skill playbook. Prefer Grok-native tools only.",
            "Do **not** start coding product features in this stage.",
        ]
    )
    return "\n".join(lines)


def artifact_contains_approve(path: Path) -> bool:
    """True if path is a text/JSON artifact with terminal APPROVE.

    - Markdown/text: whole-word case-sensitive ``APPROVE``.
    - JSON object: field ``verdict`` / ``decision`` / ``status`` equals
      ``APPROVE``, or ``approve`` is boolean true.
    """
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if not text.strip():
        return False

    # Try JSON first when content looks like object/array
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            for key in ("verdict", "decision", "status"):
                val = data.get(key)
                if isinstance(val, str) and val.strip() == "APPROVE":
                    return True
            if data.get("approve") is True:
                return True
            # nested common shapes
            nested = data.get("result") or data.get("output")
            if isinstance(nested, dict):
                for key in ("verdict", "decision", "status"):
                    val = nested.get(key)
                    if isinstance(val, str) and val.strip() == "APPROVE":
                        return True
                if nested.get("approve") is True:
                    return True

    return bool(_APPROVE_WORD_RE.search(text))


def verifier_has_approve(root: Path, run_id: str, round_n: int) -> bool:
    """Check verifier artifacts for this round (md then json)."""
    md = stage_artifact_path(root, run_id, "verifier", round_n)
    js = stage_artifact_json_path(root, run_id, "verifier", round_n)
    return artifact_contains_approve(md) or artifact_contains_approve(js)


def _execute_stage(
    stage: str,
    *,
    root: Path,
    run_id: str,
    goal: str,
    round_n: int,
    max_rounds: int,
    yolo: bool,
    safe: bool,
    dry_run: bool,
    timeout: float | None,
    extra: Sequence[str] | None = None,
) -> int:
    """Write stage prompt pack, optionally launch grok, ensure artifact stub.

    Returns process exit code (0 for dry_run).
    """
    root = Path(root)
    run_dir = _run_dir(root, run_id)
    sdir = stages_dir(root, run_id)
    sdir.mkdir(parents=True, exist_ok=True)

    prompt = build_stage_prompt(
        stage,
        goal,
        run_id=run_id,
        round_n=round_n,
        max_rounds=max_rounds,
        run_dir=run_dir,
    )
    prompt_path = stage_prompt_path(root, run_id, stage, round_n)
    prompt_path.write_text(prompt, encoding="utf-8")

    # Also mirror last stage prompt at run root for debugging
    (run_dir / "last_stage_prompt.md").write_text(prompt, encoding="utf-8")
    (run_dir / "last_stage").write_text(f"{stage}\n", encoding="utf-8")

    # Critic/verifier: strip shell at argv (defense-in-depth). Draft/revise may
    # keep shell off by default too for plan-only work, but only RO stages force it.
    argv = build_grok_argv(
        mode="ralplan",
        goal=goal,
        yolo=yolo,
        cwd=root,
        safe=safe,
        extra=extra,
        run_id=run_id,
        skill_root=plugin_root(),
        prompt=prompt,
        disallow_shell=(stage in READ_ONLY_STAGES),
    )

    # Stage-scoped argv record (full last_argv still written by _launch_grok)
    stage_argv_path = sdir / f"{stage}-{round_n:02d}.argv.json"
    stage_argv_path.write_text(
        json.dumps(argv, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    rc = _launch_grok(
        argv,
        cwd=root,
        run_dir=run_dir,
        timeout=timeout,
        dry_run=dry_run,
    )

    # Ensure text artifact exists after stage (stub if Grok did not write one).
    # dry_run / missing output: create placeholder WITHOUT APPROVE so only
    # explicit verifier artifacts can accept.
    art = stage_artifact_path(root, run_id, stage, round_n)
    if not art.is_file():
        stub_lines = [
            f"# RALPLAN stage: {stage}",
            f"round: {round_n}/{max_rounds}",
            f"run_id: {run_id}",
            f"dry_run: {bool(dry_run)}",
            "",
            "Stub artifact written by omg CLI (Grok did not produce this file).",
            "Verifier acceptance requires the case-sensitive accept token",
            "(see skill verdict contract) — this stub intentionally omits it.",
            "",
        ]
        art.write_text("\n".join(stub_lines), encoding="utf-8")

    return int(rc)


def run_ralplan(
    goal: str,
    *,
    root: Path | str | None = None,
    max_rounds: int | None = None,
    yolo: bool = False,
    safe: bool = False,
    dry_run: bool = False,
    timeout: float | None = DEFAULT_TIMEOUT,
    extra: Sequence[str] | None = None,
    force: bool = False,
    existing_run_id: str | None = None,
    stage_executor: Callable[..., int] | None = None,
) -> int:
    """Run the RALPLAN FSM. Returns 0 on accepted, non-zero on failed/error.

    Parameters
    ----------
    max_rounds:
        Max verifier attempts (default 3). After this without APPROVE → failed.
    dry_run:
        Record prompts/state only; do not exec grok. Acceptance still requires
        a verifier artifact with APPROVE (tests may write it via stage_executor).
    existing_run_id:
        Reuse an already-created run (pipeline embedding). Skips create_run;
        does not change the run's ``mode`` field.
    stage_executor:
        Optional override for ``_execute_stage`` (tests). Signature matches
        ``_execute_stage``.
    """
    root_path = Path(root) if root is not None else Path.cwd().resolve()
    goal = (goal or "").strip() or "(no goal)"

    if max_rounds is None:
        max_rounds = DEFAULT_MAX_ROUNDS
    max_rounds = max(1, int(max_rounds))

    executor = stage_executor or _execute_stage

    if existing_run_id:
        run_id = existing_run_id
        if load_run(root_path, run_id) is None:
            print(
                f"omg ralplan: no run found for existing_run_id={run_id!r}",
                file=sys.stderr,
            )
            return 1
    else:
        try:
            run = create_run(
                root_path,
                mode="ralplan",
                goal=goal,
                extra={
                    "max_rounds": max_rounds,
                    "yolo": bool(yolo),
                    "safe": bool(safe),
                    "fsm": "ralplan",
                    "note": "RALPLAN FSM — plan consensus only; no product implementation",
                },
                force=force,
            )
        except RuntimeError as exc:
            print(f"omg ralplan: {exc}", file=sys.stderr)
            return 1
        run_id = run["run_id"]

    run_dir = _run_dir(root_path, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    stages_dir(root_path, run_id).mkdir(parents=True, exist_ok=True)

    state = initial_ralplan_state(
        run_id=run_id, goal=goal, max_rounds=max_rounds
    )
    save_ralplan_state(root_path, run_id, state)
    write_status(
        root_path,
        run_id,
        "running",
        extra={"stage": "draft", "round": 0, "max_rounds": max_rounds},
    )

    last_rc = 0
    accepted = False
    # First pass: draft → critic → revise → verifier (round 1)
    # Then: while not accept and round < max_rounds: revise → verifier
    stages_queue: list[tuple[str, int]] = [
        ("draft", 1),
        ("critic", 1),
        ("revise", 1),
        ("verifier", 1),
    ]

    def _record(
        stage: str,
        round_n: int,
        exit_code: int,
        *,
        approved: bool | None = None,
    ) -> None:
        nonlocal state
        entry: dict[str, Any] = {
            "stage": stage,
            "round": round_n,
            "at": _utc_now(),
            "prompt": str(
                stage_prompt_path(root_path, run_id, stage, round_n).relative_to(
                    run_dir
                )
            ),
            "artifact": str(
                stage_artifact_path(root_path, run_id, stage, round_n).relative_to(
                    run_dir
                )
            ),
            "exit_code": exit_code,
        }
        if approved is not None:
            entry["approve"] = approved
        state["history"].append(entry)
        state["stage"] = stage
        state["round"] = round_n
        state["status"] = stage
        save_ralplan_state(root_path, run_id, state)

    qi = 0
    while qi < len(stages_queue):
        stage, round_n = stages_queue[qi]
        qi += 1

        write_status(
            root_path,
            run_id,
            "running",
            extra={
                "stage": stage,
                "round": round_n,
                "max_rounds": max_rounds,
            },
        )
        state["status"] = stage
        state["stage"] = stage
        state["round"] = round_n
        save_ralplan_state(root_path, run_id, state)

        last_rc = executor(
            stage,
            root=root_path,
            run_id=run_id,
            goal=goal,
            round_n=round_n,
            max_rounds=max_rounds,
            yolo=yolo,
            safe=safe,
            dry_run=dry_run,
            timeout=timeout,
            extra=extra,
        )

        if stage == "verifier":
            approved = verifier_has_approve(root_path, run_id, round_n)
            _record(stage, round_n, last_rc, approved=approved)
            if approved:
                accepted = True
                break
            # Not approved: either another revise→verifier round or fail
            if round_n >= max_rounds:
                break
            next_round = round_n + 1
            stages_queue.append(("revise", next_round))
            stages_queue.append(("verifier", next_round))
        else:
            _record(stage, round_n, last_rc)

        # Hard fail on non-zero launch unless dry_run (recording continues)
        if last_rc != 0 and not dry_run:
            # Stop FSM early on launch failure
            break

    # Terminal status
    current = load_run(root_path, run_id) or {}
    if accepted:
        state["status"] = "accepted"
        state["accepted"] = True
        state["stage"] = "accepted"
        save_ralplan_state(root_path, run_id, state)
        write_status(
            root_path,
            run_id,
            "completed",
            extra={
                "stage": "accepted",
                "ralplan_status": "accepted",
                "round": state.get("round", 0),
                "exit_code": 0,
                "note": "RALPLAN accepted (verifier APPROVE); not product verified",
            },
        )
        print(f"omg ralplan: accepted run {run_id} (verifier APPROVE)")
        return 0

    # failed: max rounds without APPROVE, or launch error
    fail_note = (
        "launch failed"
        if last_rc != 0 and not dry_run
        else f"no verifier APPROVE within max_rounds={max_rounds}"
    )
    state["status"] = "failed"
    state["accepted"] = False
    state["stage"] = "failed"
    state["fail_note"] = fail_note
    save_ralplan_state(root_path, run_id, state)
    write_status(
        root_path,
        run_id,
        "failed",
        extra={
            "stage": "failed",
            "ralplan_status": "failed",
            "round": state.get("round", 0),
            "exit_code": last_rc if last_rc != 0 else 1,
            "note": fail_note,
        },
    )
    print(f"omg ralplan: failed run {run_id}: {fail_note}", file=sys.stderr)
    return 1 if last_rc == 0 else int(last_rc)


__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "READ_ONLY_STAGES",
    "artifact_contains_approve",
    "build_stage_prompt",
    "initial_ralplan_state",
    "load_ralplan_state",
    "ralplan_state_path",
    "run_ralplan",
    "save_ralplan_state",
    "stage_artifact_path",
    "stage_prompt_path",
    "stages_dir",
    "verifier_has_approve",
]
