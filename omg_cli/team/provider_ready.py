"""Provider-specific readiness strategies for Team supervisor (#99).

Adapters decide ready / blocked / failed / unknown. Unknown providers must
never optimistic-success — they remain timeout/unknown.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, Sequence

from omg_cli.team.startup import BlockedReason, EvidenceCode


@dataclass(frozen=True)
class ReadinessObservation:
    """Result of one observe() poll."""

    status: str  # ready | blocked | failed | pending | unknown
    evidence_code: str
    blocked_reason: str | None = None
    failure_reason: str | None = None
    detail: str | None = None


class ProviderReadinessStrategy(Protocol):
    name: str

    def observe(
        self,
        *,
        provider_pid: int,
        alive: bool,
        capture_lines: Sequence[str],
        elapsed_s: float,
        env: Mapping[str, str] | None = None,
    ) -> ReadinessObservation: ...


_AUTH_RE = re.compile(
    r"(?i)(not\s+logged\s+in|authentication\s+required|please\s+log\s*in|"
    r"api\s*key\s*(missing|required)|unauthorized|login\s+required)"
)
_TRUST_RE = re.compile(
    r"(?i)(trust\s+this\s+(workspace|folder|project)|hooks?\s+review|"
    r"permission\s+required|allow\s+this\s+session|"
    r"do\s+you\s+want\s+to\s+trust)"
)
_IDLE_GROK_RE = re.compile(
    r"(?i)(grok>\s*$|^\s*>\s*$|waiting\s+for\s+(input|prompt)|"
    r"TEAM_PROVIDER_READY_OK)"
)
_IDLE_CODEX_RE = re.compile(
    r"(?i)(codex>|^\s*›\s*$|TEAM_PROVIDER_READY_OK|waiting\s+for\s+input)"
)
_ERROR_RE = re.compile(
    r"(?i)(fatal\s+error|traceback \(most recent|command not found|"
    r"no such file|failed to start|panic:)"
)


def _joined(lines: Sequence[str]) -> str:
    return "\n".join(str(x) for x in lines[-32:])


class _BaseStrategy:
    name = "base"
    idle_re: re.Pattern[str] | None = None
    stability_s: float = 0.35
    allow_process_stable: bool = False

    def observe(
        self,
        *,
        provider_pid: int,
        alive: bool,
        capture_lines: Sequence[str],
        elapsed_s: float,
        env: Mapping[str, str] | None = None,
    ) -> ReadinessObservation:
        text = _joined(capture_lines)
        if not alive:
            return ReadinessObservation(
                status="failed",
                evidence_code=EvidenceCode.PROVIDER_EXITED.value,
                failure_reason="provider process not alive during observe",
            )
        if _AUTH_RE.search(text):
            return ReadinessObservation(
                status="blocked",
                evidence_code=EvidenceCode.AUTH_REQUIRED.value,
                blocked_reason=BlockedReason.AUTH.value,
                detail="authentication prompt detected",
            )
        if _TRUST_RE.search(text):
            return ReadinessObservation(
                status="blocked",
                evidence_code=EvidenceCode.TRUST_REQUIRED.value,
                blocked_reason=BlockedReason.TRUST.value,
                detail="trust/hooks review prompt detected",
            )
        if _ERROR_RE.search(text) and elapsed_s < 2.0:
            return ReadinessObservation(
                status="failed",
                evidence_code=EvidenceCode.MALFORMED.value,
                failure_reason="provider emitted fatal/error output before ready",
            )
        if self.idle_re is not None and self.idle_re.search(text):
            return ReadinessObservation(
                status="ready",
                evidence_code=EvidenceCode.TUI_IDLE_PROMPT.value,
                detail="idle/input-ready marker observed",
            )
        # Known providers: process-alive through a stability interval is
        # accepted evidence when no blocked/error patterns were seen (#99).
        if (
            getattr(self, "allow_process_stable", False)
            and elapsed_s >= self.stability_s
            and alive
        ):
            return ReadinessObservation(
                status="ready",
                evidence_code=EvidenceCode.PROCESS_STABLE.value,
                detail="provider process stable without blocked/error evidence",
            )
        force = (env or os.environ).get("OMG_TEAM_PROVIDER_STABLE_READY")
        if force == "1" and elapsed_s >= self.stability_s and alive:
            return ReadinessObservation(
                status="ready",
                evidence_code=EvidenceCode.PROCESS_STABLE.value,
                detail="stable-alive override",
            )
        return ReadinessObservation(
            status="pending",
            evidence_code=EvidenceCode.PROCESS_STABLE.value,
            detail="waiting for provider readiness evidence",
        )


class GrokStrategy(_BaseStrategy):
    name = "grok"
    idle_re = _IDLE_GROK_RE
    stability_s = 0.5
    allow_process_stable = True


class CodexStrategy(_BaseStrategy):
    name = "codex"
    idle_re = _IDLE_CODEX_RE
    stability_s = 0.5
    allow_process_stable = True


class AntigravityStrategy(_BaseStrategy):
    """agy / Antigravity — capability contracts from #67 when available."""

    name = "agy"
    idle_re = re.compile(r"(?i)(TEAM_PROVIDER_READY_OK|ready\s+for\s+input)")
    stability_s = 0.5
    allow_process_stable = True


class CursorStrategy(_BaseStrategy):
    name = "cursor"
    idle_re = re.compile(r"(?i)(TEAM_PROVIDER_READY_OK)")
    stability_s = 0.5
    allow_process_stable = True


class GeminiStrategy(_BaseStrategy):
    name = "gemini"
    idle_re = re.compile(r"(?i)(TEAM_PROVIDER_READY_OK)")
    stability_s = 0.5
    allow_process_stable = True


class FixtureStrategy:
    """Hermetic fixture / fake interactive provider."""

    name = "fixture"

    def observe(
        self,
        *,
        provider_pid: int,
        alive: bool,
        capture_lines: Sequence[str],
        elapsed_s: float,
        env: Mapping[str, str] | None = None,
    ) -> ReadinessObservation:
        text = _joined(capture_lines)
        if not alive:
            return ReadinessObservation(
                status="failed",
                evidence_code=EvidenceCode.PROVIDER_EXITED.value,
                failure_reason="fixture provider exited before ready",
            )
        if "TEAM_PROVIDER_READY_OK" in text or elapsed_s >= 0.05:
            return ReadinessObservation(
                status="ready",
                evidence_code=EvidenceCode.FIXTURE_READY.value,
                detail="fixture provider alive",
            )
        return ReadinessObservation(
            status="pending",
            evidence_code=EvidenceCode.FIXTURE_READY.value,
        )


class FakeReadyStrategy:
    name = "fake-ready"

    def observe(
        self,
        *,
        provider_pid: int,
        alive: bool,
        capture_lines: Sequence[str],
        elapsed_s: float,
        env: Mapping[str, str] | None = None,
    ) -> ReadinessObservation:
        if not alive:
            return ReadinessObservation(
                status="failed",
                evidence_code=EvidenceCode.PROVIDER_EXITED.value,
                failure_reason="fake-ready provider not alive",
            )
        return ReadinessObservation(
            status="ready",
            evidence_code=EvidenceCode.FAKE_READY.value,
            detail="fake ready strategy",
        )


class FakeBlockedStrategy:
    name = "fake-blocked"

    def observe(
        self,
        *,
        provider_pid: int,
        alive: bool,
        capture_lines: Sequence[str],
        elapsed_s: float,
        env: Mapping[str, str] | None = None,
    ) -> ReadinessObservation:
        reason = (env or os.environ).get("OMG_TEAM_FAKE_BLOCKED_REASON") or "auth"
        if reason == "trust":
            return ReadinessObservation(
                status="blocked",
                evidence_code=EvidenceCode.TRUST_REQUIRED.value,
                blocked_reason=BlockedReason.TRUST.value,
            )
        return ReadinessObservation(
            status="blocked",
            evidence_code=EvidenceCode.AUTH_REQUIRED.value,
            blocked_reason=BlockedReason.AUTH.value,
        )


class FakeExitStrategy:
    name = "fake-exit"

    def observe(
        self,
        *,
        provider_pid: int,
        alive: bool,
        capture_lines: Sequence[str],
        elapsed_s: float,
        env: Mapping[str, str] | None = None,
    ) -> ReadinessObservation:
        return ReadinessObservation(
            status="failed",
            evidence_code=EvidenceCode.PROVIDER_EXITED.value,
            failure_reason="fake-exit strategy forced failure",
        )


class FakeTimeoutStrategy:
    name = "fake-timeout"

    def observe(
        self,
        *,
        provider_pid: int,
        alive: bool,
        capture_lines: Sequence[str],
        elapsed_s: float,
        env: Mapping[str, str] | None = None,
    ) -> ReadinessObservation:
        if not alive:
            return ReadinessObservation(
                status="failed",
                evidence_code=EvidenceCode.PROVIDER_EXITED.value,
                failure_reason="fake-timeout provider died",
            )
        return ReadinessObservation(
            status="pending",
            evidence_code=EvidenceCode.TIMEOUT.value,
            detail="fake-timeout never becomes ready",
        )


class UnknownStrategy:
    """Fail-closed: never optimistic success."""

    name = "unknown"

    def observe(
        self,
        *,
        provider_pid: int,
        alive: bool,
        capture_lines: Sequence[str],
        elapsed_s: float,
        env: Mapping[str, str] | None = None,
    ) -> ReadinessObservation:
        if not alive:
            return ReadinessObservation(
                status="failed",
                evidence_code=EvidenceCode.PROVIDER_EXITED.value,
                failure_reason="unknown provider exited",
            )
        return ReadinessObservation(
            status="unknown",
            evidence_code=EvidenceCode.UNKNOWN_PROVIDER.value,
            detail="unknown provider cannot prove readiness",
            blocked_reason=BlockedReason.UNKNOWN_UI.value,
        )


_REGISTRY: dict[str, Callable[[], ProviderReadinessStrategy]] = {
    "grok": GrokStrategy,
    "codex": CodexStrategy,
    "agy": AntigravityStrategy,
    "antigravity": AntigravityStrategy,
    "cursor": CursorStrategy,
    "gemini": GeminiStrategy,
    "fixture": FixtureStrategy,
    "fake-ready": FakeReadyStrategy,
    "fake-blocked": FakeBlockedStrategy,
    "fake-exit": FakeExitStrategy,
    "fake-timeout": FakeTimeoutStrategy,
}


def get_readiness_strategy(
    provider: str,
    *,
    env: Mapping[str, str] | None = None,
) -> ProviderReadinessStrategy:
    source = env if env is not None else os.environ
    override = str(source.get("OMG_TEAM_PROVIDER_STRATEGY") or "").strip()
    key = (override or provider or "unknown").strip().lower()
    factory = _REGISTRY.get(key)
    if factory is None:
        return UnknownStrategy()
    return factory()


__all__ = [
    "ReadinessObservation",
    "ProviderReadinessStrategy",
    "GrokStrategy",
    "CodexStrategy",
    "AntigravityStrategy",
    "CursorStrategy",
    "GeminiStrategy",
    "FixtureStrategy",
    "FakeReadyStrategy",
    "FakeBlockedStrategy",
    "FakeExitStrategy",
    "FakeTimeoutStrategy",
    "UnknownStrategy",
    "get_readiness_strategy",
]
