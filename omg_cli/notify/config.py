"""Strict read-only notification configuration.

Configuration is disabled by default.  Persisted HTTP headers and tmux nonces
must be environment-variable references; raw credentials are rejected.
"""
from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from omg_cli.contracts.writer_chain import canonical_json_bytes
from omg_cli.notify.http import validate_https_endpoint


MAX_CONFIG_BYTES = 65_536
MAX_ADAPTERS = 8
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_SESSION = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_PANE = re.compile(r"^%[0-9]{1,16}$")
_FORBIDDEN_HEADERS = {"connection", "content-length", "host", "transfer-encoding"}


class NotificationConfigError(ValueError):
    """Persisted notification configuration is unsafe or malformed."""


def disabled_notification_config() -> dict[str, Any]:
    return {
        "store_kind": "omg_notification_config",
        "schema_version": 1,
        "enabled": False,
        "adapters": [],
    }


def _exact(raw: Mapping[str, Any], allowed: set[str], *, label: str) -> None:
    unknown = sorted(set(raw) - allowed)
    missing = sorted(allowed - set(raw))
    if unknown:
        raise NotificationConfigError(f"{label} has unknown fields")
    if missing:
        raise NotificationConfigError(f"{label} is missing fields")


def _enabled(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise NotificationConfigError(f"{label} must be boolean")
    return value


def _terminal(raw: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"adapter", "enabled"}
    optional_identity = {"pid", "start_marker", "tty", "stderr_dev", "stderr_ino"}
    unknown = sorted(set(raw) - allowed - optional_identity)
    if unknown:
        raise NotificationConfigError(f"terminal adapter has unknown fields: {unknown}")
    if not allowed.issubset(raw):
        raise NotificationConfigError("terminal adapter is missing fields")
    result: dict[str, Any] = {"adapter": "terminal", "enabled": _enabled(raw["enabled"], label="enabled")}
    identity_present = [name in raw for name in optional_identity]
    if any(identity_present) and not all(identity_present):
        raise NotificationConfigError("terminal identity must include pid, start_marker, and tty")
    if all(identity_present):
        pid = raw["pid"]
        marker = raw["start_marker"]
        tty = raw["tty"]
        stderr_dev = raw["stderr_dev"]
        stderr_ino = raw["stderr_ino"]
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
            raise NotificationConfigError("terminal pid is invalid")
        if not isinstance(marker, str) or not marker or len(marker.encode()) > 256 or "\0" in marker:
            raise NotificationConfigError("terminal start_marker is invalid")
        if not isinstance(tty, str) or not tty or len(tty.encode()) > 256 or any(c in tty for c in "\0\r\n"):
            raise NotificationConfigError("terminal tty is invalid")
        if (
            isinstance(stderr_dev, bool)
            or not isinstance(stderr_dev, int)
            or stderr_dev < 0
            or isinstance(stderr_ino, bool)
            or not isinstance(stderr_ino, int)
            or stderr_ino < 1
        ):
            raise NotificationConfigError("terminal stderr identity is invalid")
        result.update(
            {
                "pid": pid,
                "start_marker": marker,
                "tty": tty,
                "stderr_dev": stderr_dev,
                "stderr_ino": stderr_ino,
            }
        )
    if result["enabled"] and not all(identity_present):
        raise NotificationConfigError("enabled terminal adapter requires exact identity")
    return result


def _tmux(raw: Mapping[str, Any]) -> dict[str, Any]:
    fields = {
        "adapter",
        "enabled",
        "session_name",
        "pane_id",
        "owner_nonce_env",
        "worker_nonce_env",
    }
    _exact(raw, fields, label="tmux adapter")
    session = raw["session_name"]
    pane = raw["pane_id"]
    owner_env = raw["owner_nonce_env"]
    worker_env = raw["worker_nonce_env"]
    if not isinstance(session, str) or _SESSION.fullmatch(session) is None:
        raise NotificationConfigError("tmux session_name is invalid")
    if not isinstance(pane, str) or _PANE.fullmatch(pane) is None:
        raise NotificationConfigError("tmux pane_id is invalid")
    if not isinstance(owner_env, str) or _ENV_NAME.fullmatch(owner_env) is None:
        raise NotificationConfigError("tmux owner_nonce_env is invalid")
    if not isinstance(worker_env, str) or _ENV_NAME.fullmatch(worker_env) is None:
        raise NotificationConfigError("tmux worker_nonce_env is invalid")
    return {
        "adapter": "tmux",
        "enabled": _enabled(raw["enabled"], label="enabled"),
        "session_name": session,
        "pane_id": pane,
        "owner_nonce_env": owner_env,
        "worker_nonce_env": worker_env,
    }


def _https(raw: Mapping[str, Any]) -> dict[str, Any]:
    fields = {"adapter", "enabled", "url_env", "allowed_hosts", "timeout_ms", "header_env"}
    if "url" in raw:
        raise NotificationConfigError("raw webhook URLs are forbidden; use url_env")
    if "headers" in raw:
        raise NotificationConfigError("raw headers are forbidden; use header_env references")
    _exact(raw, fields, label="https adapter")
    url_env = raw["url_env"]
    hosts = raw["allowed_hosts"]
    timeout = raw["timeout_ms"]
    header_env = raw["header_env"]
    if not isinstance(url_env, str) or _ENV_NAME.fullmatch(url_env) is None:
        raise NotificationConfigError("https url_env reference is invalid")
    if not isinstance(hosts, list) or not hosts or len(hosts) > 32:
        raise NotificationConfigError("https allowed_hosts is invalid")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 100 <= timeout <= 5_000:
        raise NotificationConfigError("https timeout_ms is outside bounds")
    if not isinstance(header_env, Mapping) or len(header_env) > 16:
        raise NotificationConfigError("https header_env is invalid")
    normalized_headers: dict[str, str] = {}
    for name, env_name in sorted(header_env.items(), key=lambda item: str(item[0]).lower()):
        if not isinstance(name, str) or _HEADER_NAME.fullmatch(name) is None:
            raise NotificationConfigError("https header name is invalid")
        lower = name.lower()
        if lower in _FORBIDDEN_HEADERS:
            raise NotificationConfigError("https hop-by-hop header is forbidden")
        if not isinstance(env_name, str) or _ENV_NAME.fullmatch(env_name) is None:
            raise NotificationConfigError("https header env reference is invalid")
        if lower in normalized_headers:
            raise NotificationConfigError("https header names must be unique")
        normalized_headers[lower] = env_name
    # Validate the non-secret host allowlist with a synthetic path.  The actual
    # secret-bearing URL is read from the environment only at dispatch time.
    normalized_hosts = sorted({_host.lower().rstrip(".") for _host in hosts if isinstance(_host, str)})
    endpoint = None
    if len(normalized_hosts) == len(hosts) and normalized_hosts:
        endpoint = validate_https_endpoint(
            {
                "url": f"https://{normalized_hosts[0]}/",
                "allowed_hosts": normalized_hosts,
                "timeout_ms": timeout,
                "headers": {},
            }
        )
    if endpoint is None:
        raise NotificationConfigError("https endpoint is unsafe or invalid")
    return {
        "adapter": "https",
        "enabled": _enabled(raw["enabled"], label="enabled"),
        "url_env": url_env,
        "allowed_hosts": endpoint["allowed_hosts"],
        "timeout_ms": timeout,
        "header_env": normalized_headers,
    }


def _command(raw: Mapping[str, Any]) -> dict[str, Any]:
    fields = {"adapter", "enabled", "argv", "allowed_executables", "timeout_ms"}
    _exact(raw, fields, label="command adapter")
    argv = raw["argv"]
    allowlist = raw["allowed_executables"]
    timeout = raw["timeout_ms"]
    if not isinstance(argv, list) or not 1 <= len(argv) <= 32:
        raise NotificationConfigError("command argv is invalid")
    if not isinstance(allowlist, list) or not 1 <= len(allowlist) <= 16:
        raise NotificationConfigError("command executable allowlist is invalid")
    normalized_argv: list[str] = []
    for argument in argv:
        if (
            not isinstance(argument, str)
            or not argument
            or len(argument.encode("utf-8")) > 4_096
            or any(char in argument for char in ("\0", "\r", "\n"))
        ):
            raise NotificationConfigError("command argv is invalid")
        normalized_argv.append(argument)
    normalized_allowlist: list[str] = []
    for executable in allowlist:
        if (
            not isinstance(executable, str)
            or not Path(executable).is_absolute()
            or len(executable.encode("utf-8")) > 1_024
            or any(char in executable for char in ("\0", "\r", "\n"))
            or executable in normalized_allowlist
        ):
            raise NotificationConfigError("command executable allowlist is invalid")
        normalized_allowlist.append(executable)
    if not Path(normalized_argv[0]).is_absolute() or normalized_argv[0] not in normalized_allowlist:
        raise NotificationConfigError("command executable is not allowlisted")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 100 <= timeout <= 5_000:
        raise NotificationConfigError("command timeout_ms is outside bounds")
    return {
        "adapter": "command",
        "enabled": _enabled(raw["enabled"], label="enabled"),
        "argv": normalized_argv,
        "allowed_executables": normalized_allowlist,
        "timeout_ms": timeout,
    }


def _desktop(raw: Mapping[str, Any]) -> dict[str, Any]:
    fields = {"adapter", "enabled", "platform", "timeout_ms"}
    _exact(raw, fields, label="desktop adapter")
    if raw["platform"] != "macos":
        raise NotificationConfigError("desktop platform is unsupported")
    timeout = raw["timeout_ms"]
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not 100 <= timeout <= 5_000:
        raise NotificationConfigError("desktop timeout_ms is outside bounds")
    return {
        "adapter": "desktop",
        "enabled": _enabled(raw["enabled"], label="enabled"),
        "platform": "macos",
        "timeout_ms": timeout,
    }


def parse_notification_config(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise NotificationConfigError("notification config must be an object")
    _exact(
        value,
        {"store_kind", "schema_version", "enabled", "adapters"},
        label="notification config",
    )
    if value["store_kind"] != "omg_notification_config" or value["schema_version"] != 1:
        raise NotificationConfigError("notification config header mismatch")
    enabled = _enabled(value["enabled"], label="enabled")
    adapters = value["adapters"]
    if not isinstance(adapters, list) or len(adapters) > MAX_ADAPTERS:
        raise NotificationConfigError("notification adapters exceed bounds")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in adapters:
        if not isinstance(raw, Mapping):
            raise NotificationConfigError("notification adapter must be an object")
        kind = raw.get("adapter")
        if kind in seen:
            raise NotificationConfigError("duplicate notification adapter")
        seen.add(str(kind))
        if kind == "terminal":
            result.append(_terminal(raw))
        elif kind == "tmux":
            result.append(_tmux(raw))
        elif kind == "https":
            result.append(_https(raw))
        elif kind == "command":
            result.append(_command(raw))
        elif kind == "desktop":
            result.append(_desktop(raw))
        else:
            raise NotificationConfigError("unknown notification adapter")
    return {
        "store_kind": "omg_notification_config",
        "schema_version": 1,
        "enabled": enabled,
        "adapters": result,
    }


def load_notification_config(path: Path | str) -> dict[str, Any]:
    source = Path(path)
    try:
        before_path = source.lstat()
    except FileNotFoundError:
        return disabled_notification_config()
    if stat.S_ISLNK(before_path.st_mode):
        raise NotificationConfigError("notification config symlink is forbidden")
    if not stat.S_ISREG(before_path.st_mode):
        raise NotificationConfigError("notification config must be a regular file")
    if before_path.st_uid != os.getuid():
        raise NotificationConfigError("notification config must be owned by the current uid")
    if stat.S_IMODE(before_path.st_mode) != 0o600:
        raise NotificationConfigError("notification config mode must be exactly 0600")
    if before_path.st_size > MAX_CONFIG_BYTES:
        raise NotificationConfigError("notification config exceeds byte bounds")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(source, flags)
    except OSError as exc:
        raise NotificationConfigError("notification config is unsafe") from exc
    try:
        before = os.fstat(descriptor)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        path_identity = (
            before_path.st_dev,
            before_path.st_ino,
            before_path.st_size,
            before_path.st_mtime_ns,
        )
        if not stat.S_ISREG(before.st_mode) or identity != path_identity:
            raise NotificationConfigError("notification config changed during read")
        if before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600:
            raise NotificationConfigError("notification config ownership or mode is unsafe")
        body = os.read(descriptor, MAX_CONFIG_BYTES + 1)
        if len(body) > MAX_CONFIG_BYTES or os.read(descriptor, 1):
            raise NotificationConfigError("notification config exceeds byte bounds")
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = source.lstat()
    except OSError as exc:
        raise NotificationConfigError("notification config changed during read") from exc
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    final_path_identity = (
        after_path.st_dev,
        after_path.st_ino,
        after_path.st_size,
        after_path.st_mtime_ns,
    )
    if (
        identity != after_identity
        or after_identity != final_path_identity
        or stat.S_ISLNK(after_path.st_mode)
        or not stat.S_ISREG(after_path.st_mode)
    ):
        raise NotificationConfigError("notification config changed during read")
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NotificationConfigError("notification config is invalid JSON") from exc
    normalized = parse_notification_config(parsed)
    if body != canonical_json_bytes(parsed):
        raise NotificationConfigError("notification config must use canonical JSON bytes")
    return normalized


__all__ = [
    "MAX_CONFIG_BYTES",
    "NotificationConfigError",
    "disabled_notification_config",
    "load_notification_config",
    "parse_notification_config",
]
