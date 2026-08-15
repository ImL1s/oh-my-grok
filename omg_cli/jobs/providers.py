"""Jobs-scoped provider registry and Antigravity preflight (#68 PR2 + #105 PR4).

Exact-name registry shared by parent ``start_job`` and child ``runner``.
No aliases, no import-by-user-string, no fallback provider.

Public admission: ``fake``, ``antigravity``, and ``grok``.
Internal-only: ``grok-acp-session`` (Team ACP sidecar; rejected by public start).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Final, Mapping

from omg_cli.jobs.models import JobStoreError
from omg_cli.providers.base import ProviderAdapter
from omg_cli.providers.errors import (
    ProviderBinaryMissing,
    ProviderProbeError,
    ProviderVersionError,
)

ProviderFactory = Callable[[], ProviderAdapter]

# Canonical Antigravity default for jobs (preserves events/usage/session).
DEFAULT_ANTIGRAVITY_OUTPUT_FORMAT: Final[str] = "stream-json"
ALLOWED_ANTIGRAVITY_OUTPUT_FORMATS: Final[frozenset[str]] = frozenset(
    {"text", "json", "stream-json"}
)
FAKE_ONLY_FLAGS: Final[frozenset[str]] = frozenset(
    {"sleep_s", "fail", "large_output", "ignore_sigterm"}
)

PUBLIC_PROVIDER_NAMES: Final[tuple[str, ...]] = ("fake", "antigravity", "grok")
INTERNAL_PROVIDER_NAMES: Final[tuple[str, ...]] = ("grok-acp-session",)
ACP_SESSION_PROVIDER: Final[str] = "grok-acp-session"


@dataclass(frozen=True, slots=True)
class JobProviderMeta:
    """Registry metadata for one jobs provider."""

    name: str
    factory: ProviderFactory
    allow_fake_flags: bool
    default_output_format: str
    requires_preflight: bool
    internal: bool = False


def _make_fake() -> ProviderAdapter:
    from omg_cli.jobs.fake import FakeProvider

    return FakeProvider()


def _make_antigravity() -> ProviderAdapter:
    from omg_cli.providers.antigravity import AntigravityProvider

    return AntigravityProvider()


def _make_grok() -> ProviderAdapter:
    from omg_cli.jobs.grok import GrokJobProvider

    return GrokJobProvider()


def _make_acp_session() -> ProviderAdapter:
    from omg_cli.jobs.acp_provider import GrokAcpSessionProvider

    return GrokAcpSessionProvider()


_REGISTRY: Final[Mapping[str, JobProviderMeta]] = {
    "fake": JobProviderMeta(
        name="fake",
        factory=_make_fake,
        allow_fake_flags=True,
        default_output_format="text",
        requires_preflight=False,
        internal=False,
    ),
    "antigravity": JobProviderMeta(
        name="antigravity",
        factory=_make_antigravity,
        allow_fake_flags=False,
        default_output_format=DEFAULT_ANTIGRAVITY_OUTPUT_FORMAT,
        requires_preflight=True,
        internal=False,
    ),
    "grok": JobProviderMeta(
        name="grok",
        factory=_make_grok,
        allow_fake_flags=False,
        default_output_format="text",
        requires_preflight=True,
        internal=False,
    ),
    ACP_SESSION_PROVIDER: JobProviderMeta(
        name=ACP_SESSION_PROVIDER,
        factory=_make_acp_session,
        allow_fake_flags=False,
        default_output_format="text",
        requires_preflight=False,
        internal=True,
    ),
}


def registered_provider_names(*, include_internal: bool = False) -> tuple[str, ...]:
    """Public names by default; set ``include_internal`` for Team/runtime paths."""
    if include_internal:
        return tuple(_REGISTRY.keys())
    return tuple(n for n, m in _REGISTRY.items() if not m.internal)


def public_provider_names() -> tuple[str, ...]:
    return PUBLIC_PROVIDER_NAMES


def get_provider_meta(name: str, *, allow_internal: bool = False) -> JobProviderMeta:
    key = (name or "").strip().lower()
    meta = _REGISTRY.get(key)
    if meta is None:
        raise JobStoreError(
            f"unknown job provider {name!r}; supported: "
            f"{', '.join(registered_provider_names(include_internal=False))}",
            code="E_JOB_PROVIDER",
        )
    if meta.internal and not allow_internal:
        raise JobStoreError(
            f"job provider {meta.name!r} is internal-only "
            f"(not admitted by public omg job start)",
            code="E_JOB_PROVIDER_INTERNAL",
        )
    if key != meta.name:
        raise JobStoreError(
            f"unknown job provider {name!r}",
            code="E_JOB_PROVIDER",
        )
    return meta


def resolve_job_provider(
    name: str, *, allow_internal: bool = False
) -> tuple[ProviderAdapter, JobProviderMeta]:
    """Resolve exact registry name → adapter; verify adapter.name matches."""
    meta = get_provider_meta(name, allow_internal=allow_internal)
    try:
        adapter = meta.factory()
    except Exception as exc:  # noqa: BLE001 — fail closed before materialization
        raise JobStoreError(
            f"job provider {meta.name!r} factory failed: {exc}",
            code="E_JOB_PROVIDER",
        ) from exc
    if not isinstance(adapter, ProviderAdapter):
        raise JobStoreError(
            f"job provider {meta.name!r} factory did not return ProviderAdapter",
            code="E_JOB_PROVIDER",
        )
    adapter_name = getattr(adapter, "name", None)
    if adapter_name != meta.name:
        raise JobStoreError(
            f"job provider registry name mismatch: "
            f"registered={meta.name!r} adapter.name={adapter_name!r}",
            code="E_JOB_PROVIDER",
        )
    return adapter, meta


@dataclass(frozen=True, slots=True)
class AntigravityPreflight:
    """Immutable snapshot produced by successful Antigravity admission."""

    provider_binary: str
    provider_version: str
    provider_compat: str
    provider_pin_revision: str
    output_format: str
    model: str | None
    effort: str | None
    mode: str | None
    timeout_s: float
    observed_formats: tuple[str, ...]


def _reject_fake_flags(*, sleep_s: float | None, fail: bool, large_output: bool, ignore_sigterm: bool) -> None:
    if sleep_s is not None or fail or large_output or ignore_sigterm:
        raise JobStoreError(
            "fake-only flags (--sleep/--fail/--large-output/--ignore-sigterm) "
            "are not allowed with provider=antigravity",
            code="E_JOB_PROVIDER_OPTIONS",
        )


def preflight_antigravity(
    *,
    output_format: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    mode: str | None = None,
    timeout_s: float | None = None,
    sleep_s: float | None = None,
    fail: bool = False,
    large_output: bool = False,
    ignore_sigterm: bool = False,
) -> AntigravityPreflight:
    """Fail-closed Antigravity admission before job directory materialization.

    Does **not** require authenticated=True or live_call_ready=True.
    """
    _reject_fake_flags(
        sleep_s=sleep_s,
        fail=fail,
        large_output=large_output,
        ignore_sigterm=ignore_sigterm,
    )

    adapter, meta = resolve_job_provider("antigravity")
    fmt = (output_format or meta.default_output_format).strip().lower()
    if fmt not in ALLOWED_ANTIGRAVITY_OUTPUT_FORMATS:
        raise JobStoreError(
            f"unsupported Antigravity output format {fmt!r}; "
            f"allowed: {', '.join(sorted(ALLOWED_ANTIGRAVITY_OUTPUT_FORMATS))}",
            code="E_JOB_PROVIDER_OPTIONS",
        )

    try:
        binary = adapter.discover_binary()
    except ProviderBinaryMissing as exc:
        raise JobStoreError(
            f"Antigravity binary missing: {exc}",
            code="E_JOB_PROVIDER_MISSING",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise JobStoreError(
            f"Antigravity binary discovery failed: {exc}",
            code="E_JOB_PROVIDER_MISSING",
        ) from exc
    if not isinstance(binary, str) or not binary.strip():
        raise JobStoreError(
            "Antigravity binary discovery returned empty path",
            code="E_JOB_PROVIDER_MISSING",
        )

    try:
        caps = adapter.probe_capabilities(binary)
    except (ProviderProbeError, ProviderVersionError) as exc:
        raise JobStoreError(
            f"Antigravity probe failed: {exc}",
            code="E_JOB_PROVIDER_PROBE",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise JobStoreError(
            f"Antigravity probe failed: {exc}",
            code="E_JOB_PROVIDER_PROBE",
        ) from exc

    compat = str(getattr(caps, "compat_status", "") or "")
    if compat != "compatible":
        raise JobStoreError(
            f"Antigravity binary incompatible (compat_status={compat!r})",
            code="E_JOB_PROVIDER_COMPAT",
        )

    if not bool(getattr(caps, "print_mode", False)):
        raise JobStoreError(
            "Antigravity binary lacks headless print mode (--print)",
            code="E_JOB_PROVIDER_CAPABILITY",
        )

    observed = tuple(getattr(caps, "output_formats", ()) or ())
    if fmt not in observed:
        raise JobStoreError(
            f"Antigravity output format {fmt!r} not observed in capabilities "
            f"(observed={list(observed)!r})",
            code="E_JOB_PROVIDER_CAPABILITY",
        )

    version = str(getattr(caps, "version", "") or "")
    pin = str(getattr(caps, "pin_revision", "") or "")
    timeout = float(timeout_s) if timeout_s is not None else 3600.0
    if timeout <= 0:
        raise JobStoreError(
            "provider timeout must be positive",
            code="E_JOB_PROVIDER_OPTIONS",
        )

    return AntigravityPreflight(
        provider_binary=str(binary),
        provider_version=version,
        provider_compat=compat,
        provider_pin_revision=pin,
        output_format=fmt,
        model=str(model) if model else None,
        effort=str(effort) if effort else None,
        mode=str(mode) if mode else None,
        timeout_s=timeout,
        observed_formats=tuple(str(x) for x in observed),
    )


@dataclass(frozen=True, slots=True)
class GrokPreflight:
    """Immutable snapshot produced by successful grok job admission."""

    provider_binary: str
    provider_version: str
    provider_compat: str
    provider_pin_revision: str
    output_format: str
    model: str | None
    effort: str | None
    mode: str | None
    timeout_s: float
    observed_formats: tuple[str, ...]


def preflight_grok(
    *,
    output_format: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    mode: str | None = None,
    timeout_s: float | None = None,
    sleep_s: float | None = None,
    fail: bool = False,
    large_output: bool = False,
    ignore_sigterm: bool = False,
) -> GrokPreflight:
    """Fail-closed grok admission before job directory materialization."""
    if sleep_s is not None or fail or large_output or ignore_sigterm:
        raise JobStoreError(
            "fake-only flags (--sleep/--fail/--large-output/--ignore-sigterm) "
            "are not allowed with provider=grok",
            code="E_JOB_PROVIDER_OPTIONS",
        )
    if (effort or "").strip() or (mode or "").strip():
        raise JobStoreError(
            "grok jobs do not forward --effort/--mode; omit them or pass --model only",
            code="E_JOB_PROVIDER_OPTIONS",
        )
    adapter, meta = resolve_job_provider("grok")
    fmt = (output_format or meta.default_output_format).strip().lower()
    if fmt not in {"text", "json", "stream-json"}:
        raise JobStoreError(
            f"unsupported grok output format {fmt!r}",
            code="E_JOB_PROVIDER_OPTIONS",
        )
    try:
        binary = adapter.discover_binary()
    except ProviderBinaryMissing as exc:
        raise JobStoreError(
            f"grok binary missing: {exc}",
            code="E_JOB_PROVIDER_MISSING",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise JobStoreError(
            f"grok binary discovery failed: {exc}",
            code="E_JOB_PROVIDER_MISSING",
        ) from exc
    if not isinstance(binary, str) or not binary.strip():
        raise JobStoreError(
            "grok binary discovery returned empty path",
            code="E_JOB_PROVIDER_MISSING",
        )
    try:
        caps = adapter.probe_capabilities(binary)
    except (ProviderProbeError, ProviderVersionError) as exc:
        raise JobStoreError(
            f"grok probe failed: {exc}",
            code="E_JOB_PROVIDER_PROBE",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise JobStoreError(
            f"grok probe failed: {exc}",
            code="E_JOB_PROVIDER_PROBE",
        ) from exc
    compat = str(getattr(caps, "compat_status", "") or "")
    if compat != "compatible":
        raise JobStoreError(
            f"grok binary incompatible (compat_status={compat!r})",
            code="E_JOB_PROVIDER_COMPAT",
        )
    version = str(getattr(caps, "version", "") or "")
    timeout = float(timeout_s) if timeout_s is not None else 3600.0
    if timeout <= 0:
        raise JobStoreError(
            "provider timeout must be positive",
            code="E_JOB_PROVIDER_OPTIONS",
        )
    from omg_cli.jobs.grok import PIN_REVISION

    observed = tuple(getattr(caps, "output_formats", ()) or ("text",))
    return GrokPreflight(
        provider_binary=str(binary),
        provider_version=version,
        provider_compat=compat,
        provider_pin_revision=PIN_REVISION,
        output_format=fmt,
        model=str(model) if model else None,
        effort=None,
        mode=None,
        timeout_s=timeout,
        observed_formats=tuple(str(x) for x in observed),
    )


def build_request_snapshot(
    provider: str,
    *,
    preflight: AntigravityPreflight | GrokPreflight | None = None,
    output_format: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    mode: str | None = None,
    timeout_s: float | None = None,
    provider_binary: str | None = None,
    session_id: str | None = None,
    parent_run_id: str | None = None,
    cwd: str | None = None,
    session_id_hash: str | None = None,
    cwd_hash: str | None = None,
) -> dict[str, Any]:
    """Build the immutable ``request`` object stored on JobRecord."""
    if provider == "antigravity":
        if preflight is None:
            raise JobStoreError(
                "antigravity request snapshot requires successful preflight",
                code="E_JOB_PROVIDER",
            )
        return {
            "output_format": preflight.output_format,
            "model": preflight.model,
            "effort": preflight.effort,
            "mode": preflight.mode,
            "timeout_s": preflight.timeout_s,
            "provider_binary": preflight.provider_binary,
            "provider_version": preflight.provider_version,
            "provider_compat": preflight.provider_compat,
            "provider_pin_revision": preflight.provider_pin_revision,
        }
    if provider == "grok":
        if preflight is None:
            raise JobStoreError(
                "grok request snapshot requires successful preflight",
                code="E_JOB_PROVIDER",
            )
        return {
            "output_format": preflight.output_format,
            "model": preflight.model,
            "effort": preflight.effort,
            "mode": preflight.mode,
            "timeout_s": preflight.timeout_s,
            "provider_binary": preflight.provider_binary,
            "provider_version": preflight.provider_version,
            "provider_compat": preflight.provider_compat,
            "provider_pin_revision": preflight.provider_pin_revision,
        }
    if provider == ACP_SESSION_PROVIDER:
        return {
            "output_format": "text",
            "model": None,
            "effort": None,
            "mode": None,
            "timeout_s": float(timeout_s) if timeout_s is not None else 30.0,
            "provider_binary": provider_binary,
            "provider_version": None,
            "provider_compat": None,
            "provider_pin_revision": None,
            "session_id": session_id,
            "parent_run_id": parent_run_id,
            "cwd": cwd,
            "session_id_hash": session_id_hash,
            "cwd_hash": cwd_hash,
            "long_lived": True,
        }
    meta = get_provider_meta(provider)
    return {
        "output_format": (output_format or meta.default_output_format),
        "model": str(model) if model else None,
        "effort": str(effort) if effort else None,
        "mode": str(mode) if mode else None,
        "timeout_s": float(timeout_s) if timeout_s is not None else 3600.0,
        "provider_binary": None,
        "provider_version": None,
        "provider_compat": None,
        "provider_pin_revision": None,
    }


def public_request_summary(request: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Sanitize request for CLI/status envelopes (no absolute binary path)."""
    if not request:
        return None
    out = {
        "output_format": request.get("output_format"),
        "model": request.get("model"),
        "effort": request.get("effort"),
        "mode": request.get("mode"),
        "timeout_s": request.get("timeout_s"),
        "provider_version": request.get("provider_version"),
        "provider_compat": request.get("provider_compat"),
        "provider_pin_revision": request.get("provider_pin_revision"),
        "has_provider_binary": bool(request.get("provider_binary")),
        "long_lived": bool(request.get("long_lived")),
        "has_session_binding": bool(request.get("session_id_hash")),
    }
    return out


def revalidate_stored_request(provider: str, request: Mapping[str, Any] | None) -> None:
    """Re-run provider preflight and ensure it still matches the stored snapshot.

    Called before consuming another retry attempt. Fail closed on drift.
    """
    provider = (provider or "").strip().lower()
    if provider == ACP_SESSION_PROVIDER:
        raise JobStoreError(
            f"job provider {ACP_SESSION_PROVIDER!r} cannot be retried via public CLI",
            code="E_JOB_PROVIDER_INTERNAL",
        )
    req = dict(request or {})
    if provider == "antigravity":
        preflight = preflight_antigravity(
            output_format=req.get("output_format"),
            model=req.get("model"),
            effort=req.get("effort"),
            mode=req.get("mode"),
            timeout_s=req.get("timeout_s"),
        )
        fresh = build_request_snapshot("antigravity", preflight=preflight)
        # Immutable request must still match on identity-critical fields.
        for key in (
            "output_format",
            "model",
            "effort",
            "mode",
            "provider_binary",
            "provider_version",
            "provider_compat",
            "provider_pin_revision",
        ):
            stored = req.get(key)
            now = fresh.get(key)
            if stored != now:
                raise JobStoreError(
                    f"immutable request field {key!r} no longer matches "
                    f"preflight (stored={stored!r}, now={now!r})",
                    code="E_JOB_RETRY_PREFLIGHT",
                )
        return
    if provider == "grok":
        preflight = preflight_grok(
            output_format=req.get("output_format"),
            model=req.get("model"),
            effort=req.get("effort"),
            mode=req.get("mode"),
            timeout_s=req.get("timeout_s"),
        )
        fresh = build_request_snapshot("grok", preflight=preflight)
        for key in (
            "output_format",
            "model",
            "effort",
            "mode",
            "provider_binary",
            "provider_version",
            "provider_compat",
            "provider_pin_revision",
        ):
            stored = req.get(key)
            now = fresh.get(key)
            if stored != now:
                raise JobStoreError(
                    f"immutable request field {key!r} no longer matches "
                    f"preflight (stored={stored!r}, now={now!r})",
                    code="E_JOB_RETRY_PREFLIGHT",
                )
        return
    if provider == "fake":
        resolve_job_provider("fake")
        return
    raise JobStoreError(
        f"unknown job provider {provider!r} for retry preflight",
        code="E_JOB_PROVIDER",
    )


__all__ = [
    "ACP_SESSION_PROVIDER",
    "ALLOWED_ANTIGRAVITY_OUTPUT_FORMATS",
    "DEFAULT_ANTIGRAVITY_OUTPUT_FORMAT",
    "FAKE_ONLY_FLAGS",
    "INTERNAL_PROVIDER_NAMES",
    "PUBLIC_PROVIDER_NAMES",
    "AntigravityPreflight",
    "GrokPreflight",
    "JobProviderMeta",
    "build_request_snapshot",
    "get_provider_meta",
    "preflight_antigravity",
    "preflight_grok",
    "public_provider_names",
    "public_request_summary",
    "registered_provider_names",
    "resolve_job_provider",
    "revalidate_stored_request",
]
