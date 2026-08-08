"""Strict ACP stdio initialize + session/resume client (#105 PR4).

Owns the JSON-RPC wire for ``grok agent stdio`` (argv-only, shell=False).
Does **not** set verified/passes, replay transcripts, or call session/close.
Conversation-content ``session/update`` notifications fail closed (no-replay).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

RECEIPT_KIND = "grok_acp_resume_receipt/v1"
TRANSPORT_KIND = "acp_stdio_job"

# Explicit conversation / transcript replay shapes — forbidden (no-replay).
_FORBIDDEN_SESSION_UPDATE_TYPES = frozenset(
    {
        "agent_message_chunk",
        "agent_thought_chunk",
        "user_message_chunk",
        "tool_call",
        "tool_call_update",
        "plan",
        "conversation",
        "message",
        "transcript",
    }
)

DEFAULT_HANDSHAKE_TIMEOUT_S = 15.0
DEFAULT_QUIET_WINDOW_S = 0.12
DEFAULT_MAX_LINE_BYTES = 256_000
DEFAULT_DRAIN_MAX_BYTES = 2_000_000


class AcpError(RuntimeError):
    """Fail-closed ACP transport / protocol error."""

    code: str = "E_ACP"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


@dataclass(frozen=True, slots=True)
class AcpResumeReceipt:
    """Bounded, content-free resume receipt (no transcript / secrets)."""

    job_id: str
    attempt: int
    parent_run_id: str
    session_id_hash: str
    cwd_hash: str
    transport: str = TRANSPORT_KIND
    initialized: bool = True
    resume_matched: bool = True
    no_replay_observed: bool = True
    restore_code_requested: bool = False
    connection_owned: bool = True
    host_version: str | None = None
    host_capability_source: str | None = None
    timestamp: str = ""
    receipt_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        body = {
            "kind": RECEIPT_KIND,
            "job_id": self.job_id,
            "attempt": int(self.attempt),
            "parent_run_id": self.parent_run_id,
            "session_id_hash": self.session_id_hash,
            "cwd_hash": self.cwd_hash,
            "transport": self.transport,
            "initialized": bool(self.initialized),
            "resume_matched": bool(self.resume_matched),
            "no_replay_observed": bool(self.no_replay_observed),
            "restore_code_requested": bool(self.restore_code_requested),
            "connection_owned": bool(self.connection_owned),
            "host_version": self.host_version,
            "host_capability_source": self.host_capability_source,
            "timestamp": self.timestamp
            or datetime.now(timezone.utc).isoformat(),
        }
        digest = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        body["receipt_sha256"] = digest
        return body


@dataclass(slots=True)
class AcpHandshakeResult:
    """Outcome of initialize + session/resume before long-lived drain."""

    ok: bool
    receipt: AcpResumeReceipt | None = None
    error: str | None = None
    error_code: str | None = None
    proc: subprocess.Popen[bytes] | None = None
    argv: tuple[str, ...] = ()


def hash_session_id(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def hash_cwd(cwd: str | Path) -> str:
    resolved = str(Path(cwd).resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()


def discover_grok_binary() -> str:
    """Resolve canonical Grok binary (argv[0] for ``agent stdio``)."""
    override = (os.environ.get("OMG_GROK_BIN") or "").strip()
    if override:
        path = Path(override)
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.resolve())
        raise AcpError(
            f"OMG_GROK_BIN is not an executable file: {override!r}",
            code="E_ACP_BINARY",
        )
    found = shutil.which("grok")
    if not found:
        raise AcpError("grok binary not found on PATH", code="E_ACP_BINARY")
    return found


def acp_stdio_argv(binary: str) -> list[str]:
    """Canonical argv — no ``--always-approve`` (no prompt/tool requests)."""
    if not isinstance(binary, str) or not binary.strip():
        raise AcpError("ACP binary path required", code="E_ACP_BINARY")
    if "\x00" in binary:
        raise AcpError("ACP binary path must not contain NUL", code="E_ACP_BINARY")
    return [binary.strip(), "agent", "stdio"]


def allowlisted_acp_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Minimal env for ACP stdio (no secrets promotion)."""
    src = dict(base) if base is not None else dict(os.environ)
    out: dict[str, str] = {}
    for key in (
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "XDG_RUNTIME_DIR",
        "OMG_GROK_BIN",
        "OMG_ACP_FAKE_SCENARIO",
        "OMG_ACP_FAKE_DELAY_S",
    ):
        val = src.get(key)
        if isinstance(val, str) and val:
            out[key] = val
    # Preserve Python for hermetic fake peer scripts.
    for key in ("PYTHONPATH", "VIRTUAL_ENV"):
        val = src.get(key)
        if isinstance(val, str) and val:
            out[key] = val
    return out


def classify_session_update(params: Mapping[str, Any]) -> str:
    """Return ``chrome`` | ``forbidden`` | ``unknown`` for a session/update."""
    update = params.get("update")
    if not isinstance(update, Mapping):
        # Bare sessionUpdate string forms.
        kind = params.get("sessionUpdate") or params.get("type")
        if isinstance(kind, str):
            return _classify_update_kind(kind)
        return "unknown"
    kind = update.get("sessionUpdate") or update.get("type")
    if not isinstance(kind, str):
        return "unknown"
    return _classify_update_kind(kind)


def _classify_update_kind(kind: str) -> str:
    k = kind.strip()
    if k in _FORBIDDEN_SESSION_UPDATE_TYPES:
        return "forbidden"
    # current-mode / available-commands / session-info chrome
    normalized = k.replace("-", "_")
    chrome = {
        "current_mode_update",
        "available_commands_update",
        "session_info_update",
        "config",
        "configuration",
    }
    if normalized in chrome or k in {
        "current_mode_update",
        "available_commands_update",
        "session_info_update",
    }:
        return "chrome"
    return "unknown"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_line(
    proc: subprocess.Popen[bytes],
    *,
    max_bytes: int,
    deadline: float,
    byte_budget: list[int],
    rx_buf: bytearray,
) -> bytes:
    """Read one NL-terminated frame; fail on timeout/EOF/overflow.

    Partial bytes are retained in *rx_buf* across calls so a quiet-window /
    poll timeout mid-frame cannot drop already-consumed bytes and turn a later
    replay notification into ``E_ACP_MALFORMED``.
    """
    if proc.stdout is None:
        raise AcpError("ACP stdout missing", code="E_ACP_IO")
    while True:
        nl = rx_buf.find(b"\n")
        if nl >= 0:
            line = bytes(rx_buf[:nl])
            del rx_buf[: nl + 1]
            byte_budget[0] += len(line) + 1
            if byte_budget[0] > max_bytes:
                raise AcpError("ACP byte overflow", code="E_ACP_OVERFLOW")
            if len(line) > max_bytes:
                raise AcpError("ACP line overflow", code="E_ACP_OVERFLOW")
            return line

        if time.monotonic() > deadline:
            raise AcpError("ACP read timed out", code="E_ACP_TIMEOUT")
        if proc.poll() is not None and not rx_buf:
            raise AcpError("ACP process exited before response", code="E_ACP_EOF")
        try:
            import select

            ready, _, _ = select.select([proc.stdout], [], [], 0.05)
            if not ready:
                if proc.poll() is not None:
                    raise AcpError(
                        "ACP process exited before complete line", code="E_ACP_EOF"
                    )
                continue
            # Read available bytes (pipe may return short); retain partials in rx_buf.
            chunk = proc.stdout.read(4096)
        except (OSError, ValueError) as exc:
            raise AcpError(f"ACP read failed: {exc}", code="E_ACP_IO") from exc
        if chunk is None or chunk == b"":
            if proc.poll() is not None:
                if rx_buf:
                    raise AcpError(
                        "ACP EOF with incomplete frame", code="E_ACP_EOF"
                    )
                raise AcpError("ACP EOF while reading line", code="E_ACP_EOF")
            time.sleep(0.01)
            continue
        rx_buf.extend(chunk)
        if len(rx_buf) > max_bytes:
            raise AcpError("ACP line overflow", code="E_ACP_OVERFLOW")


def _parse_rpc(line: bytes) -> dict[str, Any]:
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AcpError("ACP frame is not UTF-8", code="E_ACP_MALFORMED") from exc
    text = text.strip()
    if not text:
        raise AcpError("ACP empty frame", code="E_ACP_MALFORMED")
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AcpError("ACP malformed JSON", code="E_ACP_MALFORMED") from exc
    if not isinstance(obj, dict):
        raise AcpError("ACP frame must be a JSON object", code="E_ACP_MALFORMED")
    return obj


def _kill_proc_group(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.pid is None or proc.pid <= 1:
        return
    pid = int(proc.pid)
    if os.name == "posix":
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
    else:
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass
    try:
        proc.wait(timeout=2.0)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _write_rpc(proc: subprocess.Popen[bytes], payload: Mapping[str, Any]) -> None:
    if proc.stdin is None:
        raise AcpError("ACP stdin missing", code="E_ACP_IO")
    data = (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8")
    try:
        proc.stdin.write(data)
        proc.stdin.flush()
    except BrokenPipeError as exc:
        raise AcpError("ACP stdin broken pipe", code="E_ACP_EOF") from exc
    except OSError as exc:
        raise AcpError(f"ACP stdin write failed: {exc}", code="E_ACP_IO") from exc


@dataclass
class AcpStdioSession:
    """Live ACP stdio session: handshake then drain until cancel/failure."""

    proc: subprocess.Popen[bytes]
    argv: tuple[str, ...]
    session_id: str
    cwd: str
    job_id: str
    attempt: int
    parent_run_id: str
    session_id_hash: str
    cwd_hash: str
    host_version: str | None = None
    host_capability_source: str | None = None
    quiet_window_s: float = DEFAULT_QUIET_WINDOW_S
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES
    max_total_bytes: int = DEFAULT_DRAIN_MAX_BYTES
    _byte_budget: list[int] = field(default_factory=lambda: [0])
    _seen_ids: set[Any] = field(default_factory=set)
    _ready: bool = False
    _receipt: AcpResumeReceipt | None = None
    _rx_buf: bytearray = field(default_factory=bytearray)

    def handshake(
        self,
        *,
        timeout_s: float = DEFAULT_HANDSHAKE_TIMEOUT_S,
        cancel_event: threading.Event | None = None,
    ) -> AcpResumeReceipt:
        """initialize → session/resume → quiet window; fail closed on replay."""
        deadline = time.monotonic() + max(0.1, float(timeout_s))
        init_id = 1
        resume_id = 2

        _write_rpc(
            self.proc,
            {
                "jsonrpc": "2.0",
                "id": init_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientInfo": {"name": "oh-my-grok", "version": "0"},
                },
            },
        )
        self._await_result(init_id, deadline=deadline, cancel_event=cancel_event)

        _write_rpc(
            self.proc,
            {
                "jsonrpc": "2.0",
                "id": resume_id,
                "method": "session/resume",
                "params": {
                    "sessionId": self.session_id,
                    "cwd": self.cwd,
                    "noReplay": True,
                    "restoreCode": False,
                },
            },
        )
        self._await_result(resume_id, deadline=deadline, cancel_event=cancel_event)

        # Quiet window: reject late conversation replay before ready.
        quiet_deadline = time.monotonic() + max(0.0, float(self.quiet_window_s))
        while time.monotonic() < quiet_deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise AcpError("ACP handshake cancelled", code="E_ACP_CANCELLED")
            if self.proc.poll() is not None:
                raise AcpError(
                    "ACP process exited during quiet window", code="E_ACP_EOF"
                )
            remaining = quiet_deadline - time.monotonic()
            if remaining <= 0:
                break
            msg = self._try_read_message(
                deadline=min(deadline, time.monotonic() + min(0.05, remaining)),
                cancel_event=cancel_event,
                allow_timeout=True,
            )
            if msg is None:
                continue
            self._handle_notification_or_reject(msg, phase="quiet")

        if self.proc.poll() is not None:
            raise AcpError(
                "ACP process exited before readiness (transient handshake)",
                code="E_ACP_EOF",
            )

        receipt = AcpResumeReceipt(
            job_id=self.job_id,
            attempt=self.attempt,
            parent_run_id=self.parent_run_id,
            session_id_hash=self.session_id_hash,
            cwd_hash=self.cwd_hash,
            host_version=self.host_version,
            host_capability_source=self.host_capability_source,
            timestamp=_utc_now(),
        )
        # Materialize sha
        body = receipt.to_dict()
        self._receipt = AcpResumeReceipt(
            job_id=receipt.job_id,
            attempt=receipt.attempt,
            parent_run_id=receipt.parent_run_id,
            session_id_hash=receipt.session_id_hash,
            cwd_hash=receipt.cwd_hash,
            host_version=receipt.host_version,
            host_capability_source=receipt.host_capability_source,
            timestamp=body["timestamp"],
            receipt_sha256=body["receipt_sha256"],
        )
        self._ready = True
        return self._receipt

    def drain_until_cancel(
        self,
        *,
        cancel_event: threading.Event | None = None,
        idle_poll_s: float = 0.05,
    ) -> str:
        """Discard allowed chrome notifications until cancel or failure.

        Returns exit class: ``cancelled`` | ``failed``.
        """
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return "cancelled"
            if self.proc.poll() is not None:
                return "failed"
            msg = self._try_read_message(
                deadline=time.monotonic() + idle_poll_s,
                cancel_event=cancel_event,
                allow_timeout=True,
            )
            if msg is None:
                continue
            try:
                self._handle_notification_or_reject(msg, phase="drain")
            except AcpError:
                return "failed"

    def close(self) -> None:
        _kill_proc_group(self.proc)

    def _await_result(
        self,
        expect_id: Any,
        *,
        deadline: float,
        cancel_event: threading.Event | None,
    ) -> dict[str, Any]:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise AcpError("ACP handshake cancelled", code="E_ACP_CANCELLED")
            msg = self._try_read_message(
                deadline=deadline, cancel_event=cancel_event, allow_timeout=False
            )
            if msg is None:
                raise AcpError("ACP read timed out", code="E_ACP_TIMEOUT")
            if "method" in msg and "id" not in msg:
                self._handle_notification_or_reject(msg, phase="handshake")
                continue
            if "id" not in msg:
                raise AcpError("ACP response missing id", code="E_ACP_PROTOCOL")
            rid = msg["id"]
            if rid in self._seen_ids:
                raise AcpError("ACP duplicate response id", code="E_ACP_PROTOCOL")
            if rid != expect_id:
                raise AcpError(
                    f"ACP mismatched response id (want {expect_id!r} got {rid!r})",
                    code="E_ACP_PROTOCOL",
                )
            self._seen_ids.add(rid)
            if "error" in msg and msg["error"] is not None:
                err = msg["error"]
                detail = err if not isinstance(err, dict) else err.get("message", err)
                raise AcpError(f"ACP RPC error: {detail}", code="E_ACP_RPC")
            if "result" not in msg:
                raise AcpError("ACP response missing result", code="E_ACP_PROTOCOL")
            result = msg["result"]
            if not isinstance(result, dict):
                # initialize may return non-dict in some peers — accept null/object.
                if result is None:
                    return {}
                raise AcpError("ACP result must be object or null", code="E_ACP_PROTOCOL")
            return result

    def _try_read_message(
        self,
        *,
        deadline: float,
        cancel_event: threading.Event | None,
        allow_timeout: bool,
    ) -> dict[str, Any] | None:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise AcpError("ACP cancelled", code="E_ACP_CANCELLED")
            now = time.monotonic()
            if now > deadline:
                if allow_timeout:
                    return None
                raise AcpError("ACP read timed out", code="E_ACP_TIMEOUT")
            try:
                line = _read_line(
                    self.proc,
                    max_bytes=self.max_line_bytes,
                    deadline=deadline,
                    byte_budget=self._byte_budget,
                    rx_buf=self._rx_buf,
                )
            except AcpError as exc:
                if allow_timeout and exc.code == "E_ACP_TIMEOUT":
                    # Keep any partial frame in _rx_buf for the next poll.
                    return None
                raise
            if self._byte_budget[0] > self.max_total_bytes:
                raise AcpError("ACP byte overflow", code="E_ACP_OVERFLOW")
            return _parse_rpc(line)

    def _handle_notification_or_reject(
        self, msg: Mapping[str, Any], *, phase: str
    ) -> None:
        method = msg.get("method")
        if method != "session/update":
            raise AcpError(
                f"ACP disallowed notification method {method!r} in {phase}",
                code="E_ACP_PROTOCOL",
            )
        params = msg.get("params")
        if not isinstance(params, Mapping):
            raise AcpError("ACP session/update missing params", code="E_ACP_PROTOCOL")
        kind = classify_session_update(params)
        if kind == "forbidden":
            raise AcpError(
                "ACP conversation replay notification forbidden "
                f"(phase={phase}, no_replay)",
                code="E_ACP_REPLAY",
            )
        if kind == "unknown":
            raise AcpError(
                f"ACP unknown session/update in {phase}", code="E_ACP_PROTOCOL"
            )
        # chrome: discard (never write bodies to stdout/artifacts)


def spawn_acp_stdio(
    *,
    binary: str,
    cwd: str | Path,
    env: Mapping[str, str] | None = None,
    on_process_started: Any | None = None,
    argv_override: Sequence[str] | None = None,
) -> tuple[subprocess.Popen[bytes], tuple[str, ...]]:
    """Spawn ``grok agent stdio`` with stdin/stdout pipes (shell=False).

    ``argv_override`` is for hermetic fake peers only (must still be shell=False).
    """
    if argv_override is not None:
        if not argv_override:
            raise AcpError("argv_override must be non-empty", code="E_ACP_BINARY")
        argv = tuple(str(x) for x in argv_override)
        for i, part in enumerate(argv):
            if not part or "\x00" in part:
                raise AcpError(
                    f"argv_override[{i}] invalid", code="E_ACP_BINARY"
                )
    else:
        argv = tuple(acp_stdio_argv(binary))
    cwd_path = Path(cwd).resolve()
    if not cwd_path.is_dir():
        raise AcpError(f"ACP cwd is not a directory: {cwd}", code="E_ACP_CWD")
    child_env = allowlisted_acp_env(env)
    popen_kwargs: dict[str, Any] = {
        "args": list(argv),
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
        "cwd": str(cwd_path),
        "env": child_env,
        "bufsize": 0,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(**popen_kwargs)  # noqa: S603 — argv list, no shell
        if on_process_started is not None:
            on_process_started(proc)
        return proc, argv
    except BaseException:
        _kill_proc_group(proc)
        raise


def build_receipt_from_dict(data: Mapping[str, Any]) -> AcpResumeReceipt:
    return AcpResumeReceipt(
        job_id=str(data.get("job_id") or ""),
        attempt=int(data.get("attempt") or 0),
        parent_run_id=str(data.get("parent_run_id") or ""),
        session_id_hash=str(data.get("session_id_hash") or ""),
        cwd_hash=str(data.get("cwd_hash") or ""),
        transport=str(data.get("transport") or TRANSPORT_KIND),
        initialized=bool(data.get("initialized", True)),
        resume_matched=bool(data.get("resume_matched", True)),
        no_replay_observed=bool(data.get("no_replay_observed", True)),
        restore_code_requested=bool(data.get("restore_code_requested", False)),
        connection_owned=bool(data.get("connection_owned", True)),
        host_version=data.get("host_version")
        if isinstance(data.get("host_version"), str)
        else None,
        host_capability_source=data.get("host_capability_source")
        if isinstance(data.get("host_capability_source"), str)
        else None,
        timestamp=str(data.get("timestamp") or ""),
        receipt_sha256=str(data.get("receipt_sha256") or ""),
    )


def validate_receipt(
    data: Mapping[str, Any],
    *,
    session_id_hash: str,
    cwd_hash: str,
    parent_run_id: str,
) -> AcpResumeReceipt:
    """Fail-closed receipt validation (content-free, hash-bound)."""
    if data.get("kind") != RECEIPT_KIND:
        raise AcpError("receipt kind mismatch", code="E_ACP_RECEIPT")
    if data.get("transport") != TRANSPORT_KIND:
        raise AcpError("receipt transport mismatch", code="E_ACP_RECEIPT")
    if data.get("session_id_hash") != session_id_hash:
        raise AcpError("receipt session_id_hash mismatch", code="E_ACP_RECEIPT")
    if data.get("cwd_hash") != cwd_hash:
        raise AcpError("receipt cwd_hash mismatch", code="E_ACP_RECEIPT")
    if data.get("parent_run_id") != parent_run_id:
        raise AcpError("receipt parent_run_id mismatch", code="E_ACP_RECEIPT")
    if data.get("no_replay_observed") is not True:
        raise AcpError("receipt missing no_replay_observed", code="E_ACP_RECEIPT")
    if data.get("restore_code_requested") is not False:
        raise AcpError("receipt restore_code must be false", code="E_ACP_RECEIPT")
    if data.get("connection_owned") is not True:
        raise AcpError("receipt connection_owned required", code="E_ACP_RECEIPT")
    # Forbidden content keys
    for banned in (
        "transcript",
        "messages",
        "authorization",
        "token",
        "raw",
        "stdout",
        "stderr",
        "home",
    ):
        if banned in data:
            raise AcpError(
                f"receipt must not contain {banned!r}", code="E_ACP_RECEIPT"
            )
    receipt = build_receipt_from_dict(data)
    # Recompute sha over body without receipt_sha256
    body = {k: v for k, v in dict(data).items() if k != "receipt_sha256"}
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if data.get("receipt_sha256") != digest:
        raise AcpError("receipt sha256 mismatch", code="E_ACP_RECEIPT")
    return receipt


__all__ = [
    "RECEIPT_KIND",
    "TRANSPORT_KIND",
    "AcpError",
    "AcpHandshakeResult",
    "AcpResumeReceipt",
    "AcpStdioSession",
    "acp_stdio_argv",
    "allowlisted_acp_env",
    "build_receipt_from_dict",
    "classify_session_update",
    "discover_grok_binary",
    "hash_cwd",
    "hash_session_id",
    "spawn_acp_stdio",
    "validate_receipt",
]
