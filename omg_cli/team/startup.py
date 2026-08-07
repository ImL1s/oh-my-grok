"""Schema-versioned Team worker startup contract (#99).

Phases are monotonic. Only validated schema_version=2 records with a live
provider identity may prove readiness. Legacy v1 ``worker_ready`` receipts are
classified as ``wrapper_ready_legacy`` and must never produce
``startup_status=running``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Sequence

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    ContractPathError,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
    safe_path_key,
)
from omg_cli.contracts.state_schemas import require_safe_id
from omg_cli.evidence import CLI_WRITER

STARTUP_SCHEMA_VERSION = 2
STARTUP_KIND = "team_worker_startup"
LEGACY_KIND = "worker_ready"
READY_FILENAME = "ready.json"
DIAGNOSTICS_FILENAME = "startup_diagnostics.jsonl"
DEFAULT_GATE_PHASE = "task_dispatched"
GATE_PHASE_ENV = "OMG_TEAM_STARTUP_GATE_PHASE"

# Bounded diagnostics — never store prompts/secrets.
_MAX_DIAG_LINE = 512
_MAX_DIAG_LINES = 64
_SECRET_RE = re.compile(
    r"(?i)(?:"
    r"authorization\s*[=:]\s*bearer\s+\S+"
    r"|bearer\s+\S+"
    r"|(?:api[_-]?key|authorization|password|secret|token)\s*[=:]\s*\S+"
    r")"
)


class StartupPhase(str, Enum):
    PANE_CREATED = "pane_created"
    PROVIDER_SPAWNED = "provider_spawned"
    PROVIDER_READY = "provider_ready"
    TASK_DISPATCHED = "task_dispatched"
    MAILBOX_ACK = "mailbox_ack"
    FAILED = "failed"
    BLOCKED = "blocked"


class EvidenceCode(str, Enum):
    PANE_BOUND = "pane_bound"
    PROVIDER_SPAWNED = "provider_spawned"
    TUI_IDLE_PROMPT = "tui_idle_prompt"
    PROCESS_STABLE = "process_stable"
    FIXTURE_READY = "fixture_ready"
    FAKE_READY = "fake_ready"
    PROMPT_CONTRACT_ACCEPTED = "prompt_contract_accepted"
    MAILBOX_ACK = "mailbox_ack"
    AUTH_REQUIRED = "auth_required"
    TRUST_REQUIRED = "trust_required"
    PROVIDER_EXITED = "provider_exited_before_ready"
    PROVIDER_EXITED_AFTER_READY = "provider_exited_after_ready"
    TIMEOUT = "timeout"
    UNKNOWN_PROVIDER = "unknown_provider"
    MALFORMED = "malformed_output"
    PANE_DISAPPEARED = "pane_disappeared"
    WRAPPER_READY_LEGACY = "wrapper_ready_legacy"
    CANCELLED = "cancelled"


class BlockedReason(str, Enum):
    AUTH = "authentication_required"
    TRUST = "trust_or_hooks_review"
    PERMISSION = "permission_prompt"
    UNKNOWN_UI = "unknown_or_unparseable_ui"


# Monotonic rank for non-terminal progress phases.
_PHASE_RANK: dict[str, int] = {
    StartupPhase.PANE_CREATED.value: 10,
    StartupPhase.PROVIDER_SPAWNED.value: 20,
    StartupPhase.PROVIDER_READY.value: 30,
    StartupPhase.TASK_DISPATCHED.value: 40,
    StartupPhase.MAILBOX_ACK.value: 50,
}

_TERMINAL_PHASES = frozenset(
    {StartupPhase.FAILED.value, StartupPhase.BLOCKED.value}
)

_GATE_PHASES = frozenset(
    {
        StartupPhase.PROVIDER_READY.value,
        StartupPhase.TASK_DISPATCHED.value,
        StartupPhase.MAILBOX_ACK.value,
    }
)


class StartupError(RuntimeError):
    """Startup schema / transition failure (fail closed)."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def phase_rank(phase: str | StartupPhase) -> int:
    key = phase.value if isinstance(phase, StartupPhase) else str(phase)
    if key in _TERMINAL_PHASES:
        return 1000
    return _PHASE_RANK.get(key, -1)


def is_terminal_phase(phase: str | StartupPhase) -> bool:
    key = phase.value if isinstance(phase, StartupPhase) else str(phase)
    return key in _TERMINAL_PHASES


def resolve_gate_phase(env: Mapping[str, str] | None = None) -> str:
    """Required phase for Team ``running`` (default ``task_dispatched``)."""
    source = env if env is not None else os.environ
    raw = str(source.get(GATE_PHASE_ENV) or "").strip()
    if not raw:
        return DEFAULT_GATE_PHASE
    if raw not in _GATE_PHASES:
        raise StartupError(
            f"{GATE_PHASE_ENV} must be one of "
            f"{sorted(_GATE_PHASES)}; got {raw!r}"
        )
    return raw


def meets_gate(
    phase: str | None,
    *,
    gate: str | None = None,
    phases: Sequence[str] | None = None,
) -> bool:
    """True when *phase* is at or beyond the required production gate.

    Rank alone is insufficient: ``task_dispatched`` / ``mailbox_ack`` also
    require prior ``provider_spawned`` and ``provider_ready`` in *phases*
    (blocks skip-to-dispatched forged receipts).
    """
    if not phase or is_terminal_phase(phase):
        return False
    required = gate or DEFAULT_GATE_PHASE
    if phase_rank(phase) < phase_rank(required):
        return False
    # Any gate at or beyond provider_ready needs a real spawn+ready history
    # when the current phase claims dispatch-or-later.
    history = [str(p) for p in (phases or ()) if str(p).strip()]
    needs_history = (
        phase_rank(phase) >= phase_rank(StartupPhase.TASK_DISPATCHED.value)
        or required
        in (
            StartupPhase.TASK_DISPATCHED.value,
            StartupPhase.MAILBOX_ACK.value,
        )
    )
    if needs_history:
        if StartupPhase.PROVIDER_SPAWNED.value not in history:
            return False
        if StartupPhase.PROVIDER_READY.value not in history:
            return False
    elif phase_rank(required) >= phase_rank(StartupPhase.PROVIDER_READY.value):
        # Gate is provider_ready: still require spawned in history.
        if StartupPhase.PROVIDER_SPAWNED.value not in history:
            return False
    return True


def provider_identity_distinct(
    *,
    provider_pid: int | None,
    supervisor_pid: int | None,
    provider_pid_start: str | None,
) -> bool:
    """Fail-closed: provider child must be distinct from supervisor."""
    if (
        not isinstance(provider_pid, int)
        or isinstance(provider_pid, bool)
        or provider_pid <= 0
    ):
        return False
    if (
        not isinstance(supervisor_pid, int)
        or isinstance(supervisor_pid, bool)
        or supervisor_pid <= 0
    ):
        return False
    if provider_pid == supervisor_pid:
        return False
    if not isinstance(provider_pid_start, str) or not provider_pid_start.strip():
        return False
    return True


@dataclass(frozen=True)
class StartupEvidence:
    code: str
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code}
        if self.detail:
            out["detail"] = self.detail[:_MAX_DIAG_LINE]
        return out


@dataclass
class StartupRecord:
    """Authoritative per-worker startup receipt (schema v2)."""

    run_id: str
    team_id: str
    worker_id: str
    phase: str
    provider: str
    schema_version: int = STARTUP_SCHEMA_VERSION
    writer: str = CLI_WRITER
    kind: str = STARTUP_KIND
    supervisor_pid: int | None = None
    provider_pid: int | None = None
    provider_pgid: int | None = None
    provider_pid_start: str | None = None
    evidence_code: str | None = None
    blocked_reason: str | None = None
    failure_reason: str | None = None
    observed_at: str = field(default_factory=_utc_now)
    phases: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # Deterministic key order via json.dumps(sort_keys=True) at write time.
        return payload


def worker_startup_path(
    root: Path | str, *, run_id: str, team_id: str, worker_id: str
) -> Path:
    rid = require_safe_id(run_id, label="run_id")
    tid = require_safe_id(team_id, label="team_id")
    wid = require_safe_id(worker_id, label="worker_id")
    return (
        Path(root).resolve()
        / ".omg"
        / "state"
        / "runs"
        / rid
        / "team"
        / safe_path_key(tid, namespace="team")
        / "workers"
        / safe_path_key(wid, namespace="worker")
        / READY_FILENAME
    )


def worker_diagnostics_path(
    root: Path | str, *, run_id: str, team_id: str, worker_id: str
) -> Path:
    return worker_startup_path(
        root, run_id=run_id, team_id=team_id, worker_id=worker_id
    ).with_name(DIAGNOSTICS_FILENAME)


def redact_diagnostics_line(line: str) -> str:
    text = (line or "").replace("\x00", "")[:_MAX_DIAG_LINE]
    return _SECRET_RE.sub("<redacted>", text)


def append_startup_diagnostics(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
    lines: Sequence[str],
) -> Path | None:
    """Append bounded redacted diagnostics (separate from receipt)."""
    if not lines:
        return None
    path = worker_diagnostics_path(
        root, run_id=run_id, team_id=team_id, worker_id=worker_id
    )
    ensure_managed_dir(path.parent)
    existing: list[str] = []
    if path.is_file():
        try:
            existing = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            existing = []
    for raw in lines:
        cleaned = redact_diagnostics_line(str(raw))
        if cleaned:
            existing.append(cleaned)
    trimmed = existing[-_MAX_DIAG_LINES:]
    body = ("\n".join(trimmed) + "\n").encode("utf-8")
    atomic_write_bytes(path, body, mode=DATA_FILE_MODE)
    return path


def _validate_identity(
    data: Mapping[str, Any],
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
) -> None:
    if data.get("writer") != CLI_WRITER:
        raise StartupError("startup receipt lacks CLI writer authority")
    if str(data.get("run_id") or "") != run_id:
        raise StartupError("startup receipt run_id mismatch")
    if str(data.get("team_id") or "") != team_id:
        raise StartupError("startup receipt team_id mismatch")
    if str(data.get("worker_id") or "") != worker_id:
        raise StartupError("startup receipt worker_id mismatch")


def _validate_transition(current: str | None, new_phase: str) -> None:
    if current is None:
        return
    if current == new_phase:
        return
    if is_terminal_phase(current):
        raise StartupError(
            f"startup phase is terminal ({current}); cannot move to {new_phase}"
        )
    if is_terminal_phase(new_phase):
        return
    if phase_rank(new_phase) < phase_rank(current):
        raise StartupError(
            f"non-monotonic startup transition {current} -> {new_phase}"
        )


def classify_startup_payload(
    data: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Classify a ready.json blob without mutating it.

    Returns keys: ``legacy``, ``phase``, ``evidence_code``, ``ok_for_gate``.
    """
    if not isinstance(data, Mapping):
        return {
            "legacy": False,
            "phase": None,
            "evidence_code": EvidenceCode.MALFORMED.value,
            "ok_for_gate": False,
        }
    if data.get("writer") != CLI_WRITER:
        return {
            "legacy": False,
            "phase": None,
            "evidence_code": EvidenceCode.MALFORMED.value,
            "ok_for_gate": False,
        }
    kind = str(data.get("kind") or "")
    version = data.get("schema_version")
    if kind == LEGACY_KIND or version == 1:
        return {
            "legacy": True,
            "phase": "wrapper_ready_legacy",
            "evidence_code": EvidenceCode.WRAPPER_READY_LEGACY.value,
            "ok_for_gate": False,
        }
    if kind != STARTUP_KIND or version != STARTUP_SCHEMA_VERSION:
        return {
            "legacy": False,
            "phase": None,
            "evidence_code": EvidenceCode.MALFORMED.value,
            "ok_for_gate": False,
        }
    phase = str(data.get("phase") or "")
    raw_phases = data.get("phases")
    phases_list: list[str] = []
    if isinstance(raw_phases, list):
        phases_list = [str(p) for p in raw_phases if str(p).strip()]
    identity_ok = provider_identity_distinct(
        provider_pid=data.get("provider_pid")
        if isinstance(data.get("provider_pid"), int)
        else None,
        supervisor_pid=data.get("supervisor_pid")
        if isinstance(data.get("supervisor_pid"), int)
        else None,
        provider_pid_start=(
            str(data["provider_pid_start"])
            if isinstance(data.get("provider_pid_start"), str)
            else None
        ),
    )
    return {
        "legacy": False,
        "phase": phase or None,
        "phases": phases_list,
        "evidence_code": data.get("evidence_code"),
        "ok_for_gate": bool(
            meets_gate(phase, phases=phases_list) and identity_ok
        ),
        "identity_ok": identity_ok,
        "blocked_reason": data.get("blocked_reason"),
        "failure_reason": data.get("failure_reason"),
        "provider": data.get("provider"),
        "provider_pid": data.get("provider_pid"),
        "provider_pgid": data.get("provider_pgid"),
        "provider_pid_start": data.get("provider_pid_start"),
        "supervisor_pid": data.get("supervisor_pid"),
    }


def read_startup_record(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
) -> dict[str, Any] | None:
    path = worker_startup_path(
        root, run_id=run_id, team_id=team_id, worker_id=worker_id
    )
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def write_startup_phase(
    root: Path | str,
    *,
    run_id: str,
    team_id: str,
    worker_id: str,
    phase: str | StartupPhase,
    provider: str,
    supervisor_pid: int | None = None,
    provider_pid: int | None = None,
    provider_pgid: int | None = None,
    provider_pid_start: str | None = None,
    evidence_code: str | EvidenceCode | None = None,
    blocked_reason: str | BlockedReason | None = None,
    failure_reason: str | None = None,
) -> Path:
    """Atomically write a monotonic schema-v2 startup receipt under worker lock."""
    rid = require_safe_id(run_id, label="run_id")
    tid = require_safe_id(team_id, label="team_id")
    wid = require_safe_id(worker_id, label="worker_id")
    phase_s = phase.value if isinstance(phase, StartupPhase) else str(phase)
    if phase_s not in {p.value for p in StartupPhase} and phase_s != "wrapper_ready_legacy":
        raise StartupError(f"unknown startup phase: {phase_s!r}")
    evidence_s = (
        evidence_code.value
        if isinstance(evidence_code, EvidenceCode)
        else (str(evidence_code) if evidence_code else None)
    )
    blocked_s = (
        blocked_reason.value
        if isinstance(blocked_reason, BlockedReason)
        else (str(blocked_reason) if blocked_reason else None)
    )
    path = worker_startup_path(root, run_id=rid, team_id=tid, worker_id=wid)
    ensure_managed_dir(path.parent)
    lock_path = path.parent / "startup.lock"

    with exclusive_lock(lock_path):
        existing = read_startup_record(
            root, run_id=rid, team_id=tid, worker_id=wid
        )
        phases: list[str] = []
        if existing is not None:
            classified = classify_startup_payload(existing)
            if classified.get("legacy"):
                # Do not rewrite v1 receipts in place; refuse to upgrade via
                # the same path so old Teams keep inspect/stop compatibility.
                raise StartupError(
                    "refusing to overwrite legacy v1 worker_ready receipt; "
                    "new launches must use a fresh worker directory"
                )
            _validate_identity(
                existing, run_id=rid, team_id=tid, worker_id=wid
            )
            current_phase = str(existing.get("phase") or "")
            _validate_transition(current_phase or None, phase_s)
            raw_phases = existing.get("phases")
            if isinstance(raw_phases, list):
                phases = [str(p) for p in raw_phases]
            # Preserve identity fields unless explicitly replaced.
            if supervisor_pid is None:
                sp = existing.get("supervisor_pid")
                supervisor_pid = int(sp) if isinstance(sp, int) else None
            if provider_pid is None:
                pp = existing.get("provider_pid")
                provider_pid = int(pp) if isinstance(pp, int) else None
            if provider_pgid is None:
                pg = existing.get("provider_pgid")
                provider_pgid = int(pg) if isinstance(pg, int) else None
            if provider_pid_start is None:
                ps = existing.get("provider_pid_start")
                provider_pid_start = str(ps) if isinstance(ps, str) else None
            if not provider:
                provider = str(existing.get("provider") or "unknown")

        if phase_s not in phases:
            phases.append(phase_s)

        record = StartupRecord(
            run_id=rid,
            team_id=tid,
            worker_id=wid,
            phase=phase_s,
            provider=str(provider or "unknown"),
            supervisor_pid=supervisor_pid,
            provider_pid=provider_pid,
            provider_pgid=provider_pgid,
            provider_pid_start=provider_pid_start,
            evidence_code=evidence_s,
            blocked_reason=blocked_s,
            failure_reason=(
                str(failure_reason)[:_MAX_DIAG_LINE] if failure_reason else None
            ),
            phases=phases,
        )
        body = (
            json.dumps(
                record.to_dict(), indent=2, ensure_ascii=False, sort_keys=True
            )
            + "\n"
        ).encode("utf-8")
        try:
            atomic_write_bytes(path, body, mode=DATA_FILE_MODE)
        except ContractPathError as exc:
            raise StartupError(f"startup receipt write refused: {exc}") from exc
    return path


def provider_process_alive(
    *,
    provider_pid: int | None,
    provider_pid_start: str | None,
    pid_start_fn=None,
) -> bool:
    """Exact live identity: PID alive and start identity still matches."""
    if (
        not isinstance(provider_pid, int)
        or isinstance(provider_pid, bool)
        or provider_pid <= 0
        or not provider_pid_start
    ):
        return False
    try:
        os.kill(provider_pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    probe = pid_start_fn
    if probe is None:
        from omg_cli.team.plane import _pid_start_identity

        probe = _pid_start_identity
    observed = probe(provider_pid)
    return bool(observed) and observed == provider_pid_start


__all__ = [
    "STARTUP_SCHEMA_VERSION",
    "STARTUP_KIND",
    "LEGACY_KIND",
    "READY_FILENAME",
    "DEFAULT_GATE_PHASE",
    "GATE_PHASE_ENV",
    "StartupPhase",
    "EvidenceCode",
    "BlockedReason",
    "StartupEvidence",
    "StartupRecord",
    "StartupError",
    "phase_rank",
    "is_terminal_phase",
    "resolve_gate_phase",
    "meets_gate",
    "provider_identity_distinct",
    "worker_startup_path",
    "worker_diagnostics_path",
    "redact_diagnostics_line",
    "append_startup_diagnostics",
    "classify_startup_payload",
    "read_startup_record",
    "write_startup_phase",
    "provider_process_alive",
]
