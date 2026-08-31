"""Official ``agy plugin`` install and live discovery adapter (#77).

This module deliberately treats Antigravity's CLI as the authority.  A copied
directory is not observed, healthy, or live-verified until ``agy`` validates
the package, lists the import, and discovers an OMG agent.
"""

from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Iterator

from omg_cli.contracts.path_keys import (
    ContractPathError,
    atomic_write_bytes,
    confined_path,
    ensure_managed_dir,
    exclusive_lock,
    read_managed_regular_bytes,
)


PLUGIN_NAME = "oh-my-grok"
DISCOVERY_AGENT = "omg-explore"
LIVE_PROBE_TOOL = "omg.tools.doctor"
LIVE_PROBE_SERVER = "omg-tools"
LIVE_PROBE_TOKEN = "OMG_INSTALL_LIVE_PROBE_OK"


class AntigravityInstallError(RuntimeError):
    """Fail-closed Antigravity install/discovery error."""


def config_root(home: Path | None = None) -> Path:
    base = Path(home) if home is not None else Path(os.environ.get("HOME") or Path.home())
    return base.expanduser() / ".gemini" / "config"


def installed_plugin_path(home: Path | None = None) -> Path:
    return config_root(home) / "plugins" / PLUGIN_NAME


def ownership_receipt_path(home: Path | None = None) -> Path:
    return config_root(home) / "omg-ownership.json"


def mcp_registry_path(home: Path | None = None) -> Path:
    return config_root(home) / "mcp_config.json"


def _expected_mcp_entry(home: Path | None = None) -> dict[str, Any]:
    return {
        "args": [
            str(installed_plugin_path(home) / "bin" / "omg"),
            "tools",
            "serve",
            "--stdio",
            "--capability-mode",
            "read-only",
        ],
        "command": "python3",
        "disabled": False,
        "env": {"OMG_TOOLS_NETWORK": "0"},
    }


def _mcp_entry(home: Path | None = None) -> dict[str, Any] | None:
    path = _assert_nofollow_path(mcp_registry_path(home), label="Antigravity MCP registry")
    if not path.is_file():
        return None
    try:
        payload = json.loads(read_managed_regular_bytes(path).decode("utf-8"))
    except (ContractPathError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AntigravityInstallError("Antigravity MCP registry is malformed") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("mcpServers"), dict):
        raise AntigravityInstallError("Antigravity MCP registry lacks mcpServers")
    servers = payload["mcpServers"]
    if LIVE_PROBE_SERVER not in servers:
        return None
    row = servers[LIVE_PROBE_SERVER]
    if not isinstance(row, dict):
        raise AntigravityInstallError("Antigravity omg-tools MCP entry is malformed")
    return row


def mcp_registry_identity(home: Path | None = None) -> str | None:
    return _registry_row_identity(_mcp_entry(home))


def _ensure_mcp_registered(
    *, runner: Callable[..., Any], home: Path | None
) -> bool:
    expected = _expected_mcp_entry(home)
    current = _mcp_entry(home)
    if current is not None:
        current_enabled = dict(current)
        expected_enabled = dict(expected)
        current_enabled["disabled"] = False
        expected_enabled["disabled"] = False
        if current_enabled != expected_enabled:
            raise AntigravityInstallError(
                "existing Antigravity omg-tools MCP entry is foreign; preserved"
            )
    created = current is None
    if created:
        result = _run(
            [
                "agy",
                "mcp",
                "add",
                "--env",
                "OMG_TOOLS_NETWORK=0",
                LIVE_PROBE_SERVER,
                "python3",
                str(installed_plugin_path(home) / "bin" / "omg"),
                "tools",
                "serve",
                "--stdio",
                "--capability-mode",
                "read-only",
            ],
            runner=runner,
            home=home,
        )
        _require_success(result, "MCP registration")
    else:
        result = _run(["agy", "mcp", "enable", LIVE_PROBE_SERVER], runner=runner, home=home)
        _require_success(result, "MCP enable")
    if _mcp_entry(home) != expected:
        raise AntigravityInstallError("Antigravity omg-tools MCP registration did not persist")
    return created


def load_ownership_receipt(home: Path | None = None) -> dict[str, Any] | None:
    path = _assert_nofollow_path(
        ownership_receipt_path(home), label="Antigravity ownership receipt"
    )
    if not path.is_file():
        return None
    try:
        raw = json.loads(read_managed_regular_bytes(path).decode("utf-8"))
    except (ContractPathError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    references = raw.get("references", []) if isinstance(raw, dict) else []
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != "omg-agy-ownership/v1"
        or raw.get("target") != str(installed_plugin_path(home).absolute())
        or not isinstance(raw.get("plugin_digest"), str)
        or not isinstance(raw.get("registry_identity"), str)
        or not isinstance(raw.get("mcp_registry_identity"), str)
        or not isinstance(references, list)
        or any(
            not isinstance(item, str)
            or not Path(item).is_absolute()
            or str(Path(item).absolute()) != item
            for item in references
        )
        or len(set(references)) != len(references)
        or references != sorted(references)
    ):
        return None
    return raw


def persist_ownership_receipt(
    *,
    plugin_digest: str,
    registry_identity: str,
    mcp_registry_identity: str,
    references: list[str] | None = None,
    home: Path | None = None,
) -> Path:
    root = _assert_nofollow_path(config_root(home), label="Antigravity config root")
    ensure_managed_dir(root)
    path = ownership_receipt_path(home)
    atomic_write_bytes(
        path,
        json.dumps(
            {
                "schema": "omg-agy-ownership/v1",
                "config_root": str(root),
                "target": str(installed_plugin_path(home).absolute()),
                "plugin_digest": plugin_digest,
                "registry_identity": registry_identity,
                "mcp_registry_identity": mcp_registry_identity,
                "references": sorted(set(references or [])),
            },
            sort_keys=True,
        ).encode("utf-8"),
    )
    return path


def clear_ownership_receipt(
    *,
    expected_digest: str,
    expected_registry_identity: str,
    expected_mcp_registry_identity: str,
    home: Path | None = None,
) -> bool:
    receipt = load_ownership_receipt(home)
    if receipt is None:
        return False
    if (
        receipt.get("plugin_digest") != expected_digest
        or receipt.get("registry_identity") != expected_registry_identity
        or receipt.get("mcp_registry_identity") != expected_mcp_registry_identity
    ):
        return False
    path = _assert_nofollow_path(
        ownership_receipt_path(home), label="Antigravity ownership receipt"
    )
    path.unlink(missing_ok=True)
    return True


def release_ownership_reference(
    *,
    reference: str,
    expected_digest: str,
    expected_registry_identity: str,
    expected_mcp_registry_identity: str,
    home: Path | None = None,
) -> bool:
    """Release one shared manifest reference under exact receipt CAS."""
    with plugin_lock(home):
        receipt = load_ownership_receipt(home)
        if not isinstance(receipt, dict) or (
            receipt.get("plugin_digest"),
            receipt.get("registry_identity"),
            receipt.get("mcp_registry_identity"),
        ) != (expected_digest, expected_registry_identity, expected_mcp_registry_identity):
            return False
        references = receipt.get("references", [])
        if reference not in references or len(set(references)) <= 1:
            return False
        persist_ownership_receipt(
            plugin_digest=expected_digest,
            registry_identity=expected_registry_identity,
            mcp_registry_identity=expected_mcp_registry_identity,
            references=[item for item in references if item != reference],
            home=home,
        )
        updated = load_ownership_receipt(home)
        return isinstance(updated, dict) and updated.get("references") == sorted(
            {item for item in references if item != reference}
        )


def _assert_nofollow_path(path: Path, *, label: str) -> Path:
    """Reject any existing symlink/reparse ancestor using the shared backend."""
    absolute = Path(path).absolute()
    anchor = Path(absolute.anchor)
    try:
        return confined_path(anchor, *absolute.parts[1:])
    except (ContractPathError, OSError) as exc:
        raise AntigravityInstallError(f"{label} contains an unsafe path component") from exc


def _home_for_config_root(root: Path) -> Path:
    if root.name != "config" or root.parent.name != ".gemini":
        raise AntigravityInstallError("Antigravity config root is not canonical")
    return root.parent.parent


def _run(
    argv: list[str],
    *,
    runner: Callable[..., Any] = subprocess.run,
    home: Path | None = None,
    timeout: int = 60,
) -> Any:
    env = os.environ.copy()
    if home is not None:
        env["HOME"] = str(Path(home))
    try:
        return runner(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AntigravityInstallError(
            f"Antigravity command could not run: {argv[0]} {argv[1]}"
        ) from exc


def _require_success(result: Any, label: str) -> None:
    if int(getattr(result, "returncode", 1)) != 0:
        raise AntigravityInstallError(f"Antigravity {label} failed")


def _listed_imports(
    *, runner: Callable[..., Any] = subprocess.run, home: Path | None = None
) -> list[dict[str, Any]]:
    result = _run(["agy", "plugin", "list"], runner=runner, home=home)
    _require_success(result, "plugin list")
    raw = str(getattr(result, "stdout", "") or "").strip()
    if raw == "No imported plugins.":
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AntigravityInstallError("Antigravity plugin list was malformed") from exc
    rows = payload.get("imports") if isinstance(payload, dict) else None
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise AntigravityInstallError("Antigravity plugin list lacked imports")
    return [row for row in rows if isinstance(row, dict)]


def _is_listed(rows: list[dict[str, Any]]) -> bool:
    return any(str(row.get("name") or "") == PLUGIN_NAME for row in rows)


def _listed_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        if str(row.get("name") or "") == PLUGIN_NAME:
            return row
    return None


def expected_components(plugin: Path) -> set[str]:
    components: set[str] = set()
    for directory, label in (("skills", "skills"), ("agents", "agents"), ("commands", "commands")):
        if (plugin / directory).is_dir():
            components.add(label)
    if (plugin / "hooks.json").is_file():
        components.add("hooks")
    if (plugin / "mcp_config.json").is_file():
        components.add("mcpServers")
    return components


def _enabled(home: Path | None = None) -> bool:
    path = config_root(home) / "config.json"
    _assert_nofollow_path(path, label="Antigravity config")
    if not path.is_file() or path.is_symlink():
        return True
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    plugins = payload.get("plugins") if isinstance(payload, dict) else None
    row = plugins.get(PLUGIN_NAME) if isinstance(plugins, dict) else None
    if not isinstance(row, dict):
        return True
    return row.get("enabled") is not False


def _package_digest(path: Path) -> str | None:
    _assert_nofollow_path(path, label="Antigravity plugin")
    if path.is_symlink() or not path.is_dir():
        return None
    try:
        from omg_cli.setup_cmd import compute_package_identity

        base = str(compute_package_identity(path, canonicalize_posix_launchers=False)["digest"])
        supplemental = []
        for name in ("hooks.json", "mcp_config.json"):
            candidate = path / name
            if candidate.is_symlink():
                return None
            if candidate.is_file():
                supplemental.append(
                    {"path": name, "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest()}
                )
        return hashlib.sha256(
            json.dumps(
                {"base": base, "supplemental": supplemental},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    except Exception:  # noqa: BLE001 - invalid/foreign package is a classification
        return None


def _registry_row_identity(row: dict[str, Any] | None) -> str | None:
    if row is None:
        return None
    return hashlib.sha256(
        json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def plugin_registry_identity(
    *, runner: Callable[..., Any] = subprocess.run, home: Path | None = None
) -> str | None:
    return _registry_row_identity(_listed_row(_listed_imports(runner=runner, home=home)))


def _tool_event_invokes(event: Any, tool: str) -> bool:
    if not isinstance(event, dict):
        return False
    if str(event.get("event") or "").lower() != "step_update":
        return False
    update = event.get("step_update")
    if not isinstance(update, dict):
        return False
    info = update.get("tool_info")
    params = info.get("parameters") if isinstance(info, dict) else None
    return bool(
        update.get("state") == "DONE"
        and update.get("step_type") == "tool"
        and update.get("tool_name") == "call_mcp_tool"
        and isinstance(info, dict)
        and not info.get("error")
        and isinstance(params, dict)
        and params.get("ServerName") == LIVE_PROBE_SERVER
        and params.get("ToolName") == tool
    )


def _result_event_contains(event: Any, token: str) -> bool:
    if not isinstance(event, dict):
        return False
    if str(event.get("event") or event.get("type") or "").lower() != "result":
        return False
    result = event.get("result")
    if not isinstance(result, dict) or result.get("status") != "SUCCESS":
        return False
    return token in str(result.get("response") or result.get("text") or "")


def _live_agent_smoke(*, runner: Callable[..., Any], home: Path | None) -> tuple[bool, bool]:
    prompt = (
        f"Use call_mcp_tool with ServerName={LIVE_PROBE_SERVER} and "
        f"ToolName={LIVE_PROBE_TOOL} exactly once, then reply with "
        f"{LIVE_PROBE_TOKEN}. Do not answer without the tool result."
    )
    result = _run(
        [
            "agy",
            "--agent",
            DISCOVERY_AGENT,
            "--output-format",
            "stream-json",
            "--print-timeout",
            "30s",
            "-p",
            prompt,
        ],
        runner=runner,
        home=home,
        timeout=45,
    )
    if int(getattr(result, "returncode", 1)) != 0:
        return False, False
    events: list[Any] = []
    try:
        for line in str(getattr(result, "stdout", "") or "").splitlines():
            if line.strip():
                events.append(json.loads(line))
    except json.JSONDecodeError:
        return False, False
    invoked = any(_tool_event_invokes(event, LIVE_PROBE_TOOL) for event in events)
    completed = any(_result_event_contains(event, LIVE_PROBE_TOKEN) for event in events)
    return invoked, completed


def package_digest(path: Path) -> str | None:
    """Public package identity used by manifest uninstall drift checks."""
    return _package_digest(path)


def probe_plugin(
    *,
    plugin: Path | None = None,
    home: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Return fresh CLI-backed discovery tiers without leaking paths/output."""
    binary = shutil.which("agy")
    if binary is None:
        return {
            "configured": False,
            "installed": False,
            "enabled": False,
            "loadable": False,
            "observed": False,
            "healthy": False,
            "verified": False,
            "live_verified": False,
            "error": "agy binary not found",
        }
    destination = installed_plugin_path(home)
    try:
        _assert_nofollow_path(config_root(home), label="Antigravity config root")
        _assert_nofollow_path(destination, label="Antigravity plugin target")
        rows = _listed_imports(runner=runner, home=home)
        listed_row = _listed_row(rows)
        listed = listed_row is not None
        components = {
            str(item)
            for item in ((listed_row or {}).get("components") or [])
            if isinstance(item, str)
        }
        enabled = bool(listed and _enabled(home))
        validate_target = destination if destination.is_dir() else plugin
        validated = False
        if validate_target is not None:
            result = _run(["agy", "plugin", "validate", str(validate_target)], runner=runner)
            validated = int(getattr(result, "returncode", 1)) == 0
        agents = _run(["agy", "agent"], runner=runner, home=home)
        agent_discovered = bool(
            int(getattr(agents, "returncode", 1)) == 0
            and DISCOVERY_AGENT
            in {line.strip() for line in str(getattr(agents, "stdout", "") or "").splitlines()}
        )
        observed = bool(listed and agent_discovered)
        expected = expected_components(plugin or destination)
        components_current = expected.issubset(components)
        healthy = bool(observed and enabled and validated and components_current)
        hook_registered = "hooks" in components and "hooks" in expected
        mcp_registered = _mcp_entry(home) == _expected_mcp_entry(home)
        mcp_invoked = False
        live_execution = False
        if healthy and hook_registered and mcp_registered and "mcpServers" in expected:
            mcp_invoked, live_execution = _live_agent_smoke(runner=runner, home=home)
        live_verified = bool(healthy and hook_registered and mcp_invoked and live_execution)
        return {
            "configured": True,
            "installed": listed,
            "enabled": enabled,
            "loadable": observed,
            "observed": observed,
            "healthy": healthy,
            "verified": healthy,
            "live_verified": live_verified,
            "plugin_digest": _package_digest(destination),
            "registry_identity": _registry_row_identity(listed_row),
            "mcp_registry_identity": mcp_registry_identity(home),
            "registry_components": sorted(components),
            "expected_components": sorted(expected),
            "evidence": {
                "plugin_list": listed,
                "plugin_validate": validated,
                "agent_discovery": agent_discovered,
                "registry_components": components_current,
                "hook_registration": hook_registered,
                "mcp_registration": mcp_registered,
                "agent_execution": live_execution,
                "mcp_tool_invocation": mcp_invoked,
            },
        }
    except AntigravityInstallError as exc:
        return {
            "configured": True,
            "installed": False,
            "enabled": False,
            "loadable": False,
            "observed": False,
            "healthy": False,
            "verified": False,
            "live_verified": False,
            "error": str(exc),
        }


def _install_plugin_locked(
    plugin: Path,
    *,
    home: Path | None = None,
    force: bool = False,
    owned_previous_digest: str | None = None,
    recovery_dir: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Validate, install, enable, and prove discovery; undo a new failed import."""
    del force  # Ownership, not a broad force flag, authorizes replacement.
    if shutil.which("agy") is None:
        raise AntigravityInstallError("agy binary not found")
    _assert_nofollow_path(plugin, label="OMG Antigravity package")
    _assert_nofollow_path(config_root(home), label="Antigravity config root")
    _assert_nofollow_path(installed_plugin_path(home), label="Antigravity plugin target")
    source_digest = _package_digest(plugin)
    if source_digest is None:
        raise AntigravityInstallError("OMG Antigravity package identity is invalid")
    validated = _run(["agy", "plugin", "validate", str(plugin)], runner=runner)
    _require_success(validated, "plugin validate")
    rows = _listed_imports(runner=runner, home=home)
    destination = installed_plugin_path(home)
    listed_before = _is_listed(rows)
    destination_digest = _package_digest(destination)
    expected = expected_components(plugin)
    listed_row = _listed_row(rows)
    registered = {
        str(item) for item in ((listed_row or {}).get("components") or []) if isinstance(item, str)
    }
    refresh_needed = bool(expected - registered)
    replace_owned = bool(
        destination_digest and owned_previous_digest and destination_digest == owned_previous_digest
    )
    if listed_before or destination.exists() or destination.is_symlink():
        if not listed_before or (destination_digest != source_digest and not replace_owned):
            raise AntigravityInstallError(
                "existing Antigravity oh-my-grok import is foreign or drifted; preserved"
            )
        if owned_previous_digest is None:
            raise AntigravityInstallError(
                "existing Antigravity oh-my-grok import is unreceipted; preserved"
            )
        if destination_digest != source_digest or refresh_needed:
            return _replace_plugin_transactionally(
                plugin,
                source_digest=source_digest,
                home=home,
                runner=runner,
                recovery_dir=recovery_dir,
            )
        created = False
    else:
        if recovery_dir is not None:
            _mark_recovery_phase(
                recovery_dir, "installing_plugin", intended_plugin_digest=source_digest
            )
        installed = _run(["agy", "plugin", "install", str(plugin)], runner=runner, home=home)
        _require_success(installed, "plugin install")
        created = True
    mcp_created = False
    try:
        if recovery_dir is not None:
            _mark_recovery_phase(
                recovery_dir, "enabling_plugin", intended_plugin_digest=source_digest
            )
        enabled = _run(["agy", "plugin", "enable", PLUGIN_NAME], runner=runner, home=home)
        _require_success(enabled, "plugin enable")
        if recovery_dir is not None:
            _mark_recovery_phase(
                recovery_dir, "registering_mcp", intended_plugin_digest=source_digest
            )
        mcp_created = _ensure_mcp_registered(runner=runner, home=home)
        if recovery_dir is not None:
            seal_recovery_post_state(recovery_dir, runner=runner, home=home)
        evidence = probe_plugin(plugin=plugin, home=home, runner=runner)
        if not evidence.get("live_verified"):
            raise AntigravityInstallError(
                "installed plugin did not complete the bounded OMG MCP live probe"
            )
        if evidence.get("plugin_digest") != source_digest:
            raise AntigravityInstallError("installed Antigravity plugin bytes drifted")
        return {
            **evidence,
            "created": created,
            "content_hash": source_digest,
            "target": str(destination),
            "registry_refreshed": False,
        }
    except Exception:
        if recovery_dir is None:
            if mcp_created:
                _run(["agy", "mcp", "remove", LIVE_PROBE_SERVER], runner=runner, home=home)
            if created:
                _run(["agy", "plugin", "uninstall", PLUGIN_NAME], runner=runner, home=home)
        raise


def install_plugin(
    plugin: Path,
    *,
    home: Path | None = None,
    force: bool = False,
    owned_previous_digest: str | None = None,
    recovery_dir: Path | None = None,
    snapshot_callback: Callable[[], None] | None = None,
    ownership_reference: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Run the complete machine-global mutation under one canonical lock."""
    with plugin_lock(home):
        uninstall_recovery = config_root(home) / ".omg-transactions" / "agy-uninstall"
        if os.path.lexists(uninstall_recovery):
            raise AntigravityInstallError(
                "unfinished Antigravity uninstall transaction must be recovered first"
            )
        if recovery_dir is not None:
            persist_recovery_snapshot(recovery_dir, home=home, runner=runner)
            if snapshot_callback is not None:
                snapshot_callback()
        try:
            evidence = _install_plugin_locked(
                plugin,
                home=home,
                force=force,
                owned_previous_digest=owned_previous_digest,
                recovery_dir=recovery_dir,
                runner=runner,
            )
            registry_identity = plugin_registry_identity(runner=runner, home=home)
            mcp_identity = mcp_registry_identity(home)
            if not isinstance(registry_identity, str) or not isinstance(mcp_identity, str):
                raise AntigravityInstallError("Antigravity registry identity missing")
            current_receipt = load_ownership_receipt(home)
            current_references = (
                current_receipt.get("references", [])
                if isinstance(current_receipt, dict)
                else []
            )
            if recovery_dir is not None:
                _mark_recovery_phase(
                    recovery_dir,
                    "writing_receipt",
                    intended_plugin_digest=str(evidence["content_hash"]),
                )
            persist_ownership_receipt(
                plugin_digest=str(evidence["content_hash"]),
                registry_identity=registry_identity,
                mcp_registry_identity=mcp_identity,
                references=sorted(
                    set(
                        [
                            *current_references,
                            *([ownership_reference] if ownership_reference else []),
                        ]
                    )
                ),
                home=home,
            )
            if recovery_dir is not None:
                # The ownership receipt is part of the machine-global state.
                # Seal only after it is durable so later manifest failures can
                # CAS the complete transaction-owned post-state back to pre-state.
                seal_recovery_post_state(recovery_dir, runner=runner, home=home)
            return {
                **evidence,
                "registry_identity": registry_identity,
                "mcp_registry_identity": mcp_identity,
            }
        except Exception:
            if recovery_dir is not None and not restore_recovery_snapshot(
                recovery_dir, runner=runner, lock_held=True
            ):
                raise AntigravityInstallError(
                    "Antigravity mutation failed and durable rollback was not proven"
                )
            raise


def _assert_snapshot_safe(root: Path) -> None:
    _assert_nofollow_path(root, label="Antigravity plugin snapshot source")
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            raise AntigravityInstallError("installed Antigravity plugin contains a symlink")
        if not (path.is_dir() or path.is_file()):
            raise AntigravityInstallError("installed Antigravity plugin contains a special file")


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
    except OSError:
        if os.name == "nt":
            return
        raise
    try:
        os.fsync(directory_fd)
    except OSError:
        if os.name != "nt":
            raise
    finally:
        os.close(directory_fd)


def _remove_plugin_tree(target: Path) -> None:
    """Remove a plugin directory without following it, then fsync the parent."""
    if target.is_symlink():
        raise AntigravityInstallError("Antigravity plugin target is a symlink")
    if not os.path.lexists(target):
        return
    parent = target.parent
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    _fsync_directory(parent)


def _restore_plugin_tree_atomic(backup: Path, target: Path, expected_digest: str) -> None:
    """Stage/verify the prior plugin privately, then atomically publish it."""
    ensure_managed_dir(target.parent)
    temporary = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}")
    try:
        shutil.copytree(backup, temporary, copy_function=shutil.copy2)
        _assert_snapshot_safe(temporary)
        if _package_digest(temporary) != expected_digest:
            raise AntigravityInstallError("Antigravity staged rollback identity mismatch")
        if os.path.lexists(target):
            raise AntigravityInstallError("Antigravity rollback target is unexpectedly occupied")
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        if temporary.is_dir() and not temporary.is_symlink():
            shutil.rmtree(temporary, ignore_errors=True)
            try:
                _fsync_directory(temporary.parent)
            except OSError:
                pass


def _registry_snapshot(home: Path | None) -> dict[Path, tuple[bool, bytes]]:
    root = config_root(home)
    _assert_nofollow_path(root, label="Antigravity config root")
    snapshots: dict[Path, tuple[bool, bytes]] = {}
    for path in (
        root / "import_manifest.json",
        root / "config.json",
        mcp_registry_path(home),
        ownership_receipt_path(home),
    ):
        if path.is_symlink():
            raise AntigravityInstallError("Antigravity registry path is a symlink")
        if os.path.lexists(path) and not path.is_file():
            raise AntigravityInstallError(
                "Antigravity registry path is not a regular file"
            )
        snapshots[path] = (
            path.is_file(),
            read_managed_regular_bytes(path) if path.is_file() else b"",
        )
    return snapshots


def _registry_file_identities(home: Path | None) -> dict[str, str | None]:
    return {
        path.name: hashlib.sha256(body).hexdigest() if present else None
        for path, (present, body) in _registry_snapshot(home).items()
    }


def _restore_registry(snapshot: dict[Path, tuple[bool, bytes]]) -> None:
    for path, (present, body) in snapshot.items():
        _assert_nofollow_path(path, label="Antigravity registry restore target")
        ensure_managed_dir(path.parent)
        if not present:
            path.unlink(missing_ok=True)
            continue
        atomic_write_bytes(path, body)


def _owned_json_row(body: bytes, *, filename: str) -> Any:
    payload = json.loads(body.decode("utf-8")) if body else {}
    if not isinstance(payload, dict):
        raise AntigravityInstallError(f"Antigravity {filename} registry is malformed")
    if filename == "import_manifest.json":
        rows = payload.get("imports", [])
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise AntigravityInstallError("Antigravity imports registry is malformed")
        matches = [
            row for row in rows if isinstance(row, dict) and row.get("name") == PLUGIN_NAME
        ]
        if len(matches) > 1:
            raise AntigravityInstallError("Antigravity imports registry is ambiguous")
        return matches[0] if matches else None
    section_name = "plugins" if filename == "config.json" else "mcpServers"
    section = payload.get(section_name, {})
    if not isinstance(section, dict):
        raise AntigravityInstallError(f"Antigravity {section_name} registry is malformed")
    return section.get(PLUGIN_NAME if filename == "config.json" else LIVE_PROBE_SERVER)


def _payload_without_owned_row(payload: Any, *, filename: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AntigravityInstallError(f"Antigravity {filename} registry is malformed")
    stripped = json.loads(json.dumps(payload))
    if not isinstance(stripped, dict):
        raise AntigravityInstallError(f"Antigravity {filename} registry is malformed")
    if filename == "import_manifest.json":
        rows = stripped.get("imports", [])
        if rows is None:
            rows = []
        if not isinstance(rows, list):
            raise AntigravityInstallError("Antigravity imports registry is malformed")
        stripped["imports"] = [
            row
            for row in rows
            if not (isinstance(row, dict) and row.get("name") == PLUGIN_NAME)
        ]
        return stripped
    section_name = "plugins" if filename == "config.json" else "mcpServers"
    key = PLUGIN_NAME if filename == "config.json" else LIVE_PROBE_SERVER
    section = stripped.get(section_name, {})
    if not isinstance(section, dict):
        raise AntigravityInstallError(f"Antigravity {section_name} registry is malformed")
    section.pop(key, None)
    stripped[section_name] = section
    return stripped


def _restore_owned_registry_rows(snapshot: dict[Path, tuple[bool, bytes]]) -> None:
    """Restore only OMG-owned rows, preserving unrelated concurrent registry edits."""
    for path, (prior_present, prior_body) in snapshot.items():
        if path.name == "omg-ownership.json":
            current = read_managed_regular_bytes(path) if path.is_file() else b""
            if (
                current != prior_body
                and path.is_file()
                and load_ownership_receipt(_home_for_config_root(path.parent)) is None
            ):
                raise AntigravityInstallError("Antigravity ownership receipt drifted")
            if prior_present:
                atomic_write_bytes(path, prior_body)
            else:
                path.unlink(missing_ok=True)
            continue
        current_body = read_managed_regular_bytes(path) if path.is_file() else b"{}"
        current = json.loads(current_body.decode("utf-8")) if current_body else {}
        prior = json.loads(prior_body.decode("utf-8")) if prior_present else {}
        if not isinstance(current, dict) or not isinstance(prior, dict):
            raise AntigravityInstallError(f"Antigravity {path.name} registry is malformed")
        if _payload_without_owned_row(current, filename=path.name) == _payload_without_owned_row(
            prior, filename=path.name
        ):
            if prior_present:
                atomic_write_bytes(path, prior_body)
            else:
                path.unlink(missing_ok=True)
            continue
        prior_row = _owned_json_row(prior_body if prior_present else b"{}", filename=path.name)
        if path.name == "import_manifest.json":
            current_rows = current.get("imports", [])
            if not isinstance(current_rows, list):
                raise AntigravityInstallError("Antigravity imports registry is malformed")
            foreign = [
                row
                for row in current_rows
                if not (isinstance(row, dict) and row.get("name") == PLUGIN_NAME)
            ]
            current["imports"] = [*foreign, *([prior_row] if prior_row is not None else [])]
        else:
            section_name = "plugins" if path.name == "config.json" else "mcpServers"
            section = current.setdefault(section_name, {})
            if not isinstance(section, dict):
                raise AntigravityInstallError(f"Antigravity {section_name} registry is malformed")
            key = PLUGIN_NAME if path.name == "config.json" else LIVE_PROBE_SERVER
            if prior_row is None:
                section.pop(key, None)
            else:
                section[key] = prior_row
        if not prior_present:
            compact = dict(current)
            section_name = (
                "imports"
                if path.name == "import_manifest.json"
                else ("plugins" if path.name == "config.json" else "mcpServers")
            )
            if compact.get(section_name) in ({}, []):
                compact.pop(section_name, None)
            if not compact:
                path.unlink(missing_ok=True)
                continue
        atomic_write_bytes(path, json.dumps(current, sort_keys=True).encode("utf-8"))


def _owned_registry_rows_match(snapshot: dict[Path, tuple[bool, bytes]]) -> bool:
    try:
        for path, (prior_present, prior_body) in snapshot.items():
            if path.name == "omg-ownership.json":
                current = read_managed_regular_bytes(path) if path.is_file() else b""
                if current != prior_body:
                    return False
                continue
            current_body = read_managed_regular_bytes(path) if path.is_file() else b"{}"
            if _owned_json_row(current_body, filename=path.name) != _owned_json_row(
                prior_body if prior_present else b"{}", filename=path.name
            ):
                return False
        return True
    except (AntigravityInstallError, ContractPathError, OSError, ValueError, json.JSONDecodeError):
        return False


def persist_recovery_snapshot(
    backup_dir: Path,
    *,
    home: Path | None = None,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Persist the exact machine-global pre-state for crash recovery."""
    ensure_managed_dir(backup_dir)
    root = _assert_nofollow_path(config_root(home), label="Antigravity config root")
    target = _assert_nofollow_path(installed_plugin_path(home), label="Antigravity plugin target")
    plugin_backup = backup_dir / "agy-plugin.prev"
    previous_target_present = os.path.lexists(target)
    if previous_target_present:
        _assert_snapshot_safe(target)
    previous_digest = _package_digest(target)
    if previous_target_present and previous_digest is None:
        raise AntigravityInstallError(
            "existing Antigravity plugin target is incomplete or unsafe; preserved"
        )
    if previous_digest is not None:
        _assert_snapshot_safe(target)
        shutil.copytree(target, plugin_backup, copy_function=shutil.copy2)
        _assert_snapshot_safe(plugin_backup)
        if _package_digest(plugin_backup) != previous_digest:
            raise AntigravityInstallError("Antigravity plugin backup identity changed while copying")
    registry_rows: list[dict[str, Any]] = []
    for path, (present, body) in _registry_snapshot(home).items():
        name = path.name
        backup_name = f"agy-registry-{name}.bak"
        if present:
            atomic_write_bytes(backup_dir / backup_name, body)
        registry_rows.append(
            {
                "name": name,
                "present": present,
                "backup": backup_name,
                "sha256": hashlib.sha256(body).hexdigest() if present else None,
            }
        )
    state = {
        "schema": "omg-agy-recovery/v1",
        "config_root": str(root),
        "target": str(target),
        "previous_plugin_present": previous_digest is not None,
        "previous_target_state": "exact" if previous_target_present else "absent",
        "previous_plugin_digest": previous_digest,
        "previous_registry_identity": plugin_registry_identity(runner=runner, home=home),
        "previous_mcp_registry_identity": mcp_registry_identity(home),
        "registry": registry_rows,
    }
    atomic_write_bytes(
        backup_dir / "current.json",
        json.dumps(state, sort_keys=True).encode("utf-8"),
    )
    return state


def seal_recovery_post_state(
    backup_dir: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    home: Path | None = None,
    require_installed: bool | None = True,
) -> None:
    path = _assert_nofollow_path(backup_dir / "current.json", label="Antigravity recovery state")
    raw = json.loads(read_managed_regular_bytes(path).decode("utf-8"))
    if raw.get("schema") != "omg-agy-recovery/v1":
        raise AntigravityInstallError("Antigravity recovery state schema is invalid")
    target = installed_plugin_path(home)
    raw["post_plugin_digest"] = _package_digest(target)
    raw["post_registry_identity"] = plugin_registry_identity(runner=runner, home=home)
    raw["post_mcp_registry_identity"] = mcp_registry_identity(home)
    raw["post_registry_files"] = _registry_file_identities(home)
    raw["post_target_state"] = "present" if os.path.lexists(target) else "absent"
    identities = tuple(
        raw.get(key)
        for key in ("post_plugin_digest", "post_registry_identity", "post_mcp_registry_identity")
    )
    if require_installed is True and (
        raw["post_target_state"] != "present"
        or not all(isinstance(value, str) for value in identities)
    ):
        raise AntigravityInstallError("Antigravity post-state could not be sealed")
    if require_installed is False and (
        raw["post_target_state"] != "absent" or any(value is not None for value in identities)
    ):
        raise AntigravityInstallError("Antigravity removed post-state could not be sealed")
    atomic_write_bytes(path, json.dumps(raw, sort_keys=True).encode("utf-8"))


def _mark_recovery_phase(
    backup_dir: Path,
    phase: str,
    *,
    intended_plugin_digest: str,
) -> None:
    """Durably announce an exact owned transition before its mutation starts."""
    path = _assert_nofollow_path(backup_dir / "current.json", label="Antigravity recovery state")
    raw = json.loads(read_managed_regular_bytes(path).decode("utf-8"))
    if raw.get("schema") != "omg-agy-recovery/v1":
        raise AntigravityInstallError("Antigravity recovery state schema is invalid")
    raw["phase"] = phase
    raw["intended_plugin_digest"] = intended_plugin_digest
    atomic_write_bytes(path, json.dumps(raw, sort_keys=True).encode("utf-8"))


def restore_recovery_snapshot(
    backup_dir: Path,
    *,
    runner: Callable[..., Any] = subprocess.run,
    lock_held: bool = False,
    expected_current: tuple[
        tuple[str | None, str | None, str | None], dict[str, str | None]
    ]
    | None = None,
    undo_committed: bool = False,
) -> bool:
    try:
        backup_dir = _assert_nofollow_path(backup_dir, label="Antigravity recovery directory")
        raw = json.loads(read_managed_regular_bytes(backup_dir / "current.json").decode("utf-8"))
        if raw.get("schema") != "omg-agy-recovery/v1":
            return False
        if raw.get("status") == "committed" and not undo_committed:
            return True
        root = _assert_nofollow_path(
            Path(str(raw["config_root"])), label="recorded Antigravity config root"
        )
        target = _assert_nofollow_path(
            Path(str(raw["target"])), label="recorded Antigravity plugin target"
        )
        if target != root / "plugins" / PLUGIN_NAME:
            return False
        home = _home_for_config_root(root)
        previous_target_state = raw.get("previous_target_state")
        if previous_target_state not in {"absent", "exact"}:
            return False
        expected_names = {
            "config.json", "import_manifest.json", "mcp_config.json", "omg-ownership.json"
        }
        rows = raw.get("registry")
        if not isinstance(rows, list) or len(rows) != len(expected_names):
            return False
        snapshot: dict[Path, tuple[bool, bytes]] = {}
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                return False
            name = row.get("name")
            if name not in expected_names or name in seen or not isinstance(row.get("present"), bool):
                return False
            seen.add(name)
            expected_backup = f"agy-registry-{name}.bak"
            if row.get("backup") != expected_backup:
                return False
            present = row["present"]
            body = b""
            if present:
                body = read_managed_regular_bytes(backup_dir / expected_backup)
                if hashlib.sha256(body).hexdigest() != row.get("sha256"):
                    return False
            elif row.get("sha256") is not None:
                return False
            snapshot[root / str(name)] = (present, body)
        if seen != expected_names:
            return False
        previous_digest = raw.get("previous_plugin_digest")
        if previous_target_state == "exact" and not isinstance(previous_digest, str):
            return False
        if previous_target_state == "absent" and previous_digest is not None:
            return False
        backup: Path | None = None
        if raw.get("previous_plugin_present") is True:
            backup = _assert_nofollow_path(
                backup_dir / "agy-plugin.prev", label="Antigravity plugin backup"
            )
            if _package_digest(backup) != previous_digest:
                return False
        lock = nullcontext() if lock_held else plugin_lock(home)
        with lock:
            current = (
                _package_digest(target),
                plugin_registry_identity(runner=runner, home=home),
                mcp_registry_identity(home),
            )
            previous = (
                previous_digest,
                raw.get("previous_registry_identity"),
                raw.get("previous_mcp_registry_identity"),
            )
            current_files = _registry_file_identities(home)
            previous_files = {
                str(row["name"]): row["sha256"] for row in rows
            }
            current_target_state = "present" if os.path.lexists(target) else "absent"
            previous_occupancy_matches = (
                previous_target_state == "exact" and current_target_state == "present"
            ) or (
                previous_target_state == "absent" and current_target_state == "absent"
            )
            if (
                current == previous
                and previous_occupancy_matches
                and _owned_registry_rows_match(snapshot)
            ):
                return True
            post = (
                raw.get("post_plugin_digest"),
                raw.get("post_registry_identity"),
                raw.get("post_mcp_registry_identity"),
            )
            post_types_valid = all(value is None or isinstance(value, str) for value in post)
            post_target_state = raw.get("post_target_state")
            post_occupancy_matches = (
                post_target_state == "present" and current_target_state == "present"
            ) or (post_target_state == "absent" and current_target_state == "absent")
            post_files = raw.get("post_registry_files")
            sealed_match = bool(
                post_types_valid
                and post_occupancy_matches
                and current == post
                and current_files == post_files
            )
            target_matches_previous = bool(
                current[0] == previous[0] and previous_occupancy_matches
            )
            target_matches_post = bool(current[0] == post[0] and post_occupancy_matches)
            mixed_recovery_match = bool(
                post_types_valid
                and isinstance(post_files, dict)
                and (target_matches_previous or target_matches_post)
                and current[1] in {previous[1], post[1]}
                and current[2] in {previous[2], post[2]}
                and all(
                    current_files.get(name) in {previous_files.get(name), post_files.get(name)}
                    for name in previous_files
                )
            )
            locked_intermediate_match = bool(
                lock_held
                and expected_current is not None
                and current == expected_current[0]
                and current_files == expected_current[1]
            )
            phase = raw.get("phase")
            intended_digest = raw.get("intended_plugin_digest")
            phase_order = {
                "installing_plugin": 1,
                "enabling_plugin": 2,
                "registering_mcp": 3,
                "writing_receipt": 4,
            }
            phase_level = phase_order.get(str(phase), 0)
            allowed_changed_files = {"import_manifest.json"}
            if phase_level >= 2:
                allowed_changed_files.add("config.json")
            if phase_level >= 3:
                allowed_changed_files.add("mcp_config.json")
            if phase_level >= 4:
                allowed_changed_files.add("omg-ownership.json")
            unchanged_files_exact = all(
                current_files.get(name) == previous_files.get(name)
                for name in previous_files
                if name not in allowed_changed_files
            )
            listed = _listed_row(_listed_imports(runner=runner, home=home))
            intended_target = bool(
                isinstance(intended_digest, str)
                and current[0] == intended_digest
                and current_target_state == "present"
                and isinstance(current[1], str)
                and isinstance(listed, dict)
                and listed.get("name") == PLUGIN_NAME
                and listed.get("source") == "antigravity"
                and {str(item) for item in listed.get("components", [])}
                == expected_components(target)
                and (phase_level < 2 or _enabled(home))
            )
            mcp_semantics_exact = bool(
                current[2] == previous[2]
                or (
                    phase_level >= 3
                    and isinstance(current[2], str)
                    and _mcp_entry(home) == _expected_mcp_entry(home)
                )
            )
            receipt_semantics_exact = True
            if current_files.get("omg-ownership.json") != previous_files.get(
                "omg-ownership.json"
            ):
                current_receipt = load_ownership_receipt(home)
                receipt_semantics_exact = bool(
                    phase_level >= 4
                    and isinstance(current_receipt, dict)
                    and current_receipt.get("plugin_digest") == intended_digest
                    and current_receipt.get("registry_identity") == current[1]
                    and current_receipt.get("mcp_registry_identity") == current[2]
                )
            write_ahead_intermediate_match = bool(
                phase_level
                and intended_target
                and unchanged_files_exact
                and mcp_semantics_exact
                and receipt_semantics_exact
            )
            rollback_in_progress_match = bool(
                phase == "restoring_prior_plugin"
                and current_target_state
                in {
                    "absent",
                    "present",
                }
                and current[0] in {None, previous[0], post[0]}
                and current[1] in {previous[1], post[1]}
                and current[2] in {previous[2], post[2]}
            )
            incomplete_fresh_install = bool(
                previous_target_state == "absent"
                and os.path.lexists(target)
                and current[0] is None
                and phase_level >= 1
            )
            journaled_refresh_gap = bool(
                str(phase) == "installing_plugin"
                and previous_target_state == "exact"
                and isinstance(previous_digest, str)
                and current[0] in {None, previous[0]}
            )
            if undo_committed:
                if not sealed_match:
                    return False
            elif not (
                sealed_match
                or mixed_recovery_match
                or locked_intermediate_match
                or write_ahead_intermediate_match
                or rollback_in_progress_match
                or incomplete_fresh_install
                or journaled_refresh_gap
            ):
                return False
            _mark_recovery_phase(
                backup_dir,
                "restoring_prior_plugin",
                intended_plugin_digest=str(previous_digest or ""),
            )
            if incomplete_fresh_install or (
                journaled_refresh_gap
                and current[0] is None
                and os.path.lexists(target)
            ):
                _remove_plugin_tree(target)
            elif not target_matches_previous and os.path.lexists(target):
                result = _run(
                    ["agy", "plugin", "uninstall", PLUGIN_NAME], runner=runner, home=home
                )
                if int(getattr(result, "returncode", 1)) != 0 or os.path.lexists(target):
                    return False
            _restore_owned_registry_rows(snapshot)
            if backup is not None and not target_matches_previous:
                _restore_plugin_tree_atomic(backup, target, str(previous_digest))
            restored_target_state = "present" if os.path.lexists(target) else "absent"
            return bool(
                (
                    _package_digest(target),
                    plugin_registry_identity(runner=runner, home=home),
                    mcp_registry_identity(home),
                )
                == previous
                and _owned_registry_rows_match(snapshot)
                and restored_target_state
                == ("present" if previous_target_state == "exact" else "absent")
            )
    except (
        AntigravityInstallError,
        ContractPathError,
        OSError,
        KeyError,
        ValueError,
        json.JSONDecodeError,
    ):
        return False


@contextmanager
def plugin_lock(home: Path | None = None) -> Iterator[None]:
    root = _assert_nofollow_path(config_root(home), label="Antigravity config root")
    ensure_managed_dir(root)
    with exclusive_lock(root / ".omg-plugin.lock"):
        yield


def uninstall_owned_plugin(
    *,
    expected_digest: str,
    expected_registry_identity: str,
    expected_mcp_registry_identity: str,
    runner: Callable[..., Any] = subprocess.run,
    home: Path | None = None,
    retain_committed: bool = False,
) -> bool:
    """Durably remove the receipt-owned MCP+plugin as one locked transaction."""
    with plugin_lock(home):
        recovery_dir = config_root(home) / ".omg-transactions" / "agy-uninstall"
        state_path = recovery_dir / "current.json"
        resuming = False
        if state_path.is_file():
            existing_state = json.loads(read_managed_regular_bytes(state_path).decode("utf-8"))
            committed = existing_state.get("status") == "committed"
            if committed:
                exact_committed = committed_owned_uninstall_matches(
                    expected_digest=expected_digest,
                    expected_registry_identity=expected_registry_identity,
                    expected_mcp_registry_identity=expected_mcp_registry_identity,
                    home=home,
                )
                if not exact_committed:
                    return False
                if not retain_committed:
                    parent = recovery_dir.parent
                    shutil.rmtree(recovery_dir)
                    _fsync_directory(parent)
                return True
            if (
                existing_state.get("schema") != "omg-agy-recovery/v1"
                or existing_state.get("previous_plugin_digest") != expected_digest
                or existing_state.get("previous_registry_identity")
                != expected_registry_identity
                or existing_state.get("previous_mcp_registry_identity")
                != expected_mcp_registry_identity
            ):
                return False
            resuming = True
        elif os.path.lexists(recovery_dir):
            return False

        def rollback_current() -> bool:
            expected_current = (
                (
                    _package_digest(installed_plugin_path(home)),
                    plugin_registry_identity(runner=runner, home=home),
                    mcp_registry_identity(home),
                ),
                _registry_file_identities(home),
            )
            return restore_recovery_snapshot(
                recovery_dir,
                runner=runner,
                lock_held=True,
                expected_current=expected_current,
            )

        if not resuming:
            if _package_digest(installed_plugin_path(home)) != expected_digest:
                return False
            if plugin_registry_identity(runner=runner, home=home) != expected_registry_identity:
                return False
            if mcp_registry_identity(home) != expected_mcp_registry_identity:
                return False
            receipt = load_ownership_receipt(home)
            if not isinstance(receipt, dict) or (
                receipt.get("plugin_digest"),
                receipt.get("registry_identity"),
                receipt.get("mcp_registry_identity"),
            ) != (expected_digest, expected_registry_identity, expected_mcp_registry_identity):
                return False
            try:
                persist_recovery_snapshot(recovery_dir, home=home, runner=runner)
            except (AntigravityInstallError, ContractPathError, OSError):
                if os.path.lexists(recovery_dir) and not state_path.is_file():
                    parent = recovery_dir.parent
                    shutil.rmtree(recovery_dir)
                    _fsync_directory(parent)
                return False

        def mark_phase(phase: str) -> None:
            state = json.loads(read_managed_regular_bytes(state_path).decode("utf-8"))
            state["phase"] = phase
            atomic_write_bytes(state_path, json.dumps(state, sort_keys=True).encode("utf-8"))

        current_mcp = mcp_registry_identity(home)
        if current_mcp not in {expected_mcp_registry_identity, None}:
            return False
        mark_phase("removing_mcp")
        if current_mcp == expected_mcp_registry_identity:
            removed = _run(
                ["agy", "mcp", "remove", LIVE_PROBE_SERVER], runner=runner, home=home
            )
            if int(getattr(removed, "returncode", 1)) != 0:
                if not rollback_current():
                    return False
                return False
        seal_recovery_post_state(
            recovery_dir, runner=runner, home=home, require_installed=None
        )
        current_plugin = _package_digest(installed_plugin_path(home))
        current_registry = plugin_registry_identity(runner=runner, home=home)
        if (current_plugin, current_registry) not in {
            (expected_digest, expected_registry_identity),
            (None, None),
        }:
            return False
        mark_phase("removing_plugin")
        if current_plugin == expected_digest:
            result = _run(
                ["agy", "plugin", "uninstall", PLUGIN_NAME], runner=runner, home=home
            )
            if int(getattr(result, "returncode", 1)) != 0 or os.path.lexists(
                installed_plugin_path(home)
            ):
                if not rollback_current():
                    return False
                return False
        seal_recovery_post_state(
            recovery_dir, runner=runner, home=home, require_installed=None
        )
        mark_phase("removing_receipt")
        if os.path.lexists(ownership_receipt_path(home)) and not clear_ownership_receipt(
                expected_digest=expected_digest,
                expected_registry_identity=expected_registry_identity,
                expected_mcp_registry_identity=expected_mcp_registry_identity,
                home=home,
            ):
            if not rollback_current():
                return False
            return False
        seal_recovery_post_state(
            recovery_dir, runner=runner, home=home, require_installed=False
        )
        if (
            os.path.lexists(installed_plugin_path(home))
            or plugin_registry_identity(runner=runner, home=home) is not None
            or mcp_registry_identity(home) is not None
            or os.path.lexists(ownership_receipt_path(home))
        ):
            rollback_current()
            return False
        state = json.loads(read_managed_regular_bytes(state_path).decode("utf-8"))
        state["status"] = "committed"
        atomic_write_bytes(state_path, json.dumps(state, sort_keys=True).encode("utf-8"))
        if not retain_committed:
            parent = recovery_dir.parent
            shutil.rmtree(recovery_dir)
            _fsync_directory(parent)
        return True


def finalize_owned_uninstall(*, home: Path | None = None) -> bool:
    """Remove only a proven committed durable uninstall marker."""
    with plugin_lock(home):
        recovery_dir = config_root(home) / ".omg-transactions" / "agy-uninstall"
        state_path = recovery_dir / "current.json"
        if not state_path.is_file():
            return not os.path.lexists(recovery_dir)
        try:
            state = json.loads(read_managed_regular_bytes(state_path).decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ContractPathError):
            return False
        if (
            state.get("schema") != "omg-agy-recovery/v1"
            or state.get("status") != "committed"
            or not isinstance(state.get("previous_plugin_digest"), str)
            or not isinstance(state.get("previous_registry_identity"), str)
            or not isinstance(state.get("previous_mcp_registry_identity"), str)
            or not committed_owned_uninstall_matches(
                expected_digest=state["previous_plugin_digest"],
                expected_registry_identity=state["previous_registry_identity"],
                expected_mcp_registry_identity=state["previous_mcp_registry_identity"],
                home=home,
            )
        ):
            return False
        parent = recovery_dir.parent
        shutil.rmtree(recovery_dir)
        _fsync_directory(parent)
        return not os.path.lexists(recovery_dir)


def committed_owned_uninstall_matches(
    *,
    expected_digest: str,
    expected_registry_identity: str,
    expected_mcp_registry_identity: str,
    home: Path | None = None,
) -> bool:
    """Recognize an exact committed removal so cross-runtime uninstall can resume."""
    recovery_dir = config_root(home) / ".omg-transactions" / "agy-uninstall"
    state_path = recovery_dir / "current.json"
    try:
        state = json.loads(read_managed_regular_bytes(state_path).decode("utf-8"))
        return bool(
            state.get("schema") == "omg-agy-recovery/v1"
            and state.get("status") == "committed"
            and state.get("previous_plugin_digest") == expected_digest
            and state.get("previous_registry_identity") == expected_registry_identity
            and state.get("previous_mcp_registry_identity") == expected_mcp_registry_identity
            and state.get("post_target_state") == "absent"
            and state.get("post_plugin_digest") is None
            and state.get("post_registry_identity") is None
            and state.get("post_mcp_registry_identity") is None
            and not os.path.lexists(installed_plugin_path(home))
            and plugin_registry_identity(home=home) is None
            and mcp_registry_identity(home) is None
            and not os.path.lexists(ownership_receipt_path(home))
            and _registry_file_identities(home) == state.get("post_registry_files")
        )
    except (
        AntigravityInstallError,
        ContractPathError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return False


def resumable_owned_uninstall_matches(
    *,
    expected_digest: str,
    expected_registry_identity: str,
    expected_mcp_registry_identity: str,
    home: Path | None = None,
) -> bool:
    """Recognize an uncommitted owned uninstall journal so planning can resume it."""
    recovery_dir = config_root(home) / ".omg-transactions" / "agy-uninstall"
    state_path = recovery_dir / "current.json"
    try:
        state = json.loads(read_managed_regular_bytes(state_path).decode("utf-8"))
        return bool(
            state.get("schema") == "omg-agy-recovery/v1"
            and state.get("status") != "committed"
            and state.get("previous_plugin_digest") == expected_digest
            and state.get("previous_registry_identity") == expected_registry_identity
            and state.get("previous_mcp_registry_identity") == expected_mcp_registry_identity
        )
    except (
        AntigravityInstallError,
        ContractPathError,
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return False


def _replace_plugin_transactionally(
    plugin: Path,
    *,
    source_digest: str,
    home: Path | None,
    runner: Callable[..., Any],
    recovery_dir: Path | None,
) -> dict[str, Any]:
    destination = installed_plugin_path(home)
    _assert_snapshot_safe(destination)
    if recovery_dir is None:
        raise AntigravityInstallError("owned Antigravity refresh requires durable recovery state")
    expected_registry = plugin_registry_identity(runner=runner, home=home)
    backup = recovery_dir / "agy-plugin.prev"
    try:
        if _package_digest(destination) != _package_digest(backup):
            raise AntigravityInstallError("Antigravity plugin changed before uninstall")
        if plugin_registry_identity(runner=runner, home=home) != expected_registry:
            raise AntigravityInstallError("Antigravity registry changed before uninstall")
        _mark_recovery_phase(
            recovery_dir, "installing_plugin", intended_plugin_digest=source_digest
        )
        removed = _run(["agy", "plugin", "uninstall", PLUGIN_NAME], runner=runner, home=home)
        _require_success(removed, "plugin uninstall for registry refresh")
        installed = _run(["agy", "plugin", "install", str(plugin)], runner=runner, home=home)
        _require_success(installed, "plugin reinstall for registry refresh")
        enabled = _run(["agy", "plugin", "enable", PLUGIN_NAME], runner=runner, home=home)
        _require_success(enabled, "plugin enable after registry refresh")
        _ensure_mcp_registered(runner=runner, home=home)
        seal_recovery_post_state(recovery_dir, runner=runner, home=home)
        evidence = probe_plugin(plugin=plugin, home=home, runner=runner)
        if not evidence.get("live_verified") or evidence.get("plugin_digest") != source_digest:
            raise AntigravityInstallError(
                "refreshed Antigravity registry did not pass the live probe"
            )
        return {
            **evidence,
            "created": False,
            "content_hash": source_digest,
            "target": str(destination),
            "registry_refreshed": True,
        }
    except Exception:
        current_state = (
            (
                _package_digest(destination),
                plugin_registry_identity(runner=runner, home=home),
                mcp_registry_identity(home),
            ),
            _registry_file_identities(home),
        )
        if not restore_recovery_snapshot(
            recovery_dir,
            runner=runner,
            lock_held=True,
            expected_current=current_state,
        ):
            raise AntigravityInstallError(
                "Antigravity registry refresh failed and durable rollback failed"
            )
        raise


def rollback_created_plugin(
    *,
    expected_digest: str,
    runner: Callable[..., Any] = subprocess.run,
    home: Path | None = None,
) -> bool:
    """Remove only a transaction-recorded newly-created Agy import."""
    current = _package_digest(installed_plugin_path(home))
    if current is None:
        return True
    if current != expected_digest:
        return False
    try:
        result = _run(["agy", "plugin", "uninstall", PLUGIN_NAME], runner=runner, home=home)
    except AntigravityInstallError:
        return False
    return int(getattr(result, "returncode", 1)) == 0


__all__ = [
    "AntigravityInstallError",
    "clear_ownership_receipt",
    "config_root",
    "install_plugin",
    "installed_plugin_path",
    "load_ownership_receipt",
    "ownership_receipt_path",
    "package_digest",
    "persist_ownership_receipt",
    "persist_recovery_snapshot",
    "plugin_registry_identity",
    "probe_plugin",
    "restore_recovery_snapshot",
    "rollback_created_plugin",
    "uninstall_owned_plugin",
]
