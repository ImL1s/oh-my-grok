"""Silent Team worker bootstrap (#100).

Separates internal pane bootstrap from public CLI UX:

- success writes bounded diagnostics only (no pane stdout/stderr)
- failure emits one redacted pane line; details live in ``bootstrap.log``
- uses the validated canonical leader root (never generic ancestor discovery)

Does not own #99 readiness receipts or #101 operator surfaces.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    ContractPathError,
    append_locked_jsonl,
    ensure_managed_dir,
)
from omg_cli.contracts.state_schemas import require_safe_id
from omg_cli.contracts.writer_chain import canonical_json_bytes
from omg_cli.project_root import ProjectRootResolution
from omg_cli.redaction import redact_text
from omg_cli.team.startup import worker_startup_path

# Keep env key strings local to avoid importing plane (cycle risk).
TEAM_WORKER_ENV = "OMG_TEAM_WORKER"
TEAM_WORKER_ID_ENV = "OMG_TEAM_WORKER_ID"
TEAM_RUN_ID_ENV = "OMG_TEAM_RUN_ID"
TEAM_ID_ENV = "OMG_TEAM_ID"
TEAM_LEADER_ROOT_ENV = "OMG_TEAM_LEADER_ROOT"
TEAM_STATE_ROOT_ENV = "OMG_TEAM_STATE_ROOT"
TEAM_OWNER_TOKEN_ENV = "OMG_TEAM_OWNER_TOKEN"

BOOTSTRAP_FILENAME = "bootstrap.log"
BOOTSTRAP_SCHEMA = 1
_MAX_BOOTSTRAP_LINES = 64
_MAX_SUMMARY = 240
_MAX_FILE_BYTES = 65_536

# Absolute home-shaped prefixes (never persist raw /Users/… or /home/…).
_HOME_PATH_RE = re.compile(
    r"(?P<prefix>(?:/Users|/home)/)(?P<user>[^/\s\"']+)"
)
_WIN_HOME_RE = re.compile(
    r"(?P<prefix>(?i:[A-Z]:\\Users\\))(?P<user>[^\\\s\"']+)"
)


class BootstrapErrorCode(str, Enum):
    ROOT_INVALID = "ROOT_INVALID"
    READY_WRITE_FAILED = "READY_WRITE_FAILED"
    IMPORT_FAILED = "IMPORT_FAILED"
    STATE_SCHEMA = "STATE_SCHEMA"
    PERMISSION = "PERMISSION"
    SUPERVISOR = "SUPERVISOR"
    DESCRIPTOR = "DESCRIPTOR"
    IDENTITY = "IDENTITY"
    UNKNOWN = "UNKNOWN"


class BootstrapError(RuntimeError):
    """Fail-closed bootstrap error with a stable code (never a raw traceback)."""

    def __init__(
        self,
        code: BootstrapErrorCode | str,
        message: str,
        *,
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        self.code = BootstrapErrorCode(code) if not isinstance(code, BootstrapErrorCode) else code
        self.exit_code = int(exit_code)


@dataclass(frozen=True)
class BootstrapResult:
    """Internal bootstrap outcome — never emitted to pane stdout by itself."""

    ok: bool
    code: str | None = None
    reason: str | None = None
    ready_path: Path | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": self.ok}
        if self.code:
            out["code"] = self.code
        if self.reason:
            out["reason"] = sanitize_bootstrap_summary(self.reason)
        # Never expose absolute ready_path on the public/pane surface.
        if self.ready_path is not None:
            out["ready_written"] = True
        return out


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def worker_bootstrap_path(
    root: Path | str, *, run_id: str, team_id: str, worker_id: str
) -> Path:
    return worker_startup_path(
        root, run_id=run_id, team_id=team_id, worker_id=worker_id
    ).with_name(BOOTSTRAP_FILENAME)


def pane_failure_line(
    *,
    worker_id: str | None = None,
    run_id: str | None = None,
) -> str:
    """Single safe pane-facing failure hint (no paths, env, or traceback)."""
    wid = (worker_id or "").strip() or "worker"
    rid = (run_id or "").strip()
    if rid:
        return (
            f"OMG worker {wid} failed to initialize; "
            f"see `omg team status {rid} --full`."
        )
    return (
        f"OMG worker {wid} failed to initialize; "
        "see `omg team status <run> --full`."
    )


def sanitize_bootstrap_summary(text: str | None) -> str | None:
    """Redact secrets and absolute home paths before persisting summaries.

    Never store raw ``str(exc)`` path leakage under ``/Users/…`` / ``/home/…``
    or ``$HOME`` prefixes. Shared ``redact_text`` handles credentials first.
    """
    if text is None:
        return None
    raw = str(text).replace("\x00", "")
    cleaned = redact_text(raw)
    # Explicit $HOME / Path.home() prefix (longest first).
    homes: list[str] = []
    for candidate in (
        (os.environ.get("HOME") or "").strip(),
        str(Path.home()),
    ):
        if not candidate:
            continue
        try:
            homes.append(str(Path(candidate).expanduser().resolve()))
        except OSError:
            homes.append(candidate)
        homes.append(candidate)
    # Deduplicate while preferring longer prefixes.
    seen: set[str] = set()
    for home in sorted(homes, key=len, reverse=True):
        if not home or home in seen:
            continue
        seen.add(home)
        if home in cleaned:
            cleaned = cleaned.replace(home, "<home>")
    cleaned = _HOME_PATH_RE.sub(r"\g<prefix><user>", cleaned)
    cleaned = _WIN_HOME_RE.sub(r"\g<prefix><user>", cleaned)
    return cleaned[:_MAX_SUMMARY]


def classify_bootstrap_exception(exc: BaseException) -> BootstrapErrorCode:
    if isinstance(exc, BootstrapError):
        return exc.code
    if isinstance(exc, PermissionError):
        return BootstrapErrorCode.PERMISSION
    if isinstance(exc, ImportError):
        return BootstrapErrorCode.IMPORT_FAILED
    if isinstance(exc, (OSError, ContractPathError)):
        return BootstrapErrorCode.READY_WRITE_FAILED
    name = type(exc).__name__
    msg = str(exc).lower()
    if "schema" in msg or "kind" in msg or "version" in msg:
        return BootstrapErrorCode.STATE_SCHEMA
    if "descriptor" in msg:
        return BootstrapErrorCode.DESCRIPTOR
    if "identity" in msg or "leader" in msg or "root" in msg:
        return BootstrapErrorCode.ROOT_INVALID
    if "supervisor" in name.lower() or "supervisor" in msg:
        return BootstrapErrorCode.SUPERVISOR
    return BootstrapErrorCode.UNKNOWN


def _absolute_existing_dir(raw: str, *, label: str) -> Path:
    text = (raw or "").strip()
    if not text:
        raise BootstrapError(
            BootstrapErrorCode.ROOT_INVALID,
            f"{label} is required",
            exit_code=2,
        )
    try:
        path = Path(text).expanduser().resolve(strict=True)
    except OSError as exc:
        raise BootstrapError(
            BootstrapErrorCode.ROOT_INVALID,
            f"{label} is not usable",
            exit_code=2,
        ) from exc
    if not path.is_dir():
        raise BootstrapError(
            BootstrapErrorCode.ROOT_INVALID,
            f"{label} is not a directory",
            exit_code=2,
        )
    # Reject when the path itself is a symlink (control-plane confuse/redirect).
    try:
        if Path(text).expanduser().is_symlink():
            raise BootstrapError(
                BootstrapErrorCode.ROOT_INVALID,
                f"{label} must not be a symlink",
                exit_code=2,
            )
    except BootstrapError:
        raise
    except OSError as exc:
        raise BootstrapError(
            BootstrapErrorCode.ROOT_INVALID,
            f"{label} is not usable",
            exit_code=2,
        ) from exc
    return path


def validate_canonical_leader_root(
    env: Mapping[str, str] | None = None,
) -> Path:
    """Validate leader control-plane root from supervisor env (no discovery)."""
    source = env if env is not None else os.environ
    leader_raw = (source.get(TEAM_LEADER_ROOT_ENV) or "").strip()
    if not leader_raw:
        leader_raw = (source.get("OMG_PROJECT_ROOT") or "").strip()
        label = "OMG_PROJECT_ROOT"
    else:
        label = TEAM_LEADER_ROOT_ENV
    leader = _absolute_existing_dir(leader_raw, label=label)
    control = leader / ".omg"
    try:
        if not control.is_dir() or control.is_symlink():
            raise BootstrapError(
                BootstrapErrorCode.ROOT_INVALID,
                "leader root has no real .omg control plane",
                exit_code=2,
            )
    except BootstrapError:
        raise
    except OSError as exc:
        raise BootstrapError(
            BootstrapErrorCode.ROOT_INVALID,
            "leader root control plane is not usable",
            exit_code=2,
        ) from exc

    state_raw = (source.get(TEAM_STATE_ROOT_ENV) or "").strip()
    if state_raw:
        state = _absolute_existing_dir(state_raw, label=TEAM_STATE_ROOT_ENV)
        try:
            expected = (control / "state").resolve(strict=True)
        except OSError as exc:
            raise BootstrapError(
                BootstrapErrorCode.ROOT_INVALID,
                "leader state root is not usable",
                exit_code=2,
            ) from exc
        if state != expected:
            raise BootstrapError(
                BootstrapErrorCode.ROOT_INVALID,
                f"{TEAM_STATE_ROOT_ENV} does not match leader state",
                exit_code=2,
            )
    return leader


def resolve_supervisor_project_root(
    env: Mapping[str, str] | None = None,
) -> ProjectRootResolution:
    """Pin project root from validated leader env — skip ancestor discovery."""
    source = env if env is not None else os.environ
    leader = validate_canonical_leader_root(source)
    try:
        cwd = Path.cwd().resolve()
    except OSError:
        cwd = leader
    return ProjectRootResolution(
        root=leader,
        source="team_leader",
        cwd=cwd,
        shadowed_omg_ancestors=(),
        note="team supervisor: canonical leader root (discovery skipped)",
    )


def team_supervisor_context_present(
    env: Mapping[str, str] | None = None,
) -> bool:
    """True when pane supervisor env markers are present (partial counts)."""
    source = env if env is not None else os.environ
    keys = (
        TEAM_WORKER_ENV,
        TEAM_WORKER_ID_ENV,
        TEAM_RUN_ID_ENV,
        TEAM_ID_ENV,
        TEAM_LEADER_ROOT_ENV,
        TEAM_STATE_ROOT_ENV,
        TEAM_OWNER_TOKEN_ENV,
    )
    return any((source.get(name) or "").strip() for name in keys)


def append_bootstrap_log(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
    phase: str,
    code: str | None = None,
    summary: str | None = None,
) -> Path | None:
    """Append one redacted bootstrap phase line (locked, bounded, confined)."""
    try:
        path = worker_bootstrap_path(
            root, run_id=run_id, team_id=team_id, worker_id=worker_id
        )
    except (ValueError, OSError, ContractPathError):
        return None
    try:
        ensure_managed_dir(path.parent)
    except (OSError, ContractPathError):
        return None

    record = {
        "schema_version": BOOTSTRAP_SCHEMA,
        "ts": _utc_now(),
        "phase": redact_text(str(phase or "UNKNOWN"))[:64],
        "code": redact_text(str(code))[:64] if code else None,
        "summary": sanitize_bootstrap_summary(summary),
    }
    # Drop nulls for stable compact lines.
    compact = {k: v for k, v in record.items() if v is not None}
    try:
        payload = canonical_json_bytes(compact)
        # Rotate if oversized: keep a truncated tail marker file rewrite.
        if path.is_file():
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if size >= _MAX_FILE_BYTES:
                _rotate_bootstrap_log(path)
        append_locked_jsonl(path, payload)
        _trim_bootstrap_log(path)
        return path
    except (OSError, ContractPathError, ValueError, TypeError):
        return None


def _rotate_bootstrap_log(path: Path) -> None:
    """Best-effort truncate when the log exceeds the byte cap."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    lines = [ln for ln in text.splitlines() if ln.strip()]
    keep = lines[-max(8, _MAX_BOOTSTRAP_LINES // 2) :]
    body = (
        json.dumps(
            {
                "schema_version": BOOTSTRAP_SCHEMA,
                "ts": _utc_now(),
                "phase": "ROTATED",
                "code": "BOUNDED",
                "summary": "bootstrap.log rotated (byte cap)",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
        + ("\n".join(keep) + ("\n" if keep else ""))
    )
    try:
        from omg_cli.contracts.path_keys import atomic_write_bytes

        atomic_write_bytes(path, body.encode("utf-8"), mode=DATA_FILE_MODE)
    except (OSError, ContractPathError):
        return


def _trim_bootstrap_log(path: Path) -> None:
    try:
        lines = [
            ln
            for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip()
        ]
    except OSError:
        return
    if len(lines) <= _MAX_BOOTSTRAP_LINES:
        return
    keep = lines[-_MAX_BOOTSTRAP_LINES:]
    body = ("\n".join(keep) + "\n").encode("utf-8")
    try:
        from omg_cli.contracts.path_keys import atomic_write_bytes

        atomic_write_bytes(path, body, mode=DATA_FILE_MODE)
    except (OSError, ContractPathError):
        return


def read_bootstrap_summary(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
) -> dict[str, Any] | None:
    """Short descriptor for ``omg team status --full`` (not the full log)."""
    try:
        path = worker_bootstrap_path(
            root, run_id=run_id, team_id=team_id, worker_id=worker_id
        )
    except (ValueError, OSError):
        return None
    if not path.is_file():
        return None
    try:
        lines = [
            ln
            for ln in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if ln.strip()
        ]
    except OSError:
        return None
    if not lines:
        return {"present": True, "lines": 0, "last_phase": None, "last_code": None}
    last_phase = None
    last_code = None
    last_summary = None
    try:
        parsed = json.loads(lines[-1])
        if isinstance(parsed, dict):
            last_phase = parsed.get("phase")
            last_code = parsed.get("code")
            raw_sum = parsed.get("summary")
            if isinstance(raw_sum, str):
                last_summary = sanitize_bootstrap_summary(raw_sum)
    except (json.JSONDecodeError, TypeError, ValueError):
        last_phase = "UNPARSEABLE"
    return {
        "present": True,
        "lines": len(lines),
        "last_phase": last_phase,
        "last_code": last_code,
        "last_summary": last_summary,
        # Relative descriptor only — never absolute home paths.
        "artifact": "bootstrap.log",
    }


def worker_ready_internal(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
    source: str = "process",
) -> BootstrapResult:
    """Write legacy v1 ready receipt without emitting CLI JSON (#100).

    Public ``omg team worker-ready`` may emit; pane/supervisor paths must not.
    """
    from omg_cli.team.runtime import write_worker_ready_receipt

    try:
        rid = require_safe_id(run_id, label="run_id")
        tid = require_safe_id(team_id, label="team_id")
        wid = require_safe_id(worker_id, label="worker_id")
    except ValueError as exc:
        return BootstrapResult(
            ok=False,
            code=BootstrapErrorCode.IDENTITY.value,
            reason=str(exc),
        )
    try:
        append_bootstrap_log(
            root,
            run_id=rid,
            team_id=tid,
            worker_id=wid,
            phase="BOOTSTRAP_BEGIN",
            code="LEGACY_READY",
        )
        path = write_worker_ready_receipt(
            root,
            run_id=rid,
            team_id=tid,
            worker_id=wid,
            source=source,
        )
        append_bootstrap_log(
            root,
            run_id=rid,
            team_id=tid,
            worker_id=wid,
            phase="READY_WRITE_OK",
            code="LEGACY_READY",
        )
        return BootstrapResult(ok=True, ready_path=path)
    except Exception as exc:  # noqa: BLE001 — mapped to BootstrapResult
        code = classify_bootstrap_exception(exc)
        append_bootstrap_log(
            root,
            run_id=rid,
            team_id=tid,
            worker_id=wid,
            phase="READY_WRITE_FAIL",
            code=code.value,
            summary=str(exc),
        )
        return BootstrapResult(ok=False, code=code.value, reason=str(exc))


def bootstrap_env_identity(
    env: Mapping[str, str] | None = None,
) -> tuple[str, str, str, Path]:
    """Resolve run/team/worker + validated leader root for silent bootstrap."""
    source = env if env is not None else os.environ
    worker_id = (source.get(TEAM_WORKER_ID_ENV) or "").strip()
    run_id = (source.get(TEAM_RUN_ID_ENV) or "").strip()
    team_id = (source.get(TEAM_ID_ENV) or "team").strip() or "team"
    if not worker_id or not run_id:
        raise BootstrapError(
            BootstrapErrorCode.IDENTITY,
            "team bootstrap requires OMG_TEAM_WORKER_ID and OMG_TEAM_RUN_ID",
            exit_code=2,
        )
    leader = validate_canonical_leader_root(source)
    return run_id, team_id, worker_id, leader


__all__ = [
    "BOOTSTRAP_FILENAME",
    "BootstrapError",
    "BootstrapErrorCode",
    "BootstrapResult",
    "append_bootstrap_log",
    "bootstrap_env_identity",
    "classify_bootstrap_exception",
    "pane_failure_line",
    "read_bootstrap_summary",
    "resolve_supervisor_project_root",
    "sanitize_bootstrap_summary",
    "team_supervisor_context_present",
    "validate_canonical_leader_root",
    "worker_bootstrap_path",
    "worker_ready_internal",
]
