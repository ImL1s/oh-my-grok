"""Argv-only local command and macOS desktop notification adapters."""
from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from omg_cli.notify.events import (
    notification_line,
    notification_outcome,
    notification_payload,
    owner_matches,
)


MAX_LOCAL_OUTPUT_BYTES = 8_192
_SCRUBBED_ENV = {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}

LocalRunner = Callable[[Sequence[str], str, Mapping[str, str], float], Any]


def _default_runner(
    argv: Sequence[str], stdin_text: str, environment: Mapping[str, str], timeout: float
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        input=stdin_text,
        env=dict(environment),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
        shell=False,
    )


def _returncode(result: Any) -> int | None:
    if isinstance(result, Mapping):
        value = result.get("returncode", result.get("status"))
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
    else:
        value = getattr(result, "returncode", getattr(result, "status", None))
        stdout = getattr(result, "stdout", "")
        stderr = getattr(result, "stderr", "")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if len(str(stdout).encode("utf-8")) > MAX_LOCAL_OUTPUT_BYTES:
        return None
    if len(str(stderr).encode("utf-8")) > MAX_LOCAL_OUTPUT_BYTES:
        return None
    return value


def deliver_local_command(
    event: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    owner: Mapping[str, Any] | None,
    runner: LocalRunner | None = None,
) -> dict[str, Any]:
    """Run one exact allowlisted argv with the notification on stdin."""

    argv = target.get("argv")
    allowlist = target.get("allowed_executables")
    destination = str(argv[0]) if isinstance(argv, list) and argv else "local-command"
    if target.get("enabled") is not True:
        return notification_outcome("command", "skipped", "COMMAND_DISABLED", dict(event), destination)
    safe_event = notification_payload(event)
    if safe_event is None:
        return notification_outcome("command", "failed", "COMMAND_PAYLOAD_REJECTED", dict(event), destination)
    if not owner_matches(safe_event, owner):
        return notification_outcome("command", "failed", "COMMAND_OWNER_MISMATCH", safe_event, destination)
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
        or not isinstance(allowlist, list)
        or argv[0] not in allowlist
    ):
        return notification_outcome("command", "failed", "COMMAND_TARGET_REJECTED", safe_event, destination)
    timeout_ms = target.get("timeout_ms")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
        return notification_outcome("command", "failed", "COMMAND_TARGET_REJECTED", safe_event, destination)
    run = runner or _default_runner
    try:
        result = run(tuple(argv), notification_line(safe_event) + "\n", _SCRUBBED_ENV, timeout_ms / 1_000)
    except Exception as exc:  # noqa: BLE001 - optional adapter failure is contained
        return notification_outcome(
            "command", "failed", "COMMAND_FAILED", safe_event, destination, type(exc).__name__
        )
    if _returncode(result) != 0:
        return notification_outcome("command", "failed", "COMMAND_FAILED", safe_event, destination)
    return notification_outcome("command", "delivered", "COMMAND_DELIVERED", safe_event, destination)


_MACOS_SCRIPT = (
    "on run argv\n"
    "display notification (item 1 of argv) with title \"oh-my-grok\"\n"
    "end run"
)


def deliver_desktop(
    event: Mapping[str, Any],
    target: Mapping[str, Any],
    *,
    owner: Mapping[str, Any] | None,
    runner: LocalRunner | None = None,
) -> dict[str, Any]:
    """Show a macOS desktop notification using fixed script argv, never a shell."""

    destination = "macos-desktop"
    if target.get("enabled") is not True:
        return notification_outcome("desktop", "skipped", "DESKTOP_DISABLED", dict(event), destination)
    safe_event = notification_payload(event)
    if safe_event is None:
        return notification_outcome("desktop", "failed", "DESKTOP_PAYLOAD_REJECTED", dict(event), destination)
    if not owner_matches(safe_event, owner):
        return notification_outcome("desktop", "failed", "DESKTOP_OWNER_MISMATCH", safe_event, destination)
    timeout_ms = target.get("timeout_ms")
    if target.get("platform") != "macos" or isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
        return notification_outcome("desktop", "failed", "DESKTOP_TARGET_REJECTED", safe_event, destination)
    argv = ("/usr/bin/osascript", "-e", _MACOS_SCRIPT, notification_line(safe_event))
    run = runner or _default_runner
    try:
        result = run(argv, "", _SCRUBBED_ENV, timeout_ms / 1_000)
    except Exception as exc:  # noqa: BLE001 - optional adapter failure is contained
        return notification_outcome(
            "desktop", "failed", "DESKTOP_FAILED", safe_event, destination, type(exc).__name__
        )
    if _returncode(result) != 0:
        return notification_outcome("desktop", "failed", "DESKTOP_FAILED", safe_event, destination)
    return notification_outcome("desktop", "delivered", "DESKTOP_DELIVERED", safe_event, destination)


__all__ = ["deliver_desktop", "deliver_local_command"]
