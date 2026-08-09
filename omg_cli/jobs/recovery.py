"""Lease observation + explicit recover reconciliation (#68 PR4).

Observation is read-only. Recovery marks ``lost`` only after OS identity proof;
reclaim is via explicit ``omg job retry --attempt current+1`` (no auto-retry).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from omg_cli.jobs.lease import (
    DEFAULT_STARTING_STALE_AFTER_S,
    format_lease_ts,
    heartbeat_in_future,
    lease_expired,
    parse_lease_ts,
    release_lease_dict,
    validate_owner_lease,
)
from omg_cli.jobs.models import TERMINAL_STATES, JobRecord, JobState, JobStoreError
from omg_cli.jobs.ownership import (
    IdentityProbeOutcome,
    ProcessIdentity,
    probe_identity_for_recovery,
)
from omg_cli.jobs.providers import ACP_SESSION_PROVIDER
from omg_cli.jobs.store import (
    compare_and_transition_job,
    list_job_ids,
    read_job_record,
    safe_job_id,
)

# Health values observed by status/list/wait/recover.
class JobHealth(str, Enum):
    QUEUED = "queued"
    STARTING_FRESH = "starting_fresh"
    RUNNING_HEALTHY = "running_healthy"
    RUNNING_CANCELLING = "running_cancelling"
    OWNER_MISSING_BEFORE_EXPIRY = "owner_missing_before_expiry"
    LEASE_STALE_LIVE = "lease_stale_live"
    RECOVERABLE_LOST = "recoverable_lost"
    ORPHAN_PROVIDER_LIVE = "orphan_provider_live"
    IDENTITY_UNPROVEN = "identity_unproven"
    LEGACY_UNMANAGED = "legacy_unmanaged"
    TERMINAL = "terminal"


RECOVERY_REQUIRED_HEALTH: frozenset[JobHealth] = frozenset(
    {
        JobHealth.LEASE_STALE_LIVE,
        JobHealth.RECOVERABLE_LOST,
        JobHealth.ORPHAN_PROVIDER_LIVE,
        JobHealth.IDENTITY_UNPROVEN,
        JobHealth.LEGACY_UNMANAGED,
    }
)

_ABSENT = "absent"
_LIVE = "live"
_GONE = "gone"
_REUSED = "reused"
_UNPROVEN = "unproven"


@dataclass(frozen=True, slots=True)
class JobObservation:
    health: JobHealth
    observed_at: str
    lease_expired: bool
    runner_identity: str
    provider_identity: str
    recoverable: bool
    recommended_action: str
    reason: str | None = None
    generation: int | None = None
    state: str | None = None
    attempt: int | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "health": self.health.value,
            "observed_at": self.observed_at,
            "lease_expired": bool(self.lease_expired),
            "runner_identity": self.runner_identity,
            "provider_identity": self.provider_identity,
            "recoverable": bool(self.recoverable),
            "recommended_action": self.recommended_action,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    ok: bool
    job_id: str
    before_state: str | None
    after_state: str | None
    action: str
    dry_run: bool
    observation: JobObservation | None
    error_code: str | None = None
    error_message: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": bool(self.ok),
            "job_id": self.job_id,
            "before_state": self.before_state,
            "after_state": self.after_state,
            "action": self.action,
            "dry_run": bool(self.dry_run),
            "observation": (
                self.observation.to_public_dict() if self.observation else None
            ),
        }
        if self.error_code:
            out["error_code"] = self.error_code
            out["error_message"] = self.error_message
        return out


@dataclass(frozen=True, slots=True)
class RecoveryBatchResult:
    ok: bool
    results: list[RecoveryResult]
    dry_run: bool

    @property
    def counts(self) -> dict[str, int]:
        blocked = sum(1 for r in self.results if not r.ok)
        marked = sum(1 for r in self.results if r.action in {"marked_lost", "would_mark_lost"})
        noop = sum(1 for r in self.results if r.ok and r.action.startswith("noop_"))
        return {
            "total": len(self.results),
            "ok": sum(1 for r in self.results if r.ok),
            "blocked": blocked,
            "marked_lost": marked,
            "noop": noop,
        }

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "ok": bool(self.ok),
            "dry_run": bool(self.dry_run),
            "results": [r.to_public_dict() for r in self.results],
            "counts": self.counts,
        }


def _now(now: datetime | None) -> datetime:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc)


def _identity_label(outcome: IdentityProbeOutcome | None) -> str:
    if outcome is None:
        return _ABSENT
    return outcome.value


def _probe_label(identity: ProcessIdentity | None) -> tuple[str, IdentityProbeOutcome | None]:
    if identity is None:
        return _ABSENT, None
    outcome = probe_identity_for_recovery(identity)
    return _identity_label(outcome), outcome


def _runner_identity_from_record(record: JobRecord) -> ProcessIdentity | None:
    if record.pid is None or record.pgid is None:
        return None
    try:
        return ProcessIdentity(
            pid=int(record.pid),
            pgid=int(record.pgid),
            pid_starttime=record.pid_starttime,
        )
    except (TypeError, ValueError):
        return None


def provider_launch_unbound(record: JobRecord) -> bool:
    """True when provider is launching without a complete durable PID/PGID.

    Shared by cancel / observe / recover / retry: the Popen→bind crash window
    may leave a live inner process group with no identity on the job record.
    Incomplete launching must never be treated as provider-absent.
    """
    pp = record.provider_process or {}
    if not isinstance(pp, Mapping):
        return False
    if pp.get("state") != "launching":
        return False
    return pp.get("pid") is None or pp.get("pgid") is None


def _provider_identity_from_record(record: JobRecord) -> ProcessIdentity | None:
    """Return durable provider identity, or None when absent/incomplete.

    Callers that need fail-closed launch-window semantics must also check
    :func:`provider_launch_unbound` — incomplete ``launching`` is *unproven*,
    not absent (``None`` alone would falsely recover to ``lost``).
    """
    pp = record.provider_process or {}
    if not isinstance(pp, Mapping):
        return None
    # Launching without complete PID/PGID is unproven (not absent) — identity
    # helpers return None; decide_observation / cancel / retry gate separately.
    if provider_launch_unbound(record):
        return None
    if pp.get("state") not in {"bound", "launching", "exited"}:
        # pending / unknown — no recorded inner identity
        if pp.get("pid") is None:
            return None
    pid = pp.get("pid")
    pgid = pp.get("pgid")
    if pid is None or pgid is None:
        return None
    try:
        return ProcessIdentity(
            pid=int(pid),
            pgid=int(pgid),
            pid_starttime=(
                str(pp["pid_starttime"]) if pp.get("pid_starttime") is not None else None
            ),
        )
    except (TypeError, ValueError):
        return None


def _spawn_identity_path(project_root: Path, job_id: str) -> Path:
    from omg_cli.jobs.store import job_dir

    return job_dir(project_root, job_id) / "spawn_identity.json"


def _spawn_uncertain_path(project_root: Path, job_id: str) -> Path:
    from omg_cli.jobs.store import job_dir

    return job_dir(project_root, job_id) / "spawn_uncertain.json"


def _read_spawn_identity(project_root: Path, job_id: str) -> ProcessIdentity | None:
    import json

    path = _spawn_identity_path(project_root, job_id)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, Mapping):
        return None
    pid = data.get("pid")
    pgid = data.get("pgid")
    if pid is None or pgid is None:
        return None
    try:
        return ProcessIdentity(
            pid=int(pid),
            pgid=int(pgid),
            pid_starttime=(
                str(data["pid_starttime"])
                if data.get("pid_starttime") is not None
                else None
            ),
        )
    except (TypeError, ValueError):
        return None


def _spawn_uncertain(project_root: Path, job_id: str) -> bool:
    return _spawn_uncertain_path(project_root, job_id).is_file()


def _starting_stale(record: JobRecord, now: datetime) -> bool:
    stamp_raw = record.updated_at or record.created_at
    try:
        stamp = parse_lease_ts(stamp_raw, field="updated_at")
    except JobStoreError:
        return True
    return now >= stamp + timedelta(seconds=DEFAULT_STARTING_STALE_AFTER_S)


def _obs(
    *,
    health: JobHealth,
    now: datetime,
    lease_is_expired: bool,
    runner: str,
    provider: str,
    recoverable: bool,
    action: str,
    reason: str | None,
    record: JobRecord,
) -> JobObservation:
    return JobObservation(
        health=health,
        observed_at=format_lease_ts(now),
        lease_expired=lease_is_expired,
        runner_identity=runner,
        provider_identity=provider,
        recoverable=recoverable,
        recommended_action=action,
        reason=reason,
        generation=int(record.generation),
        state=record.state.value,
        attempt=int(record.attempt),
    )


def decide_observation(
    record: JobRecord,
    *,
    project_root: Path,
    now: datetime | None = None,
    runner_outcome: IdentityProbeOutcome | None = None,
    provider_outcome: IdentityProbeOutcome | None = None,
    spawn_uncertain: bool | None = None,
    spawn_identity: ProcessIdentity | None = None,
) -> JobObservation:
    """Pure decision helper (tests may inject probe outcomes)."""
    stamp = _now(now)
    root = Path(project_root)

    if record.state in TERMINAL_STATES:
        return _obs(
            health=JobHealth.TERMINAL,
            now=stamp,
            lease_is_expired=False,
            runner=_ABSENT,
            provider=_ABSENT,
            recoverable=False,
            action="none",
            reason=None,
            record=record,
        )

    if record.state == JobState.QUEUED:
        return _obs(
            health=JobHealth.QUEUED,
            now=stamp,
            lease_is_expired=False,
            runner=_ABSENT,
            provider=_ABSENT,
            recoverable=False,
            action="none",
            reason=None,
            record=record,
        )

    uncertain = (
        bool(spawn_uncertain)
        if spawn_uncertain is not None
        else _spawn_uncertain(root, record.job_id)
    )

    runner_id = _runner_identity_from_record(record)
    if runner_id is None:
        runner_id = (
            spawn_identity
            if spawn_identity is not None
            else _read_spawn_identity(root, record.job_id)
        )
    provider_id = _provider_identity_from_record(record)

    if runner_outcome is None:
        runner_label, runner_outcome = _probe_label(runner_id)
    else:
        runner_label = _identity_label(runner_outcome) if runner_id or runner_outcome else _ABSENT
        if runner_id is None and runner_outcome is None:
            runner_label = _ABSENT
        elif runner_id is None and runner_outcome is not None:
            runner_label = _identity_label(runner_outcome)

    if provider_outcome is None:
        provider_label, provider_outcome = _probe_label(provider_id)
    else:
        provider_label = (
            _identity_label(provider_outcome) if provider_id or provider_outcome else _ABSENT
        )
        if provider_id is None and provider_outcome is None:
            provider_label = _ABSENT

    # Normalize absent when no identity was recorded.
    if runner_id is None and runner_outcome is None:
        runner_label = _ABSENT
    if provider_id is None and provider_outcome is None:
        provider_label = _ABSENT

    has_lease = record.owner_lease is not None
    lease_is_expired = False
    if has_lease:
        try:
            validate_owner_lease(record.owner_lease, expected_attempt=int(record.attempt))
            if heartbeat_in_future(record.owner_lease, now=stamp):
                return _obs(
                    health=JobHealth.IDENTITY_UNPROVEN,
                    now=stamp,
                    lease_is_expired=False,
                    runner=runner_label,
                    provider=provider_label,
                    recoverable=False,
                    action="none",
                    reason="future_heartbeat",
                    record=record,
                )
            lease_is_expired = lease_expired(record.owner_lease, now=stamp)
        except JobStoreError:
            return _obs(
                health=JobHealth.IDENTITY_UNPROVEN,
                now=stamp,
                lease_is_expired=False,
                runner=runner_label,
                provider=provider_label,
                recoverable=False,
                action="none",
                reason="lease_malformed",
                record=record,
            )

    if record.state == JobState.STARTING:
        if not _starting_stale(record, stamp):
            return _obs(
                health=JobHealth.STARTING_FRESH,
                now=stamp,
                lease_is_expired=False,
                runner=runner_label,
                provider=provider_label,
                recoverable=False,
                action="none",
                reason=None,
                record=record,
            )
        if uncertain:
            return _obs(
                health=JobHealth.IDENTITY_UNPROVEN,
                now=stamp,
                lease_is_expired=False,
                runner=runner_label,
                provider=provider_label,
                recoverable=False,
                action="none",
                reason="spawn_uncertain",
                record=record,
            )
        if runner_id is None:
            return _obs(
                health=JobHealth.IDENTITY_UNPROVEN,
                now=stamp,
                lease_is_expired=False,
                runner=_ABSENT,
                provider=provider_label,
                recoverable=False,
                action="none",
                reason="starting_no_identity",
                record=record,
            )
        if runner_outcome is IdentityProbeOutcome.LIVE:
            return _obs(
                health=JobHealth.LEASE_STALE_LIVE,
                now=stamp,
                lease_is_expired=True,
                runner=runner_label,
                provider=provider_label,
                recoverable=False,
                action="cancel",
                reason="starting_runner_live",
                record=record,
            )
        if runner_outcome is IdentityProbeOutcome.UNPROVEN:
            return _obs(
                health=JobHealth.IDENTITY_UNPROVEN,
                now=stamp,
                lease_is_expired=False,
                runner=runner_label,
                provider=provider_label,
                recoverable=False,
                action="none",
                reason="starting_runner_unproven",
                record=record,
            )
        # GONE / REUSED (and provider absent/gone/reused)
        if provider_outcome is IdentityProbeOutcome.LIVE:
            return _obs(
                health=JobHealth.ORPHAN_PROVIDER_LIVE,
                now=stamp,
                lease_is_expired=True,
                runner=runner_label,
                provider=provider_label,
                recoverable=False,
                action="cancel",
                reason="starting_orphan_provider",
                record=record,
            )
        if provider_outcome is IdentityProbeOutcome.UNPROVEN:
            return _obs(
                health=JobHealth.IDENTITY_UNPROVEN,
                now=stamp,
                lease_is_expired=False,
                runner=runner_label,
                provider=provider_label,
                recoverable=False,
                action="none",
                reason="starting_provider_unproven",
                record=record,
            )
        # Launching without durable PID/PGID is unproven — never recoverable_lost.
        if provider_launch_unbound(record):
            return _obs(
                health=JobHealth.IDENTITY_UNPROVEN,
                now=stamp,
                lease_is_expired=True,
                runner=runner_label,
                provider=_UNPROVEN,
                recoverable=False,
                action="none",
                reason="provider_launch_unbound",
                record=record,
            )
        return _obs(
            health=JobHealth.RECOVERABLE_LOST,
            now=stamp,
            lease_is_expired=True,
            runner=runner_label,
            provider=provider_label,
            recoverable=True,
            action="recover",
            reason="starting_identities_gone",
            record=record,
        )

    # RUNNING
    if record.state == JobState.RUNNING:
        if record.cancel_requested_at:
            return _obs(
                health=JobHealth.RUNNING_CANCELLING,
                now=stamp,
                lease_is_expired=lease_is_expired,
                runner=runner_label,
                provider=provider_label,
                recoverable=False,
                action="none",
                reason="cancel_requested",
                record=record,
            )

        if not has_lease:
            # Legacy unmanaged active record.
            if uncertain:
                return _obs(
                    health=JobHealth.IDENTITY_UNPROVEN,
                    now=stamp,
                    lease_is_expired=False,
                    runner=runner_label,
                    provider=provider_label,
                    recoverable=False,
                    action="none",
                    reason="legacy_spawn_uncertain",
                    record=record,
                )
            if runner_outcome is IdentityProbeOutcome.LIVE or (
                provider_outcome is IdentityProbeOutcome.LIVE
            ):
                return _obs(
                    health=JobHealth.LEGACY_UNMANAGED,
                    now=stamp,
                    lease_is_expired=False,
                    runner=runner_label,
                    provider=provider_label,
                    recoverable=False,
                    action="none",
                    reason="legacy_live",
                    record=record,
                )
            if (
                runner_outcome is IdentityProbeOutcome.UNPROVEN
                or provider_outcome is IdentityProbeOutcome.UNPROVEN
            ):
                return _obs(
                    health=JobHealth.IDENTITY_UNPROVEN,
                    now=stamp,
                    lease_is_expired=False,
                    runner=runner_label,
                    provider=provider_label,
                    recoverable=False,
                    action="none",
                    reason="legacy_unproven",
                    record=record,
                )
            # Gone/reused/absent — recoverable after disappearance proof.
            if runner_id is None and provider_id is None:
                # No identity at all on legacy running — unproven.
                return _obs(
                    health=JobHealth.IDENTITY_UNPROVEN,
                    now=stamp,
                    lease_is_expired=False,
                    runner=_ABSENT,
                    provider=_ABSENT,
                    recoverable=False,
                    action="none",
                    reason="legacy_no_identity",
                    record=record,
                )
            if provider_launch_unbound(record):
                return _obs(
                    health=JobHealth.IDENTITY_UNPROVEN,
                    now=stamp,
                    lease_is_expired=True,
                    runner=runner_label,
                    provider=_UNPROVEN,
                    recoverable=False,
                    action="none",
                    reason="provider_launch_unbound",
                    record=record,
                )
            return _obs(
                health=JobHealth.RECOVERABLE_LOST,
                now=stamp,
                lease_is_expired=True,
                runner=runner_label,
                provider=provider_label,
                recoverable=True,
                action="recover",
                reason="legacy_identities_gone",
                record=record,
            )

        # Managed lease path
        if not lease_is_expired:
            if runner_outcome is IdentityProbeOutcome.LIVE and (
                provider_outcome in (None, IdentityProbeOutcome.GONE, IdentityProbeOutcome.REUSED)
                or provider_label == _ABSENT
                or provider_outcome is IdentityProbeOutcome.LIVE
            ):
                if runner_outcome is IdentityProbeOutcome.LIVE:
                    # Runner live + lease valid → healthy (provider may still be running).
                    if provider_outcome is IdentityProbeOutcome.UNPROVEN:
                        return _obs(
                            health=JobHealth.IDENTITY_UNPROVEN,
                            now=stamp,
                            lease_is_expired=False,
                            runner=runner_label,
                            provider=provider_label,
                            recoverable=False,
                            action="none",
                            reason="provider_unproven",
                            record=record,
                        )
                    return _obs(
                        health=JobHealth.RUNNING_HEALTHY,
                        now=stamp,
                        lease_is_expired=False,
                        runner=runner_label,
                        provider=provider_label,
                        recoverable=False,
                        action="none",
                        reason=None,
                        record=record,
                    )
            if runner_outcome in (
                IdentityProbeOutcome.GONE,
                IdentityProbeOutcome.REUSED,
            ) or runner_label == _ABSENT:
                if provider_outcome is IdentityProbeOutcome.UNPROVEN:
                    return _obs(
                        health=JobHealth.IDENTITY_UNPROVEN,
                        now=stamp,
                        lease_is_expired=False,
                        runner=runner_label,
                        provider=provider_label,
                        recoverable=False,
                        action="none",
                        reason="owner_missing_provider_unproven",
                        record=record,
                    )
                return _obs(
                    health=JobHealth.OWNER_MISSING_BEFORE_EXPIRY,
                    now=stamp,
                    lease_is_expired=False,
                    runner=runner_label,
                    provider=provider_label,
                    recoverable=False,
                    action="none",
                    reason="owner_missing_before_expiry",
                    record=record,
                )
            if runner_outcome is IdentityProbeOutcome.UNPROVEN:
                return _obs(
                    health=JobHealth.IDENTITY_UNPROVEN,
                    now=stamp,
                    lease_is_expired=False,
                    runner=runner_label,
                    provider=provider_label,
                    recoverable=False,
                    action="none",
                    reason="runner_unproven",
                    record=record,
                )
            # Fallback
            return _obs(
                health=JobHealth.IDENTITY_UNPROVEN,
                now=stamp,
                lease_is_expired=False,
                runner=runner_label,
                provider=provider_label,
                recoverable=False,
                action="none",
                reason="pre_expiry_ambiguous",
                record=record,
            )

        # Lease expired (+ skew)
        if uncertain:
            return _obs(
                health=JobHealth.IDENTITY_UNPROVEN,
                now=stamp,
                lease_is_expired=True,
                runner=runner_label,
                provider=provider_label,
                recoverable=False,
                action="none",
                reason="spawn_uncertain",
                record=record,
            )
        if runner_outcome is IdentityProbeOutcome.LIVE:
            return _obs(
                health=JobHealth.LEASE_STALE_LIVE,
                now=stamp,
                lease_is_expired=True,
                runner=runner_label,
                provider=provider_label,
                recoverable=False,
                action="cancel",
                reason="expired_runner_live",
                record=record,
            )
        if runner_outcome is IdentityProbeOutcome.UNPROVEN:
            return _obs(
                health=JobHealth.IDENTITY_UNPROVEN,
                now=stamp,
                lease_is_expired=True,
                runner=runner_label,
                provider=provider_label,
                recoverable=False,
                action="none",
                reason="expired_runner_unproven",
                record=record,
            )
        if provider_outcome is IdentityProbeOutcome.LIVE:
            return _obs(
                health=JobHealth.ORPHAN_PROVIDER_LIVE,
                now=stamp,
                lease_is_expired=True,
                runner=runner_label,
                provider=provider_label,
                recoverable=False,
                action="cancel",
                reason="orphan_provider_live",
                record=record,
            )
        if provider_outcome is IdentityProbeOutcome.UNPROVEN:
            return _obs(
                health=JobHealth.IDENTITY_UNPROVEN,
                now=stamp,
                lease_is_expired=True,
                runner=runner_label,
                provider=provider_label,
                recoverable=False,
                action="none",
                reason="expired_provider_unproven",
                record=record,
            )
        # Crash window: launching with no durable PID/PGID — cancel parity.
        # Outer may be GONE but an orphan provider can still exist; never lost.
        if provider_launch_unbound(record):
            return _obs(
                health=JobHealth.IDENTITY_UNPROVEN,
                now=stamp,
                lease_is_expired=True,
                runner=runner_label,
                provider=_UNPROVEN,
                recoverable=False,
                action="none",
                reason="provider_launch_unbound",
                record=record,
            )
        return _obs(
            health=JobHealth.RECOVERABLE_LOST,
            now=stamp,
            lease_is_expired=True,
            runner=runner_label,
            provider=provider_label,
            recoverable=True,
            action="recover",
            reason="expired_identities_gone",
            record=record,
        )

    return _obs(
        health=JobHealth.IDENTITY_UNPROVEN,
        now=stamp,
        lease_is_expired=False,
        runner=_ABSENT,
        provider=_ABSENT,
        recoverable=False,
        action="none",
        reason=f"unexpected_state:{record.state.value}",
        record=record,
    )


def observe_job(
    project_root: Path,
    job_id: str,
    *,
    now: datetime | None = None,
) -> JobObservation:
    """Read-only durable + OS observation. Never mutates or signals."""
    record = read_job_record(project_root, safe_job_id(job_id))
    return decide_observation(record, project_root=project_root, now=now)


def _blocked(
    *,
    job_id: str,
    before: str | None,
    action: str,
    dry_run: bool,
    observation: JobObservation | None,
    code: str,
    message: str,
) -> RecoveryResult:
    return RecoveryResult(
        ok=False,
        job_id=job_id,
        before_state=before,
        after_state=before,
        action=action,
        dry_run=dry_run,
        observation=observation,
        error_code=code,
        error_message=message,
    )


def _ok_result(
    *,
    job_id: str,
    before: str | None,
    after: str | None,
    action: str,
    dry_run: bool,
    observation: JobObservation | None,
) -> RecoveryResult:
    return RecoveryResult(
        ok=True,
        job_id=job_id,
        before_state=before,
        after_state=after,
        action=action,
        dry_run=dry_run,
        observation=observation,
    )


def recover_job(
    project_root: Path,
    job_id: str,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    allow_internal: bool = False,
) -> RecoveryResult:
    """CAS-reconcile one stale active job; never launches or retries."""
    from omg_cli.jobs.retry import classified_terminal_updates

    root = Path(project_root)
    jid = safe_job_id(job_id)
    stamp = _now(now)

    # 1. Immutable snapshot (no lock held during probes).
    try:
        snapshot = read_job_record(root, jid)
    except JobStoreError as exc:
        if getattr(exc, "code", None) == "E_JOB_LEASE_MALFORMED":
            return _blocked(
                job_id=jid,
                before=None,
                action="blocked",
                dry_run=dry_run,
                observation=None,
                code="E_JOB_LEASE_MALFORMED",
                message=str(exc),
            )
        raise

    if snapshot.provider == ACP_SESSION_PROVIDER and not allow_internal:
        obs = decide_observation(snapshot, project_root=root, now=stamp)
        return _blocked(
            job_id=jid,
            before=snapshot.state.value,
            action="protected_internal",
            dry_run=dry_run,
            observation=obs,
            code="E_JOB_PROVIDER_INTERNAL",
            message=(
                f"job provider {ACP_SESSION_PROVIDER!r} cannot be recovered "
                "via public CLI"
            ),
        )

    if snapshot.state in TERMINAL_STATES:
        obs = decide_observation(snapshot, project_root=root, now=stamp)
        return _ok_result(
            job_id=jid,
            before=snapshot.state.value,
            after=snapshot.state.value,
            action="noop_terminal",
            dry_run=dry_run,
            observation=obs,
        )

    if snapshot.state == JobState.QUEUED:
        obs = decide_observation(snapshot, project_root=root, now=stamp)
        return _ok_result(
            job_id=jid,
            before=snapshot.state.value,
            after=snapshot.state.value,
            action="noop_not_active",
            dry_run=dry_run,
            observation=obs,
        )

    # 3. Probe outside lock.
    try:
        observation = decide_observation(snapshot, project_root=root, now=stamp)
    except JobStoreError as exc:
        code = getattr(exc, "code", None) or "E_JOB_LEASE_MALFORMED"
        return _blocked(
            job_id=jid,
            before=snapshot.state.value,
            action="blocked",
            dry_run=dry_run,
            observation=None,
            code=code,
            message=str(exc),
        )

    if observation.health == JobHealth.RUNNING_HEALTHY:
        return _ok_result(
            job_id=jid,
            before=snapshot.state.value,
            after=snapshot.state.value,
            action="noop_healthy",
            dry_run=dry_run,
            observation=observation,
        )

    if observation.health in {
        JobHealth.QUEUED,
        JobHealth.STARTING_FRESH,
        JobHealth.RUNNING_CANCELLING,
        JobHealth.OWNER_MISSING_BEFORE_EXPIRY,
    }:
        return _ok_result(
            job_id=jid,
            before=snapshot.state.value,
            after=snapshot.state.value,
            action="noop_not_active" if observation.health == JobHealth.QUEUED else "noop_healthy",
            dry_run=dry_run,
            observation=observation,
        )

    if observation.health == JobHealth.IDENTITY_UNPROVEN:
        return _blocked(
            job_id=jid,
            before=snapshot.state.value,
            action="blocked",
            dry_run=dry_run,
            observation=observation,
            code="E_JOB_RECOVERY_UNPROVEN",
            message="identity probe unproven; refusing recovery mutation",
        )

    if observation.health == JobHealth.LEASE_STALE_LIVE:
        return _blocked(
            job_id=jid,
            before=snapshot.state.value,
            action="blocked",
            dry_run=dry_run,
            observation=observation,
            code="E_JOB_RECOVERY_UNPROVEN",
            message="lease expired but runner still live; use cancel if needed",
        )

    if observation.health == JobHealth.ORPHAN_PROVIDER_LIVE:
        return _blocked(
            job_id=jid,
            before=snapshot.state.value,
            action="blocked",
            dry_run=dry_run,
            observation=observation,
            code="E_JOB_RECOVERY_ORPHAN_LIVE",
            message="outer gone but provider still live; use omg job cancel",
        )

    if observation.health == JobHealth.LEGACY_UNMANAGED:
        return _blocked(
            job_id=jid,
            before=snapshot.state.value,
            action="blocked",
            dry_run=dry_run,
            observation=observation,
            code="E_JOB_RECOVERY_UNPROVEN",
            message="legacy unmanaged live job; refusing to mint or steal lease",
        )

    if observation.health != JobHealth.RECOVERABLE_LOST:
        return _blocked(
            job_id=jid,
            before=snapshot.state.value,
            action="blocked",
            dry_run=dry_run,
            observation=observation,
            code="E_JOB_RECOVERY_UNPROVEN",
            message=f"health={observation.health.value} is not recoverable",
        )

    if dry_run:
        return _ok_result(
            job_id=jid,
            before=snapshot.state.value,
            after=JobState.LOST.value,
            action="would_mark_lost",
            dry_run=True,
            observation=observation,
        )

    # 4–7. CAS under single job lock.
    reason = observation.reason or "lease_lost"
    released = release_lease_dict(snapshot.owner_lease, now=stamp)
    updates = classified_terminal_updates(
        state=JobState.LOST,
        exit_obj={
            "class": "lease_lost",
            "ok": False,
            "cancelled": False,
            "timed_out": False,
            "retryable": False,
        },
        error_message=(
            "owner lease expired and recorded process identities are absent"
        ),
        last_observed_at=format_lease_ts(stamp),
        recovery={
            "last_action": "marked_lost",
            "last_reason": reason,
            "last_at": format_lease_ts(stamp),
        },
        owner_lease=released,
    )

    expected_lease_token = None
    if isinstance(snapshot.owner_lease, Mapping):
        expected_lease_token = snapshot.owner_lease.get("owner_token")

    try:
        after = compare_and_transition_job(
            root,
            jid,
            JobState.LOST,
            expected_generation=int(snapshot.generation),
            expected_state=snapshot.state,
            expected_attempt=int(snapshot.attempt),
            expected_owner_token=expected_lease_token,
            expected_pid=snapshot.pid,
            expected_pgid=snapshot.pgid,
            updates=updates,
        )
    except JobStoreError as exc:
        code = getattr(exc, "code", None) or "E_JOB_RECOVERY_CONFLICT"
        if code in {"E_JOB_TRANSITION", "E_JOB_STORE"}:
            code = "E_JOB_RECOVERY_CONFLICT"
        if code == "E_JOB_LEASE_FENCED":
            code = "E_JOB_RECOVERY_CONFLICT"
        return _blocked(
            job_id=jid,
            before=snapshot.state.value,
            action="blocked",
            dry_run=False,
            observation=observation,
            code=code if code.startswith("E_JOB_") else "E_JOB_RECOVERY_CONFLICT",
            message=str(exc),
        )

    return _ok_result(
        job_id=jid,
        before=snapshot.state.value,
        after=after.state.value,
        action="marked_lost",
        dry_run=False,
        observation=observation,
    )


def recover_jobs(
    project_root: Path,
    *,
    run_id: str | None = None,
    provider: str | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
    allow_internal: bool = False,
) -> RecoveryBatchResult:
    """Sorted, one-lock-at-a-time active-job reconciliation."""
    root = Path(project_root)
    results: list[RecoveryResult] = []
    for jid in list_job_ids(root):
        try:
            rec = read_job_record(root, jid)
        except JobStoreError as exc:
            results.append(
                _blocked(
                    job_id=jid,
                    before=None,
                    action="blocked",
                    dry_run=dry_run,
                    observation=None,
                    code=getattr(exc, "code", None) or "E_JOB_MALFORMED",
                    message=str(exc),
                )
            )
            continue
        if rec.state in TERMINAL_STATES or rec.state == JobState.QUEUED:
            continue
        if run_id is not None and (rec.run_id or "") != run_id:
            continue
        if provider is not None and rec.provider != provider:
            continue
        if rec.provider == ACP_SESSION_PROVIDER and not allow_internal:
            obs = decide_observation(rec, project_root=root, now=now)
            results.append(
                _blocked(
                    job_id=jid,
                    before=rec.state.value,
                    action="protected_internal",
                    dry_run=dry_run,
                    observation=obs,
                    code="E_JOB_PROVIDER_INTERNAL",
                    message="protected_internal",
                )
            )
            continue
        results.append(
            recover_job(
                root,
                jid,
                dry_run=dry_run,
                now=now,
                allow_internal=allow_internal,
            )
        )

    ok = all(r.ok for r in results)
    return RecoveryBatchResult(ok=ok, results=results, dry_run=dry_run)


__all__ = [
    "JobHealth",
    "JobObservation",
    "RECOVERY_REQUIRED_HEALTH",
    "RecoveryBatchResult",
    "RecoveryResult",
    "decide_observation",
    "observe_job",
    "provider_launch_unbound",
    "recover_job",
    "recover_jobs",
]
