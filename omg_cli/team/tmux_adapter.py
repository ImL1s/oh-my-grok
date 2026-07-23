"""Optional, identity-fenced tmux display delivery.

This adapter never spawns, kills, pastes, or sends keys.  A message is shown
only after exact session/pane and owner/worker nonce readback.  Missing tmux or
missing owner options is an adapter failure, never an orchestration failure.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from omg_cli.redaction import redact_text


MAX_MESSAGE_BYTES = 4_096
MAX_CAPTURE_BYTES = 16_384
_SESSION = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_PANE = re.compile(r"^%[0-9]{1,16}$")

TmuxRunner = Callable[[Sequence[str]], Any]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _outcome(
    status: str,
    code: str,
    destination: str,
    diagnostic: str | None = None,
) -> dict[str, Any]:
    safe_diagnostic = None
    if diagnostic:
        body = redact_text(diagnostic).encode("utf-8")[:1_024]
        safe_diagnostic = body.decode("utf-8", errors="ignore")
    return {
        "adapter": "tmux",
        "status": status,
        "code": code,
        "destination_sha256": _hash(destination),
        "diagnostic": safe_diagnostic,
        "authoritative": False,
    }


def _valid_nonce(value: object) -> bool:
    return (
        isinstance(value, str)
        and 16 <= len(value) <= 4_096
        and not any(char in value for char in ("\0", "\r", "\n"))
    )


def _valid_target(target: Mapping[str, Any]) -> bool:
    return (
        _SESSION.fullmatch(str(target.get("session_name") or "")) is not None
        and _PANE.fullmatch(str(target.get("pane_id") or "")) is not None
        and _valid_nonce(target.get("owner_nonce"))
        and _valid_nonce(target.get("worker_nonce"))
    )


def _valid_message(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value.encode("utf-8")) <= MAX_MESSAGE_BYTES
        and not any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in value)
    )


def _normalize_result(result: Any) -> tuple[int | None, str, str]:
    if isinstance(result, Mapping):
        code = result.get("returncode", result.get("status"))
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
    else:
        code = getattr(result, "returncode", getattr(result, "status", None))
        stdout = getattr(result, "stdout", "")
        stderr = getattr(result, "stderr", "")
    if isinstance(code, bool) or (code is not None and not isinstance(code, int)):
        code = None
    safe_out = str(stdout or "").encode("utf-8")[:MAX_CAPTURE_BYTES].decode("utf-8", errors="ignore")
    safe_err = str(stderr or "").encode("utf-8")[:MAX_CAPTURE_BYTES].decode("utf-8", errors="ignore")
    return code, safe_out, safe_err


def _default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *argv],
        check=False,
        capture_output=True,
        text=True,
        timeout=1.5,
        shell=False,
    )


def deliver_tmux_message(
    message: str,
    target: Mapping[str, Any],
    *,
    runner: TmuxRunner | None = None,
) -> dict[str, Any]:
    """Display one bounded message after exact tmux ownership readback."""

    session = str(target.get("session_name") or "")
    pane = str(target.get("pane_id") or "")
    destination = f"session:{session}:pane:{pane}"
    if target.get("enabled") is not True:
        return _outcome("skipped", "TMUX_DISABLED", destination)
    if not _valid_message(message):
        return _outcome("failed", "TMUX_MESSAGE_REJECTED", destination)
    if not _valid_target(target):
        return _outcome("failed", "TMUX_TARGET_REJECTED", destination)
    safe_message = redact_text(message)
    run = runner or _default_runner
    commands = (
        ["display-message", "-p", "-t", pane, "#{session_name}\t#{pane_id}"],
        ["show-options", "-v", "-t", session, "@omg_owner_nonce"],
        ["show-options", "-p", "-v", "-t", pane, "@omg_worker_nonce"],
        ["list-clients", "-t", session, "-F", "#{client_name}\t#{session_name}"],
    )
    observed: list[tuple[int | None, str, str]] = []
    try:
        for command in commands:
            observed.append(_normalize_result(run(command)))
    except Exception as exc:  # noqa: BLE001 - optional adapter failure is contained
        return _outcome("failed", "TMUX_UNAVAILABLE", destination, str(exc))
    expected = (
        f"{session}\t{pane}",
        str(target["owner_nonce"]),
        str(target["worker_nonce"]),
    )
    if any(
        code != 0 or stdout.strip() != wanted
        for (code, stdout, _), wanted in zip(observed[:3], expected)
    ):
        return _outcome("failed", "TMUX_IDENTITY_MISMATCH", destination)
    client_code, client_stdout, _ = observed[3]
    client_rows = client_stdout.splitlines()
    if client_code != 0 or len(client_rows) != 1:
        return _outcome("failed", "TMUX_CLIENT_BINDING_MISMATCH", destination)
    client_parts = client_rows[0].split("\t")
    if (
        len(client_parts) != 2
        or client_parts[1] != session
        or not client_parts[0]
        or len(client_parts[0].encode("utf-8")) > 1_024
        or any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in client_parts[0])
    ):
        return _outcome("failed", "TMUX_CLIENT_BINDING_MISMATCH", destination)
    client = client_parts[0]
    try:
        code, _stdout, stderr = _normalize_result(
            run(["display-message", "-c", client, "-l", "--", safe_message])
        )
    except Exception as exc:  # noqa: BLE001 - optional adapter failure is contained
        return _outcome("failed", "TMUX_DELIVERY_FAILED", destination, str(exc))
    if code != 0:
        return _outcome("failed", "TMUX_DELIVERY_FAILED", destination, stderr)
    return _outcome("delivered", "TMUX_DELIVERED", destination)


notify_tmux = deliver_tmux_message

__all__ = ["MAX_MESSAGE_BYTES", "deliver_tmux_message", "notify_tmux"]
