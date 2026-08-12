"""Pane supervisor: spawn provider child and prove readiness (#99).

Consumes a vetted JSON descriptor (argv list + delivery metadata). Never
reconstructs shell command strings from untrusted text. Owns process-level
readiness so read-only workers do not need mailbox ACK.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    ContractPathError,
    atomic_write_bytes,
    ensure_managed_dir,
    safe_path_key,
)
from omg_cli.contracts.state_schemas import require_safe_id
from omg_cli.evidence import CLI_WRITER
from omg_cli.team.provider_ready import get_readiness_strategy
from omg_cli.team.startup import (
    EvidenceCode,
    StartupError,
    StartupPhase,
    append_startup_diagnostics,
    write_startup_phase,
)

DESCRIPTOR_SCHEMA_VERSION = 1
DESCRIPTOR_KIND = "team_provider_descriptor"
# CLI-published launch intent for pane supervisors before team.json exists.
# Lives under the existing team/ tree (not a parallel truth store).
PREPUBLISH_SCHEMA_VERSION = 1
PREPUBLISH_KIND = "team_supervisor_prepublish"
PREPUBLISH_DIRNAME = "supervisor-authority"
DEFAULT_READY_WAIT_S = 30.0
# After provisional ready (process_stable or weak TUI idle), keep watching
# for delayed auth/trust before finalizing provider_ready.
DEFAULT_POST_STABLE_OBSERVE_S = 2.0
MIN_POST_STABLE_OBSERVE_S = 0.5
# TUI idle glyphs are weaker than binary identity+stability; prefer a longer
# floor so delayed auth after ``>`` / ``grok>`` is still in-window (#99 B2).
MIN_TUI_IDLE_POST_STABLE_S = 2.0
POST_STABLE_OBSERVE_ENV = "OMG_TEAM_POST_STABLE_OBSERVE_S"
_CAPTURE_MAX_LINES = 48
_CAPTURE_MAX_BYTES = 16_384

# Default cmdline/exe basenames that may prove process_stable for a provider.
_PROVIDER_IDENTITY_DEFAULTS: dict[str, tuple[str, ...]] = {
    "grok": ("grok",),
    "codex": ("codex",),
    "agy": ("agy",),
    "antigravity": ("agy", "antigravity"),
    "cursor": ("cursor-agent", "cursor"),
    "gemini": ("gemini",),
}


def _resolve_post_stable_observe_s(env: Mapping[str, str]) -> float:
    """Positive floor for post-stable observe; ``<=0`` / nan / inf / junk → default."""
    raw = env.get(POST_STABLE_OBSERVE_ENV)
    if raw is None or str(raw).strip() == "":
        return DEFAULT_POST_STABLE_OBSERVE_S
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_POST_STABLE_OBSERVE_S
    if not math.isfinite(value) or value <= 0:
        return DEFAULT_POST_STABLE_OBSERVE_S
    return max(value, MIN_POST_STABLE_OBSERVE_S)


class SupervisorError(RuntimeError):
    """Supervisor identity / descriptor / spawn failure."""

    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = int(exit_code)


def admit_pane_supervisor(
    descriptor_path: Path | str | None,
    *,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str, str, Path]:
    """Validate a leader-authorized pane supervisor before any side effects.

    Legal Team panes intentionally run with ``OMG_TEAM_WORKER=1`` and execute
    ``omg team supervisor --descriptor …``. That worker marker must **not** be
    treated as nested-team launch refusal. This admission path instead requires:

    1. A present, schema-valid provider descriptor (read-only load).
    2. Run / team / worker identity plus a validated canonical leader root
       (``bootstrap_env_identity`` / ``validate_canonical_leader_root``).
    3. Durable CLI authority:
       - When authoritative ``team.json`` exists: bind ``team_id`` and
         (when published) ``owner_token``.
       - When ``team.json`` is absent: require a CLI-prepublished supervisor
         authority record binding root/run/team/worker, owner token, and the
         exact descriptor path + content digest. Metadata absence never
         authorizes execution.

    Raises:
        SupervisorError / BootstrapError: fail-closed before side effects.
    """
    from omg_cli.team.bootstrap import (
        TEAM_OWNER_TOKEN_ENV,
        bootstrap_env_identity,
    )

    if not descriptor_path:
        raise SupervisorError(
            "omg team supervisor: --descriptor PATH required",
            exit_code=2,
        )
    # Identity + leader root first (no writes).
    run_id, team_id, worker_id, leader = bootstrap_env_identity(env)
    # Descriptor schema only (read); never spawns the provider.
    load_provider_descriptor(descriptor_path)
    source = env if env is not None else os.environ
    _validate_supervisor_team_binding(
        leader,
        run_id=run_id,
        team_id=team_id,
        worker_id=worker_id,
        descriptor_path=Path(descriptor_path),
        env=source,
        owner_token_env=TEAM_OWNER_TOKEN_ENV,
    )
    return run_id, team_id, worker_id, leader


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def supervisor_prepublish_dir(root: Path | str, run_id: str) -> Path:
    """Directory for per-worker prepublish authority under the team tree."""
    from omg_cli.team.plane import team_dir

    rid = require_safe_id(run_id, label="run_id")
    return team_dir(root, rid) / PREPUBLISH_DIRNAME


def supervisor_prepublish_path(
    root: Path | str, run_id: str, worker_id: str
) -> Path:
    """Path for one worker's prepublish authority (hashed worker key)."""
    wid = require_safe_id(worker_id, label="worker_id")
    key = safe_path_key(wid, namespace="worker")
    return supervisor_prepublish_dir(root, run_id) / f"{key}.json"


def descriptor_content_digest(path: Path | str) -> str:
    """SHA-256 of descriptor file bytes (fail-closed on unreadable)."""
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise SupervisorError(
            f"provider descriptor unreadable for digest: {exc}",
            exit_code=2,
        ) from exc
    return hashlib.sha256(raw).hexdigest()


def publish_supervisor_authority(
    *,
    leader_root: Path | str,
    run_id: str,
    team_id: str,
    worker_id: str,
    owner_token: str,
    descriptor_path: Path | str,
    generation: int = 0,
    attempt: int = 1,
) -> Path:
    """Atomically publish CLI-owned supervisor prepublish authority.

    Must run **after** the provider descriptor is written and **before** pane
    / provider spawn. Uses managed-file path protections under the existing
    team directory (no parallel truth tree).
    """
    leader = Path(leader_root).resolve()
    rid = require_safe_id(run_id, label="run_id")
    tid = require_safe_id(team_id, label="team_id")
    wid = require_safe_id(worker_id, label="worker_id")
    token = str(owner_token or "").strip()
    if not token:
        raise SupervisorError(
            "supervisor prepublish requires non-empty owner_token",
            exit_code=2,
        )
    desc = Path(descriptor_path).resolve()
    if not desc.is_file():
        raise SupervisorError(
            "supervisor prepublish requires an existing descriptor file",
            exit_code=2,
        )
    digest = descriptor_content_digest(desc)
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
    ):
        raise SupervisorError(
            "supervisor prepublish generation must be a non-negative int",
            exit_code=2,
        )
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise SupervisorError(
            "supervisor prepublish attempt must be an int >= 1",
            exit_code=2,
        )
    target = supervisor_prepublish_path(leader, rid, wid)
    try:
        ensure_managed_dir(target.parent)
    except ContractPathError as exc:
        raise SupervisorError(
            f"supervisor prepublish dir refused: {exc}",
            exit_code=2,
        ) from exc
    payload: dict[str, Any] = {
        "schema_version": PREPUBLISH_SCHEMA_VERSION,
        "kind": PREPUBLISH_KIND,
        "writer": CLI_WRITER,
        "run_id": rid,
        "team_id": tid,
        "worker_id": wid,
        "leader_root": str(leader),
        "owner_token": token,
        "descriptor_path": str(desc),
        "descriptor_sha256": digest,
        "generation": int(generation),
        "attempt": int(attempt),
        "published_at": _utc_now_iso(),
    }
    body = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        atomic_write_bytes(target, body, mode=DATA_FILE_MODE, replace=True)
    except ContractPathError as exc:
        raise SupervisorError(
            f"supervisor prepublish write refused: {exc}",
            exit_code=2,
        ) from exc
    return target


def clear_supervisor_prepublish_authorities(
    root: Path | str, run_id: str
) -> None:
    """Best-effort remove prepublish records after authoritative team.json."""
    try:
        directory = supervisor_prepublish_dir(root, run_id)
    except (SupervisorError, ValueError, TypeError):
        return
    try:
        if not directory.is_dir() or directory.is_symlink():
            return
    except OSError:
        return
    try:
        for child in directory.iterdir():
            try:
                if child.is_symlink():
                    continue
                if child.is_file():
                    child.unlink(missing_ok=True)
            except OSError:
                continue
        try:
            directory.rmdir()
        except OSError:
            pass
    except OSError:
        return


def _load_prepublish_authority(
    leader: Path,
    *,
    run_id: str,
    worker_id: str,
) -> dict[str, Any]:
    path = supervisor_prepublish_path(leader, run_id, worker_id)
    try:
        if not path.is_file() or path.is_symlink():
            raise SupervisorError(
                "supervisor prepublish authority missing "
                "(team.json absent; forged env+descriptor cannot authorize)",
                exit_code=2,
            )
    except OSError as exc:
        raise SupervisorError(
            "supervisor prepublish authority not usable",
            exit_code=2,
        ) from exc
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupervisorError(
            "supervisor prepublish authority unreadable",
            exit_code=2,
        ) from exc
    if not isinstance(data, dict):
        raise SupervisorError(
            "supervisor prepublish authority must be a JSON object",
            exit_code=2,
        )
    if data.get("schema_version") != PREPUBLISH_SCHEMA_VERSION:
        raise SupervisorError(
            "supervisor prepublish schema_version mismatch",
            exit_code=2,
        )
    if data.get("kind") != PREPUBLISH_KIND:
        raise SupervisorError(
            "supervisor prepublish kind mismatch",
            exit_code=2,
        )
    if data.get("writer") != CLI_WRITER:
        raise SupervisorError(
            "supervisor prepublish lacks CLI writer authority",
            exit_code=2,
        )
    return data


def _validate_prepublish_authority(
    leader: Path,
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
    descriptor_path: Path,
    env: Mapping[str, str],
    owner_token_env: str,
) -> None:
    """Fail-closed when team.json is absent: require matching prepublish."""
    data = _load_prepublish_authority(leader, run_id=run_id, worker_id=worker_id)
    if str(data.get("run_id") or "").strip() != run_id:
        raise SupervisorError(
            "supervisor prepublish run_id mismatch "
            "(stale or forged pane authority)",
            exit_code=2,
        )
    if str(data.get("team_id") or "").strip() != team_id:
        raise SupervisorError(
            "supervisor prepublish team_id mismatch "
            "(stale or forged pane authority)",
            exit_code=2,
        )
    if str(data.get("worker_id") or "").strip() != worker_id:
        raise SupervisorError(
            "supervisor prepublish worker_id mismatch "
            "(stale or forged pane authority)",
            exit_code=2,
        )
    auth_root = str(data.get("leader_root") or "").strip()
    try:
        leader_resolved = str(leader.resolve())
    except OSError as exc:
        raise SupervisorError(
            "supervisor leader root not resolvable",
            exit_code=2,
        ) from exc
    if not auth_root or auth_root != leader_resolved:
        raise SupervisorError(
            "supervisor prepublish leader_root mismatch "
            "(stale or forged pane authority)",
            exit_code=2,
        )
    meta_token = str(data.get("owner_token") or "").strip()
    env_token = (env.get(owner_token_env) or "").strip()
    if not meta_token or not env_token or env_token != meta_token:
        raise SupervisorError(
            "supervisor prepublish owner_token mismatch or missing "
            "(stale or forged pane authority)",
            exit_code=2,
        )
    try:
        desc_resolved = str(Path(descriptor_path).resolve())
    except OSError as exc:
        raise SupervisorError(
            "supervisor descriptor path not resolvable",
            exit_code=2,
        ) from exc
    auth_desc = str(data.get("descriptor_path") or "").strip()
    if not auth_desc or auth_desc != desc_resolved:
        raise SupervisorError(
            "supervisor prepublish descriptor_path mismatch "
            "(stale or forged pane authority)",
            exit_code=2,
        )
    expected_digest = str(data.get("descriptor_sha256") or "").strip().lower()
    if not expected_digest or len(expected_digest) != 64:
        raise SupervisorError(
            "supervisor prepublish descriptor_sha256 missing or malformed",
            exit_code=2,
        )
    actual = descriptor_content_digest(desc_resolved)
    if actual != expected_digest:
        raise SupervisorError(
            "supervisor prepublish descriptor digest mismatch "
            "(tampered or stale descriptor)",
            exit_code=2,
        )


def _validate_supervisor_team_binding(
    leader: Path,
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
    descriptor_path: Path,
    env: Mapping[str, str],
    owner_token_env: str,
) -> None:
    """Require team.json binding or fail-closed prepublish authority."""
    # Lazy import avoids plane↔supervisor import cycles at module load.
    from omg_cli.team.plane import team_meta_path

    path = team_meta_path(leader, run_id)
    try:
        meta_present = path.is_file() and not path.is_symlink()
    except OSError as exc:
        raise SupervisorError(
            "supervisor team meta is not usable",
            exit_code=2,
        ) from exc
    if not meta_present:
        # Metadata absence never authorizes — require CLI prepublish.
        _validate_prepublish_authority(
            leader,
            run_id=run_id,
            team_id=team_id,
            worker_id=worker_id,
            descriptor_path=descriptor_path,
            env=env,
            owner_token_env=owner_token_env,
        )
        return
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupervisorError(
            "supervisor team meta unreadable",
            exit_code=2,
        ) from exc
    if not isinstance(data, dict):
        raise SupervisorError(
            "supervisor team meta must be a JSON object",
            exit_code=2,
        )
    meta_run = str(data.get("run_id") or "").strip()
    if meta_run and meta_run != run_id:
        raise SupervisorError(
            "supervisor run_id does not match team meta "
            "(stale or forged pane authority)",
            exit_code=2,
        )
    meta_team = str(data.get("team_id") or "team").strip() or "team"
    if meta_team != team_id:
        raise SupervisorError(
            "supervisor team_id does not match team meta "
            "(stale or forged pane authority)",
            exit_code=2,
        )
    meta_token = str(data.get("owner_token") or "").strip()
    if not meta_token:
        # team.json present but token unpublished: still require prepublish
        # so forged env alone cannot authorize mid-transition.
        _validate_prepublish_authority(
            leader,
            run_id=run_id,
            team_id=team_id,
            worker_id=worker_id,
            descriptor_path=descriptor_path,
            env=env,
            owner_token_env=owner_token_env,
        )
        return
    env_token = (env.get(owner_token_env) or "").strip()
    if not env_token or env_token != meta_token:
        raise SupervisorError(
            "supervisor owner_token mismatch or missing "
            "(stale or forged pane authority)",
            exit_code=2,
        )


def write_provider_descriptor(
    path: Path | str,
    *,
    provider: str,
    argv: Sequence[str],
    prompt_delivery: str = "prompt-file",
    prompt_file: Path | str | None = None,
    needs_pty: bool = False,
    cwd: Path | str | None = None,
    identity_basenames: Sequence[str] | None = None,
    provider_strategy: str | None = None,
    startup_strategy: str | None = None,
) -> Path:
    """Write a schema-versioned provider argv descriptor (atomic).

    Optional ``identity_basenames`` / ``provider_strategy`` / ``startup_strategy``
    are additive (#67-D). Schema stays at v1 so in-flight Team resume keeps
    loading prior descriptors (no launch-receipt semantic change).
    """
    target = Path(path)
    ensure_managed_dir(target.parent)
    argv_list = [str(x) for x in argv]
    if not argv_list:
        raise SupervisorError("provider descriptor requires non-empty argv")
    payload: dict[str, Any] = {
        "schema_version": DESCRIPTOR_SCHEMA_VERSION,
        "kind": DESCRIPTOR_KIND,
        "provider": str(provider or "unknown"),
        "argv": argv_list,
        "prompt_delivery": str(prompt_delivery or "prompt-file"),
        "needs_pty": bool(needs_pty),
    }
    if prompt_file is not None:
        payload["prompt_file"] = str(prompt_file)
    if cwd is not None:
        payload["cwd"] = str(cwd)
    if identity_basenames is not None:
        names = [str(x).strip() for x in identity_basenames if str(x).strip()]
        if names:
            payload["identity_basenames"] = names
    if provider_strategy:
        payload["provider_strategy"] = str(provider_strategy)
    if startup_strategy:
        payload["startup_strategy"] = str(startup_strategy)
    body = (
        json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    atomic_write_bytes(target, body, mode=DATA_FILE_MODE)
    return target


def load_provider_descriptor(path: Path | str) -> dict[str, Any]:
    target = Path(path)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise SupervisorError(f"provider descriptor unreadable: {exc}") from exc
    if not isinstance(data, dict):
        raise SupervisorError("provider descriptor must be a JSON object")
    if data.get("schema_version") != DESCRIPTOR_SCHEMA_VERSION:
        raise SupervisorError("provider descriptor schema_version mismatch")
    if data.get("kind") != DESCRIPTOR_KIND:
        raise SupervisorError("provider descriptor kind mismatch")
    argv = data.get("argv")
    if not isinstance(argv, list) or not argv or not all(
        isinstance(x, str) and x for x in argv
    ):
        raise SupervisorError("provider descriptor argv must be non-empty string list")
    return data


def expected_identity_basenames(
    provider: str,
    argv: Sequence[str],
    *,
    descriptor: Mapping[str, Any] | None = None,
) -> set[str]:
    """Basenames that may prove process_stable / provisional finalize (#99 B1)."""
    raw = None if descriptor is None else descriptor.get("identity_basenames")
    if isinstance(raw, list):
        names = {str(x).strip() for x in raw if str(x).strip()}
        if names:
            return names
    key = str(provider or "").strip().lower()
    names = set(_PROVIDER_IDENTITY_DEFAULTS.get(key, ()))
    if argv:
        # Accept argv[0] basename only when it already looks like the provider
        # binary (never auto-allow python/sh from a mislabeled argv).
        base = Path(str(argv[0])).name
        if base and base.lower() in {n.lower() for n in names}:
            names.add(base)
    return names


def _cmdline_tokens(pid: int) -> list[str]:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return []
    proc_cmd = Path(f"/proc/{pid}/cmdline")
    try:
        raw = proc_cmd.read_bytes()
    except OSError:
        raw = b""
    if raw:
        return [t.decode("utf-8", errors="replace") for t in raw.split(b"\0") if t]
    try:
        result = subprocess.run(
            ["ps", "-o", "args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return (result.stdout or "").split()


def _exe_basename(pid: int) -> str | None:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        return Path(os.readlink(f"/proc/{pid}/exe")).name
    except OSError:
        pass
    try:
        result = subprocess.run(
            ["ps", "-o", "comm=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return Path(value).name if value else None


# Interpreters that may wrap a real provider script (npm/shebang CLIs).
_INTERPRETER_BASENAMES = frozenset(
    {
        "node",
        "nodejs",
        "python",
        "python3",
        "ruby",
        "perl",
        "bash",
        "sh",
        "zsh",
        "deno",
        "bun",
    }
)
_PYTHON_VER_RE = re.compile(r"^python3?(?:\.\d+)*$")


def _is_interpreter_basename(name: str) -> bool:
    n = str(name or "").strip().lower()
    if not n:
        return False
    if n in _INTERPRETER_BASENAMES:
        return True
    # python3.11 / python3.14 / python2.7-style versioned binaries
    return bool(_PYTHON_VER_RE.match(n))


def _token_looks_like_option(token: str) -> bool:
    return str(token).startswith("-")


def provider_binary_identity_matches(
    pid: int, expected_basenames: set[str] | Sequence[str]
) -> bool:
    """True when provider identity matches expected basenames fail-closed.

    Accepts any of:
      1. exe basename ∈ expected
      2. argv[0] basename ∈ expected
      3. interpreter form: argv[0] is a known interpreter and argv[1]
         basename ∈ expected (production node/python shebang CLIs)
      4. ``env`` form: ``env <script>`` or ``env <interpreter> <script>``
         (script basename ∈ expected); options after ``env`` are skipped
         conservatively

    argv[2+] (or argv[3+] under ``env <interpreter>``) is never scanned for
    the expected name — ``python -c 'sleep(30)' grok`` stays closed (#99).
    Descriptor ``identity_basenames`` still supplies the expected set.
    """
    expected = {
        str(x).strip().lower()
        for x in expected_basenames
        if str(x).strip()
    }
    if not expected or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    exe = _exe_basename(pid)
    if exe and Path(exe).name.lower() in expected:
        return True
    tokens = _cmdline_tokens(pid)
    if not tokens:
        return False
    argv0 = Path(tokens[0]).name.lower()
    if argv0 in expected:
        return True

    def _script_basename_matches(token: str) -> bool:
        if _token_looks_like_option(token):
            return False
        return Path(token).name.lower() in expected

    # node|python|… /path/to/grok
    if _is_interpreter_basename(argv0) and len(tokens) >= 2:
        if _script_basename_matches(tokens[1]):
            return True

    # /usr/bin/env grok  OR  /usr/bin/env node /path/to/grok
    # Refuse ``env`` with flags (fail closed) to avoid option-skipping holes.
    if argv0 == "env" and len(tokens) >= 2:
        if _token_looks_like_option(tokens[1]):
            return False
        if _script_basename_matches(tokens[1]):
            return True
        if (
            _is_interpreter_basename(Path(tokens[1]).name)
            and len(tokens) >= 3
            and _script_basename_matches(tokens[2])
        ):
            return True
    return False


def _child_pids_of(parent_pid: int) -> list[int]:
    """Direct children of *parent_pid* (fail-open → empty)."""
    if (
        isinstance(parent_pid, bool)
        or not isinstance(parent_pid, int)
        or parent_pid <= 0
    ):
        return []
    out: list[int] = []
    # Linux: scan /proc
    proc_root = Path("/proc")
    if proc_root.is_dir():
        try:
            for entry in proc_root.iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    child = int(entry.name)
                except ValueError:
                    continue
                try:
                    status = (entry / "status").read_text(encoding="utf-8")
                except OSError:
                    continue
                for line in status.splitlines():
                    if line.startswith("PPid:"):
                        try:
                            ppid = int(line.split(":", 1)[1].strip())
                        except ValueError:
                            break
                        if ppid == parent_pid:
                            out.append(child)
                        break
            if out:
                return out
        except OSError:
            pass
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid="],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return out
    if result.returncode != 0:
        return out
    for line in (result.stdout or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        try:
            pid_i = int(parts[0])
            ppid_i = int(parts[1])
        except ValueError:
            continue
        if ppid_i == parent_pid:
            out.append(pid_i)
    return out


def _descendant_pids(root_pid: int, *, max_depth: int = 6) -> list[int]:
    found: list[int] = []
    frontier = [root_pid]
    seen = {root_pid}
    depth = 0
    while frontier and depth < max_depth:
        nxt: list[int] = []
        for parent in frontier:
            for child in _child_pids_of(parent):
                if child in seen:
                    continue
                seen.add(child)
                found.append(child)
                nxt.append(child)
        frontier = nxt
        depth += 1
    return found


def resolve_provider_child_pid(
    wrapper_pid: int,
    *,
    expected_basenames: set[str],
    needs_pty: bool,
    settle_s: float = 0.12,
    wait_s: float = 1.0,
) -> tuple[int | None, str | None]:
    """Return (provider_pid, error). For needs_pty, require real child identity.

    Fail-closed when *needs_pty* and no descendant matches the expected
    provider binary (#99 B3). Non-pty returns the wrapper PID (direct spawn).
    """
    if (
        isinstance(wrapper_pid, bool)
        or not isinstance(wrapper_pid, int)
        or wrapper_pid <= 0
    ):
        return None, "invalid wrapper pid"
    if not needs_pty:
        return wrapper_pid, None
    deadline = time.monotonic() + max(0.05, float(wait_s))
    time.sleep(max(0.0, float(settle_s)))
    while True:
        for child in _descendant_pids(wrapper_pid):
            if not expected_basenames:
                # No allowlist — still prefer any non-wrapper child so we do
                # not record the python pty.spawn wrapper as the provider.
                return child, None
            if provider_binary_identity_matches(child, expected_basenames):
                return child, None
        if time.monotonic() >= deadline:
            break
        time.sleep(0.05)
    if not expected_basenames:
        return None, "needs_pty: provider child identity unresolved"
    return None, (
        "needs_pty: provider child identity unresolved "
        f"(expected basenames={sorted(expected_basenames)!r})"
    )


def _env_identity() -> tuple[str, str, str, Path]:
    """Resolve worker identity + validated canonical leader root (#100).

    Never walks nested worktree ``.omg`` ancestors — leader root comes from
    the supervisor environment that the leader already validated.
    """
    from omg_cli.team.bootstrap import BootstrapError, bootstrap_env_identity

    try:
        return bootstrap_env_identity()
    except BootstrapError as exc:
        raise SupervisorError(str(exc)) from exc


def _emit_pane_failure(*, worker_id: str | None, run_id: str | None) -> None:
    """One safe stderr line for pane-facing bootstrap/supervisor failure."""
    from omg_cli.team.bootstrap import pane_failure_line

    try:
        sys.stderr.write(pane_failure_line(worker_id=worker_id, run_id=run_id) + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001 — never crash the pane wrapper
        pass


def _log_bootstrap(
    root: Path,
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
    phase: str,
    code: str | None = None,
    summary: str | None = None,
) -> None:
    from omg_cli.team.bootstrap import append_bootstrap_log

    append_bootstrap_log(
        root,
        run_id=run_id,
        team_id=team_id,
        worker_id=worker_id,
        phase=phase,
        code=code,
        summary=summary,
    )


def _fail_closed_init(
    *,
    root: Path,
    run_id: str,
    team_id: str,
    worker_id: str,
    provider: str,
    supervisor_pid: int,
    failure_reason: str,
    evidence_code: str,
    provider_pid: int | None = None,
    provider_pgid: int | None = None,
    provider_pid_start: str | None = None,
    code: str = "SUPERVISOR",
) -> int:
    """Fail-closed init exit: artifact + one pane line (never a blank pane)."""
    from omg_cli.team.bootstrap import classify_bootstrap_exception

    try:
        _log_bootstrap(
            root,
            run_id=run_id,
            team_id=team_id,
            worker_id=worker_id,
            phase="BOOTSTRAP_FAIL",
            code=code or classify_bootstrap_exception(RuntimeError(failure_reason)).value,
            summary=failure_reason,
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        write_startup_phase(
            root,
            run_id=run_id,
            team_id=team_id,
            worker_id=worker_id,
            phase=StartupPhase.FAILED,
            provider=provider,
            supervisor_pid=supervisor_pid,
            provider_pid=provider_pid,
            provider_pgid=provider_pgid,
            provider_pid_start=provider_pid_start,
            evidence_code=evidence_code,
            failure_reason=failure_reason,
        )
    except Exception:  # noqa: BLE001 — receipt best-effort
        pass
    _emit_pane_failure(worker_id=worker_id, run_id=run_id)
    return 1


def _pid_start(pid: int) -> str | None:
    from omg_cli.team.plane import _pid_start_identity

    return _pid_start_identity(pid)


def _pgid(pid: int) -> int | None:
    from omg_cli.team.plane import _pgid_for_pid

    return _pgid_for_pid(pid)


def _resolve_positional_argv(
    argv: list[str], *, prompt_file: Path | None
) -> list[str]:
    if prompt_file is None:
        raise SupervisorError("positional-text delivery requires prompt_file")
    try:
        body = prompt_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise SupervisorError(f"cannot read prompt_file: {exc}") from exc
    # Substitute path tokens that equal the prompt_file path.
    needle = str(prompt_file)
    return [body if tok == needle else tok for tok in argv]


def _spawn_provider(
    argv: list[str],
    *,
    cwd: Path | None,
    needs_pty: bool,
    stdin_path: Path | None,
) -> subprocess.Popen[bytes]:
    if needs_pty:
        # Keep pty.spawn in a child Python so the supervisor retains PID
        # authority over the wrapper and can still reap/signal.
        payload = json.dumps(argv, ensure_ascii=False)
        py = (
            "import json,pty,sys;"
            " argv=json.loads(sys.argv[1]);"
            " rc=pty.spawn(argv);"
            " sys.exit(0 if rc in (0, None) else int(rc or 1))"
        )
        cmd = [sys.executable, "-c", py, payload]
        stdin = subprocess.DEVNULL
        if stdin_path is not None:
            stdin = open(stdin_path, "rb")  # noqa: SIM115 — closed by Popen
        return subprocess.Popen(
            cmd,
            cwd=str(cwd) if cwd else None,
            stdin=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    stdin: Any = subprocess.DEVNULL
    if stdin_path is not None:
        stdin = open(stdin_path, "rb")  # noqa: SIM115
    return subprocess.Popen(
        argv,
        cwd=str(cwd) if cwd else None,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _drain_capture(
    proc: subprocess.Popen[bytes],
    bucket: list[str],
    *,
    tee: Any | None = None,
) -> None:
    """Non-blocking drain of provider stdout into a bounded line bucket.

    Must use ``os.read`` on the raw fd — buffered ``file.read()`` can block
    until EOF even after ``select`` reports readability.

    When *tee* is set (typically the supervisor's controlling stdout/tty),
    also forward raw bytes so auth/trust prompts remain visible in the pane
    (#99 B3 / #100 visibility). Diagnostics files stay redacted separately.
    """
    if proc.stdout is None:
        return
    try:
        import select

        fd = proc.stdout.fileno()
        while True:
            ready, _, _ = select.select([fd], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(fd, 1024)
            except BlockingIOError:
                break
            if not chunk:
                break
            if tee is not None:
                try:
                    if hasattr(tee, "buffer"):
                        tee.buffer.write(chunk)
                        tee.buffer.flush()
                    elif hasattr(tee, "write"):
                        # bytes file-like (stdout.buffer) or text
                        try:
                            tee.write(chunk)
                        except TypeError:
                            tee.write(chunk.decode("utf-8", errors="replace"))
                        try:
                            tee.flush()
                        except Exception:
                            pass
                except (OSError, ValueError, AttributeError):
                    pass
            text = chunk.decode("utf-8", errors="replace")
            for line in text.splitlines():
                if line:
                    bucket.append(line[:512])
            if sum(len(x) for x in bucket) > _CAPTURE_MAX_BYTES:
                del bucket[: max(0, len(bucket) - _CAPTURE_MAX_LINES)]
                break
    except (OSError, ValueError):
        return
    if len(bucket) > _CAPTURE_MAX_LINES:
        del bucket[: len(bucket) - _CAPTURE_MAX_LINES]


def _forward_signals(
    child_pgid: int | None,
    child_pid: int | None,
    *,
    wrapper_pid: int | None = None,
) -> None:
    def _handler(signum: int, _frame: Any) -> None:
        targets: list[tuple[str, int]] = []
        if child_pgid and child_pgid > 0:
            targets.append(("pg", int(child_pgid)))
        if child_pid and child_pid > 0:
            targets.append(("pid", int(child_pid)))
        if wrapper_pid and wrapper_pid > 0 and wrapper_pid != child_pid:
            targets.append(("pid", int(wrapper_pid)))
            try:
                wpg = os.getpgid(int(wrapper_pid))
            except (ProcessLookupError, PermissionError, OSError):
                wpg = None
            if wpg and wpg > 0 and wpg != child_pgid:
                targets.append(("pg", int(wpg)))
        seen: set[tuple[str, int]] = set()
        for kind, target in targets:
            key = (kind, target)
            if key in seen:
                continue
            seen.add(key)
            try:
                if kind == "pg":
                    os.killpg(target, signum)
                else:
                    os.kill(target, signum)
            except (ProcessLookupError, PermissionError, OSError):
                continue

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass


def _child_alive(
    proc: subprocess.Popen[bytes],
    *,
    provider_pid: int | None = None,
) -> bool:
    if proc.poll() is not None:
        return False
    if (
        isinstance(provider_pid, int)
        and not isinstance(provider_pid, bool)
        and provider_pid > 0
        and provider_pid != proc.pid
    ):
        try:
            os.kill(provider_pid, 0)
        except (ProcessLookupError, PermissionError, OSError):
            return False
    return True


def _tee_stream() -> Any | None:
    """Best-effort controlling tty/stdout for pane-visible provider output."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "isatty") and stream.isatty():
                return stream
        except Exception:
            continue
    return sys.stdout


def run_supervisor(
    *,
    descriptor_path: Path | str,
    ready_timeout_s: float | None = None,
    env: Mapping[str, str] | None = None,
    poll_s: float = 0.1,
) -> int:
    """Main supervisor entry: spawn provider, write phases, reap child.

    Returns the provider exit code (or 1 on supervisor-level failure).
    """
    source = dict(env) if env is not None else dict(os.environ)
    run_id, team_id, worker_id, root = _env_identity()
    desc = load_provider_descriptor(descriptor_path)
    provider = str(desc.get("provider") or "unknown")
    argv = [str(x) for x in desc["argv"]]
    delivery = str(desc.get("prompt_delivery") or "prompt-file")
    needs_pty = bool(desc.get("needs_pty"))
    prompt_file_raw = desc.get("prompt_file")
    prompt_file = Path(str(prompt_file_raw)) if prompt_file_raw else None
    cwd_raw = desc.get("cwd")
    cwd = Path(str(cwd_raw)) if cwd_raw else None
    timeout_s = (
        float(ready_timeout_s)
        if ready_timeout_s is not None
        else float(source.get("OMG_TEAM_SUPERVISOR_READY_S") or DEFAULT_READY_WAIT_S)
    )
    post_stable_s = _resolve_post_stable_observe_s(source)

    supervisor_pid = os.getpid()
    write_startup_phase(
        root,
        run_id=run_id,
        team_id=team_id,
        worker_id=worker_id,
        phase=StartupPhase.PANE_CREATED,
        provider=provider,
        supervisor_pid=supervisor_pid,
        evidence_code=EvidenceCode.PANE_BOUND,
    )

    stdin_path: Path | None = None
    if delivery == "positional-text":
        argv = _resolve_positional_argv(argv, prompt_file=prompt_file)
    elif delivery == "stdin":
        if prompt_file is None:
            raise SupervisorError("stdin delivery requires prompt_file")
        stdin_path = prompt_file
    elif delivery != "prompt-file":
        raise SupervisorError(f"unknown prompt_delivery: {delivery!r}")

    strategy = get_readiness_strategy(provider, env=source)
    identity_names = expected_identity_basenames(provider, argv, descriptor=desc)
    proc: subprocess.Popen[bytes] | None = None
    capture: list[str] = []
    provider_pid: int | None = None
    provider_pgid: int | None = None
    provider_start: str | None = None
    wrapper_pid: int | None = None
    reached_ready = False
    reached_dispatch = False
    provisional_since: float | None = None
    provisional_evidence: str | None = None
    tee = _tee_stream()

    def _observe_window_s(evidence: str | None) -> float:
        if evidence == EvidenceCode.TUI_IDLE_PROMPT.value:
            return max(post_stable_s, MIN_TUI_IDLE_POST_STABLE_S)
        return post_stable_s

    def _identity_ok(pid: int | None) -> bool:
        if not identity_names:
            return False
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            return False
        return provider_binary_identity_matches(pid, identity_names)

    def _finalize_ready(*, evidence: str) -> None:
        nonlocal reached_ready, reached_dispatch
        write_startup_phase(
            root,
            run_id=run_id,
            team_id=team_id,
            worker_id=worker_id,
            phase=StartupPhase.PROVIDER_READY,
            provider=provider,
            supervisor_pid=supervisor_pid,
            provider_pid=provider_pid,
            provider_pgid=provider_pgid,
            provider_pid_start=provider_start,
            evidence_code=evidence,
        )
        reached_ready = True
        write_startup_phase(
            root,
            run_id=run_id,
            team_id=team_id,
            worker_id=worker_id,
            phase=StartupPhase.TASK_DISPATCHED,
            provider=provider,
            supervisor_pid=supervisor_pid,
            provider_pid=provider_pid,
            provider_pgid=provider_pgid,
            provider_pid_start=provider_start,
            evidence_code=EvidenceCode.PROMPT_CONTRACT_ACCEPTED,
        )
        reached_dispatch = True

    def _reap_while_draining() -> int:
        """Wait for child while continuously draining stdout (no pipe deadlock)."""
        assert proc is not None
        while _child_alive(proc, provider_pid=provider_pid):
            _drain_capture(proc, capture, tee=tee)
            time.sleep(max(0.05, float(poll_s)))
        # Final drain after exit.
        for _ in range(8):
            before = len(capture)
            _drain_capture(proc, capture, tee=tee)
            if len(capture) == before and proc.poll() is not None:
                break
            time.sleep(0.02)
        rc = proc.wait()
        return int(rc if rc is not None else 1)

    try:
        proc = _spawn_provider(
            argv, cwd=cwd, needs_pty=needs_pty, stdin_path=stdin_path
        )
        wrapper_pid = int(proc.pid)
        # Protect the new session immediately. Child resolution may inspect the
        # process tree for several polls; a terminating signal during that
        # interval must still reach the wrapper's process group (#164).
        _forward_signals(_pgid(wrapper_pid), wrapper_pid)
        resolved_pid, resolve_err = resolve_provider_child_pid(
            wrapper_pid,
            expected_basenames=identity_names,
            needs_pty=needs_pty,
        )
        if resolve_err or resolved_pid is None:
            try:
                wpg = _pgid(wrapper_pid)
                if wpg:
                    os.killpg(wpg, signal.SIGTERM)
                else:
                    proc.terminate()
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            return _fail_closed_init(
                root=root,
                run_id=run_id,
                team_id=team_id,
                worker_id=worker_id,
                provider=provider,
                supervisor_pid=supervisor_pid,
                provider_pid=wrapper_pid,
                provider_pgid=_pgid(wrapper_pid),
                evidence_code=EvidenceCode.MALFORMED,
                failure_reason=resolve_err or "provider child identity unresolved",
                code="IDENTITY",
            )

        provider_pid = int(resolved_pid)
        # Brief settle so start identity is observable.
        time.sleep(0.02)
        provider_pgid = _pgid(provider_pid) or _pgid(wrapper_pid)
        provider_start = _pid_start(provider_pid)
        if not provider_start:
            # Fail closed: cannot prove identity.
            try:
                if provider_pgid:
                    os.killpg(provider_pgid, signal.SIGTERM)
                else:
                    proc.terminate()
                if wrapper_pid and wrapper_pid != provider_pid:
                    try:
                        os.kill(wrapper_pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
            except (ProcessLookupError, PermissionError, OSError):
                pass
            return _fail_closed_init(
                root=root,
                run_id=run_id,
                team_id=team_id,
                worker_id=worker_id,
                provider=provider,
                supervisor_pid=supervisor_pid,
                provider_pid=provider_pid,
                provider_pgid=provider_pgid,
                evidence_code=EvidenceCode.MALFORMED,
                failure_reason="provider_pid_start unavailable after spawn",
                code="IDENTITY",
            )

        if provider_pid == supervisor_pid:
            return _fail_closed_init(
                root=root,
                run_id=run_id,
                team_id=team_id,
                worker_id=worker_id,
                provider=provider,
                supervisor_pid=supervisor_pid,
                provider_pid=provider_pid,
                provider_pgid=provider_pgid,
                provider_pid_start=provider_start,
                evidence_code=EvidenceCode.MALFORMED,
                failure_reason="provider_pid equals supervisor_pid",
                code="IDENTITY",
            )

        # Refine the forwarding target before publishing provider_spawned.
        # Receipt readers may signal the supervisor as soon as this write is
        # observable, so publication must be the last step in this sequence.
        _forward_signals(
            provider_pgid, provider_pid, wrapper_pid=wrapper_pid
        )
        write_startup_phase(
            root,
            run_id=run_id,
            team_id=team_id,
            worker_id=worker_id,
            phase=StartupPhase.PROVIDER_SPAWNED,
            provider=provider,
            supervisor_pid=supervisor_pid,
            provider_pid=provider_pid,
            provider_pgid=provider_pgid,
            provider_pid_start=provider_start,
            evidence_code=EvidenceCode.PROVIDER_SPAWNED,
        )

        deadline = time.monotonic() + max(0.05, timeout_s)
        started = time.monotonic()
        while time.monotonic() < deadline:
            _drain_capture(proc, capture, tee=tee)
            alive = _child_alive(proc, provider_pid=provider_pid)
            identity_matched = _identity_ok(provider_pid)
            obs = strategy.observe(
                provider_pid=provider_pid,
                alive=alive,
                capture_lines=capture,
                elapsed_s=time.monotonic() - started,
                env=source,
                identity_matched=identity_matched,
            )
            if obs.status == "blocked":
                append_startup_diagnostics(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    lines=capture[-16:],
                )
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.BLOCKED,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=obs.evidence_code,
                    blocked_reason=obs.blocked_reason,
                    failure_reason=obs.detail,
                )
                # Keep child alive for human intervention; supervisor waits.
                break
            if obs.status == "failed":
                append_startup_diagnostics(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    lines=capture[-16:],
                )
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.FAILED,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=obs.evidence_code,
                    failure_reason=obs.failure_reason or obs.detail,
                )
                break
            if obs.status == "unknown":
                # Stay pending until timeout — never optimistic ready.
                pass
            elif (
                obs.status == "ready"
                and not reached_ready
                and obs.evidence_code != EvidenceCode.TUI_IDLE_PROMPT.value
            ):
                # Definitive ready (fixture / fake only) — finalize now.
                # TUI idle must never take this path (#99 re-review).
                _finalize_ready(evidence=obs.evidence_code)
                break
            elif (
                (
                    obs.status == "provisional"
                    or (
                        obs.status == "ready"
                        and obs.evidence_code == EvidenceCode.TUI_IDLE_PROMPT.value
                    )
                )
                and not reached_ready
            ):
                # process_stable or weak TUI idle: keep observing for delayed
                # auth/trust through the post-stable window. Identity is
                # required before provisional is accepted by the strategy.
                if provisional_since is None:
                    provisional_since = time.monotonic()
                    provisional_evidence = obs.evidence_code
                elif (
                    time.monotonic() - provisional_since
                    >= _observe_window_s(provisional_evidence)
                    and alive
                    and _identity_ok(provider_pid)
                ):
                    _finalize_ready(
                        evidence=provisional_evidence
                        or EvidenceCode.PROCESS_STABLE.value
                    )
                    break
            if not alive:
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.FAILED,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=EvidenceCode.PROVIDER_EXITED,
                    failure_reason="provider exited before ready",
                )
                break
            time.sleep(max(0.05, float(poll_s)))
        else:
            # Timeout path — final drain/observe before any provisional finalize.
            append_startup_diagnostics(
                root,
                run_id=run_id,
                team_id=team_id,
                worker_id=worker_id,
                lines=capture[-16:],
            )
            _drain_capture(proc, capture, tee=tee)
            alive = _child_alive(proc, provider_pid=provider_pid)
            identity_matched = _identity_ok(provider_pid)
            final_obs = strategy.observe(
                provider_pid=provider_pid or 0,
                alive=alive,
                capture_lines=capture,
                elapsed_s=time.monotonic() - started,
                env=source,
                identity_matched=identity_matched,
            )
            if final_obs.status == "blocked" and not reached_ready:
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.BLOCKED,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=final_obs.evidence_code,
                    blocked_reason=final_obs.blocked_reason,
                    failure_reason=final_obs.detail,
                )
            elif final_obs.status == "failed" and not reached_ready:
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.FAILED,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=final_obs.evidence_code,
                    failure_reason=final_obs.failure_reason or final_obs.detail,
                )
            elif (
                alive
                and provisional_since is not None
                and not reached_ready
                and identity_matched
                and time.monotonic() - provisional_since
                >= _observe_window_s(provisional_evidence)
                and final_obs.status in ("provisional", "ready", "pending")
            ):
                # Post-stable window complete and final observe still clean.
                _finalize_ready(
                    evidence=provisional_evidence
                    or EvidenceCode.PROCESS_STABLE.value
                )
            elif alive and provisional_since is not None and not reached_ready:
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.FAILED,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=EvidenceCode.TIMEOUT,
                    failure_reason=(
                        f"provider readiness timed out after {timeout_s}s "
                        "(post-stable window incomplete)"
                    ),
                )
            elif alive and not reached_ready:
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.FAILED,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=EvidenceCode.TIMEOUT,
                    failure_reason=f"provider readiness timed out after {timeout_s}s",
                )
            elif not reached_ready:
                write_startup_phase(
                    root,
                    run_id=run_id,
                    team_id=team_id,
                    worker_id=worker_id,
                    phase=StartupPhase.FAILED,
                    provider=provider,
                    supervisor_pid=supervisor_pid,
                    provider_pid=provider_pid,
                    provider_pgid=provider_pgid,
                    provider_pid_start=provider_start,
                    evidence_code=EvidenceCode.PROVIDER_EXITED,
                    failure_reason="provider exited before ready (timeout loop)",
                )

        # Reap / wait for child for the rest of the pane lifetime.
        # Drain continuously so a chatty provider cannot fill the PIPE (#99 B3).
        assert proc is not None
        rc = _reap_while_draining()
        if reached_dispatch and rc != 0:
            # Provider died after ready — record terminal note but do not
            # reverse phases (monotonic). Leader wait checks liveness.
            append_startup_diagnostics(
                root,
                run_id=run_id,
                team_id=team_id,
                worker_id=worker_id,
                lines=[f"provider_exit_after_ready rc={rc}"],
            )
        return int(rc if rc is not None else 1)
    except SupervisorError as exc:
        from omg_cli.team.bootstrap import classify_bootstrap_exception

        try:
            _log_bootstrap(
                root,
                run_id=run_id,
                team_id=team_id,
                worker_id=worker_id,
                phase="BOOTSTRAP_FAIL",
                code=classify_bootstrap_exception(exc).value,
                summary=str(exc),
            )
        except Exception:  # noqa: BLE001
            pass
        try:
            write_startup_phase(
                root,
                run_id=run_id,
                team_id=team_id,
                worker_id=worker_id,
                phase=StartupPhase.FAILED,
                provider=provider,
                supervisor_pid=supervisor_pid,
                provider_pid=provider_pid,
                provider_pgid=provider_pgid,
                provider_pid_start=provider_start,
                evidence_code=EvidenceCode.MALFORMED,
                failure_reason=str(exc),
            )
        except Exception:  # noqa: BLE001 — receipt best-effort on fail path
            pass
        if proc is not None and _child_alive(proc, provider_pid=provider_pid):
            try:
                if provider_pgid:
                    os.killpg(provider_pgid, signal.SIGTERM)
                else:
                    proc.terminate()
                if wrapper_pid and wrapper_pid != provider_pid:
                    try:
                        os.kill(wrapper_pid, signal.SIGTERM)
                    except (ProcessLookupError, PermissionError, OSError):
                        pass
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    if provider_pgid:
                        os.killpg(provider_pgid, signal.SIGKILL)
                    else:
                        proc.kill()
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        _emit_pane_failure(worker_id=worker_id, run_id=run_id)
        return 1
    except StartupError as exc:
        from omg_cli.team.bootstrap import classify_bootstrap_exception

        try:
            _log_bootstrap(
                root,
                run_id=run_id,
                team_id=team_id,
                worker_id=worker_id,
                phase="BOOTSTRAP_FAIL",
                code=classify_bootstrap_exception(exc).value,
                summary=str(exc),
            )
        except Exception:  # noqa: BLE001
            pass
        _emit_pane_failure(worker_id=worker_id, run_id=run_id)
        return 1
    finally:
        if proc is not None and proc.stdout is not None:
            try:
                proc.stdout.close()
            except OSError:
                pass


__all__ = [
    "DESCRIPTOR_SCHEMA_VERSION",
    "DESCRIPTOR_KIND",
    "PREPUBLISH_DIRNAME",
    "PREPUBLISH_KIND",
    "PREPUBLISH_SCHEMA_VERSION",
    "SupervisorError",
    "admit_pane_supervisor",
    "clear_supervisor_prepublish_authorities",
    "descriptor_content_digest",
    "write_provider_descriptor",
    "load_provider_descriptor",
    "expected_identity_basenames",
    "provider_binary_identity_matches",
    "publish_supervisor_authority",
    "resolve_provider_child_pid",
    "run_supervisor",
    "supervisor_prepublish_dir",
    "supervisor_prepublish_path",
]
