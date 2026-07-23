"""Isolated outbound notification dispatcher."""
from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from omg_cli.notify.config import parse_notification_config
from omg_cli.notify.events import (
    notification_line,
    notification_outcome,
    notification_payload,
    owner_matches,
)
from omg_cli.notify.http import notify_https
from omg_cli.notify.local import LocalRunner, deliver_desktop, deliver_local_command
from omg_cli.team.tmux_adapter import deliver_tmux_message


def _inspect_terminal(pid: int) -> dict[str, Any] | None:
    if pid != os.getpid() or not sys.stderr.isatty():
        return None
    try:
        stderr_fd = sys.stderr.fileno()
        tty_name = os.ttyname(stderr_fd)
        stderr_stat = os.fstat(stderr_fd)
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.0,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    parts = result.stdout.strip().split()
    if result.returncode != 0 or len(parts) < 5:
        return None
    return {
        "pid": pid,
        "start_marker": " ".join(parts[:5]),
        "tty": tty_name,
        "stderr_dev": stderr_stat.st_dev,
        "stderr_ino": stderr_stat.st_ino,
    }


def _write_terminal(line: str) -> bool:
    sys.stderr.write(line)
    sys.stderr.flush()
    return True


def _terminal(
    event: dict[str, Any],
    target: Mapping[str, Any],
    *,
    owner: dict[str, Any] | None,
    inspector: Callable[[int], Mapping[str, Any] | None],
    writer: Callable[[str], Any],
) -> dict[str, Any]:
    destination = f"pid:{target.get('pid')}:tty:{target.get('tty')}"
    if target.get("enabled") is not True:
        return notification_outcome("terminal", "skipped", "TERMINAL_DISABLED", event, destination)
    if not owner_matches(event, owner):
        return notification_outcome(
            "terminal", "failed", "TERMINAL_OWNER_MISMATCH", event, destination
        )
    pid = target.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid != os.getpid():
        return notification_outcome(
            "terminal", "failed", "TERMINAL_PROCESS_NOT_CURRENT", event, destination
        )
    observed = inspector(pid)
    expected = {
        "pid": pid,
        "start_marker": target.get("start_marker"),
        "tty": target.get("tty"),
        "stderr_dev": target.get("stderr_dev"),
        "stderr_ino": target.get("stderr_ino"),
    }
    if not isinstance(observed, Mapping) or any(observed.get(key) != value for key, value in expected.items()):
        return notification_outcome(
            "terminal", "failed", "TERMINAL_IDENTITY_MISMATCH", event, destination
        )
    try:
        delivered = writer(notification_line(event) + "\n")
    except (OSError, RuntimeError, ValueError, TypeError) as exc:
        return notification_outcome(
            "terminal", "failed", "TERMINAL_WRITE_FAILED", event, destination, type(exc).__name__
        )
    if delivered is False:
        return notification_outcome(
            "terminal", "failed", "TERMINAL_WRITE_FAILED", event, destination
        )
    return notification_outcome("terminal", "delivered", "TERMINAL_DELIVERED", event, destination)


def _resolve_header_env(
    adapter: Mapping[str, Any], environment: Mapping[str, str]
) -> tuple[dict[str, str] | None, str | None]:
    result: dict[str, str] = {}
    for name, env_name in adapter.get("header_env", {}).items():
        value = environment.get(env_name)
        if value is None:
            return None, str(env_name)
        result[str(name)] = value
    return result, None


def dispatch_notifications(
    event: dict[str, Any],
    config: Mapping[str, Any],
    *,
    owner: dict[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
    terminal_inspector: Callable[[int], Mapping[str, Any] | None] = _inspect_terminal,
    terminal_writer: Callable[[str], Any] = _write_terminal,
    tmux_runner: Callable[[Sequence[str]], Any] | None = None,
    https_resolver: Callable[[str], Any] | None = None,
    https_transport: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
    local_runner: LocalRunner | None = None,
) -> list[dict[str, Any]]:
    """Dispatch optional adapters; every failure is a bounded outcome."""

    try:
        normalized = parse_notification_config(config)
    except Exception as exc:  # noqa: BLE001 - optional adapter failure is contained
        return [
            notification_outcome(
                "config",
                "failed",
                "NOTIFICATION_CONFIG_REJECTED",
                event,
                None,
                str(exc),
            )
        ]
    if not normalized["enabled"]:
        return []
    enabled_adapters = [adapter for adapter in normalized["adapters"] if adapter["enabled"]]
    if enabled_adapters:
        safe_event = notification_payload(event)
        if safe_event is None:
            return [
                notification_outcome(
                    "event", "failed", "NOTIFICATION_EVENT_REJECTED", event, None
                )
            ]
        if not owner_matches(safe_event, owner):
            return [
                notification_outcome(
                    "event", "failed", "NOTIFICATION_OWNER_MISMATCH", safe_event, None
                )
            ]
    else:
        safe_event = event
    environment = dict(os.environ if environ is None else environ)
    outcomes: list[dict[str, Any]] = []
    for adapter in normalized["adapters"]:
        kind = adapter["adapter"]
        try:
            if kind == "terminal":
                outcomes.append(
                    _terminal(
                        safe_event,
                        adapter,
                        owner=owner,
                        inspector=terminal_inspector,
                        writer=terminal_writer,
                    )
                )
            elif kind == "tmux":
                destination = f"session:{adapter['session_name']}:pane:{adapter['pane_id']}"
                if not adapter["enabled"]:
                    outcomes.append(
                        notification_outcome("tmux", "skipped", "TMUX_DISABLED", event, destination)
                    )
                    continue
                owner_nonce = environment.get(adapter["owner_nonce_env"])
                worker_nonce = environment.get(adapter["worker_nonce_env"])
                if not owner_nonce or not worker_nonce:
                    outcomes.append(
                        notification_outcome(
                            "tmux", "failed", "TMUX_NONCE_MISSING", safe_event, destination
                        )
                    )
                    continue
                if owner is None or owner_nonce != owner.get("owner_nonce"):
                    outcomes.append(
                        notification_outcome(
                            "tmux", "failed", "TMUX_OWNER_MISMATCH", safe_event, destination
                        )
                    )
                    continue
                outcomes.append(
                    {
                        **deliver_tmux_message(
                        notification_line(safe_event),
                        {
                            "enabled": True,
                            "session_name": adapter["session_name"],
                            "pane_id": adapter["pane_id"],
                            "owner_nonce": owner_nonce,
                            "worker_nonce": worker_nonce,
                        },
                        runner=tmux_runner,
                        ),
                        "event_id": safe_event.get("event_id"),
                    }
                )
            elif kind == "https":
                if not adapter["enabled"]:
                    outcomes.append(
                        notification_outcome(
                            "https", "skipped", "HTTPS_DISABLED", safe_event, adapter["url_env"]
                        )
                    )
                    continue
                url = environment.get(adapter["url_env"])
                if not url:
                    outcomes.append(
                        notification_outcome(
                            "https",
                            "failed",
                            "HTTPS_URL_ENV_MISSING",
                            safe_event,
                            adapter["url_env"],
                        )
                    )
                    continue
                headers, missing = _resolve_header_env(adapter, environment)
                if missing is not None:
                    outcomes.append(
                        notification_outcome(
                            "https",
                            "failed",
                            "HTTPS_HEADER_ENV_MISSING",
                            safe_event,
                            adapter["url_env"],
                        )
                    )
                    continue
                outcomes.append(
                    notify_https(
                        safe_event,
                        {**adapter, "url": url, "headers": headers or {}},
                        owner=owner,
                        resolver=https_resolver,
                        transport=https_transport,
                    )
                )
            elif kind == "command":
                outcomes.append(
                    deliver_local_command(
                        safe_event, adapter, owner=owner, runner=local_runner
                    )
                )
            elif kind == "desktop":
                outcomes.append(
                    deliver_desktop(safe_event, adapter, owner=owner, runner=local_runner)
                )
        except Exception as exc:  # noqa: BLE001 - optional adapter failure is contained
            outcomes.append(
                notification_outcome(
                    kind,
                    "failed",
                    "NOTIFICATION_ADAPTER_FAILED",
                    safe_event,
                    None,
                    type(exc).__name__,
                )
            )
    return outcomes


__all__ = ["dispatch_notifications"]
