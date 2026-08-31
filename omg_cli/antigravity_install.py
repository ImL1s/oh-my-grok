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
from contextlib import contextmanager
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
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != "omg-agy-ownership/v1"
        or raw.get("target") != str(installed_plugin_path(home).absolute())
        or not isinstance(raw.get("plugin_digest"), str)
        or not isinstance(raw.get("registry_identity"), str)
    ):
        return None
    return raw


def persist_ownership_receipt(
    *, plugin_digest: str, registry_identity: str, home: Path | None = None
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
            },
            sort_keys=True,
        ).encode("utf-8"),
    )
    return path


def clear_ownership_receipt(
    *, expected_digest: str, expected_registry_identity: str, home: Path | None = None
) -> bool:
    receipt = load_ownership_receipt(home)
    if receipt is None:
        return False
    if (
        receipt.get("plugin_digest") != expected_digest
        or receipt.get("registry_identity") != expected_registry_identity
    ):
        return False
    path = _assert_nofollow_path(
        ownership_receipt_path(home), label="Antigravity ownership receipt"
    )
    path.unlink(missing_ok=True)
    return True


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
    event_type = str(event.get("type") or event.get("event") or "").lower()
    if event_type not in {"tool_call", "tool_use", "tool_start", "mcp_tool_call"}:
        return False
    candidates = [event.get("tool"), event.get("tool_name"), event.get("name")]
    nested = event.get("tool_call") or event.get("tool_use")
    if isinstance(nested, dict):
        candidates.extend((nested.get("tool"), nested.get("tool_name"), nested.get("name")))
    return tool in {str(candidate) for candidate in candidates if candidate is not None}


def _result_event_contains(event: Any, token: str) -> bool:
    if not isinstance(event, dict):
        return False
    event_type = str(event.get("type") or event.get("event") or "").lower()
    if event_type not in {"result", "final", "completion"}:
        return False
    return token in str(event.get("result") or event.get("text") or event.get("content") or "")


def _live_agent_smoke(*, runner: Callable[..., Any], home: Path | None) -> tuple[bool, bool]:
    prompt = (
        f"Invoke the MCP tool {LIVE_PROBE_TOOL} exactly once, then reply with "
        f"{LIVE_PROBE_TOKEN}. Do not answer without the tool result."
    )
    result = _run(
        [
            "agy",
            "--agent",
            DISCOVERY_AGENT,
            "--output-format",
            "stream-json",
            "--print",
            "--",
            prompt,
        ],
        runner=runner,
        home=home,
        timeout=30,
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
        mcp_invoked = False
        live_execution = False
        if healthy and hook_registered and "mcpServers" in expected:
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
            "registry_components": sorted(components),
            "expected_components": sorted(expected),
            "evidence": {
                "plugin_list": listed,
                "plugin_validate": validated,
                "agent_discovery": agent_discovered,
                "registry_components": components_current,
                "hook_registration": hook_registered,
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


def install_plugin(
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
        installed = _run(["agy", "plugin", "install", str(plugin)], runner=runner)
        _require_success(installed, "plugin install")
        created = True
    try:
        enabled = _run(["agy", "plugin", "enable", PLUGIN_NAME], runner=runner)
        _require_success(enabled, "plugin enable")
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
        if created:
            _run(["agy", "plugin", "uninstall", PLUGIN_NAME], runner=runner, home=home)
        raise


def _assert_snapshot_safe(root: Path) -> None:
    _assert_nofollow_path(root, label="Antigravity plugin snapshot source")
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            raise AntigravityInstallError("installed Antigravity plugin contains a symlink")
        if not (path.is_dir() or path.is_file()):
            raise AntigravityInstallError("installed Antigravity plugin contains a special file")


def _registry_snapshot(home: Path | None) -> dict[Path, tuple[bool, bytes]]:
    root = config_root(home)
    _assert_nofollow_path(root, label="Antigravity config root")
    snapshots: dict[Path, tuple[bool, bytes]] = {}
    for path in (
        root / "import_manifest.json",
        root / "config.json",
        ownership_receipt_path(home),
    ):
        if path.is_symlink():
            raise AntigravityInstallError("Antigravity registry path is a symlink")
        snapshots[path] = (path.is_file(), path.read_bytes() if path.is_file() else b"")
    return snapshots


def _restore_registry(snapshot: dict[Path, tuple[bool, bytes]]) -> None:
    for path, (present, body) in snapshot.items():
        _assert_nofollow_path(path, label="Antigravity registry restore target")
        ensure_managed_dir(path.parent)
        if not present:
            path.unlink(missing_ok=True)
            continue
        atomic_write_bytes(path, body)


def persist_recovery_snapshot(backup_dir: Path, *, home: Path | None = None) -> dict[str, Any]:
    """Persist the exact machine-global pre-state for crash recovery."""
    ensure_managed_dir(backup_dir)
    root = _assert_nofollow_path(config_root(home), label="Antigravity config root")
    target = _assert_nofollow_path(installed_plugin_path(home), label="Antigravity plugin target")
    plugin_backup = backup_dir / "agy-plugin.prev"
    previous_digest = _package_digest(target)
    if previous_digest is not None:
        _assert_snapshot_safe(target)
        shutil.copytree(target, plugin_backup, copy_function=shutil.copy2)
        _assert_snapshot_safe(plugin_backup)
    registry_rows: list[dict[str, Any]] = []
    for path, (present, body) in _registry_snapshot(home).items():
        name = path.name
        backup_name = f"agy-registry-{name}.bak"
        if present:
            atomic_write_bytes(backup_dir / backup_name, body)
        registry_rows.append({"name": name, "present": present, "backup": backup_name})
    state = {
        "schema": "omg-agy-recovery/v1",
        "config_root": str(root),
        "target": str(target),
        "previous_plugin_present": previous_digest is not None,
        "previous_plugin_digest": previous_digest,
        "previous_registry_identity": plugin_registry_identity(runner=subprocess.run, home=home),
        "registry": registry_rows,
    }
    atomic_write_bytes(
        backup_dir / "current.json",
        json.dumps(state, sort_keys=True).encode("utf-8"),
    )
    return state


def restore_recovery_snapshot(
    backup_dir: Path, *, runner: Callable[..., Any] = subprocess.run
) -> bool:
    try:
        backup_dir = _assert_nofollow_path(backup_dir, label="Antigravity recovery directory")
        raw = json.loads(read_managed_regular_bytes(backup_dir / "current.json").decode("utf-8"))
        root = _assert_nofollow_path(
            Path(str(raw["config_root"])), label="recorded Antigravity config root"
        )
        target = _assert_nofollow_path(
            Path(str(raw["target"])), label="recorded Antigravity plugin target"
        )
        if target != root / "plugins" / PLUGIN_NAME:
            return False
        home = _home_for_config_root(root)
        current = _package_digest(target)
        if current is not None:
            result = _run(["agy", "plugin", "uninstall", PLUGIN_NAME], runner=runner, home=home)
            if int(getattr(result, "returncode", 1)) != 0:
                return False
        if raw.get("previous_plugin_present") is True:
            backup = _assert_nofollow_path(
                backup_dir / "agy-plugin.prev", label="Antigravity plugin backup"
            )
            if _package_digest(backup) != raw.get("previous_plugin_digest"):
                return False
            ensure_managed_dir(target.parent)
            shutil.copytree(backup, target, copy_function=shutil.copy2)
        snapshot: dict[Path, tuple[bool, bytes]] = {}
        for row in raw.get("registry") or []:
            name = str(row.get("name") or "")
            if name not in {"config.json", "import_manifest.json", "omg-ownership.json"}:
                return False
            present = row.get("present") is True
            body = b""
            if present:
                body = read_managed_regular_bytes(backup_dir / str(row.get("backup") or ""))
            snapshot[root / name] = (present, body)
        _restore_registry(snapshot)
        return _package_digest(target) == raw.get("previous_plugin_digest")
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
    runner: Callable[..., Any] = subprocess.run,
    home: Path | None = None,
) -> bool:
    """Lock, revalidate exact tree+registry identity, then invoke host uninstall."""
    with plugin_lock(home):
        if _package_digest(installed_plugin_path(home)) != expected_digest:
            return False
        if plugin_registry_identity(runner=runner, home=home) != expected_registry_identity:
            return False
        result = _run(["agy", "plugin", "uninstall", PLUGIN_NAME], runner=runner, home=home)
        return int(getattr(result, "returncode", 1)) == 0


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
        with plugin_lock(home):
            if _package_digest(destination) != _package_digest(backup):
                raise AntigravityInstallError("Antigravity plugin changed before uninstall")
            if plugin_registry_identity(runner=runner, home=home) != expected_registry:
                raise AntigravityInstallError("Antigravity registry changed before uninstall")
            removed = _run(["agy", "plugin", "uninstall", PLUGIN_NAME], runner=runner, home=home)
            _require_success(removed, "plugin uninstall for registry refresh")
        installed = _run(["agy", "plugin", "install", str(plugin)], runner=runner, home=home)
        _require_success(installed, "plugin reinstall for registry refresh")
        enabled = _run(["agy", "plugin", "enable", PLUGIN_NAME], runner=runner, home=home)
        _require_success(enabled, "plugin enable after registry refresh")
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
        if not restore_recovery_snapshot(recovery_dir, runner=runner):
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
