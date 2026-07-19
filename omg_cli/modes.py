"""Mode launchers for oh-my-grok (ulw / ralph / ralplan skeleton).

Builds ``grok -p`` argv with skill bodies + HARD RULES, creates run state,
and (for ralph) loops max_iter times. Never sets verified without acceptance.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Sequence

from omg_cli.state import (
    create_run,
    load_run,
    set_verified,
    write_status,
)

# Mode short-name -> skill path relative to plugin/repo root
MODE_SKILL_REL: dict[str, str] = {
    "ulw": "skills/omg-ultrawork/SKILL.md",
    "ralph": "skills/omg-ralph/SKILL.md",
    "ralplan": "skills/omg-ralplan/SKILL.md",
}

DEFAULT_MAX_ITER: dict[str, int] = {
    "ulw": 1,
    "ralph": 3,
    "ralplan": 1,
}

# Default subprocess timeout (seconds); None = no limit
DEFAULT_TIMEOUT: float | None = None

HARD_RULES_REMINDER = """
## HARD RULES reminder (omg CLI injection — non-negotiable)
- Fan-out ONLY via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- NEVER invoke claude/codex/omc team/agy/cursor-agent as default workers.
- Use Grok tool names: read_file, search_replace, run_terminal_command, spawn_subagent, grep, list_dir.
- State: only the omg CLI is authoritative for passes/verified; write proposals under .omg/artifacts/ only.
- Do NOT set verified yourself. Cancel with `omg cancel` — never self-matching `pkill -f`.
""".strip()


def plugin_root() -> Path:
    """Repo/plugin root containing skills/ (parent of omg_cli/)."""
    return Path(__file__).resolve().parents[1]


def skill_path_for(mode: str, *, root: Path | None = None) -> Path:
    if mode not in MODE_SKILL_REL:
        raise ValueError(f"unknown mode {mode!r}; expected one of {sorted(MODE_SKILL_REL)}")
    base = Path(root) if root is not None else plugin_root()
    path = base / MODE_SKILL_REL[mode]
    if not path.is_file():
        # fallback: always resolve against installed plugin root
        alt = plugin_root() / MODE_SKILL_REL[mode]
        if alt.is_file():
            return alt
        raise FileNotFoundError(f"skill body missing for mode={mode!r}: {path}")
    return path


def load_skill_body(mode: str, *, root: Path | None = None) -> str:
    return skill_path_for(mode, root=root).read_text(encoding="utf-8")


def build_prompt(
    mode: str,
    goal: str,
    *,
    iteration: int | None = None,
    max_iter: int | None = None,
    run_id: str | None = None,
    skill_root: Path | None = None,
) -> str:
    """Compose the -p prompt: skill body + HARD RULES + goal (+ ralph iter note)."""
    skill = load_skill_body(mode, root=skill_root)
    parts = [
        skill,
        "",
        HARD_RULES_REMINDER,
        "",
        f"## Active mode: {mode}",
    ]
    if run_id:
        parts.append(f"## Run id: {run_id}")
    if mode == "ralph":
        parts.append(
            "## Ralph iteration contract\n"
            "Implement **ONE** story then **stop**. Outer CLI owns the loop. "
            "Do not mark verified. Leave evidence under `.omg/artifacts/`."
        )
        if iteration is not None:
            total = max_iter if max_iter is not None else "?"
            parts.append(f"## Iteration: {iteration}/{total}")
    parts.extend(
        [
            "",
            "## Goal",
            goal.strip() or "(no goal provided)",
            "",
            "Follow the skill playbook above. Prefer spawn_subagent for parallel work.",
        ]
    )
    return "\n".join(parts)


def build_grok_argv(
    mode: str,
    goal: str,
    yolo: bool = False,
    cwd: str | Path | None = None,
    safe: bool = False,
    extra: Sequence[str] | None = None,
    *,
    iteration: int | None = None,
    max_iter: int | None = None,
    run_id: str | None = None,
    skill_root: Path | None = None,
    prompt: str | None = None,
) -> list[str]:
    """Build argv for ``grok -p <prompt>``.

    Never includes ``--yolo`` / ``bypassPermissions`` unless ``yolo=True``.
    Grok CLI has no ``--yolo`` flag; when yolo we map to
    ``--permission-mode bypassPermissions`` (+ ``--always-approve``).
    """
    if mode not in MODE_SKILL_REL:
        raise ValueError(f"unknown mode {mode!r}")

    if prompt is None:
        prompt = build_prompt(
            mode,
            goal,
            iteration=iteration,
            max_iter=max_iter,
            run_id=run_id,
            skill_root=skill_root,
        )

    argv: list[str] = ["grok"]

    if cwd is not None:
        argv.extend(["--cwd", str(cwd)])

    # safe: prefer non-elevated defaults (no yolo). If both set, yolo wins.
    if yolo and not safe:
        # Documented mapping: grok has no --yolo; use permission-mode + always-approve
        argv.extend(["--permission-mode", "bypassPermissions"])
        argv.append("--always-approve")
    elif safe and not yolo:
        # Keep plan-friendly / non-elevated mode when user asked for safe
        argv.extend(["--permission-mode", "default"])

    argv.extend(["-p", prompt])

    if extra:
        argv.extend(list(extra))

    return argv


def _run_dir(root: Path, run_id: str) -> Path:
    return Path(root) / ".omg" / "state" / "runs" / run_id


def _write_prd_scaffold(root: Path, run_id: str, goal: str) -> Path:
    """Write ralph PRD scaffold JSON under run dir and artifacts/."""
    root = Path(root)
    payload: dict[str, Any] = {
        "goal": goal,
        "run_id": run_id,
        "stories": [],
        "current_story": None,
        "acceptance": [],
        "status": "scaffold",
        "note": "proposal only — omg CLI owns verified",
    }
    run_dir = _run_dir(root, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    prd_run = run_dir / "prd.json"
    prd_run.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    art_dir = root / ".omg" / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    prd_art = art_dir / f"prd-{run_id}.json"
    prd_art.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return prd_run


def _try_set_verified(root: Path, run_id: str) -> bool:
    """Set verified only if acceptance artifact exists. Never force."""
    try:
        set_verified(root, run_id, force=False)
        return True
    except PermissionError:
        return False
    except FileNotFoundError:
        return False


def _launch_grok(
    argv: list[str],
    *,
    cwd: Path,
    run_dir: Path,
    timeout: float | None,
    dry_run: bool,
) -> int:
    """Run grok argv (or dry-run). Writes pid file when a process starts.

    Returns process exit code (0 for dry_run).
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "last_argv.json").write_text(
        json.dumps(argv, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # Also store prompt for debugging (argv may be huge)
    try:
        p_idx = argv.index("-p")
        prompt_text = argv[p_idx + 1] if p_idx + 1 < len(argv) else ""
        (run_dir / "last_prompt.md").write_text(prompt_text, encoding="utf-8")
    except ValueError:
        pass

    if dry_run:
        (run_dir / "dry_run").write_text("1\n", encoding="utf-8")
        return 0

    # Prefer Popen so we can record child PID
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=os.environ.copy(),
    )
    (run_dir / "pid").write_text(f"{proc.pid}\n", encoding="utf-8")
    try:
        return int(proc.wait(timeout=timeout))
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        (run_dir / "timeout").write_text("1\n", encoding="utf-8")
        return 124


def run_mode(
    mode: str,
    goal: str,
    *,
    yolo: bool = False,
    safe: bool = False,
    root: Path | str | None = None,
    max_iter: int | None = None,
    dry_run: bool = False,
    timeout: float | None = DEFAULT_TIMEOUT,
    extra: Sequence[str] | None = None,
) -> int:
    """Create run, launch grok for mode, update status. Returns exit code.

    - ulw / ralplan: typically one launch (max_iter default 1)
    - ralph: loop up to max_iter (default 3); one story per iteration
    - Never sets verified without acceptance artifact
    - dry_run: build argv / scaffolds, skip subprocess
    """
    if mode not in MODE_SKILL_REL:
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2

    root_path = Path(root) if root is not None else Path.cwd().resolve()
    goal = (goal or "").strip() or "(no goal)"

    if max_iter is None:
        max_iter = DEFAULT_MAX_ITER.get(mode, 1)
    max_iter = max(1, int(max_iter))

    run = create_run(
        root_path,
        mode=mode,
        goal=goal,
        extra={"max_iter": max_iter, "yolo": bool(yolo), "safe": bool(safe)},
    )
    run_id = run["run_id"]
    run_dir = _run_dir(root_path, run_id)

    if mode == "ralph":
        _write_prd_scaffold(root_path, run_id, goal)

    write_status(root_path, run_id, "running", extra={"iteration": 0})

    last_rc = 0
    verified = False

    for i in range(1, max_iter + 1):
        argv = build_grok_argv(
            mode=mode,
            goal=goal,
            yolo=yolo,
            cwd=root_path,
            safe=safe,
            extra=extra,
            iteration=i if mode == "ralph" else None,
            max_iter=max_iter if mode == "ralph" else None,
            run_id=run_id,
            skill_root=plugin_root(),
        )

        write_status(
            root_path,
            run_id,
            "running",
            extra={"iteration": i, "passes": i - 1},
        )

        last_rc = _launch_grok(
            argv,
            cwd=root_path,
            run_dir=run_dir,
            timeout=timeout,
            dry_run=dry_run,
        )

        # After each iter: may set verified only with acceptance artifact
        if _try_set_verified(root_path, run_id):
            verified = True
            break

        if last_rc != 0 and not dry_run:
            # Failed launch — stop loop
            break

        # ulw/ralplan: single launch even if max_iter overridden higher without need
        if mode != "ralph":
            break

    # Final status
    current = load_run(root_path, run_id) or {}
    if verified or current.get("verified") is True:
        # set_verified already set status=verified
        return 0

    if last_rc != 0 and not dry_run:
        write_status(
            root_path,
            run_id,
            "failed",
            extra={"exit_code": last_rc, "passes": current.get("passes", 0)},
        )
        return last_rc

    # Completed iterations without acceptance — not verified
    write_status(
        root_path,
        run_id,
        "completed",
        extra={
            "exit_code": 0,
            "verified": False,  # write_status ignores verified keys; explicit guard
            "note": "completed without acceptance artifact; verified remains false",
        },
    )
    # Double-check verified stayed false
    final = load_run(root_path, run_id)
    if final and final.get("verified") is True:
        # Should not happen without acceptance; strip via cancel-style rewrite
        write_status(root_path, run_id, "completed", extra={"note": "verified stripped"})
    return 0
