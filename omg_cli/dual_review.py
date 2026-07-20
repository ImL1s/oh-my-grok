"""Grok-native dual-review: sequential headless critic → verifier (read-only).

**Interim mode (explicit):** the CLI path launches critic then verifier as two
sequential headless Grok processes. This is **not** native ``spawn_subagent``
parallel dual-review. Preferred TUI path: skill ``omg-dual-review`` with
``spawn_subagent`` (depth=1, capability_mode=read-only).

Set ``OMG_DUAL_REVIEW_REQUIRE_NATIVE=1`` to refuse the sequential headless
CLI path (exit 2) until a native spawn-based dual-review ships.

Never sets verified.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from omg_cli.modes import (
    DEFAULT_TIMEOUT,
    HARD_RULES_REMINDER,
    _launch_grok,
    build_grok_argv,
    plugin_root,
)
from omg_cli.state import create_run, load_run, write_status
from omg_cli.verdict import (
    apply_stage_exit_codes,
    parse_verdict,
    parse_verdict_file,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_dir(root: Path, run_id: str) -> Path:
    from omg_cli.state import _safe_run_id

    return Path(root) / ".omg" / "state" / "runs" / _safe_run_id(run_id)


def _stages_dir(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "stages"


def dual_review_state_path(root: Path, run_id: str) -> Path:
    return _run_dir(root, run_id) / "dual_review.json"


def stage_artifact_path(root: Path, run_id: str, role: str, round_n: int) -> Path:
    return _stages_dir(root, run_id) / f"dual-{role}-{round_n:02d}.md"


def stage_prompt_path(root: Path, run_id: str, role: str, round_n: int) -> Path:
    return _stages_dir(root, run_id) / f"dual-{role}-{round_n:02d}.prompt.md"


def load_agent_body(name: str, *, root: Path | None = None) -> str:
    """Load agents/omg-{name}.md body (strip YAML frontmatter if present)."""
    base = Path(root) if root is not None else plugin_root()
    path = base / "agents" / f"omg-{name}.md"
    if not path.is_file():
        alt = plugin_root() / "agents" / f"omg-{name}.md"
        if alt.is_file():
            path = alt
        else:
            return f"(agent body missing: omg-{name}.md)"
    text = path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return text.strip()


def build_dual_prompt(
    role: str,
    goal: str,
    *,
    run_id: str,
    round_n: int,
    run_dir: Path | None = None,
    critic_artifact: Path | None = None,
    plan_artifact: str | None = None,
    git_summary: str | None = None,
) -> str:
    """Compose critic or verifier prompt with agent body + HARD RULES."""
    assert role in ("critic", "verifier")
    agent = load_agent_body(role)
    lines = [
        agent,
        "",
        HARD_RULES_REMINDER,
        "",
        f"## Active mode: dual-review ({role})",
        f"## Run id: {run_id}",
        f"## Round: {round_n}",
        "",
        "## Dual-review context pack",
        f"- run_id: {run_id}",
        f"- role: {role}",
        f"- plan_artifact: {plan_artifact or '(none)'}",
        f"- git_summary: {git_summary or '(not collected)'}",
    ]
    if run_dir is not None:
        lines.append(f"- run_dir: {run_dir}")
    lines.extend(
        [
            "",
            "## Capability (mandatory)",
            "- capability_mode: **read-only** (or permissionMode plan).",
            "- Forbidden: search_replace on product source, spawn_subagent,",
            "  applying patches, running implementation agents.",
            "- Do **not** set verified / passes in `.omg/state/`.",
        ]
    )
    if role == "critic":
        lines.extend(
            [
                "",
                "## Critic task",
                "Review the goal/plan/code adversarially. Severity-rank findings.",
                "Do not APPROVE to be helpful. Output structured blockers.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## Verifier task",
                "Independent evidence check. Verdict: **APPROVE** | **REQUEST CHANGES** | **FAILED**.",
                "Critic findings are input, not authority — re-validate yourself.",
                "Write explicit verdict into the stage artifact.",
            ]
        )
        if critic_artifact is not None:
            lines.append(f"- critic artifact: `{critic_artifact}`")
    lines.extend(
        [
            "",
            "## Goal",
            goal.strip() or "(no goal provided)",
            "",
            "Grok-native only. External second opinions: human runs `omg ask` separately.",
        ]
    )
    return "\n".join(lines)


def _git_summary(root: Path) -> str:
    import subprocess

    try:
        r = subprocess.run(
            ["git", "status", "--short"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        out = (r.stdout or "").strip()
        if not out:
            return "(clean or not a git repo)"
        lines = out.splitlines()
        if len(lines) > 40:
            return "\n".join(lines[:40]) + f"\n… (+{len(lines) - 40} more)"
        return out
    except Exception as exc:
        return f"(git summary unavailable: {exc})"


def _execute_dual_stage(
    role: str,
    *,
    root: Path,
    run_id: str,
    goal: str,
    round_n: int,
    yolo: bool,
    safe: bool,
    dry_run: bool,
    timeout: float | None,
    critic_artifact: Path | None = None,
    extra: Sequence[str] | None = None,
) -> int:
    root = Path(root)
    run_dir = _run_dir(root, run_id)
    sdir = _stages_dir(root, run_id)
    sdir.mkdir(parents=True, exist_ok=True)

    prompt = build_dual_prompt(
        role,
        goal,
        run_id=run_id,
        round_n=round_n,
        run_dir=run_dir,
        critic_artifact=critic_artifact,
        git_summary=_git_summary(root),
    )
    prompt_path = stage_prompt_path(root, run_id, role, round_n)
    prompt_path.write_text(prompt, encoding="utf-8")
    (run_dir / "last_stage_prompt.md").write_text(prompt, encoding="utf-8")
    (run_dir / "last_stage").write_text(f"dual-{role}\n", encoding="utf-8")

    # Use ralplan mode skill slot only for argv machinery; prompt is fully custom.
    # Critic/verifier are always read-only: parent yolo/safe args are ignored.
    argv = build_grok_argv(
        mode="ralplan",
        goal=goal,
        yolo=False,
        cwd=root,
        safe=True,
        extra=extra,
        run_id=run_id,
        skill_root=plugin_root(),
        prompt=prompt,
        disallow_shell=True,
    )
    argv_path = sdir / f"dual-{role}-{round_n:02d}.argv.json"
    argv_path.write_text(
        json.dumps(argv, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    rc = _launch_grok(
        argv,
        cwd=root,
        run_dir=run_dir,
        timeout=timeout,
        dry_run=dry_run,
    )

    art = stage_artifact_path(root, run_id, role, round_n)
    if not art.is_file():
        # dry_run / missing output: never emit APPROVE — only real verifier
        # artifacts may accept. Use NEEDS_REVIEW so parse_verdict → UNKNOWN.
        if dry_run:
            stub = (
                f"# dual-review {role} (dry_run stub)\n"
                f"run_id: {run_id}\nround: {round_n}\n\n"
                "dry_run: no Grok exec. Verdict placeholder: NEEDS_REVIEW\n"
                "NEEDS_REVIEW\n"
            )
        else:
            stub = (
                f"# dual-review {role}\n"
                f"run_id: {run_id}\nround: {round_n}\n"
                f"dry_run: {bool(dry_run)}\n\n"
                "Stub artifact — Grok did not write this file.\n"
                "Verifier acceptance requires explicit APPROVE in real runs.\n"
            )
        art.write_text(stub, encoding="utf-8")
    return int(rc)


def require_native_dual_review() -> bool:
    """True when ``OMG_DUAL_REVIEW_REQUIRE_NATIVE=1`` (refuse sequential CLI path)."""
    return os.environ.get("OMG_DUAL_REVIEW_REQUIRE_NATIVE", "").strip() == "1"


def run_dual_review(
    goal: str,
    *,
    root: Path | str | None = None,
    run_id: str | None = None,
    round_n: int = 1,
    dry_run: bool = False,
    timeout: float | None = DEFAULT_TIMEOUT,
    yolo: bool = False,
    safe: bool = False,
    force: bool = False,
    create_if_missing: bool = True,
    stage_executor: Callable[..., int] | None = None,
    extra: Sequence[str] | None = None,
) -> str:
    """Run critic then verifier sequentially (headless PARTIAL mode).

    Returns APPROVE|REQUEST_CHANGES|FAILED|UNKNOWN.

    This is **explicit PARTIAL** dual-review: two sequential headless launches,
    not native spawn_subagent dual-review. Never sets verified.

    When ``run_id`` is None and create_if_missing, creates a mode=dual-review run.
    Raises RuntimeError if ``OMG_DUAL_REVIEW_REQUIRE_NATIVE=1``.
    """
    if require_native_dual_review():
        raise RuntimeError(
            "OMG_DUAL_REVIEW_REQUIRE_NATIVE=1: sequential headless dual-review "
            "is disabled. Native spawn_subagent dual-review is not yet shipped; "
            "unset the env var to use the sequential CLI path, or run the "
            "omg-dual-review skill in a TUI session with spawn_subagent."
        )

    root_path = Path(root) if root is not None else Path.cwd().resolve()
    goal = (goal or "").strip() or "(no goal)"
    round_n = max(1, int(round_n))
    executor = stage_executor or _execute_dual_stage

    if run_id is None:
        if not create_if_missing:
            raise ValueError("run_id required when create_if_missing=False")
        try:
            run = create_run(
                root_path,
                mode="dual-review",
                goal=goal,
                extra={
                    "note": "Grok-native dual-review; never sets verified",
                    "dual_review": True,
                },
                force=force,
            )
        except RuntimeError as exc:
            print(f"omg dual-review: {exc}", file=sys.stderr)
            return "FAILED"
        run_id = run["run_id"]
    else:
        if load_run(root_path, run_id) is None:
            print(f"omg dual-review: no run {run_id}", file=sys.stderr)
            return "FAILED"

    run_dir = _run_dir(root_path, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    _stages_dir(root_path, run_id).mkdir(parents=True, exist_ok=True)

    history: list[dict[str, Any]] = []
    state: dict[str, Any] = {
        "version": 1,
        "run_id": run_id,
        "goal": goal,
        "round": round_n,
        "history": history,
        "mode": "sequential_headless_partial",
        "note": (
            "Grok-native dual-review (sequential headless PARTIAL; "
            "not native spawn_subagent); never sets verified"
        ),
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    }

    write_status(
        root_path,
        run_id,
        "running",
        extra={"stage": "dual_critic", "dual_review_round": round_n},
    )

    # Critic
    rc_c = executor(
        "critic",
        root=root_path,
        run_id=run_id,
        goal=goal,
        round_n=round_n,
        yolo=yolo,
        safe=safe,
        dry_run=dry_run,
        timeout=timeout,
        critic_artifact=None,
        extra=extra,
    )
    critic_art = stage_artifact_path(root_path, run_id, "critic", round_n)
    history.append(
        {
            "stage": "critic",
            "round": round_n,
            "at": _utc_now(),
            "exit_code": rc_c,
            "artifact": str(critic_art),
        }
    )

    # Verifier
    write_status(
        root_path,
        run_id,
        "running",
        extra={"stage": "dual_verifier", "dual_review_round": round_n},
    )
    rc_v = executor(
        "verifier",
        root=root_path,
        run_id=run_id,
        goal=goal,
        round_n=round_n,
        yolo=yolo,
        safe=safe,
        dry_run=dry_run,
        timeout=timeout,
        critic_artifact=critic_art,
        extra=extra,
    )
    verifier_art = stage_artifact_path(root_path, run_id, "verifier", round_n)
    # Structured schema v2 can bind run_id; mismatch fails closed (research R3).
    verdict = parse_verdict_file(verifier_art, expected_run_id=run_id)
    # Fail-closed: non-zero stage rc must never leave APPROVE (Codex P0).
    verdict = apply_stage_exit_codes(
        verdict, critic_rc=rc_c, verifier_rc=rc_v
    )
    # dry_run stubs intentionally omit APPROVE (NEEDS_REVIEW → UNKNOWN).
    # Callers that need FSM progression (pipeline) handle dry_run themselves.
    history.append(
        {
            "stage": "verifier",
            "round": round_n,
            "at": _utc_now(),
            "exit_code": rc_v,
            "artifact": str(verifier_art),
            "verdict": verdict,
        }
    )

    state["verdict"] = verdict
    state["updated_at"] = _utc_now()
    dual_review_state_path(root_path, run_id).write_text(
        json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    # Synthesis under artifacts
    art_dir = root_path / ".omg" / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    synth = art_dir / f"dual-review-{run_id}.md"
    synth.write_text(
        f"# dual-review synthesis — {run_id}\n\n"
        f"- goal: {goal}\n"
        f"- round: {round_n}\n"
        f"- verdict: {verdict}\n"
        f"- critic: {critic_art}\n"
        f"- verifier: {verifier_art}\n"
        f"- dry_run: {bool(dry_run)}\n\n"
        "Does **not** set omg verified. Product verification requires "
        "`omg accept` / frozen acceptance.\n",
        encoding="utf-8",
    )

    # Mark dual-review-only runs completed (not verified)
    current = load_run(root_path, run_id) or {}
    if current.get("mode") == "dual-review":
        write_status(
            root_path,
            run_id,
            "completed",
            extra={
                "stage": "dual_review_done",
                "dual_review_verdict": verdict,
                "note": "dual-review complete; verified remains false",
            },
        )

    print(f"omg dual-review: run={run_id} verdict={verdict}")
    return verdict


def run_dual_review_cli(
    goal: str,
    *,
    root: Path | str | None = None,
    run_id: str | None = None,
    dry_run: bool = False,
    timeout: float | None = None,
    yolo: bool = False,
    safe: bool = False,
    force: bool = False,
) -> int:
    """CLI exit: 0 on APPROVE, 1 otherwise, 2 if native-only gate set.

    Never sets verified. Sequential headless path is PARTIAL (see module doc).
    """
    try:
        verdict = run_dual_review(
            goal,
            root=root,
            run_id=run_id,
            dry_run=dry_run,
            timeout=timeout,
            yolo=yolo,
            safe=safe,
            force=force,
        )
    except RuntimeError as exc:
        # Feature gate for incomplete native path
        print(f"omg dual-review: {exc}", file=sys.stderr)
        return 2
    if verdict == "APPROVE":
        return 0
    return 1


__all__ = [
    "build_dual_prompt",
    "dual_review_state_path",
    "load_agent_body",
    "parse_verdict",
    "parse_verdict_file",
    "require_native_dual_review",
    "run_dual_review",
    "run_dual_review_cli",
    "stage_artifact_path",
    "stage_prompt_path",
]
