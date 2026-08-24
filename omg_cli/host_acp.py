"""Strict ACP stdio initialize + session/resume client (#105 PR4/PR5).

Owns the JSON-RPC wire for ``grok agent stdio`` (argv-only, shell=False).
Does **not** set verified/passes, replay transcripts, or call session/close.
Conversation-content ``session/update`` notifications fail closed (no-replay).
Grok vendor notifications whose method starts with ``_x.ai/`` are chrome
(discarded; bodies are never logged — they may carry MCP env).

``session/resume`` request params are ``sessionId`` + ``cwd`` only. The
result allowlist is ``modes`` / ``models`` / ``configOptions`` / ``_meta``
(empty ``{}`` is valid). Identity is the JSON-RPC response id plus
request-derived hashes — not result echo. Peer stderr is drained.
Validate then drop the result (never persist/log ``_meta`` or raw result).
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
# Live ``grok agent stdio`` emits these during initialize/resume (MCP
# inventory, model list, announcements). Treat as chrome. Never log params.
_ACP_VENDOR_CHROME_PREFIXES = ("_x.ai/",)

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
_STDERR_DRAIN_CHUNK = 8192
_STDOUT_READ_CHUNK = 4096
_CANCEL_POLL_INTERVAL_S = 0.05


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
    resume_matched: bool = False
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
            "initialized": self.initialized is True,
            "resume_matched": self.resume_matched is True,
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


def bind_constructor_identity(
    session_id: str,
    session_id_hash: str,
    cwd: str | Path,
    cwd_hash: str,
) -> tuple[str, str]:
    """Fail closed when constructor hashes diverge from UUID/cwd.

    Returns the derived ``(session_id_hash, cwd_hash)`` pair so receipts
    cannot carry independently supplied identity hashes.
    """
    derived_sid_hash = hash_session_id(session_id)
    derived_cwd_hash = hash_cwd(cwd)
    if session_id_hash != derived_sid_hash:
        raise AcpError(
            "constructor session identity hash mismatch",
            code="E_ACP_IDENTITY",
        )
    if cwd_hash != derived_cwd_hash:
        raise AcpError(
            "constructor cwd identity hash mismatch",
            code="E_ACP_IDENTITY",
        )
    return derived_sid_hash, derived_cwd_hash


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
        "OMG_ACP_FAKE_CHROME_COUNT",
        "OMG_ACP_FAKE_SUFFIX_BYTES",
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


def is_vendor_chrome_method(method: Any) -> bool:
    """True for Grok ``_x.ai/*`` stdio chrome (not conversation replay)."""
    if not isinstance(method, str):
        return False
    return method.startswith(_ACP_VENDOR_CHROME_PREFIXES)


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


# Official ResumeSessionResponse top-level allowlist (ACP 0.11.4 / latest).
# Empty {} is valid. sessionId / resumed / replay bags are unknown keys.
_RESUME_RESULT_ALLOWED_KEYS = frozenset(
    {"modes", "models", "configOptions", "_meta"}
)


def validate_resume_result(result: Any) -> None:
    """Fail-closed top-level allowlist on ``session/resume`` result.

    Official ``ResumeSessionResponse`` may contain only ``modes``
    (object or null, SessionModeState container), ``models`` (object or
    null), ``configOptions`` (array or null), and ``_meta`` (object or
    null). Empty ``{}`` is valid. The result does not echo ``sessionId``
    or ``resumed``; handshake identity is the JSON-RPC response id plus
    request-derived hashes.

    Raises ``E_ACP_RESUME`` when the result is not a JSON object, an
    unknown top-level key is present, or an allowed key has the wrong
    container type. Does not walk nested fields (unknown top-level keys
    already reject replay bags). Diagnostics name field names only
    (never values, secrets, session ids, or ``_meta`` contents).
    """
    if not isinstance(result, dict):
        raise AcpError(
            "ACP resume result must be a JSON object", code="E_ACP_RESUME"
        )
    for key in result:
        if key not in _RESUME_RESULT_ALLOWED_KEYS:
            label = key if isinstance(key, str) else type(key).__name__
            raise AcpError(
                f"ACP resume result unknown field {label}",
                code="E_ACP_RESUME",
            )
    if "modes" in result:
        modes = result["modes"]
        if modes is not None and not isinstance(modes, dict):
            raise AcpError(
                "ACP resume result modes must be an object or null",
                code="E_ACP_RESUME",
            )
    if "models" in result:
        models = result["models"]
        if models is not None and not isinstance(models, dict):
            raise AcpError(
                "ACP resume result models must be an object or null",
                code="E_ACP_RESUME",
            )
    if "configOptions" in result:
        config_options = result["configOptions"]
        if config_options is not None and not isinstance(config_options, list):
            raise AcpError(
                "ACP resume result configOptions must be an array or null",
                code="E_ACP_RESUME",
            )
    if "_meta" in result:
        meta = result["_meta"]
        if meta is not None and not isinstance(meta, dict):
            raise AcpError(
                "ACP resume result _meta must be an object or null",
                code="E_ACP_RESUME",
            )


def _start_stderr_drain(proc: subprocess.Popen[bytes]) -> threading.Thread:
    """Daemon-read peer stderr so a PIPE cannot deadlock the handshake.

    Bytes are discarded immediately. Never written to artifacts.
    """

    def _drain() -> None:
        stream = proc.stderr
        if stream is None:
            return
        try:
            while True:
                chunk = stream.read(_STDERR_DRAIN_CHUNK)
                if not chunk:
                    break
                # Discard immediately — never persist or retain token-bearing bytes.
                del chunk
        except (OSError, ValueError):
            return

    thread = threading.Thread(
        target=_drain,
        name=f"acp-stderr-drain-{getattr(proc, 'pid', 0)}",
        daemon=True,
    )
    thread.start()
    return thread


def _received_bytes(byte_budget: list[int], rx_buf: bytearray) -> int:
    return byte_budget[0] + len(rx_buf)


def _incomplete_line_len(rx_buf: bytearray) -> int:
    nl = rx_buf.rfind(b"\n")
    if nl < 0:
        return len(rx_buf)
    return len(rx_buf) - (nl + 1)


def _extract_complete_frame(
    rx_buf: bytearray,
    byte_budget: list[int],
    *,
    max_line_bytes: int,
) -> bytes | None:
    """Pop one complete NL-terminated frame, or ``None`` if none complete.

    Increments *byte_budget* only when a frame is extracted so leftover
    is not double-counted. Does not read the pipe.
    """
    nl = rx_buf.find(b"\n")
    if nl < 0:
        return None
    line = bytes(rx_buf[:nl])
    del rx_buf[: nl + 1]
    byte_budget[0] += len(line) + 1
    if len(line) > max_line_bytes:
        raise AcpError("ACP line overflow", code="E_ACP_OVERFLOW")
    return line


def _max_buffered_frame_len(rx_buf: bytearray) -> int:
    """Max payload of every complete frame and the incomplete suffix.

    Complete-frame payload is bytes between start and next NL
    (excluding the NL). The incomplete suffix is bytes after the last NL
    (or the whole buffer if none). Empty buffer → 0. A buffer that ends
    in NL has incomplete suffix length 0; the last complete frame
    still counts.
    """
    if not rx_buf:
        return 0
    max_len = 0
    start = 0
    while True:
        nl = rx_buf.find(b"\n", start)
        if nl < 0:
            suffix = len(rx_buf) - start
            if suffix > max_len:
                max_len = suffix
            return max_len
        payload = nl - start
        if payload > max_len:
            max_len = payload
        start = nl + 1


def _raise_if_buffered_limits(
    byte_budget: list[int],
    rx_buf: bytearray,
    *,
    max_line_bytes: int,
    max_total_bytes: int,
    include_complete_frames: bool = False,
) -> None:
    """Fail closed on leftover (default) or every complete+incomplete frame.

    Extract-first leftover-only path uses the suffix after the last NL.
    Timeout/receipt paths pass ``include_complete_frames=True`` so each
    complete frame is classified individually against *max_line_bytes*.
    Total is always committed budget plus ``len(rx_buf)`` — complete-frame
    sizes are not added into *byte_budget* while scanning. Line is checked
    before total.
    """
    line_len = (
        _max_buffered_frame_len(rx_buf)
        if include_complete_frames
        else _incomplete_line_len(rx_buf)
    )
    if line_len > max_line_bytes:
        raise AcpError("ACP line overflow", code="E_ACP_OVERFLOW")
    if _received_bytes(byte_budget, rx_buf) > max_total_bytes:
        raise AcpError("ACP byte overflow", code="E_ACP_OVERFLOW")


def _absorb_pending_continuation(
    proc: subprocess.Popen[bytes],
    byte_budget: list[int],
    rx_buf: bytearray,
    *,
    max_line_bytes: int,
    max_total_bytes: int,
    deadline: float,
    cancel_event: threading.Event | None = None,
) -> bool:
    """Drain currently-ready pipe chunks; wait only for a partial suffix.

    Never returns just because *rx_buf* ends on NL. When there is no
    incomplete suffix, nonblocking-probes and drains every chunk already
    readable on stdout. When a partial suffix exists, waits in
    ``_CANCEL_POLL_INTERVAL_S`` slices (rechecking *cancel_event* each
    poll) up to *deadline*. Does not extract frames or increment
    *byte_budget*.

    Overflow of any complete or incomplete frame is classified before
    cancel, EOF, or timeout. Only an empty-bytes read (``b""``) is
    proven EOF; ``chunk is None`` is not EOF (no bytes available now).
    An empty read while a partial suffix remains is ``E_ACP_EOF``
    even if ``proc.poll()`` is still ``None``. An empty read after
    only complete frames returns ``True`` so the caller can F9-drain
    then fail ``E_ACP_EOF``. Deadline / no-ready with a partial suffix
    is ``E_ACP_TIMEOUT`` (never a silent return). Returns ``False``
    when the probe finds no further ready data.
    """
    if proc.stdout is None:
        raise AcpError("ACP stdout missing", code="E_ACP_IO")
    import select

    while True:
        # Already-buffered overflow beats cancel and EOF.
        _raise_if_buffered_limits(
            byte_budget,
            rx_buf,
            max_line_bytes=max_line_bytes,
            max_total_bytes=max_total_bytes,
            include_complete_frames=True,
        )
        if cancel_event is not None and cancel_event.is_set():
            raise AcpError("ACP handshake cancelled", code="E_ACP_CANCELLED")
        has_partial = _incomplete_line_len(rx_buf) > 0
        if has_partial:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # F12: still drain currently-ready bytes; no-ready → TIMEOUT.
                timeout = 0.0
            else:
                timeout = min(remaining, _CANCEL_POLL_INTERVAL_S)
        else:
            timeout = 0.0
        try:
            ready, _, _ = select.select([proc.stdout], [], [], timeout)
            if not ready:
                _raise_if_buffered_limits(
                    byte_budget,
                    rx_buf,
                    max_line_bytes=max_line_bytes,
                    max_total_bytes=max_total_bytes,
                    include_complete_frames=True,
                )
                if cancel_event is not None and cancel_event.is_set():
                    raise AcpError(
                        "ACP handshake cancelled", code="E_ACP_CANCELLED"
                    )
                if has_partial:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AcpError(
                            "ACP read timed out", code="E_ACP_TIMEOUT"
                        )
                    continue
                if proc.poll() is not None:
                    return True
                return False
            # Recheck cancel before read; overflow already classified.
            if cancel_event is not None and cancel_event.is_set():
                raise AcpError(
                    "ACP handshake cancelled", code="E_ACP_CANCELLED"
                )
            chunk = proc.stdout.read(_STDOUT_READ_CHUNK)
        except (OSError, ValueError) as exc:
            raise AcpError(f"ACP read failed: {exc}", code="E_ACP_IO") from exc
        if chunk == b"":
            _raise_if_buffered_limits(
                byte_budget,
                rx_buf,
                max_line_bytes=max_line_bytes,
                max_total_bytes=max_total_bytes,
                include_complete_frames=True,
            )
            if has_partial:
                raise AcpError(
                    "ACP EOF with incomplete frame", code="E_ACP_EOF"
                )
            return True
        if chunk is None:
            if cancel_event is not None and cancel_event.is_set():
                raise AcpError(
                    "ACP handshake cancelled", code="E_ACP_CANCELLED"
                )
            if has_partial:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _raise_if_buffered_limits(
                        byte_budget,
                        rx_buf,
                        max_line_bytes=max_line_bytes,
                        max_total_bytes=max_total_bytes,
                        include_complete_frames=True,
                    )
                    raise AcpError(
                        "ACP read timed out", code="E_ACP_TIMEOUT"
                    )
                continue
            return False
        rx_buf.extend(chunk)


def _read_line(
    proc: subprocess.Popen[bytes],
    *,
    max_bytes: int,
    deadline: float,
    byte_budget: list[int],
    rx_buf: bytearray,
    max_total_bytes: int = DEFAULT_DRAIN_MAX_BYTES,
    cancel_event: threading.Event | None = None,
) -> bytes:
    """Read one NL-terminated frame; fail on timeout/EOF/overflow/cancel.

    *max_bytes* is the **per-line** cap only. The cumulative ceiling is
    committed ``byte_budget`` plus currently buffered leftover bytes
    versus *max_total_bytes*.

    Partial bytes are retained in *rx_buf* across calls so a quiet-window /
    poll timeout mid-frame cannot drop already-consumed bytes and turn a later
    replay notification into ``E_ACP_MALFORMED``.

    Leftover incomplete suffix (bytes after the last NL, or the whole
    buffer if none) is checked against *max_bytes* before poll/timeout
    so a quiet window cannot treat an already-over-limit incomplete
    frame as "no message". Extract-first uses leftover-only checks;
    all complete+incomplete frames are classified individually before
    timeout/receipt. Committed budget plus leftover is also
    checked against *max_total_bytes* before poll/timeout and after
    append. ``byte_budget`` is incremented only when a complete NL frame is
    extracted so leftover is not double-counted when that frame later
    completes.

    *cancel_event* is sampled every ``_CANCEL_POLL_INTERVAL_S``. A complete
    frame already in *rx_buf* is returned even if cancel is set. Leftover
    overflow beats cancel. ``chunk == b""`` is proven EOF regardless of
    ``poll()``; ``chunk is None`` is not EOF (no bytes now).
    """
    if proc.stdout is None:
        raise AcpError("ACP stdout missing", code="E_ACP_IO")
    import select

    while True:
        extracted = _extract_complete_frame(
            rx_buf, byte_budget, max_line_bytes=max_bytes
        )
        if extracted is not None:
            return extracted

        # Leftover-only overflow beats cancel and timeout.
        _raise_if_buffered_limits(
            byte_budget,
            rx_buf,
            max_line_bytes=max_bytes,
            max_total_bytes=max_total_bytes,
        )

        if cancel_event is not None and cancel_event.is_set():
            raise AcpError("ACP handshake cancelled", code="E_ACP_CANCELLED")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AcpError("ACP read timed out", code="E_ACP_TIMEOUT")

        try:
            ready, _, _ = select.select(
                [proc.stdout],
                [],
                [],
                min(remaining, _CANCEL_POLL_INTERVAL_S),
            )
            if not ready:
                # Not-ready is never EOF (only b"" is). Overflow, then cancel,
                # then deadline — same order as absorb's poll-exit path.
                _raise_if_buffered_limits(
                    byte_budget,
                    rx_buf,
                    max_line_bytes=max_bytes,
                    max_total_bytes=max_total_bytes,
                    include_complete_frames=True,
                )
                if cancel_event is not None and cancel_event.is_set():
                    raise AcpError(
                        "ACP handshake cancelled", code="E_ACP_CANCELLED"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AcpError("ACP read timed out", code="E_ACP_TIMEOUT")
                continue
            if cancel_event is not None and cancel_event.is_set():
                raise AcpError(
                    "ACP handshake cancelled", code="E_ACP_CANCELLED"
                )
            chunk = proc.stdout.read(_STDOUT_READ_CHUNK)
        except (OSError, ValueError) as exc:
            raise AcpError(f"ACP read failed: {exc}", code="E_ACP_IO") from exc
        if chunk == b"":
            _raise_if_buffered_limits(
                byte_budget,
                rx_buf,
                max_line_bytes=max_bytes,
                max_total_bytes=max_total_bytes,
                include_complete_frames=True,
            )
            if rx_buf:
                raise AcpError(
                    "ACP EOF with incomplete frame", code="E_ACP_EOF"
                )
            raise AcpError("ACP EOF while reading line", code="E_ACP_EOF")
        if chunk is None:
            if cancel_event is not None and cancel_event.is_set():
                raise AcpError(
                    "ACP handshake cancelled", code="E_ACP_CANCELLED"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _raise_if_buffered_limits(
                    byte_budget,
                    rx_buf,
                    max_line_bytes=max_bytes,
                    max_total_bytes=max_total_bytes,
                    include_complete_frames=True,
                )
                raise AcpError("ACP read timed out", code="E_ACP_TIMEOUT")
            continue
        rx_buf.extend(chunk)
        # Incomplete-frame cap only: many complete small lines may share a
        # read chunk; do not treat their combined size as one-line overflow.
        # Extract complete frames first; leftover-only total ceiling.
        if b"\n" not in rx_buf:
            _raise_if_buffered_limits(
                byte_budget,
                rx_buf,
                max_line_bytes=max_bytes,
                max_total_bytes=max_total_bytes,
            )


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
        """initialize → session/resume → quiet window; fail closed on replay.

        Pre-read protocol, overflow, and EOF evidence keeps precedence over
        cancellation.  Cancellation is checked before any future/blocking
        read and again at the final receipt boundary; timeout is considered
        only after already-buffered protocol evidence.
        """
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
                },
            },
        )
        resume_result = self._await_result(
            resume_id,
            deadline=deadline,
            cancel_event=cancel_event,
            require_object=True,
        )
        validate_resume_result(resume_result)
        derived_sid_hash, derived_cwd_hash = bind_constructor_identity(
            self.session_id,
            self.session_id_hash,
            self.cwd,
            self.cwd_hash,
        )

        # The resume response may share one read with complete notifications.
        # Classify every already-buffered frame before cancellation so a
        # coalesced replay/malformed/protocol violation cannot be masked.
        cancel_observed = self._drain_complete_buffered_frames(
            phase="pre-receipt", cancel_event=cancel_event
        )
        if cancel_observed:
            raise AcpError("ACP handshake cancelled", code="E_ACP_CANCELLED")

        # Quiet window: reject late conversation replay before ready.
        quiet_deadline = time.monotonic() + max(0.0, float(self.quiet_window_s))
        while time.monotonic() < quiet_deadline:
            # A previous read may have coalesced more than one complete frame.
            # Drain those observed frames before honoring cancellation.
            cancel_observed = self._drain_complete_buffered_frames(
                phase="quiet", cancel_event=cancel_event
            )
            if self.proc.poll() is not None:
                # Overflow beats EOF so an exited peer with an already
                # buffered oversized complete/incomplete frame cannot
                # be classified as a transient handshake failure.
                self._raise_if_rx_over_limits(include_complete_frames=True)
                raise AcpError(
                    "ACP process exited during quiet window", code="E_ACP_EOF"
                )
            if cancel_observed:
                raise AcpError("ACP handshake cancelled", code="E_ACP_CANCELLED")
            remaining = quiet_deadline - time.monotonic()
            if remaining <= 0:
                break
            msg = self._try_read_message(
                deadline=min(
                    deadline,
                    time.monotonic() + min(_CANCEL_POLL_INTERVAL_S, remaining),
                ),
                cancel_event=cancel_event,
                allow_timeout=True,
            )
            if msg is None:
                continue
            self._handle_notification_or_reject(msg, phase="quiet")

        # A handler can observe cancellation after one frame in a coalesced
        # read. Finish classifying all frames from that read before deciding
        # whether any future pipe read is allowed.
        cancel_observed = self._drain_complete_buffered_frames(
            phase="pre-receipt", cancel_event=cancel_event
        )
        if cancel_observed:
            raise AcpError("ACP handshake cancelled", code="E_ACP_CANCELLED")

        # Absorb + overflow BEFORE poll→EOF. An exited peer may have
        # already written an oversized complete or incomplete leftover;
        # classifying exit first would mask E_ACP_OVERFLOW as E_ACP_EOF.
        # Absorb also reports stdout EOF (empty read) so a poll race
        # cannot issue a receipt after F9 classifies leftover frames.
        stdout_eof = self._absorb_pending_continuation(
            deadline=deadline, cancel_event=cancel_event
        )
        self._raise_if_rx_over_limits(include_complete_frames=True)

        # Quiet loop is skipped when quiet_window_s=0 (or already
        # exhausted). A peer may coalesce the resume result plus a
        # complete notification in one os.write; leftover complete
        # frames must be parsed before any receipt. F9 first, then
        # EOF — replay/malformed/unknown beat a poll-racy E_ACP_EOF.
        cancel_observed = self._drain_complete_buffered_frames(
            phase="pre-receipt", cancel_event=cancel_event
        )

        if self.proc.poll() is not None or stdout_eof:
            raise AcpError(
                "ACP process exited before readiness (transient handshake)",
                code="E_ACP_EOF",
            )

        if cancel_observed or (
            cancel_event is not None and cancel_event.is_set()
        ):
            raise AcpError("ACP handshake cancelled", code="E_ACP_CANCELLED")

        receipt = AcpResumeReceipt(
            job_id=self.job_id,
            attempt=self.attempt,
            parent_run_id=self.parent_run_id,
            session_id_hash=derived_sid_hash,
            cwd_hash=derived_cwd_hash,
            resume_matched=True,
            host_version=self.host_version,
            host_capability_source=self.host_capability_source,
            timestamp=_utc_now(),
        )
        # Materialize sha
        body = receipt.to_dict()
        # Receipt construction is local only.  Recheck immediately before
        # publishing durable session readiness so an observed cancellation
        # cannot leave a receipt or _ready behind.
        if cancel_event is not None and cancel_event.is_set():
            raise AcpError("ACP handshake cancelled", code="E_ACP_CANCELLED")
        self._receipt = AcpResumeReceipt(
            job_id=receipt.job_id,
            attempt=receipt.attempt,
            parent_run_id=receipt.parent_run_id,
            session_id_hash=derived_sid_hash,
            cwd_hash=derived_cwd_hash,
            resume_matched=True,
            host_version=receipt.host_version,
            host_capability_source=receipt.host_capability_source,
            timestamp=body["timestamp"],
            receipt_sha256=body["receipt_sha256"],
        )
        self._ready = True
        if cancel_event is not None and cancel_event.is_set():
            self._ready = False
            self._receipt = None
            raise AcpError("ACP handshake cancelled", code="E_ACP_CANCELLED")
        return self._receipt

    def drain_until_cancel(
        self,
        *,
        cancel_event: threading.Event | None = None,
        idle_poll_s: float = _CANCEL_POLL_INTERVAL_S,
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
        require_object: bool = False,
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
            if require_object:
                if not isinstance(result, dict):
                    raise AcpError(
                        "ACP resume result must be a JSON object",
                        code="E_ACP_RESUME",
                    )
                return result
            if not isinstance(result, dict):
                # initialize may return non-dict in some peers — accept null/object.
                if result is None:
                    return {}
                raise AcpError("ACP result must be object or null", code="E_ACP_PROTOCOL")
            return result

    def _raise_if_rx_over_limits(self, *, include_complete_frames: bool = False) -> None:
        _raise_if_buffered_limits(
            self._byte_budget,
            self._rx_buf,
            max_line_bytes=self.max_line_bytes,
            max_total_bytes=self.max_total_bytes,
            include_complete_frames=include_complete_frames,
        )

    def _absorb_pending_continuation(
        self,
        *,
        deadline: float,
        cancel_event: threading.Event | None,
    ) -> bool:
        return _absorb_pending_continuation(
            self.proc,
            self._byte_budget,
            self._rx_buf,
            max_line_bytes=self.max_line_bytes,
            max_total_bytes=self.max_total_bytes,
            deadline=deadline,
            cancel_event=cancel_event,
        )

    def _drain_complete_buffered_frames(
        self,
        *,
        phase: str,
        cancel_event: threading.Event | None = None,
    ) -> bool:
        """Parse every complete NL frame already in ``_rx_buf``.

        Does not wait on the pipe. Incomplete suffix stays buffered.
        Overflow of a complete frame is classified by the caller
        (``include_complete_frames=True``) before this runs; extract
        still re-checks the per-line cap. Forbidden conversation
        updates raise ``E_ACP_REPLAY``; malformed JSON raises
        ``E_ACP_MALFORMED``; unknown / unexpected frames raise
        ``E_ACP_PROTOCOL``. Cancellation is checked only after all complete
        frames already in the buffer have been classified, including when a
        notification handler observes cancellation while discarding chrome.
        Returns whether cancellation was observed after the buffer was drained,
        leaving callers to preserve any already-observed EOF precedence.
        """
        self._raise_if_rx_over_limits(include_complete_frames=True)
        while True:
            line = _extract_complete_frame(
                self._rx_buf,
                self._byte_budget,
                max_line_bytes=self.max_line_bytes,
            )
            if line is None:
                return cancel_event is not None and cancel_event.is_set()
            msg = _parse_rpc(line)
            if "method" in msg and "id" not in msg:
                self._handle_notification_or_reject(msg, phase=phase)
                continue
            raise AcpError(
                f"ACP unexpected frame after resume (phase={phase})",
                code="E_ACP_PROTOCOL",
            )

    def _try_read_message(
        self,
        *,
        deadline: float,
        cancel_event: threading.Event | None,
        allow_timeout: bool,
    ) -> dict[str, Any] | None:
        while True:
            # Leftover-only overflow first (same order as absorb): already-
            # buffered over-limit suffix beats cancel and timeout.
            self._raise_if_rx_over_limits()
            if cancel_event is not None and cancel_event.is_set():
                raise AcpError("ACP cancelled", code="E_ACP_CANCELLED")
            now = time.monotonic()
            if now > deadline:
                # ALL frames beat timeout so a complete oversized leftover
                # cannot ride an expired window as "no message".
                self._raise_if_rx_over_limits(include_complete_frames=True)
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
                    max_total_bytes=self.max_total_bytes,
                    cancel_event=cancel_event,
                )
            except AcpError as exc:
                if allow_timeout and exc.code == "E_ACP_TIMEOUT":
                    # Keep any partial frame in _rx_buf for the next poll.
                    self._raise_if_rx_over_limits(include_complete_frames=True)
                    return None
                raise
            # Leftover-only after extract — return the valid line.
            self._raise_if_rx_over_limits()
            return _parse_rpc(line)

    def _handle_notification_or_reject(
        self, msg: Mapping[str, Any], *, phase: str
    ) -> None:
        method = msg.get("method")
        if is_vendor_chrome_method(method):
            # Discard. Params may include MCP env; never copy into errors.
            return
        if method != "session/update":
            label = method if isinstance(method, str) else type(method).__name__
            raise AcpError(
                f"ACP disallowed notification method {label!r} in {phase}",
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
        _start_stderr_drain(proc)
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
        initialized=data.get("initialized") is True,
        resume_matched=data.get("resume_matched") is True,
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
    if data.get("resume_matched") is not True:
        raise AcpError("receipt resume_matched must be true", code="E_ACP_RECEIPT")
    if data.get("initialized") is not True:
        raise AcpError("receipt initialized must be true", code="E_ACP_RECEIPT")
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
    "bind_constructor_identity",
    "build_receipt_from_dict",
    "classify_session_update",
    "discover_grok_binary",
    "hash_cwd",
    "hash_session_id",
    "is_vendor_chrome_method",
    "spawn_acp_stdio",
    "validate_receipt",
    "validate_resume_result",
]
