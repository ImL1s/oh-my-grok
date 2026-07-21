"""RALPLAN CLI-owned finite state machine (plan consensus, no implementation).

FSM::

    draft → critic → revise → verifier → (accept | revise)* → accepted | failed

State is persisted under ``runs/<id>/ralplan.json``. Each stage writes a
prompt pack under the run dir and may launch Grok via ``modes.build_grok_argv``
/ ``modes._launch_grok``. With ``dry_run=True`` only artifacts are recorded.

Terminal ``accepted`` requires a verifier stage artifact with **strict**
terminal ``APPROVE`` (see ``omg_cli.verdict`` — negation and free-floating
mentions do not count). After ``max_rounds`` without accept → ``failed``.

This module **never** implements product code and never sets ``verified``.
"""
from __future__ import annotations

import json
import sys
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable, Sequence

from omg_cli.modes import (
    DEFAULT_TIMEOUT,
    _launch_grok,
    build_grok_argv,
    plugin_root,
)
from omg_cli.state import create_run, load_run, write_status
from omg_cli.verdict import (
    artifact_contains_approve,
    parse_structured_verdict,
    parse_verdict_file,
)

DEFAULT_MAX_ROUNDS = 3
V2_MAX_ROUNDS = 5

# Stages that must run read-only (no product edits)
READ_ONLY_STAGES = frozenset({"critic", "verifier"})
V2_READ_ONLY_STAGES = frozenset({"planner", "architect", "critic"})

STAGE_ORDER_NOTE = "draft → critic → revise → verifier → (accept | revise)*"
V2_STAGE_ORDER_NOTE = (
    "planner → architect → critic → "
    "(consensus | planner revision → architect → critic)*"
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_dir(root: Path, run_id: str) -> Path:
    from omg_cli.state import _safe_run_id

    return Path(root) / ".omg" / "state" / "runs" / _safe_run_id(run_id)


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
    invocation_id: str | None = None,
    session_id: str | None = None,
    input_sha256: str | None = None,
) -> str:
    """Compose stage-specific prompt. Critic/verifier force read-only."""
    from omg_cli.modes import HARD_RULES_REMINDER, load_skill_body

    skill = load_skill_body("ralplan", root=plugin_root())
    read_only = stage in READ_ONLY_STAGES or stage in V2_READ_ONLY_STAGES

    lines = [
        skill,
        "",
        HARD_RULES_REMINDER,
        "",
        "## Active mode: ralplan",
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
    elif stage in V2_READ_ONLY_STAGES:
        required = {
            "planner": (
                'verdict "READY" plus non-empty plan, principles, drivers, '
                "options, and acceptance fields"
            ),
            "architect": (
                'terminal verdict "APPROVE" | "ITERATE" | '
                '"REQUEST_CHANGES" | "FAILED"; APPROVE also requires '
                "steelman, tradeoff, and synthesis"
            ),
            "critic": (
                'terminal verdict "APPROVE" | "ITERATE" | '
                '"REQUEST_CHANGES" | "FAILED" and evidence-backed critique'
            ),
        }[stage]
        lines.extend(
            [
                "",
                f"## Strict-v2 {stage} contract",
                f"- Ordered loop: {V2_STAGE_ORDER_NOTE}",
                f"- Return one JSON object in `stages/{stage}-{round_n:02d}.json`.",
                "- Markdown/prose verdicts and legacy verifier artifacts are invalid.",
                f"- Required content: {required}.",
                f'- schema_version: 2; run_id: "{run_id}"; stage/role: "{stage}";',
                f"  round: {round_n}; invocation_id: {invocation_id};",
                f"  session_id: {session_id}; input_sha256: {input_sha256}.",
                "- Do not set writer/authority fields; the CLI stamps valid proposals.",
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


# Cross-artifact severity ranks (mirror verdict._SEVERITY_RANK; local to avoid
# exporting a private helper). FAILED > REQUEST_CHANGES > APPROVE; UNKNOWN=0.
_VERIFIER_SEVERITY_RANK = {
    "FAILED": 3,
    "REQUEST_CHANGES": 2,
    "APPROVE": 1,
}


def verifier_has_approve(root: Path, run_id: str, round_n: int) -> bool:
    """Check verifier artifacts for this round via cross-artifact severity.

    Parse BOTH sibling artifacts (``.md`` and ``.json``) independently, then
    take the most severe verdict (FAILED > REQUEST_CHANGES > APPROVE). Approve
    ONLY if the aggregate is APPROVE. A real REQUEST_CHANGES/FAILED in either
    sibling must beat an APPROVE in the other (closes the raw-``or`` false-green
    where a path-bound md REQUEST_CHANGES was overridden by a legacy-exempt
    unbound json APPROVE).

    Dry-run stub ``.md`` (no severity signal → UNKNOWN) + real ``.json`` APPROVE
    still aggregates to APPROVE — stubs are not rejects.
    """
    md = stage_artifact_path(root, run_id, "verifier", round_n)
    js = stage_artifact_json_path(root, run_id, "verifier", round_n)
    best: str | None = None
    best_rank = 0
    for path in (md, js):
        v = parse_verdict_file(path, expected_run_id=run_id)
        rank = _VERIFIER_SEVERITY_RANK.get(v, 0)
        if rank > best_rank:
            best = v
            best_rank = rank
    return best == "APPROVE"


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
    invocation_id: str | None = None,
    session_id: str | None = None,
    session_attempt: int = 0,
    input_sha256: str | None = None,
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
        invocation_id=invocation_id,
        session_id=session_id,
        input_sha256=input_sha256,
    )
    prompt_path = stage_prompt_path(root, run_id, stage, round_n)
    prompt_path.write_text(prompt, encoding="utf-8")

    # Also mirror last stage prompt at run root for debugging
    (run_dir / "last_stage_prompt.md").write_text(prompt, encoding="utf-8")
    (run_dir / "last_stage").write_text(f"{stage}\n", encoding="utf-8")

    # Critic/verifier: always RO — ignore parent yolo, force safe + no shell.
    # Draft/revise may still inherit parent yolo/safe.
    ro = stage in READ_ONLY_STAGES or stage in V2_READ_ONLY_STAGES
    argv = build_grok_argv(
        mode="ralplan",
        goal=goal,
        yolo=False if ro else yolo,
        cwd=root,
        safe=True if ro else safe,
        extra=extra,
        run_id=run_id,
        skill_root=plugin_root(),
        prompt=prompt,
        disallow_shell=ro,
        new_session_id=(
            session_id if session_id is not None and session_attempt == 0 else None
        ),
        resume_session_id=(
            session_id if session_id is not None and session_attempt > 0 else None
        ),
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


def _run_ralplan_v1(
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
    """Run the frozen legacy-v1 RALPLAN FSM.

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
            # Fail-closed: non-zero stage exit cannot accept (Codex P0)
            approved = (
                last_rc == 0
                and verifier_has_approve(root_path, run_id, round_n)
            )
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


def _v2_stamp_path(root: Path, run_id: str, stage: str, round_n: int) -> Path:
    return stages_dir(root, run_id) / f"{stage}-{round_n:02d}.stamp.json"


def _sha_json(value: Any) -> str:
    raw = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(raw).hexdigest()


def _v2_entry(
    state: dict[str, Any], stage: str, round_n: int
) -> dict[str, Any] | None:
    for entry in reversed(state.get("history", [])):
        if entry.get("stage") == stage and entry.get("round") == round_n:
            return entry
    return None


def _v2_input_hash(state: dict[str, Any], stage: str, round_n: int) -> str:
    if stage == "planner":
        prior = [
            {
                "stage": role,
                "proposal_sha256": (
                    (_v2_entry(state, role, round_n - 1) or {}).get(
                        "proposal_sha256"
                    )
                ),
                "verdict": (
                    (_v2_entry(state, role, round_n - 1) or {}).get("verdict")
                ),
            }
            for role in ("planner", "architect", "critic")
        ]
        return _sha_json(
            {
                "run_id": state["run_id"],
                "goal": state["goal"],
                "stage": stage,
                "round": round_n,
                "prior": prior if round_n > 1 else [],
            }
        )
    roles = ("planner",) if stage == "architect" else ("planner", "architect")
    return _sha_json(
        {
            "run_id": state["run_id"],
            "stage": stage,
            "round": round_n,
            "inputs": [
                {
                    "stage": role,
                    "proposal_sha256": (
                        (_v2_entry(state, role, round_n) or {}).get(
                            "proposal_sha256"
                        )
                    ),
                    "valid": bool(
                        (_v2_entry(state, role, round_n) or {}).get("valid")
                    ),
                }
                for role in roles
            ],
        }
    )


def _has_value(value: Any) -> bool:
    return bool(value.strip()) if isinstance(value, str) else bool(value)


def _validate_v2_proposal(
    root: Path,
    run_id: str,
    stage: str,
    round_n: int,
    *,
    invocation_id: str,
    session_id: str,
    input_sha256: str,
    started_at: datetime,
) -> dict[str, Any]:
    """Validate one current JSON proposal and add a distinct CLI stamp."""

    proposal_path = stage_artifact_json_path(root, run_id, stage, round_n)
    if not proposal_path.is_file():
        raise ValueError(f"missing structured {stage} proposal")
    if proposal_path.stat().st_mtime < started_at.timestamp():
        raise ValueError(f"stale {stage} proposal predates current invocation")
    try:
        payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid structured {stage} proposal") from exc
    if not isinstance(payload, dict) or "writer" in payload:
        raise ValueError(f"{stage} proposal is not untrusted structured output")
    expected = {
        "schema_version": 2,
        "run_id": run_id,
        "stage": stage,
        "role": stage,
        "round": round_n,
        "invocation_id": invocation_id,
        "session_id": session_id,
        "input_sha256": input_sha256,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"{stage} proposal identity mismatch for {key}")
    verdict = parse_structured_verdict(payload.get("verdict"))
    allowed = (
        {"READY"}
        if stage == "planner"
        else {"APPROVE", "ITERATE", "REQUEST_CHANGES", "FAILED"}
    )
    if verdict not in allowed:
        raise ValueError(f"invalid terminal {stage} verdict")
    if payload.get("stub") is True or payload.get("is_stub") is True:
        raise ValueError(f"stub {stage} proposal cannot satisfy consensus")
    if stage == "planner":
        required = ("plan", "principles", "drivers", "options", "acceptance")
        if any(not _has_value(payload.get(key)) for key in required):
            raise ValueError("planner proposal lacks structured plan fields")
    elif stage == "architect" and verdict == "APPROVE":
        if any(
            not _has_value(payload.get(key))
            for key in ("steelman", "tradeoff", "synthesis")
        ):
            raise ValueError("architect approval lacks steelman/tradeoff/synthesis")
    elif stage == "critic" and verdict == "APPROVE":
        checks = (
            ("options_assessment", "options"),
            ("premortem", "pre_mortem"),
            ("acceptance_assessment", "acceptance"),
            ("test_plan", "tests"),
            ("synthesis",),
        )
        if any(
            not any(_has_value(payload.get(key)) for key in aliases)
            for aliases in checks
        ):
            raise ValueError("critic approval lacks options/risk/acceptance/test proof")
    proposal_sha256 = sha256(proposal_path.read_bytes()).hexdigest()
    stamp = {
        "writer": "omg-cli",
        "schema_version": 2,
        "run_id": run_id,
        "stage": stage,
        "role": stage,
        "round": round_n,
        "invocation_id": invocation_id,
        "session_id": session_id,
        "input_sha256": input_sha256,
        "proposal": str(proposal_path.relative_to(_run_dir(root, run_id))),
        "proposal_sha256": proposal_sha256,
        "verdict": verdict,
        "stamped_at": _utc_now(),
    }
    _atomic_write_json(_v2_stamp_path(root, run_id, stage, round_n), stamp)
    return stamp


def _initial_v2_state(
    run_id: str, goal: str, max_rounds: int
) -> dict[str, Any]:
    now = _utc_now()
    return {
        "schema_version": 2,
        "lifecycle_version": 2,
        "run_id": run_id,
        "goal": goal,
        "status": "planner",
        "stage": "planner",
        "round": 0,
        "max_rounds": max_rounds,
        "history": [],
        "sessions": {
            role: {"session_id": str(uuid.uuid4()), "attempts": 0}
            for role in ("planner", "architect", "critic")
        },
        "accepted": False,
        "fsm": V2_STAGE_ORDER_NOTE,
        "created_at": now,
        "updated_at": now,
    }


def _run_ralplan_v2(
    goal: str,
    *,
    root: Path,
    run_id: str,
    max_rounds: int | None,
    yolo: bool,
    safe: bool,
    dry_run: bool,
    timeout: float | None,
    extra: Sequence[str] | None,
    stage_executor: Callable[..., int] | None,
) -> int:
    from omg_cli.state import LifecycleLockError, execution_lease

    ceiling = min(
        V2_MAX_ROUNDS,
        max(1, V2_MAX_ROUNDS if max_rounds is None else int(max_rounds)),
    )
    executor = stage_executor or _execute_stage
    try:
        with execution_lease(
            root, run_id, intent="ralplan-v2-consensus", timeout_s=5.0
        ) as lease:
            stages_dir(root, run_id).mkdir(parents=True, exist_ok=True)
            state = load_ralplan_state(root, run_id)
            if state is None:
                state = _initial_v2_state(run_id, goal, ceiling)
            elif (
                state.get("schema_version") != 2
                or state.get("lifecycle_version") != 2
                or state.get("goal") != goal
            ):
                print("omg ralplan: invalid strict-v2 resume state", file=sys.stderr)
                return 1
            if state.get("accepted") is True:
                return 0
            state["max_rounds"] = ceiling
            save_ralplan_state(root, run_id, state)

            prior_rounds = [
                item.get("round", 0)
                for item in state.get("history", [])
                if item.get("stage") in {"architect", "critic"}
            ]
            first_round = max(prior_rounds, default=0) + 1
            for round_n in range(first_round, ceiling + 1):
                for stage in ("planner", "architect", "critic"):
                    if stage == "critic":
                        planner = _v2_entry(state, "planner", round_n) or {}
                        architect = _v2_entry(state, "architect", round_n) or {}
                        if not (
                            planner.get("valid") is True
                            and architect.get("valid") is True
                            and architect.get("verdict") == "APPROVE"
                            and architect.get("exit_code") == 0
                        ):
                            break
                    binding = state["sessions"][stage]
                    session_id = str(binding["session_id"])
                    session_attempt = int(binding["attempts"])
                    binding["attempts"] = session_attempt + 1
                    invocation_id = str(uuid.uuid4())
                    input_sha256 = _v2_input_hash(state, stage, round_n)
                    state.update({"status": stage, "stage": stage, "round": round_n})
                    save_ralplan_state(root, run_id, state)
                    write_status(
                        root,
                        run_id,
                        "running",
                        extra={
                            "stage": stage,
                            "round": round_n,
                            "ralplan_status": stage,
                            "max_rounds": ceiling,
                        },
                        lease=lease,
                    )
                    started_at = datetime.now(timezone.utc)
                    rc = int(
                        executor(
                            stage,
                            root=root,
                            run_id=run_id,
                            goal=goal,
                            round_n=round_n,
                            max_rounds=ceiling,
                            yolo=yolo,
                            safe=safe,
                            dry_run=dry_run,
                            timeout=timeout,
                            extra=extra,
                            invocation_id=invocation_id,
                            session_id=session_id,
                            session_attempt=session_attempt,
                            input_sha256=input_sha256,
                        )
                    )
                    stamp: dict[str, Any] | None = None
                    error: str | None = None
                    if rc == 0:
                        try:
                            stamp = _validate_v2_proposal(
                                root,
                                run_id,
                                stage,
                                round_n,
                                invocation_id=invocation_id,
                                session_id=session_id,
                                input_sha256=input_sha256,
                                started_at=started_at,
                            )
                        except (OSError, TypeError, ValueError) as exc:
                            error = str(exc)
                    entry = {
                        "stage": stage,
                        "role": stage,
                        "round": round_n,
                        "invocation_id": invocation_id,
                        "session_id": session_id,
                        "input_sha256": input_sha256,
                        "proposal_sha256": (
                            stamp.get("proposal_sha256") if stamp else None
                        ),
                        "verdict": stamp.get("verdict") if stamp else "INVALID",
                        "exit_code": rc,
                        "valid": stamp is not None and rc == 0,
                        "error": error,
                        "at": _utc_now(),
                    }
                    state["history"].append(entry)
                    save_ralplan_state(root, run_id, state)
                    if rc != 0:
                        break
                    if stage == "architect" and not (
                        entry["valid"] and entry["verdict"] == "APPROVE"
                    ):
                        break
                    if stage == "critic":
                        if entry["valid"] and entry["verdict"] == "APPROVE":
                            state.update(
                                {"status": "accepted", "stage": "accepted", "accepted": True}
                            )
                            save_ralplan_state(root, run_id, state)
                            write_status(
                                root,
                                run_id,
                                "running",
                                extra={
                                    "stage": "accepted",
                                    "ralplan_status": "accepted",
                                    "ralplan_consensus": True,
                                    "round": round_n,
                                },
                                lease=lease,
                            )
                            print(
                                f"omg ralplan: accepted strict-v2 run {run_id} "
                                f"(Architect then Critic)"
                            )
                            return 0
                        break

            state.update(
                {
                    "status": "blocked",
                    "stage": "blocked",
                    "accepted": False,
                    "blocker": {
                        "code": "ralplan_consensus_not_reached",
                        "resumable": True,
                        "message": f"no consensus within max_rounds={ceiling}",
                    },
                }
            )
            save_ralplan_state(root, run_id, state)
            write_status(
                root,
                run_id,
                "blocked",
                extra={
                    "stage": "blocked",
                    "ralplan_status": "blocked",
                    "ralplan_consensus": False,
                    "blocker": state["blocker"],
                },
                lease=lease,
            )
            return 1
    except LifecycleLockError as exc:
        print(f"omg ralplan: strict lifecycle lease failed: {exc}", file=sys.stderr)
        return 1


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
    """Dispatch deterministically to frozen v1 or ordered strict-v2 RALPLAN."""

    from omg_cli.state import RunSchema, classify_run_schema

    root_path = Path(root) if root is not None else Path.cwd().resolve()
    goal = (goal or "").strip() or "(no goal)"
    if existing_run_id is None:
        return _run_ralplan_v1(
            goal,
            root=root_path,
            max_rounds=max_rounds,
            yolo=yolo,
            safe=safe,
            dry_run=dry_run,
            timeout=timeout,
            extra=extra,
            force=force,
            stage_executor=stage_executor,
        )
    run = load_run(root_path, existing_run_id)
    if run is None:
        print(f"omg ralplan: no run found: {existing_run_id!r}", file=sys.stderr)
        return 1
    try:
        schema = classify_run_schema(run)
    except (TypeError, ValueError) as exc:
        print(f"omg ralplan: refusing malformed run schema: {exc}", file=sys.stderr)
        return 1
    if schema is RunSchema.LEGACY_V1:
        return _run_ralplan_v1(
            goal,
            root=root_path,
            max_rounds=max_rounds,
            yolo=yolo,
            safe=safe,
            dry_run=dry_run,
            timeout=timeout,
            extra=extra,
            force=force,
            existing_run_id=existing_run_id,
            stage_executor=stage_executor,
        )
    return _run_ralplan_v2(
        goal,
        root=root_path,
        run_id=existing_run_id,
        max_rounds=max_rounds,
        yolo=yolo,
        safe=safe,
        dry_run=dry_run,
        timeout=timeout,
        extra=extra,
        stage_executor=stage_executor,
    )


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
