# omg_cli/acceptance.py
"""Frozen acceptance runner + PRD schema validation.

Only this module (via the omg CLI) may write ``acceptance.result.json`` with
``writer: "omg-cli"``. ``set_verified`` requires that stamp + matching manifest
sha **and** a process-local token registered by ``run_acceptance`` — a full
disk forge (writer + passed + correct sha) without the token is rejected.

Acceptance commands are filtered by a **basename allowlist** (default safe test
runners / language tools). External agent CLIs and destructive bins are denied.
``--no-allowlist`` is an emergency escape hatch and must not be used by models.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable


CLI_WRITER = "omg-cli"
MANIFEST_NAME = "acceptance.manifest.json"
SHA_NAME = "acceptance.sha256"
RESULT_NAME = "acceptance.result.json"
PRD_NAME = "prd.json"

# Env keys stripped from child processes so models cannot inject allow-lists.
_STRIP_ENV_KEYS = frozenset(
    {
        "OMG_ALLOW_EXTERNAL_CLI",
    }
)

DEFAULT_COMMAND_TIMEOUT: float | None = 300.0

# Default basename allowlist for acceptance argv[0] (after Path.name / basename).
DEFAULT_ALLOWLIST: frozenset[str] = frozenset(
    {
        "pytest",
        "python",
        "python3",
        "true",
        "false",
        "make",
        "npm",
        "npx",
        "node",
        "cargo",
        "go",
        "dart",
        "flutter",
        "ruff",
        "mypy",
        "black",
        "git",
    }
)

# Always denied even when listed via --allow-cmd (security floor).
ALWAYS_DENY_BASENAMES: frozenset[str] = frozenset(
    {
        "claude",
        "codex",
        "omx",
        "agy",
        "cursor-agent",
        "kimi",
        "rm",
        "sudo",
        "doas",
    }
)

# Shell interpreters: never allowed as acceptance argv[0] (curl|sh, -c escapes).
SHELL_BASENAMES: frozenset[str] = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "dash",
        "csh",
        "tcsh",
        "fish",
        "ksh",
    }
)


class CommandAllowlistError(ValueError):
    """Raised when an acceptance command is rejected by the allowlist / denylist."""

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
    return _runs_dir(root) / run_id


def manifest_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / MANIFEST_NAME


def sha_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / SHA_NAME


def result_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / RESULT_NAME


def prd_path(root: Path, run_id: str) -> Path:
    return run_dir(root, run_id) / PRD_NAME


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


def command_basename(argv0: str) -> str:
    """Return the executable basename for allowlist checks (handles paths)."""
    name = Path(str(argv0)).name
    # Windows-style trailing .exe is uncommon here; strip for robustness.
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return name


def resolve_allowlist(
    extra: Iterable[str] | None = None,
    *,
    base: Iterable[str] | None = None,
) -> frozenset[str]:
    """Default allowlist plus optional ``--allow-cmd`` extensions."""
    allowed = set(DEFAULT_ALLOWLIST if base is None else base)
    if extra:
        for name in extra:
            n = command_basename(str(name).strip())
            if n:
                allowed.add(n)
    return frozenset(allowed)


# Exact: python | python2 | python3 | python2.N | python3.N
# Rejects python3evil, python3-config, python3foo, etc.
_PYTHON_BIN_RE = re.compile(r"^python([23](\.\d+)?)?$")


def _basename_allowed(base: str, allowed: frozenset[str]) -> bool:
    """True if *base* is in *allowed* or a versioned python binary family match.

    Versioned form is only ``python``, ``python2``, ``python3``, or
    ``python2.N`` / ``python3.N`` (e.g. python3.12). Prefix tricks like
    ``python3evil`` are rejected.
    """
    if base in allowed:
        return True
    if not _PYTHON_BIN_RE.match(base):
        return False
    # Family membership: versioned bins map to exact allowlist names.
    if base == "python":
        return "python" in allowed
    if base.startswith("python3"):
        return "python3" in allowed or "python" in allowed
    if base.startswith("python2"):
        return "python2" in allowed or "python" in allowed
    return False


def check_command_allowlist(
    cmd: list[str],
    *,
    allowlist: Iterable[str] | None = None,
    no_allowlist: bool = False,
    where: str = "command",
) -> None:
    """Raise ``CommandAllowlistError`` if *cmd* is not permitted for acceptance.

    Policy (in order):
    1. Shell interpreters as argv[0] → always deny (blocks ``bash -c``, ``curl|sh``).
    2. Always-deny basenames (``claude``, ``rm``, …) → always deny, even with
       ``--allow-cmd`` / ``--no-allowlist`` (``--no-allowlist`` still cannot run
       agent CLIs or ``rm``; it only skips the *positive* allowlist).
    3. Unless ``no_allowlist``, argv[0] basename must be in the allowlist
       (``python3.N`` matches when ``python3`` is allowed).
    """
    if not cmd:
        raise CommandAllowlistError(f"{where}: empty command")
    base = command_basename(cmd[0])
    if not base:
        raise CommandAllowlistError(f"{where}: empty argv[0] basename")

    if base in SHELL_BASENAMES:
        raise CommandAllowlistError(
            f"{where}: shell interpreter {base!r} is not allowed as acceptance "
            "command (use direct argv like pytest/python, not bash -c)"
        )

    # Always-deny floor: agent CLIs + destructive bins. --no-allowlist does NOT
    # lift these (emergency only extends past the positive allowlist).
    if base in ALWAYS_DENY_BASENAMES:
        raise CommandAllowlistError(
            f"{where}: basename {base!r} is permanently denied for acceptance"
        )

    if no_allowlist:
        return

    allowed = (
        frozenset(allowlist)
        if allowlist is not None
        else DEFAULT_ALLOWLIST
    )
    if not _basename_allowed(base, allowed):
        raise CommandAllowlistError(
            f"{where}: basename {base!r} not in acceptance allowlist "
            f"({', '.join(sorted(allowed))}); use --allow-cmd {base} or "
            "--no-allowlist (dangerous)"
        )


def check_commands_allowlist(
    commands: list[list[str]],
    *,
    allowlist: Iterable[str] | None = None,
    no_allowlist: bool = False,
) -> None:
    """Validate every command in a frozen list; raise on first rejection."""
    for i, cmd in enumerate(commands):
        check_command_allowlist(
            cmd,
            allowlist=allowlist,
            no_allowlist=no_allowlist,
            where=f"manifest.commands[{i}]",
        )


def _validate_argv_command(cmd: Any, *, where: str) -> list[str]:
    if not isinstance(cmd, list) or not cmd:
        raise ValueError(f"{where}: command must be a non-empty argv array, got {cmd!r}")
    out: list[str] = []
    for i, part in enumerate(cmd):
        if not isinstance(part, str) or part == "":
            raise ValueError(
                f"{where}: argv[{i}] must be a non-empty string, got {part!r}"
            )
        # Reject bare shell strings that look like multi-token one-liners
        # when the whole command is a single string with spaces — already
        # handled by requiring list; still reject nested lists.
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
    """Flatten story + global commands (already validated or raw)."""
    commands: list[list[str]] = []
    for story in prd.get("stories") or []:
        if isinstance(story, dict):
            for cmd in story.get("commands") or []:
                if isinstance(cmd, list):
                    commands.append([str(x) for x in cmd])
    for cmd in prd.get("global_commands") or []:
        if isinstance(cmd, list):
            commands.append([str(x) for x in cmd])
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
) -> dict[str, Any]:
    """Validate PRD, write frozen manifest + sha256 under the run dir.

    Returns the manifest dict (includes ``commands`` flat list + ``sha256``).
    """
    root = Path(root)
    if prd is None:
        prd = load_prd(root, run_id)
    if prd is None:
        raise FileNotFoundError(f"no prd.json for run_id={run_id!r}")

    normalized = validate_prd(prd)
    commands = collect_commands(normalized)
    manifest: dict[str, Any] = {
        "version": 1,
        "run_id": run_id,
        "goal": normalized["goal"],
        "stories": normalized["stories"],
        "global_commands": normalized["global_commands"],
        "commands": commands,
    }
    body = _canonical_json_bytes(manifest)
    digest = sha256_hex(body)
    manifest["sha256"] = digest

    mpath = manifest_path(root, run_id)
    # Write without embedding sha in the hashed body — store sha alongside.
    # Manifest file content = canonical body (no sha256 field) for stable hash.
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
    """Copy env with OMG_ALLOW_EXTERNAL_CLI and related inject keys stripped."""
    env = dict(base if base is not None else os.environ)
    for key in list(env.keys()):
        if key in _STRIP_ENV_KEYS or key.startswith("OMG_ALLOW_"):
            env.pop(key, None)
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


def format_commands_review(commands: list[list[str]]) -> str:
    """Human-readable listing of acceptance commands for ``--review``."""
    lines = [f"acceptance commands ({len(commands)}):"]
    for i, cmd in enumerate(commands):
        lines.append(f"  [{i}] {' '.join(cmd)}")
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

    Commands are checked against the acceptance allowlist unless
    ``no_allowlist=True`` (emergency only; still cannot run always-deny bins
    or shell interpreters).
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
    # Validate allowlist before any exec (also on dry_run so review fails closed).
    check_commands_allowlist(
        argv_list,
        allowlist=effective_allow,
        no_allowlist=no_allowlist,
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
        freeze_acceptance(root, run_id, prd)
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
