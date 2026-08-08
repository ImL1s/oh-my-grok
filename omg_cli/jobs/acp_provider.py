"""Internal ``grok-acp-session`` ProviderAdapter (#105 PR4).

Long-lived ACP stdio sidecar: handshake → atomic receipt → drain until cancel.
Not admitted by public ``omg job start``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import DATA_FILE_MODE, atomic_write_bytes, ensure_managed_dir
from omg_cli.host_acp import (
    RECEIPT_KIND,
    AcpError,
    AcpStdioSession,
    discover_grok_binary,
    hash_cwd,
    hash_session_id,
    spawn_acp_stdio,
)
from omg_cli.providers.models import (
    DoctorReport,
    ProviderCapabilities,
    ProviderLaunchEnvelope,
    ProviderLaunchRequest,
    ProviderRunRequest,
    ProviderRunResult,
    VersionInfo,
)

PROVIDER_NAME = "grok-acp-session"
RECEIPT_RELPATH = "artifacts/grok_acp_resume_receipt.json"


def receipt_path(job_dir: Path) -> Path:
    return job_dir / RECEIPT_RELPATH


def write_receipt_atomic(job_dir: Path, receipt_body: dict[str, Any]) -> Path:
    """Atomic content-free receipt under the job dir."""
    ensure_managed_dir(job_dir / "artifacts")
    path = receipt_path(job_dir)
    data = json.dumps(receipt_body, sort_keys=True, indent=2).encode("utf-8")
    # Defense: never embed forbidden keys
    for banned in ("transcript", "messages", "authorization", "token", "raw"):
        if banned.encode() in data.lower() and banned in receipt_body:
            raise AcpError(f"refusing to write receipt with {banned}", code="E_ACP_RECEIPT")
    atomic_write_bytes(path, data, mode=DATA_FILE_MODE, replace=True)
    return path


class GrokAcpSessionProvider:
    """Internal jobs provider: durable ACP session/resume sidecar."""

    name: str = PROVIDER_NAME

    def discover_binary(self) -> str:
        override = (os.environ.get("OMG_ACP_BIN") or "").strip()
        if override:
            return override
        return discover_grok_binary()

    def probe_version(self, binary: str | None = None) -> VersionInfo:
        del binary
        return VersionInfo(raw="0.0.0-acp-session", major=0, minor=0, patch=0)

    def probe_capabilities(self, binary: str | None = None) -> ProviderCapabilities:
        ver = self.probe_version(binary)
        path = binary or self.discover_binary()
        return ProviderCapabilities(
            provider=PROVIDER_NAME,
            binary=path,
            version=ver.raw,
            version_tuple=ver.as_tuple(),
            compat_status="compatible",
            authenticated=None,
            live_call_ready=False,
            output_formats=("text",),
            print_mode=False,
            limitations=(
                "Internal ACP session sidecar only; not a public job provider.",
                "No session/close; cancel is process-group teardown.",
            ),
        )

    def doctor(self, *, strict: bool = False) -> DoctorReport:
        del strict
        caps = self.probe_capabilities()
        return DoctorReport(
            ok=True,
            exit_code=0,
            checks=("OK: internal grok-acp-session provider",),
            capabilities=caps,
        )

    def build_launch_envelope(
        self, request: ProviderLaunchRequest
    ) -> ProviderLaunchEnvelope:
        del request
        raise RuntimeError(
            "grok-acp-session does not support Team launch envelopes "
            "(ACP stdio is owned by the jobs runner)"
        )

    def run(self, request: ProviderRunRequest) -> ProviderRunResult:
        job_dir_s = (os.environ.get("OMG_JOB_DIR") or "").strip()
        if not job_dir_s:
            return _fail("OMG_JOB_DIR required for ACP sidecar", "spawn_error")
        job_dir = Path(job_dir_s)
        session_id = (request.session_id or request.resume_id or "").strip()
        # Prefer immutable request snapshot fields via env (set by runner).
        session_id = (
            (os.environ.get("OMG_ACP_SESSION_ID") or "").strip() or session_id
        )
        parent_run_id = (os.environ.get("OMG_ACP_PARENT_RUN_ID") or "").strip()
        cwd = (request.cwd or os.environ.get("OMG_ACP_CWD") or "").strip()
        if not session_id:
            return _fail("ACP session UUID missing", "spawn_error")
        if not cwd:
            return _fail("ACP cwd missing", "spawn_error")
        if not parent_run_id:
            return _fail("ACP parent run_id missing", "spawn_error")

        binary = request.binary or self.discover_binary()
        spawn_env = dict(os.environ)
        argv_override = None
        # Hermetic fake peer: OMG_ACP_BIN / binary points at *.py fixture.
        if str(binary).endswith(".py"):
            argv_override = [sys.executable, str(Path(binary).resolve())]

        attempt = 1
        try:
            attempt = int(os.environ.get("OMG_ACP_ATTEMPT") or "1")
        except ValueError:
            attempt = 1
        job_id = (os.environ.get("OMG_JOB_ID") or "").strip() or "unknown"
        sid_hash = hash_session_id(session_id)
        cwd_h = hash_cwd(cwd)

        proc = None
        session: AcpStdioSession | None = None
        try:
            proc, argv = spawn_acp_stdio(
                binary=binary if argv_override is None else sys.executable,
                cwd=cwd,
                env=spawn_env,
                on_process_started=request.on_process_started,
                argv_override=argv_override,
            )
            session = AcpStdioSession(
                proc=proc,
                argv=argv,
                session_id=session_id,
                cwd=str(Path(cwd).resolve()),
                job_id=job_id,
                attempt=attempt,
                parent_run_id=parent_run_id,
                session_id_hash=sid_hash,
                cwd_hash=cwd_h,
                host_version=os.environ.get("OMG_ACP_HOST_VERSION") or None,
                host_capability_source=os.environ.get("OMG_ACP_HOST_CAP_SOURCE")
                or None,
                quiet_window_s=float(
                    os.environ.get("OMG_ACP_QUIET_WINDOW_S") or "0.12"
                ),
            )
            timeout_s = float(request.timeout_s or 15.0)
            receipt = session.handshake(
                timeout_s=timeout_s,
                cancel_event=request.cancel_event,
            )
            body = receipt.to_dict()
            write_receipt_atomic(job_dir, body)

            # Long-lived: drain until cancel / peer death / protocol failure.
            exit_class = session.drain_until_cancel(cancel_event=request.cancel_event)
            if exit_class == "cancelled":
                session.close()
                return ProviderRunResult(
                    ok=False,
                    exit_class="cancelled",
                    returncode=0,
                    output="",
                    cancelled=True,
                    argv=argv,
                    error_message="ACP sidecar cancelled (process-group teardown; not session/close)",
                    session_id=None,
                    resume_supported=False,
                )
            session.close()
            return ProviderRunResult(
                ok=False,
                exit_class="nonzero",
                returncode=1,
                output="",
                argv=argv,
                error_message="ACP sidecar ended unexpectedly after ready",
            )
        except AcpError as exc:
            if session is not None:
                session.close()
            elif proc is not None:
                from omg_cli.host_acp import _kill_proc_group

                _kill_proc_group(proc)
            cancelled = bool(
                request.cancel_event is not None and request.cancel_event.is_set()
            )
            return ProviderRunResult(
                ok=False,
                exit_class="cancelled" if cancelled else "spawn_error",
                returncode=1,
                output="",
                cancelled=cancelled,
                error_message=_sanitize_error(str(exc)),
                overflow=exc.code == "E_ACP_OVERFLOW",
                timed_out=exc.code == "E_ACP_TIMEOUT",
            )
        except BaseException as exc:
            if session is not None:
                session.close()
            elif proc is not None:
                from omg_cli.host_acp import _kill_proc_group

                _kill_proc_group(proc)
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return ProviderRunResult(
                ok=False,
                exit_class="spawn_error",
                returncode=1,
                output="",
                error_message=_sanitize_error(str(exc)),
            )


def _sanitize_error(text: str) -> str:
    """Strip likely replay bodies / home paths from failure text."""
    banned = ("SECRET_REPLAY", "LATE_SECRET", str(Path.home()))
    out = text
    for b in banned:
        if b and b in out:
            out = out.replace(b, "[redacted]")
    return out[:500]


def _fail(msg: str, exit_class: str) -> ProviderRunResult:
    return ProviderRunResult(
        ok=False,
        exit_class=exit_class,  # type: ignore[arg-type]
        returncode=1,
        output="",
        error_message=msg,
    )


def get_acp_session_provider() -> GrokAcpSessionProvider:
    return GrokAcpSessionProvider()


__all__ = [
    "PROVIDER_NAME",
    "RECEIPT_KIND",
    "RECEIPT_RELPATH",
    "GrokAcpSessionProvider",
    "get_acp_session_provider",
    "receipt_path",
    "write_receipt_atomic",
]
