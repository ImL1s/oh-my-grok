"""Pinned offline GitHub issue-state evidence for closure-sensitive claims.

This is bounded release-time observation, not perpetual live GitHub truth.
Production ``--strict`` consumes the committed receipt; tests must not be
the only place that knows ``#67`` / ``#68`` / ``#78`` are closure-sensitive.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.parity_schema import load_json_object
from omg_cli.contracts.state_schemas import (
    ContractValidationError,
    require_git_oid,
    require_integer,
    require_iso8601,
    require_nonempty_string,
    require_object,
    require_sha256,
)
from omg_cli.parity_completeness import canonical_json_digest

ISSUE_STATE_EVIDENCE_RELATIVE = "docs/parity/issue-state/v1.json"
ISSUE_STATE_STORE_KIND = "parity-issue-state-evidence"
ISSUE_STATE_SCHEMA_VERSION = 1
# Existing canonical representation already used by docs/parity/issue-state/v1.json.
CANONICAL_ISSUE_STATE_HOST = "github.com"
CANONICAL_ISSUE_STATE_OWNER = "ImL1s"
CANONICAL_ISSUE_STATE_NAME = "oh-my-grok"
CANONICAL_ISSUE_STATE_HTML_URL = "https://github.com/ImL1s/oh-my-grok"
_ISSUE_KEY_RE = re.compile(r"^#([1-9][0-9]*)$")
FRESHNESS_SEMANTICS = frozenset({"release_pin", "ttl"})
CLOSED_STATES = frozenset({"closed"})
OPEN_STATES = frozenset({"open"})

__all__ = [
    "CANONICAL_ISSUE_STATE_HOST",
    "CANONICAL_ISSUE_STATE_HTML_URL",
    "CANONICAL_ISSUE_STATE_NAME",
    "CANONICAL_ISSUE_STATE_OWNER",
    "ISSUE_STATE_EVIDENCE_RELATIVE",
    "ISSUE_STATE_SCHEMA_VERSION",
    "ISSUE_STATE_STORE_KIND",
    "check_issue_state_evidence",
    "load_and_validate_issue_state_evidence",
]


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_iso8601(value: Any, *, label: str) -> datetime:
    text = require_iso8601(str(value) if value is not None else "", label=label)
    parsed = datetime.fromisoformat(
        text[:-1] + "+00:00" if text.endswith("Z") else text
    )
    return _as_utc(parsed)


def _issue_state_digest(payload: dict[str, Any]) -> str:
    body = {key: value for key, value in payload.items() if key != "content_digest"}
    return canonical_json_digest(body)


def load_and_validate_issue_state_evidence(
    path: Path | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Load and fail-closed-validate one issue-state receipt (offline)."""
    evidence_path = Path(path)
    if not evidence_path.is_file():
        raise ContractValidationError(
            f"issue-state evidence missing: {evidence_path.as_posix()}"
        )
    raw = load_json_object(evidence_path)
    if raw.get("store_kind") != ISSUE_STATE_STORE_KIND:
        raise ContractValidationError(
            f"unknown issue-state store_kind: {raw.get('store_kind')!r}"
        )
    schema_version = raw.get("schema_version")
    # bool is a subclass of int; True == 1 must not authorize.
    if isinstance(schema_version, bool) or schema_version != ISSUE_STATE_SCHEMA_VERSION:
        raise ContractValidationError(
            f"unknown issue-state schema_version: {schema_version!r}"
        )
    if raw.get("repository_id") != "OMG":
        raise ContractValidationError("issue-state repository_id must be OMG")

    source = require_object(raw.get("source"), label="issue-state.source")
    host = require_nonempty_string(source.get("host"), label="issue-state.source.host")
    owner = require_nonempty_string(
        source.get("owner"), label="issue-state.source.owner"
    )
    name = require_nonempty_string(source.get("name"), label="issue-state.source.name")
    html_url = require_nonempty_string(
        source.get("html_url"), label="issue-state.source.html_url"
    )
    if host != CANONICAL_ISSUE_STATE_HOST:
        raise ContractValidationError(
            f"issue-state source.host must be {CANONICAL_ISSUE_STATE_HOST!r}, "
            f"got {host!r}"
        )
    if owner != CANONICAL_ISSUE_STATE_OWNER:
        raise ContractValidationError(
            f"issue-state source.owner must be {CANONICAL_ISSUE_STATE_OWNER!r}, "
            f"got {owner!r}"
        )
    if name != CANONICAL_ISSUE_STATE_NAME:
        raise ContractValidationError(
            f"issue-state source.name must be {CANONICAL_ISSUE_STATE_NAME!r}, "
            f"got {name!r}"
        )
    if html_url != CANONICAL_ISSUE_STATE_HTML_URL:
        raise ContractValidationError(
            f"issue-state source.html_url must be {CANONICAL_ISSUE_STATE_HTML_URL!r}, "
            f"got {html_url!r}"
        )
    require_git_oid(
        source.get("observed_git_commit"),
        label="issue-state.source.observed_git_commit",
    )
    observed_at = source.get("observed_at")
    if not observed_at:
        raise ContractValidationError("issue-state observed_at missing (stale)")
    observed_dt = _parse_iso8601(observed_at, label="issue-state.source.observed_at")

    freshness = require_object(raw.get("freshness"), label="issue-state.freshness")
    semantics = require_nonempty_string(
        freshness.get("semantics"), label="issue-state.freshness.semantics"
    )
    if semantics not in FRESHNESS_SEMANTICS:
        raise ContractValidationError(
            f"unknown issue-state freshness.semantics: {semantics!r}"
        )
    if semantics == "ttl":
        max_age = require_integer(
            freshness.get("max_age_days"),
            label="issue-state freshness.max_age_days",
            minimum=1,
        )
        clock = _as_utc(now or datetime.now(timezone.utc))
        age_days = (clock - observed_dt).total_seconds() / 86400.0
        if age_days > max_age:
            raise ContractValidationError("issue-state evidence stale")
    elif freshness.get("max_age_days") is not None:
        raise ContractValidationError(
            "issue-state freshness.max_age_days only valid for ttl"
        )

    digest = require_sha256(raw.get("content_digest"), label="issue-state.content_digest")
    expected = _issue_state_digest(raw)
    if digest != expected:
        raise ContractValidationError("issue-state content_digest tampered")

    sensitive = raw.get("closure_sensitive")
    if not isinstance(sensitive, list) or not sensitive:
        raise ContractValidationError("issue-state closure_sensitive must be non-empty")
    if not all(
        isinstance(item, str) and _ISSUE_KEY_RE.fullmatch(item) for item in sensitive
    ):
        raise ContractValidationError("issue-state closure_sensitive must be #N ids")

    issues = require_object(raw.get("issues"), label="issue-state.issues")
    for issue_id, raw_row in issues.items():
        row = require_object(raw_row, label=f"issue-state.issues.{issue_id}")
        _validate_issue_identity(issue_id, row)
    for issue_id in sensitive:
        if issue_id not in issues:
            raise ContractValidationError(
                f"issue-state missing closure-sensitive {issue_id}"
            )
        row = require_object(issues[issue_id], label=f"issue-state.issues.{issue_id}")
        _validate_issue_row(issue_id, row)
    return raw


def _canonical_issue_number(issue_id: str) -> int:
    match = _ISSUE_KEY_RE.fullmatch(issue_id)
    if match is None:
        raise ContractValidationError(
            f"issue-state issues key is not canonical #N: {issue_id!r}"
        )
    return int(match.group(1))


def _validate_issue_identity(issue_id: str, row: dict[str, Any]) -> int:
    expected_number = _canonical_issue_number(issue_id)
    number = require_integer(
        row.get("number"), label=f"{issue_id}.number", minimum=1
    )
    if number != expected_number:
        raise ContractValidationError(
            f"{issue_id} number mismatch: expected {expected_number}, got {number!r}"
        )
    expected_url = f"{CANONICAL_ISSUE_STATE_HTML_URL}/issues/{expected_number}"
    url = require_nonempty_string(row.get("url"), label=f"{issue_id}.url")
    if url != expected_url:
        raise ContractValidationError(
            f"{issue_id} url must be {expected_url!r}, got {url!r}"
        )
    return expected_number


def _validate_issue_row(issue_id: str, row: dict[str, Any]) -> None:
    _validate_issue_identity(issue_id, row)
    require_nonempty_string(row.get("issue_node_id"), label=f"{issue_id}.issue_node_id")
    state = require_nonempty_string(
        row.get("observed_state"), label=f"{issue_id}.observed_state"
    )
    if row.get("blocks_open_p0") is not True:
        raise ContractValidationError(f"{issue_id} blocks_open_p0 must be true")
    close_event = require_object(row.get("close_event"), label=f"{issue_id}.close_event")
    if close_event.get("id") in (None, "") or not close_event.get("node_id"):
        raise ContractValidationError(f"{issue_id} close_event identity missing")
    close_at = _parse_iso8601(
        close_event.get("created_at"), label=f"{issue_id}.close_event.created_at"
    )
    reopen = row.get("reopen_event")
    reopen_at = None
    if reopen is not None:
        reopen_obj = require_object(reopen, label=f"{issue_id}.reopen_event")
        if reopen_obj.get("id") in (None, "") or not reopen_obj.get("node_id"):
            raise ContractValidationError(f"{issue_id} reopen_event identity missing")
        reopen_at = _parse_iso8601(
            reopen_obj.get("created_at"), label=f"{issue_id}.reopen_event.created_at"
        )

    if state in CLOSED_STATES:
        require_nonempty_string(row.get("closed_at"), label=f"{issue_id}.closed_at")
        if reopen_at is not None and close_at <= reopen_at:
            raise ContractValidationError(
                f"{issue_id} observed_state closed without close_event after reopen"
            )
        return

    if state not in OPEN_STATES:
        raise ContractValidationError(f"{issue_id} unknown observed_state: {state!r}")
    # An earlier close without a later reopen cannot stay "open" (reopen mutation).
    if reopen_at is None or reopen_at <= close_at:
        raise ContractValidationError(
            f"{issue_id} observed_state must be closed (reopen mutation)"
        )


_HISTORICAL_CLOSURE_GAP_IDS = frozenset(
    {
        "gap.antigravity.provider",
        "gap.jobs.durable",
        "gap.parity.governance.remaining",
    }
)


def _inventory_requires_issue_state(inventory: dict[str, Any]) -> bool:
    gaps = inventory.get("gaps")
    if not isinstance(gaps, list):
        return False
    return any(
        isinstance(gap, dict) and gap.get("id") in _HISTORICAL_CLOSURE_GAP_IDS
        for gap in gaps
    )


def check_issue_state_evidence(
    inventory: dict[str, Any],
    *,
    repo_root: Path | str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Bind inventory closure claims to the committed offline receipt."""
    root = Path(repo_root)
    path = root / ISSUE_STATE_EVIDENCE_RELATIVE
    if not path.is_file() and not _inventory_requires_issue_state(inventory):
        return {"ok": True, "path": None, "skipped": True}
    evidence = load_and_validate_issue_state_evidence(path, now=now)
    sensitive = {str(item) for item in evidence["closure_sensitive"]}
    blocked = {
        issue_id
        for issue_id, row in evidence["issues"].items()
        if isinstance(row, dict) and row.get("blocks_open_p0") is True
    }
    gaps = inventory.get("gaps")
    if not isinstance(gaps, list):
        raise ContractValidationError("inventory gaps required for issue-state check")
    all_gap_issues: set[str] = set()
    open_p0: set[str] = set()
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        owners = gap.get("issues") or []
        if not isinstance(owners, list):
            continue
        issue_ids = {str(item) for item in owners}
        all_gap_issues.update(issue_ids)
        if gap.get("status") == "open" and gap.get("priority") == "P0":
            open_p0.update(issue_ids)
    missing = sorted(sensitive - all_gap_issues)
    if missing:
        raise ContractValidationError(
            "issue-state/inventory disagreement: missing gap references "
            + ",".join(missing)
        )
    reopened = sorted(blocked & open_p0)
    if reopened:
        raise ContractValidationError(
            "issue-state mutation reopening open P0 owners: " + ",".join(reopened)
        )
    return {
        "ok": True,
        "path": ISSUE_STATE_EVIDENCE_RELATIVE,
        "closure_sensitive": list(evidence["closure_sensitive"]),
        "content_digest": evidence["content_digest"],
    }
