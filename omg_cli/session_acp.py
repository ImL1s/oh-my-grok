"""One-shot ACP ``session/resume`` for ``omg session acp-resume`` (#74 leftover).

Reuses ``host_acp`` initialize + session/resume. Emits a content-free receipt
(no transcript). This is **not** the durable jobs sidecar, **not** ACP
``session/close``, **not** restore-code, and **not** live AG history import.
A fake ``OMG_ACP_BIN`` peer is not live Grok. Never writes ``verified``.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from omg_cli.host_acp import (
    DEFAULT_HANDSHAKE_TIMEOUT_S,
    DEFAULT_QUIET_WINDOW_S,
    AcpError,
    AcpStdioSession,
    discover_grok_binary,
    hash_cwd,
    hash_session_id,
    spawn_acp_stdio,
)

SESSION_CLI_JOB_ID = "session-cli"
SESSION_CLI_PARENT_RUN_ID = "session-cli"

_RECEIPT_BANNED_KEYS = (
    "transcript",
    "messages",
    "authorization",
    "token",
    "raw",
    "stdout",
    "stderr",
    "home",
)


def sanitize_acp_cli_error(text: str) -> str:
    """Strip likely replay bodies / home paths from failure text."""
    out = text
    home = str(Path.home())
    if home:
        out = out.replace(home, "[redacted]")
    for token in ("SECRET_REPLAY", "LATE_SECRET"):
        if token in out:
            out = out.replace(token, "[redacted]")
    return out[:500]


def _canonical_session_uuid(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        raise AcpError("session id must be a UUID", code="E_ACP_SESSION_ID")
    try:
        return str(uuid.UUID(raw))
    except (ValueError, AttributeError, TypeError) as exc:
        raise AcpError(
            "session id must be a canonical UUID",
            code="E_ACP_SESSION_ID",
        ) from exc


def _quiet_window_s() -> float:
    raw = (os.environ.get("OMG_ACP_QUIET_WINDOW_S") or "").strip()
    if not raw:
        return DEFAULT_QUIET_WINDOW_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_QUIET_WINDOW_S
    return max(0.0, value)


def _handshake_timeout_s() -> float:
    raw = (os.environ.get("OMG_ACP_HANDSHAKE_TIMEOUT_S") or "").strip()
    if not raw:
        return DEFAULT_HANDSHAKE_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_HANDSHAKE_TIMEOUT_S
    return max(0.1, min(value, 60.0))


def resolve_acp_peer() -> tuple[str, tuple[str, ...] | None, str]:
    """Return ``(binary, argv_override_or_None, peer_label)``.

    ``OMG_ACP_BIN`` pointing at a ``.py`` fixture is a hermetic fake peer
    (not live Grok). Unset override falls back to ``grok`` on PATH.
    """
    override = (os.environ.get("OMG_ACP_BIN") or "").strip()
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.is_file():
            raise AcpError("OMG_ACP_BIN is not a file", code="E_ACP_BINARY")
        resolved = str(path.resolve())
        if resolved.endswith(".py"):
            return resolved, (sys.executable, "-u", resolved), "fake_fixture"
        if not os.access(resolved, os.X_OK):
            raise AcpError(
                "OMG_ACP_BIN is not an executable file",
                code="E_ACP_BINARY",
            )
        return resolved, None, "acp_bin_override"
    return discover_grok_binary(), None, "grok_agent_stdio"


def session_acp_resume(
    *,
    session_id: str,
    cwd: str | Path,
    restore_code: bool = False,
) -> dict[str, Any]:
    """initialize + session/resume, then process-group teardown.

    Returns a content-free CLI payload wrapping ``AcpResumeReceipt.to_dict()``.
    ``--restore-code`` is always refused (resume ≠ restore).
    """
    if restore_code:
        raise AcpError(
            "ACP session resume does not restore code "
            "(resume is not restore; host restore_code_explicit stays BLOCKED)",
            code="E_ACP_RESTORE_CODE",
        )

    sid = _canonical_session_uuid(session_id)
    cwd_path = Path(cwd).expanduser()
    if not cwd_path.is_absolute():
        cwd_path = Path.cwd() / cwd_path
    cwd_path = cwd_path.resolve()
    if not cwd_path.is_dir():
        raise AcpError("ACP cwd is not a directory", code="E_ACP_CWD")

    binary, argv_override, peer = resolve_acp_peer()
    sid_hash = hash_session_id(sid)
    cwd_h = hash_cwd(cwd_path)

    proc = None
    session: AcpStdioSession | None = None
    try:
        try:
            proc, argv = spawn_acp_stdio(
                binary=sys.executable if argv_override is not None else binary,
                cwd=cwd_path,
                env=os.environ,
                argv_override=argv_override,
            )
        except AcpError:
            raise
        except OSError as exc:
            raise AcpError("ACP spawn failed", code="E_ACP_IO") from exc
        session = AcpStdioSession(
            proc=proc,
            argv=argv,
            session_id=sid,
            cwd=str(cwd_path),
            job_id=SESSION_CLI_JOB_ID,
            attempt=1,
            parent_run_id=SESSION_CLI_PARENT_RUN_ID,
            session_id_hash=sid_hash,
            cwd_hash=cwd_h,
            quiet_window_s=_quiet_window_s(),
        )
        receipt = session.handshake(timeout_s=_handshake_timeout_s())
        body = receipt.to_dict()
        for banned in _RECEIPT_BANNED_KEYS:
            if banned in body:
                raise AcpError(
                    f"receipt must not contain {banned!r}",
                    code="E_ACP_RECEIPT",
                )
        payload: dict[str, Any] = {
            "receipt": body,
            "kind": body.get("kind"),
            "resume_matched": body.get("resume_matched") is True,
            "initialized": body.get("initialized") is True,
            "no_replay_observed": body.get("no_replay_observed") is True,
            "restore_code_requested": False,
            "session_close": False,
            "ag_history_imported": False,
            "live_grok": False,
            "durable_sidecar": False,
            "peer": peer,
            "teardown": "process_group",
            "content_free": True,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).lower()
        for banned in ("transcript", "messages"):
            if banned in blob:
                raise AcpError(
                    "content-free receipt leaked a banned field name",
                    code="E_ACP_RECEIPT",
                )
        return payload
    finally:
        if session is not None:
            session.close()
        elif proc is not None:
            from omg_cli.host_acp import _kill_proc_group

            _kill_proc_group(proc)


__all__ = [
    "SESSION_CLI_JOB_ID",
    "SESSION_CLI_PARENT_RUN_ID",
    "resolve_acp_peer",
    "sanitize_acp_cli_error",
    "session_acp_resume",
]
