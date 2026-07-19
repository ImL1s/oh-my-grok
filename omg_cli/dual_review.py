"""Grok-native dual-review: critic → verifier (read-only). Never sets verified."""
from __future__ import annotations

import json
import re
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

# Verdict tokens
_REQUEST_CHANGES_RE = re.compile(
    r"(?<![A-Za-z0-9_])REQUEST[_\s-]?CHANGES(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_FAILED_RE = re.compile(
    r"(?<![A-Za-z0-9_])FAILED(?![A-Za-z0-9_])",
)
_APPROVE_WORD_RE = re.compile(r"(?<![A-Za-z0-9_])APPROVE(?![A-Za-z0-9_])")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_dir(root: Path, run_id: str) -> Path:
    return Path(root) / ".omg" / "state" / "runs" / run_id


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


def parse_verdict(text: str) -> str:
    """Return APPROVE | REQUEST_CHANGES | FAILED | UNKNOWN.

    Prefer JSON fields when content is a JSON object. Whole-word APPROVE
    only counts when REQUEST CHANGES / FAILED are not also terminal winners.
    Order: FAILED > REQUEST_CHANGES > APPROVE when multiple present in prose.
    """
    if not text or not text.strip():
        return "UNKNOWN"
    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            for key in ("verdict", "decision", "status"):
                val = data.get(key)
                if isinstance(val, str):
                    v = val.strip().upper().replace(" ", "_").replace("-", "_")
                    if v in ("APPROVE", "REQUEST_CHANGES", "FAILED"):
                        return v
                    if v == "REQUESTCHANGES":
                        return "REQUEST_CHANGES"
            if data.get("approve") is True:
                return "APPROVE"

    # Prose: priority FAILED > REQUEST_CHANGES > APPROVE when co-present.
    # "do not APPROVE lightly" still matches APPROVE whole-word — document edge case.
    has_failed = bool(_FAILED_RE.search(text))
    has_rc = bool(_REQUEST_CHANGES_RE.search(text))
    has_approve = bool(_APPROVE_WORD_RE.search(text))

    if has_failed:
        return "FAILED"
    if has_rc:
        return "REQUEST_CHANGES"
    if has_approve:
        return "APPROVE"
    return "UNKNOWN"


def parse_verdict_file(path: Path) -> str:
    if not path.is_file():
        return "UNKNOWN"
    try:
        return parse_verdict(path.read_text(encoding="utf-8"))
    except OSError:
        return "UNKNOWN"


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

    # Use ralplan mode skill slot only for argv machinery; prompt is fully custom
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
        if dry_run and role == "verifier":
            # dry_run: allow pipeline FSM to proceed without fake product verify
            stub = (
                f"# dual-review {role} (dry_run stub)\n"
                f"run_id: {run_id}\nround: {round_n}\n\n"
                "dry_run: no Grok exec. Verdict placeholder: APPROVE\n"
                "APPROVE\n"
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
    """Run critic then verifier. Returns APPROVE|REQUEST_CHANGES|FAILED|UNKNOWN.

    Never sets verified. When ``run_id`` is None and create_if_missing, creates
    a mode=dual-review run.
    """
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
        "note": "Grok-native dual-review; never sets verified",
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
    verdict = parse_verdict_file(verifier_art)
    if dry_run and verdict == "UNKNOWN":
        verdict = "APPROVE"  # dry_run progression
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
    """CLI exit: 0 on APPROVE, 1 otherwise. Never sets verified."""
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
    if verdict == "APPROVE":
        return 0
    return 1


__all__ = [
    "build_dual_prompt",
    "dual_review_state_path",
    "load_agent_body",
    "parse_verdict",
    "parse_verdict_file",
    "run_dual_review",
    "run_dual_review_cli",
    "stage_artifact_path",
    "stage_prompt_path",
]
