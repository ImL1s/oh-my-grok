"""Mode launchers for oh-my-grok (ulw / ralph / ralplan skeleton).

Builds ``grok -p`` argv with skill bodies + HARD RULES, creates run state,
and (for ralph) loops max_iter times. Never sets verified without acceptance.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Sequence

from omg_cli.host_session import (
    HostSessionError,
    allocate_host_session,
    load_host_session,
    session_flag_argv,
)
from omg_cli.state import (
    LifecycleLockError,
    RunSchema,
    cancel_run,
    classify_run_schema,
    create_run,
    execution_lease,
    load_active_run,
    load_cancellation_request,
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
    # ralplan: max_rounds for verifier attempts (CLI FSM in ralplan.py)
    "ralplan": 3,
}

# Default subprocess timeout (seconds) for non-dry-run launches.
# None means "use DEFAULT_TIMEOUT when launching" (see resolve_launch_timeout).
# Override via run_mode(timeout=…) / CLI --timeout. Use 0 for unlimited.
DEFAULT_TIMEOUT: float = 3600.0

HARD_RULES_REMINDER = """
## HARD RULES reminder (omg CLI injection — non-negotiable)
- Fan-out ONLY via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- NEVER invoke claude/codex/omc team/agy/cursor-agent as default workers.
- Use Grok tool names: read_file, search_replace, run_terminal_command, spawn_subagent, grep, list_dir.
- State: only the omg CLI is authoritative for passes/verified; write proposals under .omg/artifacts/ only.
- Do NOT set verified yourself. Cancel with `omg cancel` — never self-matching `pkill -f`.
- MUST spawn implementers with capability_mode=read-write (no shell / no Execute).
- MUST spawn critic/verifier/explore with capability_mode=read-only.
- If spawn_subagent is DENIED for missing/wrong capability_mode: RETRY IMMEDIATELY same turn
  with the required capability_mode. Do NOT abandon multi-agent; do NOT switch to solo-only.
- Shell/tests/acceptance only via `omg accept` (semantic command policy); never forge verified.
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


def resolve_launch_timeout(
    timeout: float | None,
    *,
    dry_run: bool = False,
) -> float | None:
    """Effective subprocess timeout for a grok launch.

    - dry_run: unused (no process); returns timeout unchanged
    - timeout is None → DEFAULT_TIMEOUT (3600s)
    - timeout == 0 → unlimited (None for subprocess.wait)
    - timeout > 0 → that many seconds
    """
    if dry_run:
        return timeout
    if timeout is None:
        return float(DEFAULT_TIMEOUT)
    if timeout == 0 or timeout == 0.0:
        return None
    return float(timeout)


def ralph_context_pack(
    *,
    run_id: str | None = None,
    iteration: int | None = None,
    max_iter: int | None = None,
    project_root: Path | str | None = None,
    story: str | None = None,
    frozen_commands_summary: str | None = None,
    acceptance_result_path: str | None = None,
) -> str:
    """Build ralph iteration context block for prompt injection.

    Includes run_id, iteration, current story, frozen commands summary, and
    path to acceptance.result.json (for the worker to read failures — not write).
    """
    root = Path(project_root) if project_root is not None else None
    story_text = story
    cmds_summary = frozen_commands_summary
    acc_path = acceptance_result_path

    if root is not None and run_id:
        try:
            from omg_cli.acceptance import (
                collect_commands,
                load_prd,
                result_path,
            )

            if acc_path is None:
                acc_path = str(result_path(root, run_id))

            prd = load_prd(root, run_id)
            if prd is not None:
                if story_text is None:
                    current = prd.get("current_story")
                    if isinstance(current, str) and current.strip():
                        story_text = current.strip()
                    elif isinstance(current, dict):
                        sid = current.get("id") or "?"
                        title = current.get("title") or ""
                        story_text = f"{sid}: {title}".strip(": ")
                    else:
                        stories = prd.get("stories") or []
                        if stories and isinstance(stories[0], dict):
                            s0 = stories[0]
                            story_text = (
                                f"{s0.get('id', '?')}: {s0.get('title', '')}".strip(
                                    ": "
                                )
                            )
                        else:
                            story_text = "(none — pick ONE story; update prd current_story)"

                if cmds_summary is None:
                    try:
                        cmds = collect_commands(prd)
                    except Exception:
                        cmds = []
                    if cmds:
                        shown = [" ".join(c) for c in cmds[:8]]
                        cmds_summary = "; ".join(shown)
                        if len(cmds) > 8:
                            cmds_summary += f" … (+{len(cmds) - 8} more)"
                    else:
                        cmds_summary = (
                            "(none — fill prd stories[].commands / global_commands "
                            "as argv arrays; CLI freezes before verified)"
                        )
        except Exception:
            # Context pack is best-effort; never fail prompt build
            pass

    if story_text is None:
        story_text = "(none — pick ONE story this iteration)"
    if cmds_summary is None:
        cmds_summary = (
            "(none — fill prd stories[].commands; acceptance only via omg CLI)"
        )
    if acc_path is None and run_id:
        acc_path = f".omg/state/runs/{run_id}/acceptance.result.json"
    if acc_path is None:
        acc_path = "(no run_id — path unknown)"

    total = max_iter if max_iter is not None else "?"
    iter_label = f"{iteration}/{total}" if iteration is not None else f"?/{total}"

    lines = [
        "## Ralph context pack (CLI injection — fresh each iteration)",
        f"- run_id: {run_id or '(unknown)'}",
        f"- iteration: {iter_label}",
        f"- story: {story_text}",
        f"- frozen_commands_summary: {cmds_summary}",
        f"- acceptance.result.json: {acc_path}",
        "- Do **not** forge acceptance.result.json; only `omg accept` / CLI runner stamps "
        "`writer: omg-cli`. MUST spawn implementers with capability_mode=read-write (no shell); "
        "shell/tests via omg CLI only.",
    ]
    return "\n".join(lines)


def build_prompt(
    mode: str,
    goal: str,
    *,
    iteration: int | None = None,
    max_iter: int | None = None,
    run_id: str | None = None,
    skill_root: Path | None = None,
    project_root: Path | str | None = None,
    story: str | None = None,
    frozen_commands_summary: str | None = None,
    acceptance_result_path: str | None = None,
) -> str:
    """Compose the -p prompt: skill body + HARD RULES + goal (+ ralph context pack)."""
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
            # Full context pack when iteration is set (ralph loop)
            parts.append("")
            parts.append(
                ralph_context_pack(
                    run_id=run_id,
                    iteration=iteration,
                    max_iter=max_iter,
                    project_root=project_root,
                    story=story,
                    frozen_commands_summary=frozen_commands_summary,
                    acceptance_result_path=acceptance_result_path,
                )
            )
    parts.extend(
        [
            "",
            "## Capability spawn contract (hard — host-enforced when set)",
            "- Implementers (`omg-executor`, write `general-purpose`): "
            "**MUST** spawn with `capability_mode=read-write` (edit tools; **no Execute/shell**).",
            "- Critic / verifier / explore / plan: **MUST** spawn with `capability_mode=read-only`.",
            "- Do **not** give workers `execute` or `all`. Shell/tests only via outer `omg accept`.",
            "- If PreToolUse **denies** spawn for capability_mode: **RETRY IMMEDIATELY** same turn "
            "with the mode named in the deny reason. Do **not** abandon multi-agent; do **not** "
            "fall back to solo-only just because spawn was denied once.",
            "- Children must not call `spawn_subagent` (depth=1); executor also disallows "
            "`run_terminal_command` + `spawn_subagent` in frontmatter.",
            "",
            "## Goal",
            goal.strip() or "(no goal provided)",
            "",
            "Follow the skill playbook above. Prefer spawn_subagent for parallel work.",
        ]
    )
    return "\n".join(parts)


# Built-in tools stripped when disallow_shell is active (Grok --disallowed-tools).
# Do NOT inject this for ulw/ralph leaders — they may need shell for tests via omg CLI
# coordination. Prefer critic/verifier (read-only stages) and opt-in env.
# Both tool ids used across headless vs interactive naming.
DISALLOW_SHELL_TOOLS = "run_terminal_command,run_terminal_cmd"


def _env_disallow_shell() -> bool:
    """True when OMG_DISALLOW_SHELL is set to a truthy value (1/true/yes/on)."""
    raw = (os.environ.get("OMG_DISALLOW_SHELL") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _argv_has_disallowed_tools_flag(argv: Sequence[str]) -> bool:
    """True if argv already sets --disallowed-tools / --disallowedTools / --deny."""
    for a in argv:
        if a in ("--disallowed-tools", "--disallowedTools", "--deny"):
            return True
        if isinstance(a, str) and (
            a.startswith("--disallowed-tools=")
            or a.startswith("--disallowedTools=")
            or a.startswith("--deny=")
        ):
            return True
    return False


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
    project_root: Path | str | None = None,
    prompt: str | None = None,
    story: str | None = None,
    frozen_commands_summary: str | None = None,
    acceptance_result_path: str | None = None,
    output_format: str | None = "plain",
    disallow_shell: bool = False,
    new_session_id: str | None = None,
    resume_session_id: str | None = None,
) -> list[str]:
    """Build argv for ``grok -p <prompt>``.

    Grok CLI has no bare ``--yolo`` flag. When ``yolo=True`` (and safe is
    not set) we map to ``--permission-mode bypassPermissions`` plus
    ``--always-approve``. **safe wins**: if ``safe=True``, always pass
    ``--permission-mode plan`` even when yolo is also set (read-only /
    plan permissions; matches critic/verifier agent frontmatter).

    Always passes ``--cwd`` when ``cwd`` is known. Headless default
    ``--output-format plain`` (documented Grok flag).

    ``disallow_shell`` (or env ``OMG_DISALLOW_SHELL=1``): when True and the
    flag is not already present, inject
    ``--disallowed-tools run_terminal_command``. Use for dual-review /
    ralplan critic+verifier stages only — **not** for ulw/ralph leaders
    (they may need shell; workers rely on capability_mode).
    """
    if mode not in MODE_SKILL_REL:
        raise ValueError(f"unknown mode {mode!r}")

    # Prefer project_root for context pack; fall back to cwd when path known
    root_for_pack = project_root if project_root is not None else cwd

    if prompt is None:
        prompt = build_prompt(
            mode,
            goal,
            iteration=iteration,
            max_iter=max_iter,
            run_id=run_id,
            skill_root=skill_root,
            project_root=root_for_pack,
            story=story,
            frozen_commands_summary=frozen_commands_summary,
            acceptance_result_path=acceptance_result_path,
        )

    argv: list[str] = ["grok"]

    # Always pass --cwd when path is known
    if cwd is not None:
        argv.extend(["--cwd", str(cwd)])

    if output_format:
        argv.extend(["--output-format", str(output_format)])

    # A resumable launch has exactly one continuity flag.  UUID validation is
    # centralized in host_session.py so malformed persisted state cannot reach
    # the host process.
    argv.extend(
        session_flag_argv(
            new_session_id=new_session_id,
            resume_session_id=resume_session_id,
        )
    )

    # safe wins over yolo for elevation (safer default when both present)
    if safe:
        argv.extend(["--permission-mode", "plan"])
    elif yolo:
        # Documented mapping: grok has no --yolo; use permission-mode + always-approve
        argv.extend(["--permission-mode", "bypassPermissions"])
        argv.append("--always-approve")

    # Defense-in-depth shell clamp (opt-in / stage-specific; never default for leaders)
    want_disallow = bool(disallow_shell) or _env_disallow_shell()
    if want_disallow and not _argv_has_disallowed_tools_flag(
        list(extra) if extra else []
    ):
        argv.extend(["--disallowed-tools", DISALLOW_SHELL_TOOLS])

    # Prefer --prompt-file over -p: skill bodies start with YAML ``---`` which
    # Grok CLI treats as an unexpected argument when passed as -p value.
    # Caller (_launch_grok) materializes the file next to last_prompt.md.
    # Here we only mark intent; _launch_grok rewrites to --prompt-file path.
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
        "version": 1,
        "goal": goal,
        "run_id": run_id,
        "stories": [],
        "global_commands": [],
        "current_story": None,
        "acceptance": [],
        "status": "scaffold",
        "note": "proposal only — omg CLI owns verified; fill stories[].commands",
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


def _try_set_verified(root: Path, run_id: str, *, lease: Any = None) -> bool:
    """Set verified only if CLI acceptance result exists. Never force."""
    try:
        set_verified(root, run_id, force=False, lease=lease)
        return True
    except PermissionError:
        return False
    except FileNotFoundError:
        return False


def _try_acceptance_and_verify(
    root: Path,
    run_id: str,
    *,
    dry_run: bool = False,
    timeout: float | None = None,
    lease: Any = None,
) -> bool:
    """If PRD has valid commands: freeze, run_acceptance, then set_verified.

    dry_run validates schema / freezes but does not exec commands and never
    marks verified. Without valid acceptance commands → never verified.
    """
    from omg_cli.acceptance import (
        DEFAULT_COMMAND_TIMEOUT,
        freeze_acceptance,
        load_prd,
        prd_has_acceptance_commands,
        run_acceptance,
        validate_prd,
    )

    prd = load_prd(root, run_id)
    if prd is None or not prd_has_acceptance_commands(prd):
        return False

    try:
        validate_prd(prd)
    except ValueError:
        return False

    try:
        freeze_acceptance(root, run_id, prd)
    except (ValueError, FileNotFoundError, OSError):
        # CommandPolicyError subclasses ValueError — policy rejects return False.
        return False

    if dry_run:
        # Schema OK + frozen; do not exec; cannot verify
        try:
            run_acceptance(
                root,
                run_id,
                timeout=timeout if timeout is not None else DEFAULT_COMMAND_TIMEOUT,
                dry_run=True,
            )
        except (ValueError, FileNotFoundError, OSError):
            pass
        return False

    try:
        ok = run_acceptance(
            root,
            run_id,
            timeout=timeout if timeout is not None else DEFAULT_COMMAND_TIMEOUT,
            dry_run=False,
        )
    except (ValueError, FileNotFoundError, OSError):
        return False

    if not ok:
        return False
    return _try_set_verified(root, run_id, lease=lease)


def _materialize_prompt_file(argv: list[str], run_dir: Path) -> list[str]:
    """Rewrite ``-p <prompt>`` to ``--prompt-file <path>`` when present.

    Skill bodies begin with YAML ``---`` frontmatter; Grok CLI rejects that as
    an unexpected argument when embedded after ``-p``. Writing the prompt to a
    file and using ``--prompt-file`` avoids the parse error.
    """
    out = list(argv)
    try:
        p_idx = out.index("-p")
    except ValueError:
        return out
    if p_idx + 1 >= len(out):
        return out
    prompt_text = out[p_idx + 1]
    run_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = run_dir / "last_prompt.md"
    prompt_path.write_text(prompt_text, encoding="utf-8")
    # Replace -p PROMPT with --prompt-file PATH (single path arg, no --- issues)
    out[p_idx : p_idx + 2] = ["--prompt-file", str(prompt_path)]
    return out


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
    # Convert -p skill bodies to --prompt-file before exec / record
    argv = _materialize_prompt_file(list(argv), run_dir)
    (run_dir / "last_argv.json").write_text(
        json.dumps(argv, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # Ensure last_prompt.md exists even if argv already used --prompt-file
    if not (run_dir / "last_prompt.md").is_file():
        try:
            if "--prompt-file" in argv:
                pf = argv[argv.index("--prompt-file") + 1]
                (run_dir / "last_prompt.md").write_text(
                    Path(pf).read_text(encoding="utf-8"), encoding="utf-8"
                )
        except (ValueError, OSError, IndexError):
            pass

    if dry_run:
        (run_dir / "dry_run").write_text("1\n", encoding="utf-8")
        return 0

    # Prefer Popen so we can record child PID. OSError (e.g. FileNotFoundError
    # when grok is missing) must not leave status stuck at "running".
    # start_new_session=True on POSIX makes the child a session leader so
    # cancel_run can killpg the whole process group.
    from omg_cli.evidence import safe_supervised_child_env

    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd),
        "env": safe_supervised_child_env(os.environ),
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except OSError as exc:
        (run_dir / "launch_error").write_text(f"{exc}\n", encoding="utf-8")
        return 127

    # Record pid + starttime + pgid so cancel can refuse PID-reused kills.
    try:
        from omg_cli.state import write_pid_metadata

        pgid: int | None = proc.pid
        if os.name == "posix":
            try:
                pgid = os.getpgid(proc.pid)
            except (ProcessLookupError, PermissionError, OSError):
                pgid = proc.pid
        write_pid_metadata(
            run_dir / "pid.json",
            pid=proc.pid,
            pgid=pgid,
        )
    except Exception:
        # Never fail the launch because metadata write failed; keep legacy pid.
        (run_dir / "pid").write_text(f"{proc.pid}\n", encoding="utf-8")
    try:
        return int(proc.wait(timeout=timeout))
    except subprocess.TimeoutExpired:
        # Prefer killing the process group when we started a new session
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
    timeout: float | None = None,
    extra: Sequence[str] | None = None,
    require_acceptance: bool | None = None,
    acceptance_timeout: float | None = None,
    existing_run_id: str | None = None,
    resume_run_id: str | None = None,
    lease_timeout: float = 5.0,
    force: bool = False,
) -> int:
    """Create run, launch grok for mode, update status. Returns exit code.

    - ulw: typically one launch (max_iter default 1)
    - ralph: loop up to max_iter (default 3); one story per iteration
    - ralplan: delegates to ``omg_cli.ralplan.run_ralplan`` FSM
      (draft → critic → revise → verifier; max_rounds default 3)
    - Never sets verified without CLI-stamped acceptance.result.json
    - dry_run: build argv / scaffolds, skip grok + acceptance exec (schema ok)
    - require_acceptance: default True for ralph; when True and not verified → non-zero
    - timeout: seconds for each grok launch; None → DEFAULT_TIMEOUT (3600);
      0 → unlimited. Configurable via CLI ``--timeout``.
    - existing_run_id: reuse run (pipeline embedding); skips create_run
    - resume_run_id: Ralph CLI process-level resume (``__active__`` resolves
      the active run); frozen goal/config and cumulative ceiling are enforced
    """
    if mode not in MODE_SKILL_REL:
        print(f"unknown mode: {mode}", file=sys.stderr)
        return 2
    if resume_run_id is not None and mode != "ralph":
        print("--resume is only supported for ralph", file=sys.stderr)
        return 2
    if resume_run_id is not None and existing_run_id is not None:
        print("cannot combine resume_run_id with existing_run_id", file=sys.stderr)
        return 2

    root_path = Path(root) if root is not None else Path.cwd().resolve()
    requested_goal = (goal or "").strip()

    # RALPLAN is owned by the CLI FSM (artifacts + max rounds), not the
    # generic single/loop launcher below.
    if mode == "ralplan":
        goal = requested_goal or "(no goal)"
        if max_iter is None:
            max_iter = DEFAULT_MAX_ITER["ralplan"]
        max_iter = max(1, int(max_iter))
        launch_timeout = resolve_launch_timeout(timeout, dry_run=dry_run)
        from omg_cli.ralplan import run_ralplan

        return run_ralplan(
            goal,
            root=root_path,
            max_rounds=max_iter,
            yolo=yolo,
            safe=safe,
            dry_run=dry_run,
            timeout=launch_timeout,
            extra=extra,
            force=force,
            existing_run_id=existing_run_id,
        )

    explicit_resume = resume_run_id is not None
    run: dict[str, Any] | None = None

    if explicit_resume:
        if resume_run_id == "__active__":
            run = load_active_run(root_path)
            if run is None:
                print("omg ralph: no active run to resume", file=sys.stderr)
                return 1
            resume_run_id = str(run["run_id"])
        else:
            run = load_run(root_path, str(resume_run_id))
        if run is None:
            print(f"omg ralph: no run found: {resume_run_id!r}", file=sys.stderr)
            return 1
        try:
            schema = classify_run_schema(run)
        except (TypeError, ValueError) as exc:
            print(f"omg ralph: refusing malformed run schema: {exc}", file=sys.stderr)
            return 1
        if str(run.get("mode") or "") != "ralph":
            print(
                f"omg ralph: run {resume_run_id!r} belongs to "
                f"mode={run.get('mode')!r}",
                file=sys.stderr,
            )
            return 1
        frozen_goal = str(run.get("goal") or "").strip()
        if requested_goal and requested_goal != frozen_goal:
            print(
                "omg ralph: conflicting goal on resume; omit goal text or use "
                "the frozen goal exactly",
                file=sys.stderr,
            )
            return 2
        goal = frozen_goal
        run_id = str(run["run_id"])
        stored_max = int(run.get("max_iter") or DEFAULT_MAX_ITER["ralph"])
        completed = int(run.get("iterations_completed") or 0)
        if max_iter is None:
            max_iter = stored_max
        max_iter = int(max_iter)
        if max_iter < completed or max_iter < stored_max:
            print(
                f"omg ralph: --max-iter is a cumulative ceiling; requested "
                f"{max_iter}, stored={stored_max}, completed={completed}",
                file=sys.stderr,
            )
            return 2
        yolo = bool(run.get("yolo", False))
        safe = bool(run.get("safe", False))
        if require_acceptance is None:
            require_acceptance = bool(run.get("require_acceptance", True))
        stored_timeout = run.get("timeout")
        if timeout is None and isinstance(stored_timeout, (int, float)):
            timeout = float(stored_timeout)
    else:
        goal = requested_goal or "(no goal)"
        if max_iter is None:
            max_iter = DEFAULT_MAX_ITER.get(mode, 1)
        max_iter = max(1, int(max_iter))
        if require_acceptance is None:
            require_acceptance = mode == "ralph"
        schema = RunSchema.LEGACY_V1

    launch_timeout = resolve_launch_timeout(timeout, dry_run=dry_run)
    create_extra: dict[str, Any] = {
        "max_iter": int(max_iter),
        "yolo": bool(yolo),
        "safe": bool(safe),
        "require_acceptance": bool(require_acceptance),
        "timeout": timeout,
    }
    # New standalone Ralph runs use the strict lifecycle kernel.  Existing v1
    # pipeline runs remain v1 and are never rewritten in place.
    if mode == "ralph" and not explicit_resume and existing_run_id is None:
        create_extra.update({"schema_version": 2, "lifecycle_version": 2})
    # ULW convergence: record leader base_sha when git is available so
    # integrate_results can reject envelopes built on a different base.
    # Optional metadata: never fail run creation if git is unavailable or
    # tests have monkeypatched subprocess for grok isolation.
    if mode == "ulw":
        try:
            from omg_cli.integrate import git_rev_parse_head

            base_sha = git_rev_parse_head(root_path)
            if base_sha:
                create_extra["base_sha"] = base_sha
        except Exception:
            pass

    if explicit_resume:
        assert run is not None
    elif existing_run_id:
        run_id = existing_run_id
        run = load_run(root_path, run_id)
        if run is None:
            print(
                f"omg {mode}: no run found for existing_run_id={run_id!r}",
                file=sys.stderr,
            )
            return 1
        try:
            schema = classify_run_schema(run)
        except (TypeError, ValueError) as exc:
            print(f"omg {mode}: refusing malformed run schema: {exc}", file=sys.stderr)
            return 1
    else:
        try:
            run = create_run(
                root_path,
                mode=mode,
                goal=goal,
                extra=create_extra,
                force=force,
            )
        except RuntimeError as exc:
            # Active-run mutex: refuse concurrent non-terminal runs
            print(f"omg {mode}: {exc}", file=sys.stderr)
            return 1
        run_id = run["run_id"]
        schema = classify_run_schema(run)
    run_dir = _run_dir(root_path, run_id)

    if mode == "ralph":
        # Only scaffold if missing (pipeline may re-enter implement)
        prd_path = run_dir / "prd.json"
        if not prd_path.is_file():
            _write_prd_scaffold(root_path, run_id, goal)

    strict = schema is RunSchema.STRICT_V2
    lease_cm = (
        execution_lease(
            root_path,
            run_id,
            intent="ralph-resume" if explicit_resume else f"{mode}-run",
            timeout_s=float(lease_timeout),
        )
        if strict
        else nullcontext(None)
    )

    try:
        with lease_cm as lease:
            # A committed cancellation request preempts all ordinary resume and
            # host launch.  cancel_run uses transition only and finalizes it.
            if strict and load_cancellation_request(root_path, run_id) is not None:
                cancelled = cancel_run(
                    root_path,
                    run_id,
                    kill_grace_s=0.0,
                    lease=lease,
                )
                print(
                    f"omg {mode}: cancellation {cancelled.get('cancel_outcome', 'cancelled')}",
                    file=sys.stderr,
                )
                return 1

            def status_write(status: str, *, extra_fields: dict[str, Any]) -> dict[str, Any]:
                return write_status(
                    root_path,
                    run_id,
                    status,
                    extra=extra_fields,
                    lease=lease,
                )

            current = load_run(root_path, run_id) or {}
            if explicit_resume and current.get("status") in ("verified", "cancelled"):
                print(
                    f"omg ralph: run is already {current.get('status')}",
                    file=sys.stderr,
                )
                return 0 if current.get("status") == "verified" else 1

            if explicit_resume and int(max_iter) > int(current.get("max_iter") or 0):
                status_write(
                    "running",
                    extra_fields={"max_iter": int(max_iter), "next_action": None},
                )
                current = load_run(root_path, run_id) or current

            binding = None
            if mode == "ralph":
                try:
                    binding = load_host_session(current, required=explicit_resume)
                except HostSessionError as exc:
                    status_write(
                        "blocked" if strict else "failed",
                        extra_fields={
                            "blocker": {
                                "code": "missing_or_invalid_session_binding",
                                "message": str(exc),
                            },
                            "next_action": (
                                f"inspect .omg/state/runs/{run_id}/status.json; "
                                "do not start a replacement session implicitly"
                            ),
                        },
                    )
                    print(f"omg ralph: {exc}", file=sys.stderr)
                    return 1
                if binding is None:
                    binding = allocate_host_session()
                    status_write(
                        "running",
                        extra_fields={
                            **binding.status_fields(),
                            "iteration": int(current.get("iteration") or 0),
                            "iterations_completed": int(
                                current.get("iterations_completed") or 0
                            ),
                        },
                    )
                    current = load_run(root_path, run_id) or current

            completed = int(current.get("iterations_completed") or 0)
            start_iteration = completed + 1
            if mode == "ralph" and start_iteration > int(max_iter):
                next_ceiling = start_iteration
                status_write(
                    "blocked" if strict else "completed",
                    extra_fields={
                        "blocker": {
                            "code": "iteration_ceiling_reached",
                            "message": (
                                f"completed={completed}, cumulative ceiling={max_iter}"
                            ),
                        },
                        "next_action": (
                            f"omg ralph --resume {run_id} --max-iter {next_ceiling}"
                        ),
                    },
                )
                print(
                    f"omg ralph: cumulative ceiling reached; resume with "
                    f"--max-iter {next_ceiling}",
                    file=sys.stderr,
                )
                return 1

            status_write(
                "running",
                extra_fields={
                    "iteration": completed,
                    "iterations_completed": completed,
                    "max_iter": int(max_iter),
                    "blocker": None,
                    "next_action": f"omg cancel --run {run_id}",
                },
            )

            last_rc = 0
            verified = False
            iterations = (
                range(start_iteration, int(max_iter) + 1)
                if mode == "ralph"
                else range(1, int(max_iter) + 1)
            )
            for i in iterations:
                new_session_id: str | None = None
                resume_session_id: str | None = None
                if binding is not None:
                    if binding.is_first_launch:
                        new_session_id = binding.session_id
                    else:
                        resume_session_id = binding.session_id
                    # Persist attempted state before Popen.  A crash or rc!=0
                    # therefore resumes this UUID; it can never allocate anew.
                    binding = binding.attempted()

                status_write(
                    "running",
                    extra_fields={
                        "iteration": i,
                        "passes": completed,
                        **(binding.status_fields() if binding is not None else {}),
                    },
                )
                argv = build_grok_argv(
                    mode=mode,
                    goal=goal,
                    yolo=yolo,
                    cwd=root_path,
                    safe=safe,
                    extra=extra,
                    iteration=i if mode == "ralph" else None,
                    max_iter=int(max_iter) if mode == "ralph" else None,
                    run_id=run_id,
                    skill_root=plugin_root(),
                    project_root=root_path,
                    new_session_id=new_session_id,
                    resume_session_id=resume_session_id,
                )
                last_rc = _launch_grok(
                    argv,
                    cwd=root_path,
                    run_dir=run_dir,
                    timeout=launch_timeout,
                    dry_run=dry_run,
                )

                if last_rc != 0 and not dry_run:
                    if binding is not None:
                        binding = type(binding)(
                            binding.session_id, binding.attempts, "blocked"
                        )
                    retry = f"omg ralph --resume {run_id} --max-iter {max_iter}"
                    status_write(
                        "blocked" if strict else "failed",
                        extra_fields={
                            "exit_code": last_rc,
                            "passes": completed,
                            **(binding.status_fields() if binding is not None else {}),
                            "blocker": {
                                "code": "grok_resume_failed",
                                "session_id": (
                                    binding.session_id if binding is not None else None
                                ),
                                "rc": last_rc,
                            },
                            "next_action": retry,
                        },
                    )
                    print(
                        f"omg {mode}: Grok launch/resume failed rc={last_rc}; "
                        f"session preserved; retry: {retry}",
                        file=sys.stderr,
                    )
                    return last_rc

                completed = i
                if binding is not None:
                    binding = type(binding)(
                        binding.session_id, binding.attempts, "resumable"
                    )
                status_write(
                    "running",
                    extra_fields={
                        "iteration": i,
                        "iterations_completed": completed,
                        "passes": completed,
                        **(binding.status_fields() if binding is not None else {}),
                    },
                )

                if _try_acceptance_and_verify(
                    root_path,
                    run_id,
                    dry_run=dry_run,
                    timeout=acceptance_timeout,
                    lease=lease,
                ):
                    verified = True
                    break
                if _try_set_verified(root_path, run_id, lease=lease):
                    verified = True
                    break
                if mode != "ralph":
                    break

            current = load_run(root_path, run_id) or {}
            if verified or current.get("verified") is True:
                if mode == "ulw" and not dry_run:
                    int_rc = _ulw_auto_integrate(root_path, run_id)
                    if int_rc != 0:
                        return int_rc
                return 0

            if mode == "ulw" and not dry_run:
                int_rc = _ulw_auto_integrate(root_path, run_id)
                if int_rc != 0:
                    return int_rc

            if strict:
                next_ceiling = int(max_iter) + 1
                status_write(
                    "blocked",
                    extra_fields={
                        "exit_code": 0,
                        "note": "iteration ceiling reached without CLI acceptance",
                        "require_acceptance": bool(require_acceptance),
                        "blocker": {
                            "code": "not_verified",
                            "message": "CLI acceptance has not verified this run",
                        },
                        "next_action": (
                            f"omg ralph --resume {run_id} --max-iter {next_ceiling}"
                        ),
                    },
                )
            else:
                status_write(
                    "completed",
                    extra_fields={
                        "exit_code": 0,
                        "note": "completed without CLI acceptance; verified remains false",
                        "require_acceptance": bool(require_acceptance),
                    },
                )
            if require_acceptance:
                print(
                    f"omg {mode}: not verified (require_acceptance); "
                    "fill prd stories/commands and re-run or use `omg accept`",
                    file=sys.stderr,
                )
                return 1
            return 0
    except (LifecycleLockError, HostSessionError, PermissionError) as exc:
        print(f"omg {mode}: {exc}", file=sys.stderr)
        return 1


def _ulw_auto_integrate(root: Path, run_id: str) -> int:
    """After ULW launch: integrate envelopes if present.

    - status missing → OK (solo smoke / no envelopes yet); print next step
    - status ok → OK
    - status failed → non-zero (dirty envelopes must not silently complete)
    """
    try:
        from omg_cli.integrate import IntegrateError, integrate_results
    except Exception as exc:  # pragma: no cover
        print(f"omg ulw: integrate import failed: {exc}", file=sys.stderr)
        return 1
    try:
        result = integrate_results(root, run_id)
    except FileNotFoundError as exc:
        print(f"omg ulw: integrate skipped: {exc}", file=sys.stderr)
        return 0
    except IntegrateError as exc:
        print(f"omg ulw: integrate failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"omg ulw: integrate error: {exc}", file=sys.stderr)
        return 1

    status = (result or {}).get("status") or "unknown"
    if status == "missing":
        print(
            f"omg ulw: no ULW envelopes under "
            f".omg/artifacts/ulw-results/{run_id}/ "
            "(workers should seal with `omg worker seal` then re-run "
            "`omg integrate` if needed)",
            file=sys.stderr,
        )
        return 0
    if status == "ok":
        print(f"omg ulw: integrated envelopes for run {run_id}", file=sys.stderr)
        return 0
    # failed / other
    err = (result or {}).get("error") or (result or {}).get("note") or status
    print(f"omg ulw: integrate status={status!r}: {err}", file=sys.stderr)
    return 1
