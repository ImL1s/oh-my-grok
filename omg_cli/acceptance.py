# omg_cli/acceptance.py
"""Frozen acceptance runner + PRD schema validation.

Only this module (via the omg CLI) may write ``acceptance.result.json`` with
``writer: "omg-cli"``. ``set_verified`` requires that stamp + matching manifest
sha **and** a process-local token registered by ``run_acceptance`` — a full
disk forge (writer + passed + correct sha) without the token is rejected.

Acceptance commands are filtered by the semantic policy in
``omg_cli.command_policy`` (executable family + argv grammar). External agent
CLIs, shells, ``python -c``, and ``npx`` are denied. ``--no-allowlist`` is a
TTY-only break-glass that still applies the always-deny floor.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Iterable

from omg_cli.command_policy import (
    ALWAYS_DENY_BASENAMES,
    DEFAULT_ALLOWLIST,
    POLICY_VERSION,
    SHELL_BASENAMES,
    CommandPolicyError,
    check_command_policy,
    check_commands_policy,
    coalesce_pytest_marker_expr,
    command_basename,
    is_python_bin,
    resolve_allowlist,
    _basename_allowed,
)

# Back-compat name used by tests / callers.
CommandAllowlistError = CommandPolicyError
check_command_allowlist = check_command_policy
check_commands_allowlist = check_commands_policy

CLI_WRITER = "omg-cli"
MANIFEST_NAME = "acceptance.manifest.json"
SHA_NAME = "acceptance.sha256"
RESULT_NAME = "acceptance.result.json"
PRD_NAME = "prd.json"

# Env keys stripped from child processes so models cannot inject allow-lists
# or hijack allowed runners (PYTHONSTARTUP, GIT_DIR, LD_PRELOAD, …).
_STRIP_ENV_KEYS = frozenset(
    {
        "OMG_ALLOW_EXTERNAL_CLI",
        "PYTHONSTARTUP",
        "PYTHONPATH",
        "PERL5OPT",
        "RUBYOPT",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "NODE_OPTIONS",
        "NODE_PATH",
    }
)
# Prefixes: any key matching is stripped (in addition to OMG_ALLOW_*).
_STRIP_ENV_PREFIXES = ("npm_config_",)

DEFAULT_COMMAND_TIMEOUT: float | None = 300.0

# Re-export policy sets for callers that imported from acceptance.
__all__ = [
    "ALWAYS_DENY_BASENAMES",
    "CLI_WRITER",
    "CommandAllowlistError",
    "CommandPolicyError",
    "DEFAULT_ALLOWLIST",
    "DEFAULT_COMMAND_TIMEOUT",
    "POLICY_VERSION",
    "SHELL_BASENAMES",
    "check_command_allowlist",
    "check_command_policy",
    "check_commands_allowlist",
    "check_commands_policy",
    "clear_cli_acceptance_tokens",
    "collect_commands",
    "command_basename",
    "compute_manifest_sha256",
    "format_commands_review",
    "freeze_acceptance",
    "freeze_and_run",
    "has_cli_acceptance_token",
    "is_cli_acceptance_result",
    "is_python_bin",
    "is_trusted_acceptance",
    "load_frozen_commands",
    "build_prd_from_ultraqa",
    "load_prd",
    "manifest_path",
    "materialize_prd_from_ultraqa",
    "prd_has_acceptance_commands",
    "prd_path",
    "read_manifest_sha256",
    "register_cli_acceptance_token",
    "resolve_allowlist",
    "result_path",
    "run_acceptance",
    "run_dir",
    "sanitized_env",
    "sha_path",
    "sha256_hex",
    "validate_prd",
    "_basename_allowed",
]


# Process-local trust: only ``run_acceptance`` may register tokens after it
# writes acceptance.result.json. Agent-forged disk stamps (even with writer /
# passed / correct manifest sha) lack a token and cannot set_verified.
# Key: (root.resolve(), run_id, manifest_sha256)
_CLI_ACCEPTANCE_TOKENS: set[tuple[str, str, str]] = set()


def _token_key(root: Path | str, run_id: str, manifest_sha: str) -> tuple[str, str, str]:
    return (str(Path(root).resolve()), str(run_id), str(manifest_sha))


def register_cli_acceptance_token(
    root: Path | str, run_id: str, manifest_sha: str
) -> None:
    """Record that this process wrote a CLI acceptance result (internal / tests)."""
    if not manifest_sha:
        return
    _CLI_ACCEPTANCE_TOKENS.add(_token_key(root, run_id, manifest_sha))


def clear_cli_acceptance_tokens() -> None:
    """Clear process-local tokens (tests only)."""
    _CLI_ACCEPTANCE_TOKENS.clear()


def has_cli_acceptance_token(
    root: Path | str,
    run_id: str,
    manifest_sha: str | None = None,
) -> bool:
    """True if this process registered a token for root/run_id (and optional sha)."""
    root_s = str(Path(root).resolve())
    rid = str(run_id)
    if manifest_sha is not None:
        return (root_s, rid, str(manifest_sha)) in _CLI_ACCEPTANCE_TOKENS
    return any(t[0] == root_s and t[1] == rid for t in _CLI_ACCEPTANCE_TOKENS)


def _runs_dir(root: Path) -> Path:
    return Path(root) / ".omg" / "state" / "runs"


def run_dir(root: Path, run_id: str) -> Path:
    rid = (run_id or "").strip()
    if (
        not rid
        or rid in {".", ".."}
        or "/" in rid
        or "\\" in rid
        or ".." in rid
    ):
        raise ValueError(f"invalid run_id {run_id!r}")
    return _runs_dir(root) / rid


def manifest_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / MANIFEST_NAME


def sha_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / SHA_NAME


def result_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / RESULT_NAME


def prd_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / PRD_NAME


def build_prd_from_ultraqa(
    root: Path | str,
    run_id: str,
    *,
    goal: str | None = None,
) -> dict[str, Any]:
    """Build a PRD dict from CLI-stamped clean UltraQA scenarios.

    Scenarios with a ``command`` become stories (argv after coalesce + policy).
    ``always_pass``-only scenarios (hermetic test freeze) map to ``[["true"]]``.
    Raises ``ValueError`` when ultraqa is missing, not clean, or has no
    runnable scenarios after policy/coalesce.
    """
    import shlex

    from omg_cli.command_policy import (
        coalesce_pytest_marker_expr,
        check_command_policy,
    )
    from omg_cli.qa import load_qa

    root = Path(root).resolve()
    rid = str(run_id)
    try:
        qa = load_qa(root, rid)
    except Exception as exc:  # QAError / missing
        raise ValueError(f"cannot load ultraqa for prd: {exc}") from exc
    if qa.get("writer") != CLI_WRITER:
        raise ValueError("ultraqa lacks CLI writer; refuse prd materialize")
    if qa.get("clean") is not True or qa.get("status") != "clean":
        raise ValueError(
            "ultraqa must be clean (status=clean) before materializing prd "
            f"(got status={qa.get('status')!r} clean={qa.get('clean')!r})"
        )
    if qa.get("invalidated") is True:
        raise ValueError("ultraqa stamp invalidated; re-run QA before prd")

    stories: list[dict[str, Any]] = []
    for sc in qa.get("scenarios") or []:
        if not isinstance(sc, dict):
            continue
        sid = str(sc.get("id") or "").strip()
        if not sid:
            continue
        if sc.get("check") == "always_pass" and not sc.get("command"):
            # Hermetic-only always_pass: map to true so accept still has work
            stories.append(
                {
                    "id": sid,
                    "title": f"ultraqa:{sid}",
                    "commands": [["true"]],
                }
            )
            continue
        cmd = sc.get("command")
        if not cmd:
            continue
        if isinstance(cmd, list):
            argv = [str(x) for x in cmd]
        else:
            try:
                argv = shlex.split(str(cmd).strip())
            except ValueError as exc:
                raise ValueError(
                    f"ultraqa scenario {sid!r}: bad command: {exc}"
                ) from exc
        argv = coalesce_pytest_marker_expr(argv)
        if not argv:
            continue
        try:
            check_command_policy(argv, project_root=root)
        except CommandPolicyError as exc:
            raise ValueError(
                f"ultraqa scenario {sid!r} command not accept-safe: {exc}"
            ) from exc
        stories.append(
            {
                "id": sid,
                "title": f"ultraqa:{sid}",
                "commands": [argv],
            }
        )

    if not stories:
        raise ValueError(
            "ultraqa clean but has no materializable scenarios "
            "(need command or always_pass)"
        )

    goal_text = (goal or "").strip() or f"acceptance from ultraqa for {rid}"
    return validate_prd(
        {
            "version": 1,
            "goal": goal_text,
            "stories": stories,
            "global_commands": [],
            "run_id": rid,
            "note": "materialized_from_ultraqa",
        }
    )


def materialize_prd_from_ultraqa(
    root: Path | str,
    run_id: str,
    *,
    goal: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write ``prd.json`` from clean ultraqa when missing (or overwrite=True).

    Returns the validated PRD. Does not run acceptance.
    """
    root = Path(root).resolve()
    path = prd_path(root, run_id)
    if path.is_file() and not overwrite:
        existing = load_prd(root, run_id)
        if existing is not None:
            return existing
        # Corrupt file — fall through to rewrite
    prd = build_prd_from_ultraqa(root, run_id, goal=goal)
    _atomic_write_text(
        path,
        json.dumps(prd, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
    )
    return prd


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write_text(path, text)


def _canonical_json_bytes(data: dict[str, Any]) -> bytes:
    """Stable encoding for sha256 (sort_keys, no trailing ambiguity)."""
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True).encode(
        "utf-8"
    ) + b"\n"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_argv_command(cmd: Any, *, where: str) -> list[str]:
    if not isinstance(cmd, list) or not cmd:
        raise ValueError(f"{where}: command must be a non-empty argv array, got {cmd!r}")
    out: list[str] = []
    for i, part in enumerate(cmd):
        if not isinstance(part, str) or part == "":
            raise ValueError(
                f"{where}: argv[{i}] must be a non-empty string, got {part!r}"
            )
        if any(ch in part for ch in ("\n", "\r", "\0")):
            raise ValueError(f"{where}: argv[{i}] contains illegal control chars")
        out.append(part)
    return out


def _validate_command_list(cmds: Any, *, where: str) -> list[list[str]]:
    if cmds is None:
        return []
    if not isinstance(cmds, list):
        raise ValueError(f"{where}: must be a list of argv arrays, got {type(cmds).__name__}")
    return [
        _validate_argv_command(c, where=f"{where}[{i}]") for i, c in enumerate(cmds)
    ]


def collect_commands(prd: dict[str, Any]) -> list[list[str]]:
    """Flatten story + global commands (already validated or raw).

    Applies ``coalesce_pytest_marker_expr`` so unquoted ``-m not live`` matches
    freeze and run digests.
    """
    commands: list[list[str]] = []
    for story in prd.get("stories") or []:
        if isinstance(story, dict):
            for cmd in story.get("commands") or []:
                if isinstance(cmd, list):
                    commands.append(
                        coalesce_pytest_marker_expr([str(x) for x in cmd])
                    )
    for cmd in prd.get("global_commands") or []:
        if isinstance(cmd, list):
            commands.append(
                coalesce_pytest_marker_expr([str(x) for x in cmd])
            )
    return commands


def validate_prd(data: Any) -> dict[str, Any]:
    """Validate PRD / acceptance schema. Returns a normalized copy.

    Raises ``ValueError`` on bad schema or when there are no runnable commands
    (empty stories without commands / empty global_commands).
    """
    if not isinstance(data, dict):
        raise ValueError(f"prd must be an object, got {type(data).__name__}")

    if "version" not in data:
        raise ValueError("prd.version is required")
    version = data["version"]
    if version != 1 and version != "1":
        raise ValueError(f"prd.version must be 1, got {version!r}")

    goal = data.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("prd.goal must be a non-empty string")

    stories_raw = data.get("stories")
    if stories_raw is None:
        raise ValueError("prd.stories is required (list; may be empty if global_commands set)")
    if not isinstance(stories_raw, list):
        raise ValueError(f"prd.stories must be a list, got {type(stories_raw).__name__}")

    stories: list[dict[str, Any]] = []
    for i, story in enumerate(stories_raw):
        where = f"prd.stories[{i}]"
        if not isinstance(story, dict):
            raise ValueError(f"{where}: must be an object")
        sid = story.get("id")
        if not isinstance(sid, str) or not sid.strip():
            raise ValueError(f"{where}.id must be a non-empty string")
        title = story.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"{where}.title must be a non-empty string")
        if "commands" not in story:
            raise ValueError(f"{where}.commands is required (list of argv arrays)")
        commands = _validate_command_list(story["commands"], where=f"{where}.commands")
        stories.append({"id": sid.strip(), "title": title.strip(), "commands": commands})

    global_commands = _validate_command_list(
        data.get("global_commands"), where="prd.global_commands"
    )

    total = sum(len(s["commands"]) for s in stories) + len(global_commands)
    if total == 0:
        raise ValueError(
            "prd has no acceptance commands: stories empty/without commands "
            "and global_commands missing/empty"
        )

    normalized: dict[str, Any] = {
        "version": 1,
        "goal": goal.strip(),
        "stories": stories,
        "global_commands": global_commands,
    }
    # Preserve optional non-conflicting metadata keys (read-only extras)
    for key in ("run_id", "current_story", "note", "status"):
        if key in data and key not in normalized:
            normalized[key] = data[key]
    return normalized


def prd_has_acceptance_commands(data: Any) -> bool:
    """True if data looks like a PRD with at least one argv command (no raise)."""
    try:
        validate_prd(data)
        return True
    except (ValueError, TypeError):
        return False


def load_prd(root: Path, run_id: str) -> dict[str, Any] | None:
    return _read_json(prd_path(root, run_id))


def freeze_acceptance(
    root: Path,
    run_id: str,
    prd: dict[str, Any] | None = None,
    *,
    allowlist: Iterable[str] | None = None,
    extra_allow: Iterable[str] | None = None,
    no_allowlist: bool = False,
    skip_policy: bool = False,
) -> dict[str, Any]:
    """Validate PRD, apply command policy, write frozen manifest + sha256.

    Returns the manifest dict (includes ``commands`` flat list + ``sha256``).
    Policy failures raise ``CommandPolicyError`` so bad PRDs never freeze.
    """
    root = Path(root)
    if prd is None:
        prd = load_prd(root, run_id)
    if prd is None:
        raise FileNotFoundError(f"no prd.json for run_id={run_id!r}")

    normalized = validate_prd(prd)
    # Coalesce marker expr into stories so frozen manifest matches exec argv.
    for story in normalized.get("stories") or []:
        if isinstance(story, dict) and isinstance(story.get("commands"), list):
            story["commands"] = [
                coalesce_pytest_marker_expr([str(x) for x in cmd])
                if isinstance(cmd, list)
                else cmd
                for cmd in story["commands"]
            ]
    if isinstance(normalized.get("global_commands"), list):
        normalized["global_commands"] = [
            coalesce_pytest_marker_expr([str(x) for x in cmd])
            if isinstance(cmd, list)
            else cmd
            for cmd in normalized["global_commands"]
        ]
    commands = collect_commands(normalized)

    effective_allow = resolve_allowlist(extra_allow, base=allowlist)
    extra_list = sorted({command_basename(str(x)) for x in (extra_allow or []) if str(x).strip()})

    if not skip_policy:
        check_commands_policy(
            commands,
            allowlist=effective_allow,
            no_allowlist=no_allowlist,
            project_root=root,
        )

    # Hashed body includes policy identity so overrides change the digest.
    manifest: dict[str, Any] = {
        "version": 1,
        "run_id": run_id,
        "goal": normalized["goal"],
        "stories": normalized["stories"],
        "global_commands": normalized["global_commands"],
        "commands": commands,
        "policy_version": POLICY_VERSION,
        "allow_cmd": extra_list,
        "no_allowlist": bool(no_allowlist),
    }
    body = _canonical_json_bytes(manifest)
    digest = sha256_hex(body)
    manifest["sha256"] = digest

    mpath = manifest_path(root, run_id)
    # Write without embedding sha in the hashed body — store sha alongside.
    _atomic_write_text(mpath, body.decode("utf-8"))
    _atomic_write_text(sha_path(root, run_id), digest + "\n")
    return manifest


def read_manifest_sha256(root: Path, run_id: str) -> str | None:
    """Return expected sha of frozen manifest (from file or recompute)."""
    sp = sha_path(root, run_id)
    if sp.is_file():
        text = sp.read_text(encoding="utf-8").strip()
        if text:
            return text
    mp = manifest_path(root, run_id)
    if mp.is_file():
        return sha256_hex(mp.read_bytes())
    return None


def compute_manifest_sha256(root: Path, run_id: str) -> str | None:
    mp = manifest_path(root, run_id)
    if not mp.is_file():
        return None
    return sha256_hex(mp.read_bytes())


def sanitized_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """Copy env with lifecycle allows and known runner-hijack keys stripped.

    Still inherits PATH/HOME/VIRTUAL_ENV so venv pytest works. Not an OS
    sandbox — see docs/security-model.md. Opt-out: OMG_ACCEPT_KEEP_PYTHONPATH=1
    re-adds PYTHONPATH after scrub (operator weaken).
    """
    keep_pp = False
    raw = base if base is not None else os.environ
    if str(raw.get("OMG_ACCEPT_KEEP_PYTHONPATH", "")).strip() == "1":
        keep_pp = True
        saved_pp = raw.get("PYTHONPATH")
    env = dict(raw)
    for key in list(env.keys()):
        if (
            key in _STRIP_ENV_KEYS
            or key.startswith("OMG_ALLOW_")
            or any(key.startswith(p) for p in _STRIP_ENV_PREFIXES)
        ):
            env.pop(key, None)
    if keep_pp and saved_pp:
        env["PYTHONPATH"] = saved_pp
    return env


def load_frozen_commands(root: Path, run_id: str) -> list[list[str]]:
    """Load normalized argv lists from the frozen acceptance manifest."""
    root = Path(root)
    mpath = manifest_path(root, run_id)
    if not mpath.is_file():
        raise FileNotFoundError(
            f"no frozen acceptance manifest for run_id={run_id!r}; "
            "call freeze_acceptance first"
        )
    manifest = _read_json(mpath)
    if not manifest:
        raise ValueError(f"invalid acceptance manifest at {mpath}")
    commands = manifest.get("commands")
    if not isinstance(commands, list) or not commands:
        commands = collect_commands(manifest)
    if not commands:
        raise ValueError("frozen manifest has no commands")
    argv_list: list[list[str]] = []
    for i, cmd in enumerate(commands):
        argv_list.append(_validate_argv_command(cmd, where=f"manifest.commands[{i}]"))
    return argv_list


def format_commands_review(
    commands: list[list[str]],
    *,
    root: Path | str | None = None,
    run_id: str | None = None,
    manifest_sha: str | None = None,
) -> str:
    """Human-readable listing for ``--review`` (sha, cwd, numbered shlex)."""
    lines: list[str] = []
    if run_id is not None:
        lines.append(f"run_id: {run_id}")
    if root is not None:
        lines.append(f"cwd: {Path(root).resolve()}")
    if manifest_sha:
        lines.append(f"manifest_sha256: {manifest_sha}")
    lines.append(f"policy_version: {POLICY_VERSION}")
    lines.append(f"acceptance commands ({len(commands)}):")
    for i, cmd in enumerate(commands):
        lines.append(f"  [{i}] {shlex.join(list(cmd))}")
    return "\n".join(lines)


def run_acceptance(
    root: Path,
    run_id: str,
    *,
    timeout: float | None = DEFAULT_COMMAND_TIMEOUT,
    dry_run: bool = False,
    allowlist: Iterable[str] | None = None,
    extra_allow: Iterable[str] | None = None,
    no_allowlist: bool = False,
) -> bool:
    """Execute frozen manifest commands; write acceptance.result.json.

    Returns True iff all commands exit 0. Always stamps ``writer: omg-cli``.
    Does not set verified — caller must invoke ``set_verified``.

    Commands are always checked against the semantic policy (floors apply even
    when ``no_allowlist=True``).
    """
    root = Path(root)
    mpath = manifest_path(root, run_id)
    if not mpath.is_file():
        raise FileNotFoundError(
            f"no frozen acceptance manifest for run_id={run_id!r}; "
            "call freeze_acceptance first"
        )

    manifest = _read_json(mpath)
    if not manifest:
        raise ValueError(f"invalid acceptance manifest at {mpath}")

    expected_sha = read_manifest_sha256(root, run_id)
    actual_sha = sha256_hex(mpath.read_bytes())
    if expected_sha and expected_sha != actual_sha:
        raise ValueError(
            f"manifest sha mismatch: file={actual_sha} recorded={expected_sha}"
        )
    manifest_sha = actual_sha

    argv_list = load_frozen_commands(root, run_id)

    effective_allow = resolve_allowlist(extra_allow, base=allowlist)
    # Validate policy before any exec (also on dry_run so review fails closed).
    check_commands_policy(
        argv_list,
        allowlist=effective_allow,
        no_allowlist=no_allowlist,
        project_root=root,
    )

    results: list[dict[str, Any]] = []
    all_ok = True
    env = sanitized_env()

    if dry_run:
        for cmd in argv_list:
            results.append(
                {
                    "command": cmd,
                    "returncode": None,
                    "skipped": True,
                    "reason": "dry_run",
                }
            )
        all_ok = False  # dry_run never passes verification gate
        payload = {
            "writer": CLI_WRITER,
            "passed": False,
            "manifest_sha256": manifest_sha,
            "dry_run": True,
            "results": results,
        }
        _atomic_write_json(result_path(root, run_id), payload)
        # Still register: proves CLI wrote the file; passed=false blocks verify.
        register_cli_acceptance_token(root, run_id, manifest_sha)
        return False

    for cmd in argv_list:
        entry: dict[str, Any] = {
            "command": cmd,
            "returncode": -1,
            "stdout_tail": "",
            "stderr_tail": "",
        }
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(root),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            entry["returncode"] = int(proc.returncode)
            entry["stdout_tail"] = (proc.stdout or "")[-4000:]
            entry["stderr_tail"] = (proc.stderr or "")[-4000:]
        except subprocess.TimeoutExpired as exc:
            entry["returncode"] = 124
            entry["error"] = "timeout"
            out = exc.stdout if isinstance(exc.stdout, str) else (
                exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
            )
            err = exc.stderr if isinstance(exc.stderr, str) else (
                exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
            )
            entry["stdout_tail"] = (out or "")[-4000:]
            entry["stderr_tail"] = (err or "")[-4000:]
        except OSError as exc:
            entry["returncode"] = 127
            entry["error"] = str(exc)

        if entry["returncode"] != 0:
            all_ok = False
        results.append(entry)

    payload = {
        "writer": CLI_WRITER,
        "passed": all_ok,
        "manifest_sha256": manifest_sha,
        "results": results,
    }
    _atomic_write_json(result_path(root, run_id), payload)
    register_cli_acceptance_token(root, run_id, manifest_sha)
    return all_ok


def is_cli_acceptance_result(
    path_or_data: Path | str | dict[str, Any] | None,
    *,
    root: Path | None = None,
    run_id: str | None = None,
    require_token: bool = True,
) -> bool:
    """True if result is CLI-stamped, passed, matches frozen manifest sha.

    Accepts a path, a loaded dict, or (with root+run_id) reads the standard
    result path. When a frozen manifest exists, ``manifest_sha256`` must match.

    When ``require_token`` is True (default) and root/run_id can be resolved,
    also requires a process-local token from ``run_acceptance`` so full disk
    forgeries (writer + passed + correct sha) cannot pass.
    """
    data: dict[str, Any] | None
    if path_or_data is None:
        if root is None or run_id is None:
            return False
        data = _read_json(result_path(Path(root), run_id))
    elif isinstance(path_or_data, dict):
        data = path_or_data
    else:
        data = _read_json(Path(path_or_data))

    if not data:
        return False
    if data.get("writer") != CLI_WRITER:
        return False
    if data.get("passed") is not True:
        return False

    result_sha = data.get("manifest_sha256")
    if not isinstance(result_sha, str) or not result_sha:
        return False

    # If root/run_id known, require match against frozen manifest
    check_root = root
    check_id = run_id
    if check_root is None and not isinstance(path_or_data, dict):
        # Infer run dir: .../runs/<id>/acceptance.result.json
        p = Path(path_or_data)
        if p.name == RESULT_NAME and p.parent.parent.name == "runs":
            check_id = p.parent.name
            # .../.omg/state/runs/<id>/file → root is parents[3]
            try:
                check_root = p.parents[3]
            except IndexError:
                check_root = None

    if check_root is not None and check_id is not None:
        expected = compute_manifest_sha256(Path(check_root), check_id)
        if expected is None:
            # No frozen manifest on disk — cannot trust
            return False
        if result_sha != expected:
            return False
        recorded = read_manifest_sha256(Path(check_root), check_id)
        if recorded is not None and result_sha != recorded:
            return False
        if require_token and not has_cli_acceptance_token(
            check_root, check_id, result_sha
        ):
            return False
    elif require_token:
        # Cannot bind disk fields to a process token without root/run_id.
        # Pure dict checks without location are not trusted for set_verified.
        return False

    return True


def is_trusted_acceptance(root: Path | str, run_id: str) -> bool:
    """True only when CLI acceptance result is on disk *and* token is in-process.

    Used by ``set_verified``. Empty PRD / no runnable commands never produce a
    passing trusted result (freeze/run refuse empty command lists).
    """
    root = Path(root)
    return is_cli_acceptance_result(
        None, root=root, run_id=run_id, require_token=True
    )


def freeze_and_run(
    root: Path,
    run_id: str,
    prd: dict[str, Any] | None = None,
    *,
    timeout: float | None = DEFAULT_COMMAND_TIMEOUT,
    dry_run: bool = False,
    allowlist: Iterable[str] | None = None,
    extra_allow: Iterable[str] | None = None,
    no_allowlist: bool = False,
) -> bool:
    """Convenience: freeze (if prd available) then run_acceptance."""
    root = Path(root)
    if prd is None:
        prd = load_prd(root, run_id)
    if prd is None and not manifest_path(root, run_id).is_file():
        raise FileNotFoundError(f"no prd or frozen manifest for run_id={run_id!r}")
    if prd is not None:
        freeze_acceptance(
            root,
            run_id,
            prd,
            allowlist=allowlist,
            extra_allow=extra_allow,
            no_allowlist=no_allowlist,
        )
    elif not manifest_path(root, run_id).is_file():
        raise FileNotFoundError(f"no frozen manifest for run_id={run_id!r}")
    return run_acceptance(
        root,
        run_id,
        timeout=timeout,
        dry_run=dry_run,
        allowlist=allowlist,
        extra_allow=extra_allow,
        no_allowlist=no_allowlist,
    )
