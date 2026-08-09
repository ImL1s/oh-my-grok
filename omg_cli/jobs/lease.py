"""Owner lease / heartbeat helpers for durable jobs (#68 PR4).

Lease TTL and heartbeat interval are internal constants — not public CLI flags.
"""

from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from omg_cli.jobs.models import JobRecord, JobState, JobStoreError

DEFAULT_JOB_HEARTBEAT_INTERVAL_S = 5.0
DEFAULT_JOB_LEASE_TTL_S = 30.0
JOB_RECOVERY_CLOCK_SKEW_S = 5.0
DEFAULT_STARTING_STALE_AFTER_S = 30.0

# Bound expires_at - heartbeat_at (must be positive and finite).
_MAX_LEASE_TTL_S = 24 * 3600.0

_OWNER_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


def generate_owner_token() -> str:
    """Return a 32-char lowercase hex owner token."""
    return secrets.token_hex(16)


def parse_lease_ts(raw: str | None, *, field: str = "timestamp") -> datetime:
    """Parse an RFC3339 / ISO-8601 timestamp; require timezone awareness."""
    if not isinstance(raw, str) or not raw.strip():
        raise JobStoreError(
            f"owner_lease.{field} missing or empty",
            code="E_JOB_LEASE_MALFORMED",
        )
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise JobStoreError(
            f"owner_lease.{field} is not a valid timestamp",
            code="E_JOB_LEASE_MALFORMED",
        ) from exc
    if ts.tzinfo is None:
        raise JobStoreError(
            f"owner_lease.{field} must be timezone-aware",
            code="E_JOB_LEASE_MALFORMED",
        )
    return ts.astimezone(timezone.utc)


def format_lease_ts(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).isoformat()


def validate_owner_lease(
    lease: Mapping[str, Any] | None,
    *,
    expected_attempt: int | None = None,
    require_active: bool = False,
) -> dict[str, Any]:
    """Strict nested owner_lease validation. Returns a normalized dict copy."""
    if lease is None:
        raise JobStoreError(
            "owner_lease is required",
            code="E_JOB_LEASE_MALFORMED",
        )
    if not isinstance(lease, Mapping):
        raise JobStoreError(
            "owner_lease must be an object",
            code="E_JOB_LEASE_MALFORMED",
        )
    try:
        schema = int(lease.get("schema", 0))
    except (TypeError, ValueError) as exc:
        raise JobStoreError(
            "owner_lease.schema invalid",
            code="E_JOB_LEASE_MALFORMED",
        ) from exc
    if schema != 1:
        raise JobStoreError(
            f"unsupported owner_lease.schema {schema!r}",
            code="E_JOB_LEASE_MALFORMED",
        )

    token = lease.get("owner_token")
    if not isinstance(token, str) or not _OWNER_TOKEN_RE.fullmatch(token):
        raise JobStoreError(
            "owner_lease.owner_token must be 32 lowercase hex characters",
            code="E_JOB_LEASE_MALFORMED",
        )

    try:
        attempt = int(lease.get("attempt", 0))
    except (TypeError, ValueError) as exc:
        raise JobStoreError(
            "owner_lease.attempt invalid",
            code="E_JOB_LEASE_MALFORMED",
        ) from exc
    if attempt < 1:
        raise JobStoreError(
            "owner_lease.attempt must be >= 1",
            code="E_JOB_LEASE_MALFORMED",
        )
    if expected_attempt is not None and attempt != int(expected_attempt):
        raise JobStoreError(
            "owner_lease.attempt does not match job attempt",
            code="E_JOB_LEASE_MALFORMED",
        )

    acquired = parse_lease_ts(lease.get("acquired_at"), field="acquired_at")
    heartbeat = parse_lease_ts(lease.get("heartbeat_at"), field="heartbeat_at")
    expires = parse_lease_ts(lease.get("expires_at"), field="expires_at")

    if acquired > heartbeat:
        raise JobStoreError(
            "owner_lease acquired_at must be <= heartbeat_at",
            code="E_JOB_LEASE_MALFORMED",
        )
    if heartbeat > expires:
        raise JobStoreError(
            "owner_lease heartbeat_at must be <= expires_at",
            code="E_JOB_LEASE_MALFORMED",
        )
    ttl = (expires - heartbeat).total_seconds()
    if ttl <= 0 or ttl > _MAX_LEASE_TTL_S:
        raise JobStoreError(
            "owner_lease expires_at - heartbeat_at must be positive and bounded",
            code="E_JOB_LEASE_MALFORMED",
        )

    released_raw = lease.get("released_at")
    if released_raw is None:
        released_at: str | None = None
    elif isinstance(released_raw, str) and released_raw.strip():
        released_at = format_lease_ts(
            parse_lease_ts(released_raw, field="released_at")
        )
    else:
        raise JobStoreError(
            "owner_lease.released_at must be null or a timestamp",
            code="E_JOB_LEASE_MALFORMED",
        )

    if require_active and released_at is not None:
        raise JobStoreError(
            "owner_lease is already released",
            code="E_JOB_LEASE_MALFORMED",
        )

    return {
        "schema": 1,
        "owner_token": token,
        "attempt": attempt,
        "acquired_at": format_lease_ts(acquired),
        "heartbeat_at": format_lease_ts(heartbeat),
        "expires_at": format_lease_ts(expires),
        "released_at": released_at,
    }


def validate_recovery_meta(raw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Validate optional recovery metadata block (additive schema-v1)."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise JobStoreError(
            "recovery must be an object",
            code="E_JOB_LEASE_MALFORMED",
        )
    last_action = raw.get("last_action")
    if last_action is not None and not isinstance(last_action, str):
        raise JobStoreError(
            "recovery.last_action must be a string or null",
            code="E_JOB_LEASE_MALFORMED",
        )
    last_reason = raw.get("last_reason")
    if last_reason is not None and not isinstance(last_reason, str):
        raise JobStoreError(
            "recovery.last_reason must be a string or null",
            code="E_JOB_LEASE_MALFORMED",
        )
    last_at = raw.get("last_at")
    if last_at is not None:
        if not isinstance(last_at, str):
            raise JobStoreError(
                "recovery.last_at must be a string or null",
                code="E_JOB_LEASE_MALFORMED",
            )
        parse_lease_ts(last_at, field="recovery.last_at")
    return {
        "last_action": last_action,
        "last_reason": last_reason,
        "last_at": last_at,
    }


def acquire_owner_lease(
    *,
    attempt: int,
    now: datetime | None = None,
    ttl_s: float = DEFAULT_JOB_LEASE_TTL_S,
    owner_token: str | None = None,
) -> dict[str, Any]:
    """Mint a fresh active owner lease for *attempt*."""
    if int(attempt) < 1:
        raise JobStoreError(
            "owner_lease.attempt must be >= 1",
            code="E_JOB_LEASE_MALFORMED",
        )
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    stamp = stamp.astimezone(timezone.utc)
    token = owner_token or generate_owner_token()
    if not _OWNER_TOKEN_RE.fullmatch(token):
        raise JobStoreError(
            "owner_lease.owner_token must be 32 lowercase hex characters",
            code="E_JOB_LEASE_MALFORMED",
        )
    expires = stamp + timedelta(seconds=float(ttl_s))
    lease = {
        "schema": 1,
        "owner_token": token,
        "attempt": int(attempt),
        "acquired_at": format_lease_ts(stamp),
        "heartbeat_at": format_lease_ts(stamp),
        "expires_at": format_lease_ts(expires),
        "released_at": None,
    }
    return validate_owner_lease(lease, expected_attempt=int(attempt), require_active=True)


def renew_lease_dict(
    lease: Mapping[str, Any],
    *,
    now: datetime | None = None,
    ttl_s: float = DEFAULT_JOB_LEASE_TTL_S,
) -> dict[str, Any]:
    """Advance heartbeat/expires on an active lease (same token/attempt)."""
    validated = validate_owner_lease(lease, require_active=True)
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    stamp = stamp.astimezone(timezone.utc)
    # Future heartbeat beyond skew → caller treats as unproven; still validate.
    validated["heartbeat_at"] = format_lease_ts(stamp)
    validated["expires_at"] = format_lease_ts(
        stamp + timedelta(seconds=float(ttl_s))
    )
    return validate_owner_lease(
        validated,
        expected_attempt=int(validated["attempt"]),
        require_active=True,
    )


def release_lease_dict(
    lease: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a released copy of *lease*, or None when no lease was present."""
    if lease is None:
        return None
    validated = validate_owner_lease(lease)
    if validated.get("released_at") is not None:
        return validated
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    validated["released_at"] = format_lease_ts(stamp.astimezone(timezone.utc))
    return validated


def public_lease_summary(lease: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Redacted lease view — never includes owner_token."""
    if lease is None:
        return None
    validated = validate_owner_lease(lease)
    return {
        "attempt": int(validated["attempt"]),
        "acquired_at": validated["acquired_at"],
        "heartbeat_at": validated["heartbeat_at"],
        "expires_at": validated["expires_at"],
        "released_at": validated.get("released_at"),
    }


def lease_is_active(lease: Mapping[str, Any] | None) -> bool:
    if lease is None:
        return False
    try:
        validated = validate_owner_lease(lease)
    except JobStoreError:
        return False
    return validated.get("released_at") is None


def assert_owner_fence(
    record: JobRecord,
    *,
    expected_attempt: int,
    expected_owner_token: str,
    expected_runner_pid: int,
) -> None:
    """Fail closed when the caller is not the fenced owner of a running job."""
    if record.state != JobState.RUNNING:
        raise JobStoreError(
            f"owner fence requires running state; got {record.state.value}",
            code="E_JOB_LEASE_FENCED",
        )
    if int(record.attempt) != int(expected_attempt):
        raise JobStoreError(
            "owner fence attempt mismatch",
            code="E_JOB_LEASE_FENCED",
        )
    if record.pid is None or int(record.pid) != int(expected_runner_pid):
        raise JobStoreError(
            "owner fence runner pid mismatch",
            code="E_JOB_LEASE_FENCED",
        )
    lease = record.owner_lease
    if not isinstance(lease, Mapping):
        raise JobStoreError(
            "owner fence missing owner_lease",
            code="E_JOB_LEASE_FENCED",
        )
    try:
        validated = validate_owner_lease(
            lease,
            expected_attempt=int(expected_attempt),
            require_active=True,
        )
    except JobStoreError as exc:
        if getattr(exc, "code", None) == "E_JOB_LEASE_MALFORMED":
            raise JobStoreError(
                "owner fence malformed lease",
                code="E_JOB_LEASE_FENCED",
            ) from exc
        raise
    if validated["owner_token"] != expected_owner_token:
        raise JobStoreError(
            "owner fence token mismatch",
            code="E_JOB_LEASE_FENCED",
        )


def lease_expired(
    lease: Mapping[str, Any],
    *,
    now: datetime,
    skew_s: float = JOB_RECOVERY_CLOCK_SKEW_S,
) -> bool:
    """True when recovery may consider the lease expired (includes skew grace)."""
    validated = validate_owner_lease(lease)
    if validated.get("released_at") is not None:
        return True
    expires = parse_lease_ts(validated["expires_at"], field="expires_at")
    stamp = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return stamp >= expires + timedelta(seconds=float(skew_s))


def heartbeat_in_future(
    lease: Mapping[str, Any],
    *,
    now: datetime,
    skew_s: float = JOB_RECOVERY_CLOCK_SKEW_S,
) -> bool:
    """True when heartbeat_at is beyond allowed clock skew into the future."""
    validated = validate_owner_lease(lease)
    hb = parse_lease_ts(validated["heartbeat_at"], field="heartbeat_at")
    stamp = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    return hb > stamp + timedelta(seconds=float(skew_s))


__all__ = [
    "DEFAULT_JOB_HEARTBEAT_INTERVAL_S",
    "DEFAULT_JOB_LEASE_TTL_S",
    "DEFAULT_STARTING_STALE_AFTER_S",
    "JOB_RECOVERY_CLOCK_SKEW_S",
    "acquire_owner_lease",
    "assert_owner_fence",
    "format_lease_ts",
    "generate_owner_token",
    "heartbeat_in_future",
    "lease_expired",
    "lease_is_active",
    "parse_lease_ts",
    "public_lease_summary",
    "release_lease_dict",
    "renew_lease_dict",
    "validate_owner_lease",
    "validate_recovery_meta",
]
