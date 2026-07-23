"""Host-owned Grok LSP registration inspection.

OMG does not proxy semantic LSP operations.  It can only validate a plugin
``.lsp.json`` registration and report local command availability.  A valid
configuration is *configured/unobserved*, never evidence that a host server is
healthy.
"""
from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any


LSP_CONFIG_NAME = ".lsp.json"
SEMANTIC_PROXY_OPERATIONS: tuple[str, ...] = ()
_OPTIONAL_KEYS = {
    "args",
    "transport",
    "env",
    "initializationOptions",
    "settings",
    "workspaceFolder",
    "startupTimeout",
    "shutdownTimeout",
    "restartOnCrash",
    "maxRestarts",
}
_SERVER_KEYS = {"command", "extensionToLanguage"} | _OPTIONAL_KEYS


class LSPRegistrationError(ValueError):
    """The plugin LSP registration is invalid or unsafe to classify."""


def _require_int(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise LSPRegistrationError(f"{label} must be an integer >= {minimum}")
    return value


def validate_registration(value: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Validate Grok's documented named-server ``lsp.json`` shape."""
    if not isinstance(value, Mapping) or not value:
        raise LSPRegistrationError("LSP registration must be a non-empty object")
    normalized: dict[str, dict[str, Any]] = {}
    for name in sorted(value):
        raw = value[name]
        if not isinstance(name, str) or not name or len(name) > 128:
            raise LSPRegistrationError("LSP server name must be bounded non-empty text")
        if not isinstance(raw, Mapping):
            raise LSPRegistrationError(f"LSP server {name!r} must be an object")
        server = dict(raw)
        unknown = sorted(set(server) - _SERVER_KEYS)
        if unknown:
            raise LSPRegistrationError(f"LSP server {name!r} has unknown fields: {unknown}")
        command = server.get("command")
        if not isinstance(command, str) or not command.strip() or len(command) > 4096:
            raise LSPRegistrationError(f"LSP server {name!r} command is required")
        mapping = server.get("extensionToLanguage")
        if not isinstance(mapping, dict) or not mapping:
            raise LSPRegistrationError(
                f"LSP server {name!r} extensionToLanguage is required"
            )
        for extension, language in mapping.items():
            if (
                not isinstance(extension, str)
                or not extension.startswith(".")
                or "/" in extension
                or "\\" in extension
                or not isinstance(language, str)
                or not language.strip()
            ):
                raise LSPRegistrationError(
                    f"LSP server {name!r} has invalid extension mapping"
                )
        args = server.get("args", [])
        if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
            raise LSPRegistrationError(f"LSP server {name!r} args must be text array")
        if server.get("transport", "stdio") not in {"stdio", "socket"}:
            raise LSPRegistrationError(f"LSP server {name!r} transport is unsupported")
        env = server.get("env", {})
        if not isinstance(env, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in env.items()
        ):
            raise LSPRegistrationError(f"LSP server {name!r} env must be text mapping")
        for timeout_key in ("startupTimeout", "shutdownTimeout", "maxRestarts"):
            if timeout_key in server:
                _require_int(server[timeout_key], f"{name}.{timeout_key}")
        if "restartOnCrash" in server and not isinstance(server["restartOnCrash"], bool):
            raise LSPRegistrationError(f"LSP server {name!r} restartOnCrash must be boolean")
        normalized[name] = server
    return normalized


def load_registration(
    root: Path | str | None = None, *, config_path: Path | str | None = None
) -> tuple[Path, dict[str, dict[str, Any]] | None, str | None]:
    """Load and validate a plugin registration without starting any server."""
    base = Path(root).resolve() if root is not None else Path.cwd().resolve()
    path = Path(config_path) if config_path is not None else base / LSP_CONFIG_NAME
    if not path.is_absolute():
        path = base / path
    path = path.resolve(strict=False)
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise LSPRegistrationError("LSP registration path escapes plugin root") from exc
    if not path.is_file() or path.is_symlink():
        return path, None, None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise LSPRegistrationError("LSP registration must be a JSON object")
        return path, validate_registration(parsed), None
    except (OSError, UnicodeError, json.JSONDecodeError, LSPRegistrationError) as exc:
        return path, None, str(exc)


def _command_available(command: str) -> bool:
    candidate = Path(command).expanduser()
    if candidate.is_absolute():
        return candidate.is_file() and os.access(candidate, os.X_OK)
    return shutil.which(command) is not None


def registration_status(
    root: Path | str | None = None,
    *,
    config_path: Path | str | None = None,
    host_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return registration/local availability; health needs explicit host proof."""
    path, registration, error = load_registration(root, config_path=config_path)
    servers: list[dict[str, Any]] = []
    if registration is not None:
        for name, server in registration.items():
            servers.append(
                {
                    "name": name,
                    "command": server["command"],
                    "transport": server.get("transport", "stdio"),
                    "extensions": sorted(server["extensionToLanguage"]),
                    "command_available": _command_available(server["command"]),
                }
            )
    observed = isinstance(host_observation, Mapping) and bool(
        host_observation.get("observed")
    )
    healthy = (
        observed
        and isinstance(host_observation, Mapping)
        and host_observation.get("healthy") is True
    )
    if error is not None:
        status = "invalid_registration"
    elif registration is None:
        status = "missing_registration"
    elif not observed:
        status = "configured_unobserved"
    elif healthy:
        status = "host_observed_healthy"
    else:
        status = "host_observed_unhealthy"
    return {
        "ok": error is None,
        "ownership": "host_owned",
        "status": status,
        "registration_path": str(path),
        "registered": registration is not None,
        "configuration_valid": registration is not None and error is None,
        "host_observed": observed,
        "healthy": healthy,
        "servers": servers,
        "error": error,
        "semantic_proxy_operations": [],
        "semantic_proxy_count": 0,
        "honesty": (
            "OMG validates registration and local command presence only; "
            "configured but unobserved is not healthy, and semantic operations belong to Grok."
        ),
    }


def probe_tools(root: Path | str | None = None) -> dict[str, Any]:
    """Backward-compatible status entrypoint with no semantic tool execution."""
    status = registration_status(root)
    status["available"] = [
        row["name"] for row in status["servers"] if row["command_available"]
    ]
    status["missing"] = [
        row["name"] for row in status["servers"] if not row["command_available"]
    ]
    return status


__all__ = [
    "LSP_CONFIG_NAME",
    "LSPRegistrationError",
    "SEMANTIC_PROXY_OPERATIONS",
    "load_registration",
    "probe_tools",
    "registration_status",
    "validate_registration",
]
