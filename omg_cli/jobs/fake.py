"""Hermetic FakeProvider — implements ProviderAdapter (#68 PR1).

Used only for durable-job tests. Modes via env (set by the job runner):

- ``OMG_JOB_FAKE_SLEEP`` — seconds to sleep before exit (default 0.05)
- ``OMG_JOB_FAKE_FAIL=1`` — exit class nonzero / returncode 1
- ``OMG_JOB_FAKE_LARGE=1`` — write ``artifacts/result.md`` (≥100KiB)
- ``OMG_JOB_FAKE_IGNORE_SIGTERM=1`` — install SIG_IGN for SIGTERM
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes, ensure_managed_dir
from omg_cli.providers.models import (
    DoctorReport,
    ProviderCapabilities,
    ProviderLaunchEnvelope,
    ProviderLaunchRequest,
    ProviderRunRequest,
    ProviderRunResult,
    ProviderUsage,
    VersionInfo,
)

PROVIDER_NAME = "fake"
_LARGE_BYTES = 100 * 1024 + 64


def _env_truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _sleep_s() -> float:
    raw = (os.environ.get("OMG_JOB_FAKE_SLEEP") or "0.05").strip()
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.05


class FakeProvider:
    """:class:`~omg_cli.providers.base.ProviderAdapter` for hermetic job tests."""

    name: str = PROVIDER_NAME

    def discover_binary(self) -> str:
        return "fake-provider"

    def probe_version(self, binary: str | None = None) -> VersionInfo:
        del binary
        return VersionInfo(raw="0.0.1", major=0, minor=0, patch=1)

    def probe_capabilities(self, binary: str | None = None) -> ProviderCapabilities:
        ver = self.probe_version(binary)
        return ProviderCapabilities(
            provider=PROVIDER_NAME,
            binary=self.discover_binary(),
            version=ver.raw,
            version_tuple=ver.as_tuple(),
            compat_status="compatible",
            authenticated=True,
            live_call_ready=False,
            output_formats=("text",),
            print_mode=True,
            limitations=("Hermetic fake provider for #68 PR1 jobs only.",),
        )

    def doctor(self, *, strict: bool = False) -> DoctorReport:
        caps = self.probe_capabilities()
        return DoctorReport(
            ok=True,
            exit_code=0,
            checks=("OK: fake provider ready (hermetic)",),
            capabilities=caps,
        )

    def build_launch_envelope(
        self, request: ProviderLaunchRequest
    ) -> ProviderLaunchEnvelope:
        """Not used by jobs; Team panes must not call fake launch."""
        del request
        raise RuntimeError("FakeProvider does not support Team launch envelopes")

    def run(self, request: ProviderRunRequest) -> ProviderRunResult:
        """Scripted worker invoked via Adapter.run inside the job runner child."""
        ignore_sigterm = _env_truthy("OMG_JOB_FAKE_IGNORE_SIGTERM")
        prev_handler = None
        if ignore_sigterm:
            prev_handler = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, signal.SIG_IGN)

        try:
            return self._run_body(request, ignore_sigterm=ignore_sigterm)
        finally:
            if ignore_sigterm and prev_handler is not None:
                try:
                    signal.signal(signal.SIGTERM, prev_handler)
                except (ValueError, OSError, TypeError):
                    # Best-effort restore (e.g. non-main thread).
                    pass

    def _run_body(
        self, request: ProviderRunRequest, *, ignore_sigterm: bool
    ) -> ProviderRunResult:
        job_dir = (os.environ.get("OMG_JOB_DIR") or "").strip()
        prompt = request.prompt or ""
        if request.prompt_file:
            try:
                prompt = Path(request.prompt_file).read_text(encoding="utf-8")
            except OSError as exc:
                return ProviderRunResult(
                    ok=False,
                    exit_class="spawn_error",
                    returncode=1,
                    output="",
                    error_message=f"failed to read prompt: {exc}",
                    argv=("fake",),
                )

        sleep_s = _sleep_s()
        # Keep ignore_sigterm coverage short for hermetic/CI (still long enough
        # for cancel's SIGTERM→SIGKILL path).
        if ignore_sigterm and sleep_s < 2.0:
            sleep_s = max(sleep_s, 2.0)

        # Honour cancel_event during normal sleeps (ignore_sigterm still needs
        # forced SIGKILL of the outer runner — cancel_event alone is ignored).
        deadline = time.monotonic() + sleep_s
        while time.monotonic() < deadline:
            if (
                not ignore_sigterm
                and request.cancel_event is not None
                and request.cancel_event.is_set()
            ):
                return ProviderRunResult(
                    ok=False,
                    exit_class="cancelled",
                    returncode=-15,
                    output="fake:cancelled\n",
                    stdout="fake:cancelled\n",
                    stderr="",
                    argv=("fake", "run", "--cancelled"),
                    cancelled=True,
                    partial_output=True,
                    error_message="fake worker cancelled",
                )
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

        lines = [
            f"fake:start provider={PROVIDER_NAME}",
            f"fake:prompt_len={len(prompt)}",
        ]
        artifacts: list[Any] = []
        result_rel: str | None = None

        if _env_truthy("OMG_JOB_FAKE_LARGE") and job_dir:
            art_dir = Path(job_dir) / "artifacts"
            ensure_managed_dir(art_dir)
            result_path = art_dir / "result.md"
            body = ("# fake large result\n" + ("x" * _LARGE_BYTES)).encode("utf-8")
            atomic_write_bytes(result_path, body, mode=DATA_FILE_MODE, replace=True)
            result_rel = "artifacts/result.md"
            from omg_cli.providers.models import ProviderArtifactRef

            artifacts.append(
                ProviderArtifactRef(
                    path=result_rel,
                    kind="result",
                    media_type="text/markdown",
                )
            )
            lines.append(f"fake:large_artifact={result_rel} bytes={len(body)}")

        stdout = "\n".join(lines) + "\n"
        usage = ProviderUsage(
            input_tokens=len(prompt),
            output_tokens=len(stdout),
            total_tokens=len(prompt) + len(stdout),
        )

        if _env_truthy("OMG_JOB_FAKE_FAIL"):
            return ProviderRunResult(
                ok=False,
                exit_class="nonzero",
                returncode=1,
                output=stdout,
                stdout=stdout,
                stderr="fake:fail\n",
                argv=("fake", "run", "--fail"),
                usage=usage,
                artifacts=tuple(artifacts),
                error_message="fake worker failed",
                retryable=True,
            )

        return ProviderRunResult(
            ok=True,
            exit_class="success",
            returncode=0,
            output=stdout,
            stdout=stdout,
            stderr="",
            argv=("fake", "run"),
            usage=usage,
            artifacts=tuple(artifacts),
        )


def get_adapter() -> FakeProvider:
    return FakeProvider()


__all__ = ["PROVIDER_NAME", "FakeProvider", "get_adapter"]
