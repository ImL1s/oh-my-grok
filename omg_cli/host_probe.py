"""Canonical Grok Build host capability probe (#105 PR2 / sequence D).

Truth priority (highest first):

1. behavior probe
2. ACP capability advertisement
3. CLI inspect JSON
4. version fallback (last)

Version alone never overrides a higher-priority negative observation
(``version-lies`` regression). Missing required capabilities surface as
``BLOCKED`` / ``LEGACY`` gates — never silent false success.

No auth, session id, transcript, cwd, or home-path leaks in reports.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from omg_cli.host_models import (
    CAPABILITY_KEYS,
    MODERN_CAPS_MIN,
    TESTED_MAX,
    TESTED_MAX_STR,
    TESTED_MIN,
    TESTED_MIN_STR,
    CapabilityTruthSource,
    FeatureGateResult,
    GateState,
    HostCapabilitySet,
    HostCompatStatus,
    HostProbeReport,
)
from omg_cli.redaction import redact_text, redact_value

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_HOME_PATH_RE = re.compile(
    r"(?i)(?:/Users/[^/\s\"']+|/home/[^/\s\"']+|~[/\\][^\s\"']*)"
)
_SENSITIVE_KEYS = frozenset(
    {
        "session",
        "session_id",
        "sessionid",
        "auth",
        "authorization",
        "token",
        "access_token",
        "refresh_token",
        "transcript",
        "cwd",
        "workdir",
        "working_directory",
        "home",
        "homedir",
        "prompt",
        "replay",
        "api_key",
        "apikey",
        "password",
        "secret",
        "cookie",
    }
)

# Inspect / ACP advertisement key aliases → canonical capability name.
_CAP_ALIASES: dict[str, tuple[str, ...]] = {
    "session_resume": (
        "session_resume",
        "session/resume",
        "acp_resume",
        "acp_resume_no_replay",
        "grok.session.acp_resume_no_replay",
        "supports_session_resume",
        "resume",
    ),
    "session_close": (
        "session_close",
        "session/close",
        "acp_close",
        "grok.session.acp_close",
        "supports_session_close",
        "close",
    ),
    "restore_code_explicit": (
        "restore_code_explicit",
        "restore_code",
        "explicit_restore_code",
        "grok.session.restore_code_explicit",
        "supports_restore_code",
    ),
    "uuid_search": (
        "uuid_search",
        "uuid_cross_directory_search",
        "cross_directory_uuid_search",
        "grok.session.uuid_cross_directory_search",
        "supports_uuid_search",
    ),
}

# Default gate policy when a capability is absent.
_GATE_ABSENT_POLICY: dict[str, GateState] = {
    "session_resume": "LEGACY",  # safe conversation load when available
    "session_close": "BLOCKED",  # no silent close success
    "restore_code_explicit": "BLOCKED",  # never mix with resume
    "uuid_search": "LEGACY",  # current-directory lookup only
}

_GATE_ABSENT_REASON: dict[str, str] = {
    "session_resume": (
        "host lacks ACP session/resume; use documented legacy session load "
        "(conversation only; no code restore)"
    ),
    "session_close": (
        "host lacks ACP session/close; refuse close rather than pretend success"
    ),
    "restore_code_explicit": (
        "host lacks explicit restore-code; resume must not restore code"
    ),
    "uuid_search": (
        "host lacks cross-directory UUID search; use current-directory lookup only"
    ),
}

_GATE_ABSENT_NEXT: dict[str, str] = {
    "session_resume": (
        "Upgrade grok to ≥0.2.121 or use legacy load; see docs/host-compat.md"
    ),
    "session_close": (
        "Upgrade grok to a host that advertises session/close, or skip close"
    ),
    "restore_code_explicit": (
        "Request restore-code only via session/load when the host advertises it"
    ),
    "uuid_search": (
        "Search within the current project directory only, or upgrade grok"
    ),
}


@dataclass
class HostProbeInputs:
    """Injectable probe inputs for hermetic fixtures (no real grok required)."""

    binary: str = "grok"
    binary_found: bool | None = None
    version_text: str | None = None
    version_json: Mapping[str, Any] | dict[str, Any] | None = None
    inspect_json: Mapping[str, Any] | dict[str, Any] | None = None
    acp_advertisement: Mapping[str, Any] | dict[str, Any] | None = None
    behavior: Mapping[str, bool] | dict[str, bool] | None = None
    observations: list[str] = field(default_factory=list)


def parse_host_version(text: str | None) -> tuple[int, int, int] | None:
    """Parse a grok semver from CLI text or JSON (currentVersion/version keys)."""
    if not text or not str(text).strip():
        return None
    stripped = str(text).strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            raw = data.get("currentVersion") or data.get("version")
            if raw is not None:
                nested = parse_host_version(str(raw))
                if nested is not None:
                    return nested
    m = _VERSION_RE.search(stripped)
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def format_version(ver: tuple[int, int, int] | None) -> str | None:
    if ver is None:
        return None
    return f"{ver[0]}.{ver[1]}.{ver[2]}"


def scrub_path_for_json(path: str) -> str:
    """Redact home-directory prefixes from a filesystem path for JSON output."""
    return _scrub_observation(path)


def _scrub_observation(text: str) -> str:
    """Redact home paths and credential-like substrings from observations."""
    scrubbed = _HOME_PATH_RE.sub("[REDACTED_PATH]", text)
    return redact_text(scrubbed)


def _drop_sensitive(obj: Any, *, depth: int = 0) -> Any:
    """Strip sensitive keys before any capability scrape (fail-closed)."""
    if depth > 8:
        return None
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for key, value in obj.items():
            nk = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if nk in _SENSITIVE_KEYS or any(p in nk for p in ("token", "secret", "auth")):
                continue
            if nk in {"path", "filepath", "realpath"} and isinstance(value, str):
                if _HOME_PATH_RE.search(value) or value.startswith("~"):
                    continue
            out[str(key)] = _drop_sensitive(value, depth=depth + 1)
        return out
    if isinstance(obj, list):
        return [_drop_sensitive(x, depth=depth + 1) for x in obj[:200]]
    if isinstance(obj, str):
        return _scrub_observation(obj) if len(obj) < 500 else obj[:500]
    return obj


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"true", "yes", "1", "supported", "available"}:
            return True
        if low in {"false", "no", "0", "unsupported", "missing", "absent"}:
            return False
    return None


def _extract_caps_from_mapping(
    data: Mapping[str, Any] | None,
) -> dict[str, bool]:
    """Pull known capability booleans from a nested advertisement/inspect blob."""
    found: dict[str, bool] = {}
    if not isinstance(data, Mapping):
        return found

    scrubbed = _drop_sensitive(dict(data))
    if not isinstance(scrubbed, dict):
        return found

    flat: dict[str, Any] = {}

    def _walk(obj: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = str(k)
                flat[key] = v
                flat[key.lower()] = v
                # Nested capabilities / methods / features blocks
                if key.lower() in {
                    "capabilities",
                    "methods",
                    "features",
                    "supported",
                    "supports",
                    "acp",
                    "session",
                }:
                    if isinstance(v, dict):
                        for nk, nv in v.items():
                            flat[str(nk)] = nv
                            flat[str(nk).lower()] = nv
                    elif isinstance(v, list):
                        for item in v:
                            if isinstance(item, str):
                                flat[item] = True
                                flat[item.lower()] = True
                            elif isinstance(item, dict):
                                name = item.get("name") or item.get("id") or item.get("method")
                                if name:
                                    enabled = item.get("supported", item.get("enabled", True))
                                    flat[str(name)] = enabled
                _walk(v, depth + 1)
        elif isinstance(obj, list):
            for x in obj[:100]:
                _walk(x, depth + 1)

    _walk(scrubbed)

    for canon, aliases in _CAP_ALIASES.items():
        for alias in aliases:
            if alias in flat:
                parsed = _as_bool(flat[alias])
                if parsed is not None:
                    found[canon] = parsed
                    break
            # list membership style: methods: ["session/resume", ...]
            low = alias.lower()
            if low in flat and flat[low] is True:
                found[canon] = True
                break
    return found


def _version_fallback_caps(
    ver: tuple[int, int, int] | None,
) -> dict[str, bool]:
    """Last-resort capability map from semver only (never beats higher sources)."""
    if ver is None:
        return {k: False for k in CAPABILITY_KEYS}
    if ver >= MODERN_CAPS_MIN:
        return {
            "session_resume": True,
            "session_close": True,
            "restore_code_explicit": True,
            "uuid_search": True,
        }
    # Legacy tested window: Stop gate ok, but no ACP resume/close assumed.
    return {k: False for k in CAPABILITY_KEYS}


def _classify_compat(ver: tuple[int, int, int] | None) -> HostCompatStatus:
    if ver is None:
        return "unknown"
    if ver < TESTED_MIN:
        return "too_old"
    if ver > TESTED_MAX:
        return "too_new"
    if ver < MODERN_CAPS_MIN:
        return "legacy"
    return "compatible"


def _merge_capability(
    *,
    key: str,
    behavior: Mapping[str, bool] | None,
    advertisement: Mapping[str, bool],
    advertisement_present: bool,
    inspect: Mapping[str, bool],
    inspect_present: bool,
    version_caps: Mapping[str, bool],
    allow_version_fallback: bool,
) -> tuple[bool, CapabilityTruthSource]:
    """Resolve one capability with truth priority.

    When an ACP advertisement or inspect layer is present, keys not
    explicitly observed in that layer are fail-closed (false) and must
    **not** fall through to version fallback. Empty ``methods: []`` is an
    authoritative empty set. Version is last resort only when neither
    advertisement nor inspect layer was provided (and not malformed).
    """
    if behavior is not None and key in behavior:
        return bool(behavior[key]), "behavior"
    if advertisement_present:
        if key in advertisement:
            return bool(advertisement[key]), "advertisement"
        # Omission under advertisement = not available (no version fill).
        return False, "advertisement"
    if inspect_present:
        if key in inspect:
            return bool(inspect[key]), "inspect"
        return False, "inspect"
    if allow_version_fallback and key in version_caps:
        return bool(version_caps[key]), "version"
    return False, "none"


def evaluate_feature_gate(
    capability: str,
    caps: HostCapabilitySet,
    *,
    required: bool = False,
) -> FeatureGateResult:
    """Three-state gate: AVAILABLE / LEGACY / BLOCKED.

    When ``required`` is True and the capability is missing, always BLOCKED
    (never silent success). When not required, use the per-capability absent
    policy (LEGACY for resume/uuid; BLOCKED for close/restore-code).
    """
    if capability not in CAPABILITY_KEYS:
        return FeatureGateResult(
            capability=capability,
            state="BLOCKED",
            reason=f"unknown capability {capability!r}",
            next_action="Use a documented host capability id",
            required=required,
        )
    present = caps.get(capability)
    if present:
        return FeatureGateResult(
            capability=capability,
            state="AVAILABLE",
            reason=f"host capability {capability} observed "
            f"(source={caps.source_for(capability)})",
            required=required,
        )
    if required:
        return FeatureGateResult(
            capability=capability,
            state="BLOCKED",
            reason=(
                f"required capability {capability} missing "
                f"(source={caps.source_for(capability)})"
            ),
            next_action=_GATE_ABSENT_NEXT.get(capability),
            required=True,
        )
    state = _GATE_ABSENT_POLICY.get(capability, "BLOCKED")
    return FeatureGateResult(
        capability=capability,
        state=state,
        reason=_GATE_ABSENT_REASON.get(
            capability, f"capability {capability} unavailable"
        ),
        next_action=_GATE_ABSENT_NEXT.get(capability),
        required=False,
    )


def default_gates(caps: HostCapabilitySet) -> tuple[FeatureGateResult, ...]:
    """Standard doctor gates (not required — report AVAILABLE/LEGACY/BLOCKED)."""
    return tuple(evaluate_feature_gate(k, caps, required=False) for k in CAPABILITY_KEYS)


def load_fixture(path: str | Path) -> HostProbeInputs:
    """Load a hermetic host probe fixture JSON into :class:`HostProbeInputs`."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"host fixture must be an object: {path}")
    behavior = raw.get("behavior")
    return HostProbeInputs(
        binary=str(raw.get("binary") or "grok"),
        binary_found=raw.get("binary_found"),
        version_text=raw.get("version_text"),
        version_json=raw.get("version_json"),
        inspect_json=raw.get("inspect_json"),
        acp_advertisement=raw.get("acp_advertisement"),
        behavior=behavior if isinstance(behavior, dict) else None,
        observations=list(raw.get("observations") or []),
    )


def _run_json(argv: list[str], *, timeout: float = 8.0) -> Any | None:
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        for start in ("{", "["):
            idx = out.find(start)
            if idx >= 0:
                try:
                    return json.loads(out[idx:])
                except json.JSONDecodeError:
                    continue
        return None


def _live_collect(*, binary: str = "grok") -> HostProbeInputs:
    """Best-effort live collection (skipped in unit tests via injected inputs)."""
    found = shutil.which(binary) is not None
    version_json = _run_json([binary, "version", "--json"]) if found else None
    version_text: str | None = None
    if found:
        try:
            proc = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=8.0,
                check=False,
            )
            if proc.returncode == 0:
                version_text = (proc.stdout or proc.stderr or "").strip() or None
        except (OSError, subprocess.TimeoutExpired):
            version_text = None
    inspect_json = _run_json([binary, "inspect", "--json"]) if found else None
    # ACP advertisement: prefer inspect nested acp/capabilities; optional env for tests.
    acp_raw = os.environ.get("OMG_HOST_ACP_ADVERTISEMENT_JSON")
    acp_advertisement: dict[str, Any] | None = None
    if acp_raw:
        try:
            parsed = json.loads(acp_raw)
            if isinstance(parsed, dict):
                acp_advertisement = parsed
        except json.JSONDecodeError:
            acp_advertisement = None
    behavior_raw = os.environ.get("OMG_HOST_BEHAVIOR_JSON")
    behavior: dict[str, bool] | None = None
    if behavior_raw:
        try:
            parsed_b = json.loads(behavior_raw)
            if isinstance(parsed_b, dict):
                behavior = {
                    str(k): bool(v)
                    for k, v in parsed_b.items()
                    if k in CAPABILITY_KEYS
                }
        except json.JSONDecodeError:
            behavior = None
    return HostProbeInputs(
        binary=binary,
        binary_found=found,
        version_text=version_text,
        version_json=version_json if isinstance(version_json, dict) else None,
        inspect_json=inspect_json if isinstance(inspect_json, dict) else None,
        acp_advertisement=acp_advertisement,
        behavior=behavior,
    )


def probe_host(
    inputs: HostProbeInputs | None = None,
    *,
    binary: str = "grok",
) -> HostProbeReport:
    """Probe the Grok host and return a redacted :class:`HostProbeReport`."""
    src = inputs if inputs is not None else _live_collect(binary=binary)
    observations: list[str] = [
        _scrub_observation(o) for o in (src.observations or []) if o
    ]

    binary_found = (
        bool(src.binary_found)
        if src.binary_found is not None
        else (shutil.which(src.binary or binary) is not None)
    )
    if not binary_found:
        observations.append("grok binary not found on PATH")

    ver: tuple[int, int, int] | None = None
    if isinstance(src.version_json, Mapping):
        raw = src.version_json.get("currentVersion") or src.version_json.get("version")
        if raw is not None:
            ver = parse_host_version(str(raw))
            if ver is not None:
                observations.append("version from CLI version --json")
    if ver is None and src.version_text:
        ver = parse_host_version(src.version_text)
        if ver is not None:
            observations.append("version from --version text")

    # Capability layers (priority applied in _merge_capability).
    behavior_map: dict[str, bool] | None = None
    if src.behavior is not None:
        behavior_map = {
            k: bool(v) for k, v in src.behavior.items() if k in CAPABILITY_KEYS
        }
        observations.append(
            "behavior layer present (fixture/env injection; not a live ACP handshake)"
        )

    advertisement_present = isinstance(src.acp_advertisement, Mapping)
    advertisement = _extract_caps_from_mapping(
        src.acp_advertisement if advertisement_present else None
    )
    if advertisement_present:
        observations.append(
            "ACP advertisement layer present "
            f"({len(advertisement)} explicit cap(s); omissions fail-closed)"
        )

    inspect_present = isinstance(src.inspect_json, Mapping)
    inspect_caps = _extract_caps_from_mapping(
        src.inspect_json if inspect_present else None
    )
    malformed_layer = False
    if src.inspect_json is not None and not inspect_present:
        observations.append("inspect JSON malformed; ignored (fail-closed)")
        malformed_layer = True
        inspect_caps = {}
        inspect_present = False
    elif inspect_present:
        observations.append(
            "CLI inspect layer present "
            f"({len(inspect_caps)} explicit cap(s); omissions fail-closed)"
        )

    # Malformed advertisement: non-mapping → fail closed (no caps from it).
    if src.acp_advertisement is not None and not advertisement_present:
        observations.append("ACP advertisement malformed; ignored (fail-closed)")
        advertisement = {}
        advertisement_present = False
        malformed_layer = True

    # Version fallback only when no ad/inspect layer was provided at all.
    # Malformed higher layers also suppress version (never false-green).
    allow_version = (
        not malformed_layer
        and not advertisement_present
        and not inspect_present
    )
    if malformed_layer:
        version_caps = {k: False for k in CAPABILITY_KEYS}
        observations.append(
            "version fallback suppressed after malformed capability layer"
        )
    elif not allow_version:
        version_caps = {k: False for k in CAPABILITY_KEYS}
        observations.append(
            "version fallback suppressed "
            "(advertisement and/or inspect layer is authoritative)"
        )
    else:
        version_caps = _version_fallback_caps(ver)
        if ver is not None:
            observations.append(
                f"version fallback layer available ({format_version(ver)})"
            )

    resolved: dict[str, bool] = {}
    sources: dict[str, CapabilityTruthSource] = {}
    for key in CAPABILITY_KEYS:
        value, source = _merge_capability(
            key=key,
            behavior=behavior_map,
            advertisement=advertisement,
            advertisement_present=advertisement_present,
            inspect=inspect_caps,
            inspect_present=inspect_present,
            version_caps=version_caps,
            allow_version_fallback=allow_version,
        )
        resolved[key] = value
        sources[key] = source

    # Honesty: if a higher layer explicitly denies, version must not flip true.
    # (Already enforced by _merge_capability priority.)

    caps = HostCapabilitySet(
        session_resume=resolved["session_resume"],
        session_close=resolved["session_close"],
        restore_code_explicit=resolved["restore_code_explicit"],
        uuid_search=resolved["uuid_search"],
        sources=sources,
    )
    gates = default_gates(caps)
    compat = _classify_compat(ver)

    # Bound + redact observations (no home/auth leaks).
    safe_obs = tuple(
        _scrub_observation(o)[:240]
        for o in observations
        if o and "session_id" not in o.lower()
    )[:32]

    return HostProbeReport(
        binary=src.binary or binary,
        version=format_version(ver),
        version_tuple=ver,
        tested_min=TESTED_MIN_STR,
        tested_max=TESTED_MAX_STR,
        compatibility=compat,
        capabilities=caps,
        observations=safe_obs,
        gates=gates,
        binary_found=binary_found,
    )


def probe_host_from_fixture(path: str | Path) -> HostProbeReport:
    """Convenience: load fixture JSON and probe."""
    return probe_host(load_fixture(path))


def host_report_for_doctor(report: HostProbeReport) -> dict[str, Any]:
    """Compact host block matching the #105 PR2 doctor JSON plan shape."""
    raw = report.to_dict()
    # Plan-shaped nested host object (no schema noise at top of host key).
    return redact_value(
        {
            "binary": raw["binary"],
            "version": raw["version"],
            "tested_min": raw["tested_min"],
            "tested_max": raw["tested_max"],
            "compatibility": raw["compatibility"],
            "capabilities": raw["capabilities"],
            "capability_sources": raw["capability_sources"],
            "gates": raw["gates"],
            "observations": raw["observations"],
            "binary_found": raw["binary_found"],
            "schema": raw["schema"],
        }
    )


__all__ = [
    "HostProbeInputs",
    "default_gates",
    "evaluate_feature_gate",
    "format_version",
    "host_report_for_doctor",
    "load_fixture",
    "parse_host_version",
    "probe_host",
    "probe_host_from_fixture",
    "scrub_path_for_json",
]
