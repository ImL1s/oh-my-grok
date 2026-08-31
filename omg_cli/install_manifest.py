"""Versioned multi-runtime install manifest (#77 first cut).

Extends ``omg setup`` / ``omg doctor`` — not a separate installer. Default
``--runtime grok --scope project`` preserves today's setup. File copy is never
live verification. Foreign/user-owned files are preserved unless ``--force``.

Never sets ``verified``.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

SCHEMA = "omg-install-manifest/v1"
RUNTIMES = ("grok", "antigravity", "both")
SCOPES = ("project", "user")
OWNERSHIPS = ("OMG-managed", "imported", "user-owned", "foreign")
CLASSES = (
    "missing",
    "exact",
    "stale",
    "user_owned",
    "user_owned_conflict",
    "foreign",
    "malformed",
)
MANAGED_MARKER = "<!-- OMG:MANAGED -->"
OMG_START = "<!-- OMG:START -->"
MAX_BACKUP_BYTES = 1_048_576
STATE_MARKER_TYPE = "state marker"
RUNTIME_ENABLED_TYPES = frozenset(
    {
        "rules",
        "skill",
        "agent",
        "hook",
        "plugin metadata",
        "MCP config",
    }
)
OPTIONAL_ARTIFACT_IDS = frozenset({"user.grok.rules", "user.grok.hook", "user.ag.plugin"})
# Exact id set for each (runtime, scope). Optional machine-scoped grok
# rules/hook rows are extra and are subtracted before this comparison.
EXPECTED_IDS_BY_RUNTIME_SCOPE = MappingProxyType(
    {
        ("grok", "project"): frozenset({"project.agents", "project.gitignore"}),
        ("antigravity", "project"): frozenset({"project.ag.projection", "project.gitignore"}),
        ("both", "project"): frozenset(
            {"project.agents", "project.gitignore", "project.ag.projection"}
        ),
        ("grok", "user"): frozenset({"user.manifest.marker"}),
        ("antigravity", "user"): frozenset({"user.ag.projection", "user.manifest.marker"}),
        ("both", "user"): frozenset({"user.ag.projection", "user.manifest.marker"}),
    }
)


class InstallManifestError(ValueError):
    """Fail-closed manifest / transaction error."""

    def __init__(self, code: str, message: str, *, details: Any | None = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def user_store() -> Path:
    return Path.home() / ".omg-user"


def project_manifest_path(project_root: Path) -> Path:
    return Path(project_root) / ".omg" / "install" / "manifest.json"


def user_manifest_path() -> Path:
    return user_store() / "install-manifest.json"


def classify_bytes(
    *,
    desired: bytes | None,
    actual: bytes | None,
    text: str | None = None,
) -> str:
    """Classify a target file. Never treats copy as live-verified."""
    if actual is None:
        return "missing"
    if desired is not None and actual == desired:
        return "exact"
    sample = text if text is not None else ""
    try:
        sample = actual.decode("utf-8")
    except UnicodeDecodeError:
        return "malformed"
    if OMG_START in sample or MANAGED_MARKER in sample:
        if desired is not None and actual != desired:
            return "stale"
        return "exact" if desired is not None else "stale"
    if sample.strip().startswith("{") or sample.strip().startswith("["):
        try:
            json.loads(sample)
        except json.JSONDecodeError:
            return "malformed"
    return "user_owned"


def classify_path(path: Path, *, desired: bytes | None = None) -> str:
    """Classify a target path. Directories and non-files are foreign, not missing."""
    if path.is_symlink():
        return "foreign"
    if path.exists() and not path.is_file():
        return "foreign"
    if not path.is_file():
        return "missing"
    try:
        actual = path.read_bytes()
    except OSError:
        return "malformed"
    return classify_bytes(desired=desired, actual=actual)


def _is_real_directory(path: Path) -> bool:
    """True when a real directory (not a symlink) occupies the path."""
    return bool(path.exists() and path.is_dir() and not path.is_symlink())


def assert_expected_artifact_ids(
    runtime: str, scope: str, rows: Sequence[Mapping[str, Any]]
) -> None:
    """Fail-closed: desired ids must match the frozen expected set."""
    ids = [str(row.get("id") or "") for row in rows]
    if any(not ident for ident in ids):
        raise InstallManifestError("E_IDS", "artifact row missing id")
    if len(ids) != len(set(ids)):
        raise InstallManifestError("E_IDS", "duplicate artifact ids")
    expected = EXPECTED_IDS_BY_RUNTIME_SCOPE.get((runtime, scope))
    if expected is None:
        raise InstallManifestError(
            "E_IDS", f"no expected artifact ids for runtime={runtime!r} scope={scope!r}"
        )
    core = frozenset(ids) - OPTIONAL_ARTIFACT_IDS
    if core != expected:
        raise InstallManifestError(
            "E_IDS",
            f"desired artifact ids {sorted(core)} != expected {sorted(expected)}",
        )
    extras = frozenset(ids) & OPTIONAL_ARTIFACT_IDS
    allowed_extras: set[str] = set()
    if scope == "project" and runtime in {"grok", "both"}:
        allowed_extras.update({"user.grok.rules", "user.grok.hook"})
    if runtime in {"antigravity", "both"}:
        allowed_extras.add("user.ag.plugin")
    if not extras.issubset(allowed_extras):
        raise InstallManifestError(
            "E_IDS",
            f"optional artifact ids not allowed for runtime={runtime!r} scope={scope!r}",
        )


def _install_root(scope: str, project_root: Path | None) -> Path:
    if scope == "user":
        return user_store()
    if project_root is None:
        raise InstallManifestError("E_SCOPE", "project scope requires a project root")
    return Path(project_root)


def _lexical_under(path: Path, root: Path) -> bool:
    try:
        path.absolute().relative_to(root.absolute())
        return True
    except ValueError:
        return False


def _assert_parents_not_symlink(path: Path, root: Path) -> None:
    """Refuse writes that would follow a symlinked parent under the install root."""
    root_abs = root.absolute()
    path_abs = path.absolute()
    if not _lexical_under(path_abs, root_abs):
        raise InstallManifestError("E_PATH", f"target escapes install root: {path}")
    current = path_abs.parent
    while True:
        if current.is_symlink():
            raise InstallManifestError(
                "E_SYMLINK",
                f"refusing symlinked path component: {current}",
            )
        if current == root_abs:
            return
        parent = current.parent
        if parent == current:
            raise InstallManifestError("E_PATH", f"target escapes install root: {path}")
        current = parent


def _machine_grok_home() -> Path:
    from omg_cli.hook_install import grok_home as hook_grok_home

    return hook_grok_home()


def _machine_antigravity_config() -> Path:
    from omg_cli.antigravity_install import config_root

    return config_root()


def _containment_roots(
    *,
    scope: str,
    project_root: Path | None,
    runtime: str,
) -> tuple[Path, ...]:
    roots = [_install_root(scope, project_root)]
    if scope == "project" and runtime in {"grok", "both"}:
        roots.append(_machine_grok_home())
    if runtime in {"antigravity", "both"}:
        roots.append(_machine_antigravity_config())
    return tuple(roots)


def _root_for_path(path: Path, roots: tuple[Path, ...]) -> Path | None:
    for root in roots:
        if _lexical_under(path, root):
            return root
    return None


def _hook_companion_paths(json_path: Path) -> tuple[Path, Path]:
    from omg_cli.hook_install import STANDALONE_BASENAME, WRAPPER_BASENAME

    parent = json_path.parent
    return (parent / STANDALONE_BASENAME, parent / WRAPPER_BASENAME)


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _fsync_dir(path: Path) -> None:
    """POSIX directory fsync; Windows may deny opening a directory fd."""
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


def _write_text_nofollow(path: Path, text: str) -> None:
    """Write a regular file atomically. Never follow a symlink to an outside path."""
    if path.is_symlink() or (os.path.lexists(path) and not path.is_file()):
        raise InstallManifestError("E_SYMLINK", f"unsafe managed write target: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        _fsync_dir(path.parent)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _discard_backup(backup_dir: Path, ident: str) -> None:
    """Drop a backup so rollback cannot republish that target."""

    prev = backup_dir / f"{ident}.prev.json"
    bak = backup_dir / f"{ident}.bak"
    meta = bak.with_suffix(".json")
    prev.unlink(missing_ok=True)
    bak.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)


def _backup_existing(backup_dir: Path, ident: str, target: Path) -> None:
    prev = backup_dir / f"{ident}.prev.json"
    if target.is_symlink():
        _write_text_nofollow(
            prev,
            json.dumps(
                {
                    "target": str(target),
                    "kind": "symlink",
                    "link": os.readlink(target),
                    "prior_mode": target.lstat().st_mode & 0o777,
                }
            ),
        )
        return
    if target.is_file():
        data = target.read_bytes()
        if len(data) > MAX_BACKUP_BYTES:
            raise InstallManifestError(
                "E_BACKUP",
                f"refusing to overwrite {target}; file exceeds backup limit",
            )
        bak = backup_dir / f"{ident}.bak"
        digest = _sha256_bytes(data)
        _publish_intended_file(bak, data)
        if _sha256_bytes(bak.read_bytes()) != digest:
            raise InstallManifestError(
                "E_BACKUP",
                f"backup digest mismatch for {target}",
            )
        _write_text_nofollow(bak.with_suffix(".json"), json.dumps({"target": str(target)}))
        _write_text_nofollow(
            prev,
            json.dumps(
                {
                    "target": str(target),
                    "kind": "file",
                    "backup": str(bak),
                    "prior_sha256": digest,
                    "prior_mode": target.stat().st_mode & 0o777,
                }
            ),
        )
        return
    _write_text_nofollow(prev, json.dumps({"target": str(target), "kind": "created"}))


def _seal_backup_post_state(backup_dir: Path, ident: str, target: Path) -> None:
    """Seal the exact transaction-owned state required for rollback CAS."""
    prev = backup_dir / f"{ident}.prev.json"
    if not prev.is_file() or prev.is_symlink():
        raise InstallManifestError("E_BACKUP", f"missing recovery metadata for {ident}")
    try:
        meta = json.loads(prev.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallManifestError("E_BACKUP", f"malformed recovery metadata for {ident}") from exc
    expected_kind = meta.get("post_kind")
    if expected_kind is not None:
        actual_matches = False
        if expected_kind == "absent":
            actual_matches = not os.path.lexists(target)
        elif expected_kind == "symlink":
            actual_matches = target.is_symlink() and os.readlink(target) == meta.get(
                "post_link"
            )
        elif expected_kind == "file":
            actual_matches = bool(
                target.is_file()
                and not target.is_symlink()
                and _sha256_bytes(target.read_bytes()) == meta.get("post_sha256")
                and (target.stat().st_mode & 0o777) == meta.get("post_mode")
            )
        if not actual_matches:
            raise InstallManifestError("E_BACKUP", f"intended post-state mismatch for {target}")
        return
    if not os.path.lexists(target):
        meta["post_kind"] = "absent"
    elif target.is_symlink():
        meta["post_kind"] = "symlink"
        meta["post_link"] = os.readlink(target)
    elif target.is_file():
        data = target.read_bytes()
        meta["post_kind"] = "file"
        meta["post_sha256"] = _sha256_bytes(data)
        meta["post_mode"] = target.stat().st_mode & 0o777
    else:
        raise InstallManifestError("E_BACKUP", f"unsafe post-state for {target}")
    _write_text_nofollow(prev, json.dumps(meta, sort_keys=True))


def _seal_intended_file(
    backup_dir: Path, ident: str, *, body: bytes, mode: int = 0o644
) -> None:
    """Persist exact intended bytes before publishing a managed file."""
    prev = backup_dir / f"{ident}.prev.json"
    try:
        meta = json.loads(prev.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallManifestError("E_BACKUP", f"malformed recovery metadata for {ident}") from exc
    meta.update(
        {
            "post_kind": "file",
            "post_sha256": _sha256_bytes(body),
            "post_mode": mode,
        }
    )
    _write_text_nofollow(prev, json.dumps(meta, sort_keys=True))


def _publish_intended_file(path: Path, body: bytes, *, mode: int = 0o644) -> None:
    """Publish already-journaled bytes without following a target symlink."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temporary, flags, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _publish_intended_symlink(path: Path, link: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp-link")
    try:
        temporary.symlink_to(link)
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _seal_observed_identity(row: dict[str, Any], target: Path) -> None:
    """Record on-disk bytes for mergeable artifacts (e.g. AGENTS.md)."""
    if row.get("content_hash"):
        return
    if target.is_symlink() or not target.is_file():
        return
    try:
        data = target.read_bytes()
        sample = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return
    if OMG_START in sample or MANAGED_MARKER in sample:
        row["content_hash"] = _sha256_bytes(data)
        row["classification"] = "exact"


def _seal_written_identity(row: dict[str, Any], target: Path) -> None:
    """Record the bytes we just merged/installed (gitignore has no OMG:START)."""
    if target.is_symlink() or not target.is_file():
        return
    try:
        data = target.read_bytes()
    except OSError:
        return
    row["content_hash"] = _sha256_bytes(data)
    row["classification"] = "exact"


def desired_artifacts(
    *,
    runtime: str,
    scope: str,
    project_root: Path | None,
    plugin: Path | None = None,
    install_rules: bool = False,
    install_hook: bool = False,
    install_antigravity: bool = False,
) -> list[dict[str, Any]]:
    if runtime not in RUNTIMES:
        raise InstallManifestError("E_RUNTIME", f"runtime must be one of {RUNTIMES}")
    if scope not in SCOPES:
        raise InstallManifestError("E_SCOPE", f"scope must be one of {SCOPES}")
    plugin = plugin or plugin_root()
    rows: list[dict[str, Any]] = []
    runtimes = ("grok", "antigravity") if runtime == "both" else (runtime,)
    if scope == "project":
        if project_root is None:
            raise InstallManifestError("E_SCOPE", "project scope requires a project root")
        base = Path(project_root).resolve()
        if "grok" in runtimes:
            rows.append(
                {
                    "id": "project.agents",
                    "runtime": "grok",
                    "scope": "project",
                    "type": "rules",
                    "target": str(base / "AGENTS.md"),
                    "ownership": "OMG-managed",
                    "enabled": True,
                    "mergeable": True,
                }
            )
        if "antigravity" in runtimes:
            dest = base / ".omg" / "projections" / "antigravity" / "README.md"
            rows.append(
                {
                    "id": "project.ag.projection",
                    "runtime": "antigravity",
                    "scope": "project",
                    "type": "plugin metadata",
                    "target": str(dest),
                    "ownership": "OMG-managed",
                    "enabled": True,
                    "mergeable": False,
                    "note": "static projection copy; not live AG evidence",
                }
            )
        rows.append(
            {
                "id": "project.gitignore",
                "runtime": runtime,
                "scope": "project",
                "type": "wrapper",
                "target": str(base / ".gitignore"),
                "ownership": "OMG-managed",
                "enabled": True,
                "mergeable": True,
                "note": "generic .omg gitignore init; not live runtime evidence",
            }
        )
        if "grok" in runtimes:
            machine_home = _machine_grok_home()
            if install_rules:
                from omg_cli.guidance import rules_file_path

                rows.append(
                    {
                        "id": "user.grok.rules",
                        "runtime": "grok",
                        "scope": "user",
                        "type": "rules",
                        "target": str(rules_file_path(home=machine_home)),
                        "ownership": "OMG-managed",
                        "enabled": True,
                        "mergeable": True,
                        "machine_scoped": True,
                        "note": "user-machine grok rules; not live verification",
                    }
                )
            if install_hook:
                from omg_cli.hook_install import HOOK_JSON_NAME

                hook_json = machine_home / "hooks" / HOOK_JSON_NAME
                rows.append(
                    {
                        "id": "user.grok.hook",
                        "runtime": "grok",
                        "scope": "user",
                        "type": "hook",
                        "target": str(hook_json),
                        "ownership": "OMG-managed",
                        "enabled": True,
                        "mergeable": True,
                        "machine_scoped": True,
                        "note": "user-machine grok hook; not live verification",
                    }
                )
    else:
        dest = user_store() / "projections" / "antigravity" / "README.md"
        if "antigravity" in runtimes:
            rows.append(
                {
                    "id": "user.ag.projection",
                    "runtime": "antigravity",
                    "scope": "user",
                    "type": "plugin metadata",
                    "target": str(dest),
                    "ownership": "OMG-managed",
                    "enabled": True,
                    "mergeable": False,
                    "note": "user-scope projection; does not create a project .omg",
                }
            )
        rows.append(
            {
                "id": "user.manifest.marker",
                "runtime": "grok" if "grok" in runtimes else runtime,
                "scope": "user",
                "type": STATE_MARKER_TYPE,
                "target": str(user_store() / "README.md"),
                "ownership": "OMG-managed",
                "enabled": True,
                "mergeable": False,
            }
        )
    if install_antigravity and "antigravity" in runtimes:
        from omg_cli.antigravity_install import installed_plugin_path

        rows.append(
            {
                "id": "user.ag.plugin",
                "runtime": "antigravity",
                "scope": "user",
                "type": "plugin metadata",
                "target": str(installed_plugin_path()),
                # The host plugin is machine-global. Project manifests observe
                # it but cannot independently claim uninstall ownership.
                "ownership": "OMG-managed" if scope == "user" else "imported",
                "enabled": True,
                "mergeable": False,
                "machine_scoped": True,
                "external_cli_managed": True,
                "note": "installed and discovered through the official agy plugin CLI",
            }
        )
    assert_expected_artifact_ids(runtime, scope, rows)
    for row in rows:
        target = Path(row["target"])
        body = _desired_body(row, plugin=plugin)
        row["content_hash"] = _sha256_bytes(body) if body is not None else None
        row["classification"] = classify_path(target, desired=body)
    return rows


def _desired_body(row: Mapping[str, Any], *, plugin: Path) -> bytes | None:
    ident = row["id"]
    if ident in {
        "project.agents",
        "project.gitignore",
        "user.grok.rules",
        "user.grok.hook",
    }:
        return None  # merge/install inside the transaction
    if ident.endswith("ag.projection"):
        src = plugin / "docs" / "parity" / "projections" / "antigravity"
        banner = (
            f"{MANAGED_MARKER}\n"
            "# OMG Antigravity projection (install first cut)\n\n"
            "This directory is **OMG-managed**. It is not proof that `agy` loaded "
            "the plugin. File copy is not live verification.\n"
        )
        if (src / "README.md").is_file():
            extra = (src / "README.md").read_text(encoding="utf-8")
            return (banner + "\n" + extra).encode("utf-8")
        return banner.encode("utf-8")
    if ident == "user.manifest.marker":
        return (
            f"{MANAGED_MARKER}\n"
            "# oh-my-grok user-scope install\n\n"
            "This store is user-scope. It does not create a project `.omg`.\n"
        ).encode("utf-8")
    return None


def _writable_restore_paths(
    *,
    runtime: str,
    scope: str,
    project_root: Path | None,
    plugin: Path | None = None,
) -> set[Path]:
    """Paths this transaction may create or overwrite, including mergeable files."""
    plugin = plugin or plugin_root()
    allowed: set[Path] = set()
    for row in desired_artifacts(
        runtime=runtime,
        scope=scope,
        project_root=project_root,
        plugin=plugin,
        install_rules=True,
        install_hook=True,
    ):
        target = Path(row["target"]).absolute()
        allowed.add(target)
        if row["id"] == "user.grok.hook":
            py_path, wrapper_path = _hook_companion_paths(target)
            allowed.add(py_path.absolute())
            allowed.add(wrapper_path.absolute())
        if row["id"] == "user.grok.rules":
            allowed.add(target.with_suffix(".md.bak").absolute())
    if scope == "user":
        allowed.add(user_manifest_path().absolute())
    elif project_root is not None:
        allowed.add(project_manifest_path(project_root).absolute())
    return allowed


def build_manifest(
    *,
    runtime: str,
    scope: str,
    project_root: Path | None,
    transaction_id: str,
    plugin: Path | None = None,
    source_version: str | None = None,
    install_rules: bool = False,
    install_hook: bool = False,
    install_antigravity: bool = False,
) -> dict[str, Any]:
    artifacts = desired_artifacts(
        runtime=runtime,
        scope=scope,
        project_root=project_root,
        plugin=plugin,
        install_rules=install_rules,
        install_hook=install_hook,
        install_antigravity=install_antigravity,
    )
    return {
        "schema": SCHEMA,
        "kind": "omg_install_manifest",
        "runtime": runtime,
        "scope": scope,
        "transaction_id": transaction_id,
        "source_version": source_version,
        "source_commit": None,
        "created_at": _utc_now(),
        "verified": False,
        "observed": False,
        "healthy": False,
        "note": (
            "File copy is not live verification. Antigravity truth comes from "
            "fresh agy validate/list/agent probes. Foreign/user-owned files are preserved."
        ),
        "artifacts": artifacts,
    }


def _tx_dir(scope: str, project_root: Path | None) -> Path:
    if scope == "user":
        return user_store() / "tx"
    assert project_root is not None
    return Path(project_root) / ".omg" / "install" / "tx"


def rollback_interrupted(
    scope: str,
    project_root: Path | None,
    *,
    fallback: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tx_root = _tx_dir(scope, project_root)
    marker = tx_root / "current.json"
    if marker.is_symlink():
        return {
            "ok": False,
            "rolled_back": False,
            "recoverable": True,
            "note": "symlinked tx marker preserved for manual recovery",
        }
    elif not marker.is_file():
        if fallback is None:
            return {"ok": True, "rolled_back": False}
        state = fallback
    else:
        try:
            state = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "ok": False,
                "rolled_back": False,
                "recoverable": True,
                "note": "malformed tx marker preserved for manual recovery",
            }
    if state.get("status") == "committed":
        return {"ok": True, "rolled_back": False}
    tx_id = str(state.get("transaction_id") or "")
    runtime = str(state.get("runtime") or "")
    backups = Path(state.get("backup_dir") or "")
    expected = (tx_root / tx_id).absolute() if tx_id else None
    if (
        not tx_id
        or runtime not in RUNTIMES
        or expected is None
        or backups.is_symlink()
        or backups.absolute() != expected
        or not backups.is_dir()
    ):
        return {
            "ok": False,
            "rolled_back": False,
            "recoverable": True,
            "note": "rejected tx marker preserved for manual recovery",
        }
    if state.get("agy_recovery_snapshot") is True:
        from omg_cli.antigravity_install import restore_recovery_snapshot

        if runtime not in {"antigravity", "both"} or not restore_recovery_snapshot(backups):
            return {
                "ok": False,
                "rolled_back": False,
                "recoverable": True,
                "note": "agy plugin durable rollback failed; transaction marker preserved",
            }
    try:
        roots = _containment_roots(scope=scope, project_root=project_root, runtime=runtime)
    except InstallManifestError:
        return {
            "ok": False,
            "rolled_back": False,
            "recoverable": True,
            "note": "tx marker root rejected and preserved",
        }
    allowed_targets = _writable_restore_paths(
        runtime=runtime,
        scope=scope,
        project_root=project_root,
    )
    restored: list[str] = []
    created_removed: list[str] = []
    recovery_rows: list[tuple[Path, str, bytes | str | None, int | None, bool]] = []
    prev_paths = list(backups.glob("*.prev.json"))
    orphan_backups = [
        backup
        for backup in backups.glob("*.bak")
        if not backup.name.startswith("agy-registry-")
        if not backup.with_suffix(".prev.json").is_file()
    ]
    if orphan_backups:
        return {
            "ok": False,
            "rolled_back": False,
            "recoverable": True,
            "note": "incomplete tx backup set preserved for manual recovery",
        }
    for prev in prev_paths:
        try:
            meta = json.loads(prev.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "ok": False,
                "rolled_back": False,
                "recoverable": True,
                "note": "malformed tx backup metadata preserved for manual recovery",
            }
        target = Path(meta.get("target") or "")
        contain = _root_for_path(target, roots)
        if contain is None or target.absolute() not in allowed_targets:
            return {
                "ok": False,
                "rolled_back": False,
                "recoverable": True,
                "note": "out-of-root tx backup preserved for manual recovery",
            }
        try:
            _assert_parents_not_symlink(target, contain)
        except InstallManifestError:
            return {
                "ok": False,
                "rolled_back": False,
                "recoverable": True,
                "note": "unsafe tx restore target preserved for manual recovery",
            }
        kind = meta.get("kind")
        post_kind = meta.get("post_kind")
        if post_kind == "absent":
            post_matches = not os.path.lexists(target)
        elif post_kind == "symlink":
            post_matches = target.is_symlink() and os.readlink(target) == meta.get("post_link")
        elif post_kind == "file":
            post_matches = bool(
                target.is_file()
                and not target.is_symlink()
                and _sha256_bytes(target.read_bytes()) == meta.get("post_sha256")
                and (target.stat().st_mode & 0o777) == meta.get("post_mode")
            )
        else:
            post_matches = False
        if kind == "created":
            prior_matches = not os.path.lexists(target)
            if not (post_matches or prior_matches):
                return {
                    "ok": False,
                    "rolled_back": False,
                    "recoverable": True,
                    "note": "occupied created target preserved; ownership is unproven",
                }
            recovery_rows.append((target, kind, None, None, post_matches))
        elif kind == "symlink":
            link = meta.get("link")
            if not isinstance(link, str) or not link:
                return {
                    "ok": False,
                    "rolled_back": False,
                    "recoverable": True,
                    "note": "invalid symlink backup preserved for manual recovery",
                }
            prior_matches = target.is_symlink() and os.readlink(target) == link
            if not (post_matches or prior_matches):
                return {
                    "ok": False, "rolled_back": False, "recoverable": True,
                    "note": "tx-owned post-state drifted; marker preserved",
                }
            recovery_rows.append((target, kind, link, None, post_matches))
        elif kind == "file":
            bak = Path(meta.get("backup") or "")
            if (
                bak.is_symlink()
                or not bak.is_file()
                or not _lexical_under(bak, backups)
            ):
                return {
                    "ok": False,
                    "rolled_back": False,
                    "recoverable": True,
                    "note": "missing or unsafe tx file backup preserved for manual recovery",
                }
            prior_bytes = bak.read_bytes()
            if (
                _sha256_bytes(prior_bytes) != meta.get("prior_sha256")
                or not isinstance(meta.get("prior_mode"), int)
            ):
                return {
                    "ok": False,
                    "rolled_back": False,
                    "recoverable": True,
                    "note": "tx file backup identity mismatch; marker preserved",
                }
            file_prior_mode = int(meta["prior_mode"])
            prior_matches = bool(
                target.is_file()
                and not target.is_symlink()
                and target.read_bytes() == prior_bytes
                and target.stat().st_mode & 0o777 == file_prior_mode
            )
            if not (post_matches or prior_matches):
                return {
                    "ok": False, "rolled_back": False, "recoverable": True,
                    "note": "tx-owned post-state drifted; marker preserved",
                }
            recovery_rows.append((target, kind, prior_bytes, file_prior_mode, post_matches))
        else:
            return {
                "ok": False,
                "rolled_back": False,
                "recoverable": True,
                "note": "unknown tx backup kind preserved for manual recovery",
            }
    for target, kind, prior, prior_mode, restore_needed in recovery_rows:
        if not restore_needed:
            continue
        if kind == "created":
            target.unlink(missing_ok=True)
            if target.parent.is_dir():
                _fsync_dir(target.parent)
            created_removed.append(str(target))
        elif kind == "symlink":
            _publish_intended_symlink(target, str(prior))
            restored.append(str(target))
        else:
            assert isinstance(prior, bytes)
            assert isinstance(prior_mode, int)
            _publish_intended_file(target, prior, mode=prior_mode)
            restored.append(str(target))
    for target, kind, prior, prior_mode, _restore_needed in recovery_rows:
        if kind == "created" and os.path.lexists(target):
            return {"ok": False, "rolled_back": False, "recoverable": True,
                    "note": "tx rollback readback failed; marker preserved"}
        if kind == "symlink" and (not target.is_symlink() or os.readlink(target) != prior):
            return {"ok": False, "rolled_back": False, "recoverable": True,
                    "note": "tx rollback readback failed; marker preserved"}
        if kind == "file" and (
            not target.is_file()
            or target.read_bytes() != prior
            or target.stat().st_mode & 0o777 != prior_mode
        ):
            return {"ok": False, "rolled_back": False, "recoverable": True,
                    "note": "tx rollback readback failed; marker preserved"}
    marker.unlink(missing_ok=True)
    if marker.parent.is_dir():
        _fsync_dir(marker.parent)
    return {
        "ok": True,
        "rolled_back": True,
        "restored": restored,
        "removed": created_removed,
    }


def _ensure_project_omg_dirs(root: Path) -> None:
    """Create the project ``.omg`` layout. POSIX stays confined; Windows falls back."""
    from omg_cli.contracts.path_keys import ContractPathError
    from omg_cli.state import OMG_PROJECT_SUBDIRS, OMG_RUN_STATE_SUBDIRS, ensure_omg_dirs

    try:
        ensure_omg_dirs(root)
        return
    except ContractPathError as exc:
        if os.name == "posix":
            raise InstallManifestError(
                "E_PATH", f"cannot create confined .omg layout: {exc}"
            ) from exc
    for sub in (*OMG_PROJECT_SUBDIRS, *OMG_RUN_STATE_SUBDIRS):
        _mkdir(Path(root) / ".omg" / sub)


def _merge_kind(row: Mapping[str, Any]) -> str | None:
    ident = str(row.get("id") or "")
    if ident == "project.agents":
        return "agents"
    if ident == "project.gitignore":
        return "gitignore"
    if ident == "user.grok.rules":
        return "rules"
    if ident == "user.grok.hook":
        return "hook"
    return None


def _skip_foreign_or_malformed(
    row: dict[str, Any],
    target: Path,
    klass: str,
    skipped: list[dict[str, str]],
) -> None:
    skipped.append({"target": str(target), "class": klass})
    row["content_hash"] = None
    row["enabled"] = False
    if klass in {"user_owned", "user_owned_conflict", "foreign"}:
        row["ownership"] = "user-owned" if str(klass).startswith("user") else "foreign"
    row["classification"] = klass


def _apply_merge_or_install(
    *,
    kind: str,
    row: dict[str, Any],
    target: Path,
    project_root: Path | None,
    backup_dir: Path,
) -> str:
    """Backup, mutate, seal. Returns a short action label for CLI output."""
    ident = str(row["id"])
    _mkdir(target.parent)
    if kind == "hook":
        py_path, wrapper_path = _hook_companion_paths(target)
        _backup_existing(backup_dir, ident, target)
        _backup_existing(backup_dir, f"{ident}.py", py_path)
        _backup_existing(backup_dir, f"{ident}.wrapper", wrapper_path)
    else:
        _backup_existing(backup_dir, ident, target)

    def seal_expected(path: Path, row_id: str, body: bytes, mode: int | None = None) -> None:
        _seal_intended_file(
            backup_dir,
            row_id,
            body=body,
            mode=mode
            if mode is not None
            else ((path.stat().st_mode & 0o777) if path.is_file() and not path.is_symlink() else 0o644),
        )

    precomputed_action: str | None = None
    if kind in {"agents", "gitignore"}:
        from omg_cli import setup_fragments

        existing = (
            target.read_text(encoding="utf-8")
            if target.is_file() and not target.is_symlink()
            else ""
        )
        if kind == "agents":
            fragment = setup_fragments._read_template("AGENTS.fragment.md").rstrip() + "\n"
            if setup_fragments.OMG_START not in fragment:
                fragment = (
                    f"{setup_fragments.OMG_START}\n{fragment}{setup_fragments.OMG_END}\n"
                )
            elif setup_fragments.OMG_END not in fragment:
                fragment = fragment.rstrip() + f"\n{setup_fragments.OMG_END}\n"
            intended = existing
            if setup_fragments.OMG_START not in existing:
                sep = "" if existing.endswith("\n") or not existing else "\n"
                intended = existing + sep + ("\n" if existing else "") + fragment
                precomputed_action = "appended" if existing else "created"
            else:
                precomputed_action = "unchanged"
        else:
            fragment = setup_fragments._read_template("gitignore.fragment").rstrip() + "\n"
            block = (
                fragment
                if setup_fragments.GITIGNORE_MARKER in fragment
                else f"{setup_fragments.GITIGNORE_MARKER}\n{fragment}"
            )
            key_lines = [
                line.strip()
                for line in fragment.splitlines()
                if line.strip() and not line.strip().startswith("#")
            ]
            intended = existing
            if setup_fragments.GITIGNORE_MARKER not in existing and not (
                key_lines
                and all(
                    any(key in line for line in existing.splitlines()) for key in key_lines
                )
            ):
                sep = "" if existing.endswith("\n") or not existing else "\n"
                intended = existing + sep + ("\n" if existing else "") + block
                precomputed_action = "appended" if existing else "created"
            else:
                precomputed_action = "unchanged"
        seal_expected(target, ident, intended.encode("utf-8"))
        _publish_intended_file(target, intended.encode("utf-8"))
    elif kind == "rules":
        from omg_cli.guidance import reconcile_rules_text, render_managed_block

        existing = (
            target.read_text(encoding="utf-8")
            if target.is_file() and not target.is_symlink()
            else ""
        )
        intended, action = reconcile_rules_text(existing, render_managed_block())
        precomputed_action = action
        seal_expected(target, ident, intended.encode("utf-8"), 0o600)
        _publish_intended_file(target, intended.encode("utf-8"), mode=0o600)
        bak_sidecar = target.with_suffix(".md.bak")
        _backup_existing(backup_dir, f"{ident}.md.bak", bak_sidecar)
        if action == "unchanged":
            bak_body = bak_sidecar.read_bytes() if bak_sidecar.is_file() else b""
            if bak_sidecar.is_file():
                seal_expected(bak_sidecar, f"{ident}.md.bak", bak_body)
        elif existing:
            seal_expected(bak_sidecar, f"{ident}.md.bak", existing.encode("utf-8"))
            _publish_intended_file(bak_sidecar, existing.encode("utf-8"))
    elif kind == "hook":
        from omg_cli.hook_install import (
            committed_standalone,
            install_global_hook as live_hook_installer,
            python3_executable,
            render_hook_json,
            render_wrapper,
        )

        source = committed_standalone()
        if source.is_file() and live_hook_installer.__module__ == "omg_cli.hook_install":
            source_body = source.read_bytes()
            wrapper_body = render_wrapper(
                py_path, python3=python3_executable()
            ).encode("utf-8")
            json_body = render_hook_json(py_path).encode("utf-8")
            seal_expected(py_path, f"{ident}.py", source_body, 0o755)
            seal_expected(
                wrapper_path,
                f"{ident}.wrapper",
                wrapper_body,
                0o755,
            )
            seal_expected(target, ident, json_body, 0o644)
            for candidate, body, mode in (
                (py_path, source_body, 0o755),
                (wrapper_path, wrapper_body, 0o755),
                (target, json_body, 0o644),
            ):
                if candidate.is_symlink():
                    _publish_intended_file(candidate, body, mode=mode)

    if kind == "agents":
        if project_root is None:
            raise InstallManifestError("E_SCOPE", "project.agents requires a project root")
        action = str(precomputed_action)
        label = f"AGENTS.md: {action}"
    elif kind == "gitignore":
        if project_root is None:
            raise InstallManifestError("E_SCOPE", "project.gitignore requires a project root")
        action = str(precomputed_action)
        label = f".gitignore: {action}"
    elif kind == "rules":
        bak_sidecar = target.with_suffix(".md.bak")
        label = f"{target}: {precomputed_action}"
    elif kind == "hook":
        from omg_cli.hook_install import install_global_hook

        hpath, haction = install_global_hook(home=_machine_grok_home())
        if haction.startswith("failed") or haction in {
            "quarantined-no-source",
            "skipped-no-source",
        }:
            # Quarantine (and the except-path `failed:*` after a successful
            # rename) must not be undone by rolling the backup onto grok's
            # `*.json` discovery path. Discard whenever the installer left
            # the active JSON gone, not only when the action text says
            # "quarantined".
            if "quarantined" in haction or not os.path.lexists(target):
                _discard_backup(backup_dir, ident)
            if (backup_dir / f"{ident}.prev.json").is_file():
                _seal_backup_post_state(backup_dir, ident, target)
            _seal_backup_post_state(backup_dir, f"{ident}.py", py_path)
            _seal_backup_post_state(backup_dir, f"{ident}.wrapper", wrapper_path)
            raise InstallManifestError("E_TX", f"global hook install failed: {haction}")
        label = f"{hpath}: {haction}"
    else:
        raise InstallManifestError("E_TX", f"unknown merge kind {kind!r}")
    _seal_backup_post_state(backup_dir, ident, target)
    if kind == "hook":
        py_path, wrapper_path = _hook_companion_paths(target)
        _seal_backup_post_state(backup_dir, f"{ident}.py", py_path)
        _seal_backup_post_state(backup_dir, f"{ident}.wrapper", wrapper_path)
    elif kind == "rules":
        _seal_backup_post_state(backup_dir, f"{ident}.md.bak", target.with_suffix(".md.bak"))
    _seal_written_identity(row, target)
    row["action"] = label
    return label


def _apply_manifest_locked(
    manifest: dict[str, Any],
    *,
    project_root: Path | None,
    force: bool = False,
    plugin: Path | None = None,
) -> dict[str, Any]:
    """Write OMG-managed artifacts with backup/rollback. Preserve foreign files."""
    plugin = plugin or plugin_root()
    scope = manifest["scope"]
    runtime = manifest["runtime"]
    tx_id = manifest["transaction_id"]
    install_root = _install_root(scope, project_root)
    roots = _containment_roots(scope=scope, project_root=project_root, runtime=runtime)
    recovery = rollback_interrupted(scope, project_root)
    if recovery.get("ok") is False:
        raise InstallManifestError(
            "E_RECOVERY", str(recovery.get("note") or "interrupted recovery failed")
        )
    previous_ag_digest: str | None = None
    from omg_cli.antigravity_install import AntigravityInstallError, load_ownership_receipt

    try:
        ownership_receipt = load_ownership_receipt()
    except AntigravityInstallError as exc:
        raise InstallManifestError("E_TX", str(exc)) from exc
    if ownership_receipt is not None:
        previous_ag_digest = str(ownership_receipt["plugin_digest"])
    tx_root = _tx_dir(scope, project_root)
    backup_dir = tx_root / tx_id
    _assert_parents_not_symlink(backup_dir, install_root)
    _mkdir(backup_dir)
    marker = tx_root / "current.json"
    _assert_parents_not_symlink(marker, install_root)
    _write_text_nofollow(
        marker,
        json.dumps(
            {
                "status": "committing",
                "transaction_id": tx_id,
                "backup_dir": str(backup_dir),
                "runtime": runtime,
                "scope": scope,
            }
        ),
    )
    written: list[str] = []
    skipped: list[dict[str, str]] = []
    actions: list[str] = []
    agy_plugin_created = False
    antigravity_evidence: dict[str, Any] | None = None
    try:
        if scope == "project" and project_root is not None:
            _ensure_project_omg_dirs(Path(project_root))
        for row in manifest["artifacts"]:
            target = Path(row["target"])
            if row.get("external_cli_managed") is True:
                from omg_cli.antigravity_install import (
                    AntigravityInstallError,
                    install_plugin,
                    package_digest,
                )

                source_digest = package_digest(plugin)
                if source_digest is None:
                    raise InstallManifestError("E_TX", "invalid Antigravity package identity")
                # Durable pre-state lives inside this transaction. It records
                # the original canonical config/target, so HOME changes do not
                # redirect crash recovery.
                def mark_agy_snapshot_durable() -> None:
                    _write_text_nofollow(
                        marker,
                        json.dumps(
                            {
                                "status": "committing",
                                "transaction_id": tx_id,
                                "backup_dir": str(backup_dir),
                                "runtime": runtime,
                                "scope": scope,
                                "agy_recovery_snapshot": True,
                            }
                        ),
                    )

                try:
                    evidence = install_plugin(
                        plugin,
                        force=force,
                        owned_previous_digest=previous_ag_digest,
                        recovery_dir=backup_dir,
                        snapshot_callback=mark_agy_snapshot_durable,
                        ownership_reference=str(
                            (
                                user_manifest_path()
                                if scope == "user"
                                else project_manifest_path(Path(project_root))  # type: ignore[arg-type]
                            ).absolute()
                        ),
                    )
                except AntigravityInstallError as exc:
                    code = "E_CONFLICT" if "existing Antigravity" in str(exc) else "E_TX"
                    raise InstallManifestError(code, str(exc)) from exc
                agy_plugin_created = bool(evidence.get("created"))
                antigravity_evidence = evidence
                registry_identity = str(evidence["registry_identity"])
                mcp_identity = str(evidence["mcp_registry_identity"])
                row["content_hash"] = evidence.get("content_hash")
                row["registry_identity"] = registry_identity
                row["mcp_registry_identity"] = mcp_identity
                row["classification"] = "exact"
                row["observed"] = bool(evidence.get("observed"))
                row["healthy"] = bool(evidence.get("healthy"))
                row["live_verified"] = bool(evidence.get("live_verified"))
                row["action"] = "agy plugin: installed and discovered"
                actions.append(str(row["action"]))
                if agy_plugin_created:
                    written.append(str(target))
                continue
            body = _desired_body(row, plugin=plugin)
            klass = classify_path(target, desired=body)
            row["classification"] = klass
            contain = _root_for_path(target, roots)
            if contain is None:
                raise InstallManifestError("E_PATH", f"target escapes install roots: {target}")
            merge_kind = _merge_kind(row)
            if _is_real_directory(target):
                if not force:
                    _skip_foreign_or_malformed(row, target, "foreign", skipped)
                    continue
                raise InstallManifestError(
                    "E_TX",
                    f"refusing to write onto directory occupying managed path: {target}",
                )
            if merge_kind is not None:
                if klass == "foreign" and not force and merge_kind != "hook":
                    _skip_foreign_or_malformed(row, target, klass, skipped)
                    continue
                if klass == "malformed" and not force and merge_kind != "hook":
                    _skip_foreign_or_malformed(row, target, klass, skipped)
                    continue
                _assert_parents_not_symlink(target, contain)
                label = _apply_merge_or_install(
                    kind=merge_kind,
                    row=row,
                    target=target,
                    project_root=project_root,
                    backup_dir=backup_dir,
                )
                actions.append(label)
                if not str(label).endswith(": unchanged"):
                    written.append(str(target))
                continue
            if body is None:
                _seal_observed_identity(row, target)
                continue
            if klass in {"user_owned", "user_owned_conflict", "foreign"} and not force:
                _skip_foreign_or_malformed(row, target, klass, skipped)
                continue
            if klass == "malformed" and not force:
                _skip_foreign_or_malformed(row, target, klass, skipped)
                continue
            if _is_real_directory(target):
                raise InstallManifestError(
                    "E_TX",
                    f"refusing to write onto directory occupying managed path: {target}",
                )
            _assert_parents_not_symlink(target, contain)
            _mkdir(target.parent)
            _backup_existing(backup_dir, str(row["id"]), target)
            _seal_intended_file(backup_dir, str(row["id"]), body=body)
            _publish_intended_file(target, body)
            _seal_backup_post_state(backup_dir, str(row["id"]), target)
            written.append(str(target))
            actions.append(f"{row['id']}: written")
        dest = (
            user_manifest_path() if scope == "user" else project_manifest_path(Path(project_root))  # type: ignore[arg-type]
        )
        manifest["runtime_evidence"] = (
            {"antigravity": antigravity_evidence} if antigravity_evidence is not None else {}
        )
        manifest["observed"] = bool(
            runtime in {"antigravity", "both"}
            and antigravity_evidence
            and antigravity_evidence.get("observed")
        )
        manifest["healthy"] = bool(
            runtime in {"antigravity", "both"}
            and antigravity_evidence
            and antigravity_evidence.get("healthy")
        )
        manifest["verified"] = bool(
            runtime in {"antigravity", "both"}
            and antigravity_evidence
            and antigravity_evidence.get("verified")
        )
        manifest["live_verified"] = bool(
            runtime == "antigravity"
            and antigravity_evidence
            and antigravity_evidence.get("live_verified")
        )
        _assert_parents_not_symlink(dest, install_root)
        _mkdir(dest.parent)
        _backup_existing(backup_dir, "omg.install.manifest", dest)
        manifest_body = (json.dumps(manifest, indent=2, ensure_ascii=False) + "\n").encode(
            "utf-8"
        )
        _seal_intended_file(backup_dir, "omg.install.manifest", body=manifest_body)
        _publish_intended_file(dest, manifest_body)
        _seal_backup_post_state(backup_dir, "omg.install.manifest", dest)
        _write_text_nofollow(
            marker,
            json.dumps({"status": "committed", "transaction_id": tx_id}),
        )
        return {
            "ok": True,
            "verified": bool(
                runtime in {"antigravity", "both"}
                and antigravity_evidence
                and antigravity_evidence.get("verified")
            ),
            "observed": bool(
                runtime in {"antigravity", "both"}
                and antigravity_evidence
                and antigravity_evidence.get("observed")
            ),
            "healthy": bool(
                runtime in {"antigravity", "both"}
                and antigravity_evidence
                and antigravity_evidence.get("healthy")
            ),
            "live_verified": bool(
                runtime == "antigravity"
                and antigravity_evidence
                and antigravity_evidence.get("live_verified")
            ),
            "runtime_evidence": (
                {"antigravity": antigravity_evidence} if antigravity_evidence is not None else {}
            ),
            "runtime": runtime,
            "scope": scope,
            "transaction_id": tx_id,
            "written": written,
            "skipped": skipped,
            "actions": actions,
            "manifest": str(dest),
            "note": (
                "manifest written; Antigravity was live-discovered through agy"
                if antigravity_evidence is not None
                else "manifest written; not live-verified"
            ),
        }
    except Exception as exc:
        rollback = rollback_interrupted(
            scope,
            project_root,
            fallback={
                "status": "committing",
                "transaction_id": tx_id,
                "backup_dir": str(backup_dir),
                "runtime": runtime,
                "scope": scope,
            },
        )
        if rollback.get("ok") is not True:
            raise InstallManifestError(
                "E_RECOVERY",
                str(rollback.get("note") or "install rollback was not proven"),
            ) from exc
        if isinstance(exc, InstallManifestError) and exc.code in {
            "E_TX",
            "E_PATH",
            "E_CONFLICT",
            "E_RECOVERY",
        }:
            raise
        raise InstallManifestError(
            "E_TX", f"install transaction rolled back ({type(exc).__name__})"
        ) from exc


def apply_manifest(
    manifest: dict[str, Any],
    *,
    project_root: Path | None,
    force: bool = False,
    plugin: Path | None = None,
) -> dict[str, Any]:
    """Serialize marker, backups, runtime mutation, and rollback machine-globally."""
    from omg_cli.contracts.path_keys import ensure_managed_dir, exclusive_lock

    lock_root = user_store()
    ensure_managed_dir(lock_root)
    with exclusive_lock(lock_root / ".install-manifest.lock"):
        grok_uninstall_journal = _machine_grok_home() / "omg" / "uninstall-current.json"
        if os.path.lexists(grok_uninstall_journal):
            _assert_parents_not_symlink(grok_uninstall_journal, _machine_grok_home())
            raise InstallManifestError(
                "E_RECOVERY",
                "unfinished Grok uninstall transaction must be recovered before install",
            )
        return _apply_manifest_locked(
            manifest,
            project_root=project_root,
            force=force,
            plugin=plugin,
        )


def inspect_install_manifest(
    *,
    project_root: Path | None,
    scope: str = "project",
) -> dict[str, Any]:
    path = (
        user_manifest_path()
        if scope == "user"
        else (project_manifest_path(project_root) if project_root is not None else None)
    )
    if path is None:
        return {
            "ok": True,
            "configured": False,
            "installed": False,
            "enabled": False,
            "loadable": False,
            "observed": False,
            "healthy": False,
            "verified": False,
            "note": "no install manifest yet",
        }
    if path.is_symlink():
        return {
            "ok": False,
            "configured": True,
            "installed": False,
            "enabled": False,
            "loadable": False,
            "observed": False,
            "healthy": False,
            "verified": False,
            "drift": [{"id": "manifest", "class": "foreign", "target": str(path)}],
            "error": "manifest path is a symlink",
        }
    if not path.is_file():
        return {
            "ok": True,
            "configured": False,
            "installed": False,
            "enabled": False,
            "loadable": False,
            "observed": False,
            "healthy": False,
            "verified": False,
            "note": "no install manifest yet",
        }
    inspect_root = (
        user_store()
        if scope == "user"
        else (Path(project_root) if project_root is not None else None)
    )
    if inspect_root is not None:
        try:
            _assert_parents_not_symlink(path, inspect_root)
        except InstallManifestError as exc:
            return {
                "ok": False,
                "configured": True,
                "installed": False,
                "enabled": False,
                "loadable": False,
                "observed": False,
                "healthy": False,
                "verified": False,
                "drift": [{"id": "manifest", "class": "foreign", "target": str(path)}],
                "error": str(exc),
            }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "ok": False,
            "configured": True,
            "installed": False,
            "verified": False,
            "error": f"malformed manifest: {exc}",
        }
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != SCHEMA
        or raw.get("kind") != "omg_install_manifest"
        or raw.get("scope") not in SCOPES
        or not isinstance(raw.get("artifacts"), list)
    ):
        return {
            "ok": False,
            "configured": True,
            "installed": False,
            "verified": False,
            "error": "malformed manifest: required schema/kind/scope/artifacts missing",
        }
    rows = [
        row
        for row in raw.get("artifacts") or []
        if isinstance(row, dict) and row.get("id") and row.get("target")
    ]
    if not rows:
        return {
            "ok": False,
            "configured": True,
            "installed": False,
            "verified": False,
            "error": "malformed manifest: no artifact rows",
        }
    drift = []
    plugin = plugin_root()
    klasses: dict[str, str] = {}
    antigravity_evidence: dict[str, Any] | None = None
    inspect_runtime = str(raw.get("runtime") or "")
    try:
        inspect_roots = _containment_roots(
            scope=scope,
            project_root=project_root if scope == "project" else None,
            runtime=inspect_runtime if inspect_runtime in RUNTIMES else "antigravity",
        )
    except InstallManifestError:
        inspect_roots = (inspect_root,) if inspect_root is not None else tuple()
    for row in rows:
        if row.get("enabled") is False:
            continue
        target = Path(row.get("target") or "")
        claimed = row.get("content_hash")
        ident = str(row.get("id") or "")
        if row.get("external_cli_managed") is True:
            from omg_cli.antigravity_install import load_ownership_receipt, probe_plugin

            antigravity_evidence = probe_plugin(plugin=plugin)
            ownership = load_ownership_receipt()
            ownership_references = (
                ownership.get("references", []) if isinstance(ownership, dict) else []
            )
            actual = antigravity_evidence.get("plugin_digest")
            registry_identity = row.get("registry_identity")
            mcp_registry_identity = row.get("mcp_registry_identity")
            if (
                antigravity_evidence.get("healthy")
                and isinstance(claimed, str)
                and claimed
                and actual == claimed
                and isinstance(registry_identity, str)
                and registry_identity
                and antigravity_evidence.get("registry_identity") == registry_identity
                and isinstance(mcp_registry_identity, str)
                and mcp_registry_identity
                and antigravity_evidence.get("mcp_registry_identity")
                == mcp_registry_identity
                and isinstance(ownership, dict)
                and ownership.get("plugin_digest") == claimed
                and ownership.get("registry_identity") == registry_identity
                and ownership.get("mcp_registry_identity") == mcp_registry_identity
                and str(path.absolute()) in ownership_references
            ):
                klasses[ident] = "exact"
            else:
                klass = "stale" if actual else "missing"
                klasses[ident] = klass
                drift.append({"id": ident, "class": klass, "target": str(target)})
            continue
        contain = _root_for_path(target, inspect_roots)
        if contain is None:
            klasses[ident] = "foreign"
            drift.append({"id": ident, "class": "foreign", "target": str(target)})
            continue
        try:
            _assert_parents_not_symlink(target, contain)
        except InstallManifestError:
            klasses[ident] = "foreign"
            drift.append({"id": ident, "class": "foreign", "target": str(target)})
            continue
        if target.is_symlink() or _is_real_directory(target):
            klass = "foreign"
        elif isinstance(claimed, str) and claimed:
            if not target.is_file():
                klass = "missing"
            else:
                try:
                    digest = _sha256_bytes(target.read_bytes())
                except OSError:
                    klass = "malformed"
                else:
                    klass = "exact" if digest == claimed else "stale"
        else:
            body = _desired_body(row, plugin=plugin)
            klass = classify_path(target, desired=body)
        klasses[ident] = klass
        if klass in {"stale", "missing", "malformed", "foreign"}:
            drift.append({"id": ident, "class": klass, "target": str(target)})
    enabled_rows = [row for row in rows if row.get("enabled") is not False]
    enabled_markers = [
        str(row["id"]) for row in enabled_rows if row.get("type") == STATE_MARKER_TYPE
    ]
    enabled_runtime = [
        str(row["id"]) for row in enabled_rows if row.get("type") != STATE_MARKER_TYPE
    ]
    runtime_exact = False
    for row in enabled_rows:
        if row.get("type") not in RUNTIME_ENABLED_TYPES:
            continue
        if klasses.get(str(row.get("id") or "")) == "exact":
            runtime_exact = True
            break
    runtime_name = str(raw.get("runtime") or "")
    ag_enabled = bool(antigravity_evidence and antigravity_evidence.get("enabled"))
    if runtime_name in {"antigravity", "both"} and antigravity_evidence is not None:
        enabled_value = bool(not drift and ag_enabled and (runtime_exact if runtime_name == "both" else True))
        observed_value = bool(not drift and antigravity_evidence.get("observed"))
        healthy_value = bool(not drift and antigravity_evidence.get("healthy"))
        verified_value = bool(not drift and antigravity_evidence.get("verified"))
        live_verified_value = bool(
            runtime_name == "antigravity"
            and not drift
            and antigravity_evidence.get("live_verified")
        )
    else:
        enabled_value = bool(runtime_exact)
        observed_value = False
        healthy_value = False
        verified_value = False
        live_verified_value = False
    return {
        "ok": not drift,
        "configured": True,
        "installed": bool(enabled_rows),
        "enabled": enabled_value,
        "enabled_runtime": enabled_runtime,
        "enabled_markers": enabled_markers,
        "loadable": observed_value if antigravity_evidence is not None else runtime_exact,
        "observed": observed_value,
        "healthy": healthy_value,
        "verified": verified_value,
        "live_verified": live_verified_value,
        "runtime_evidence": (
            {"antigravity": antigravity_evidence} if antigravity_evidence is not None else {}
        ),
        "runtime": raw.get("runtime"),
        "scope": raw.get("scope"),
        "transaction_id": raw.get("transaction_id"),
        "drift": drift,
        "note": raw.get("note"),
    }


def refuse_home_project(root: Path, *, here: bool, home: Path | None = None) -> None:
    if here:
        return
    try:
        home_path = home if home is not None else Path.home()
        if root.resolve() == home_path.resolve():
            raise InstallManifestError(
                "E_SETUP_HOME",
                "refusing to create project .omg in $HOME; use --scope user or --here",
            )
    except RuntimeError:
        return


def classify_auth(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Never false-green. Invalid/placeholder keys stay failures. No raw secrets."""
    e = env if env is not None else os.environ
    raw = ""
    for key in ("GROK_API_KEY", "XAI_API_KEY", "OMG_GROK_API_KEY"):
        val = str(e.get(key, "")).strip()
        if val:
            raw = val
            break
    if not raw:
        return {
            "ok": False,
            "state": "missing",
            "note": "no API key configured; cannot be healthy",
        }
    lowered = raw.lower()
    if lowered in {"invalid", "changeme", "test", "none", "null"} or "fake" in lowered:
        return {
            "ok": False,
            "state": "invalid",
            "note": "placeholder/invalid API key cannot false-green",
        }
    return {
        "ok": False,
        "state": "configured_unproven",
        "note": "key present but not live-verified; auth cannot be healthy from file copy",
    }


def load_manifest(
    *,
    project_root: Path | None,
    scope: str = "project",
    strict: bool = False,
) -> dict[str, Any] | None:
    """Load a stored install manifest. Missing → None. Malformed → None or raise."""
    if scope == "user":
        path = user_manifest_path()
        inspect_root = user_store()
    elif project_root is None:
        if strict:
            raise InstallManifestError("E_SCOPE", "project scope requires a project root")
        return None
    else:
        path = project_manifest_path(Path(project_root))
        inspect_root = Path(project_root)
    if path.is_symlink():
        if strict:
            raise InstallManifestError("E_SYMLINK", f"manifest path is a symlink: {path}")
        return None
    if not path.is_file():
        return None
    try:
        _assert_parents_not_symlink(path, inspect_root)
    except InstallManifestError:
        if strict:
            raise
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if strict:
            raise InstallManifestError("E_MALFORMED", f"malformed install manifest: {exc}") from exc
        return None
    if (
        not isinstance(raw, dict)
        or raw.get("schema") != SCHEMA
        or raw.get("kind") != "omg_install_manifest"
        or raw.get("scope") not in SCOPES
        or not isinstance(raw.get("artifacts"), list)
    ):
        if strict:
            raise InstallManifestError(
                "E_MALFORMED",
                "malformed install manifest: required schema/kind/scope/artifacts missing",
            )
        return None
    return raw


def persist_manifest(
    document: dict[str, Any],
    *,
    project_root: Path | None,
    scope: str,
) -> Path:
    """Write an install manifest. Honesty flags stay false. Never follows symlinks."""
    if scope not in SCOPES:
        raise InstallManifestError("E_SCOPE", f"scope must be one of {SCOPES}")
    payload = dict(document)
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise InstallManifestError("E_MALFORMED", "manifest artifacts must be a list")
    payload["schema"] = SCHEMA
    payload["kind"] = "omg_install_manifest"
    payload["scope"] = scope
    payload["verified"] = False
    payload["observed"] = False
    payload["healthy"] = False
    payload["live_verified"] = False
    if not payload.get("transaction_id"):
        payload["transaction_id"] = uuid.uuid4().hex
    if not payload.get("created_at"):
        payload["created_at"] = _utc_now()
    payload["updated_at"] = _utc_now()
    dest = (
        user_manifest_path() if scope == "user" else project_manifest_path(Path(project_root))  # type: ignore[arg-type]
    )
    install_root = _install_root(scope, project_root)
    _assert_parents_not_symlink(dest, install_root)
    _mkdir(dest.parent)
    _write_text_nofollow(
        dest,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
    )
    return dest


def upsert_manifest_artifacts(
    document: dict[str, Any],
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge artifact rows by id. Never replace an existing user-owned row."""
    payload = dict(document)
    by_id: dict[str, dict[str, Any]] = {}
    existing = payload.get("artifacts") or []
    if not isinstance(existing, list):
        existing = []
    for row in existing:
        if isinstance(row, dict) and row.get("id"):
            by_id[str(row["id"])] = dict(row)
    for row in rows:
        if not isinstance(row, dict) or not row.get("id"):
            continue
        ident = str(row["id"])
        prior = by_id.get(ident)
        if prior is not None and prior.get("ownership") == "user-owned":
            continue
        by_id[ident] = dict(row)
    payload["artifacts"] = list(by_id.values())
    payload["verified"] = False
    payload["observed"] = False
    payload["healthy"] = False
    payload["live_verified"] = False
    return payload


def path_is_under(path: Path, root: Path) -> bool:
    """Lexical containment (no symlink resolve)."""
    return _lexical_under(path, root)


def run_scoped_setup(
    *,
    runtime: str = "grok",
    scope: str = "project",
    project_root: Path | None = None,
    here: bool = False,
    force: bool = False,
    source_version: str | None = None,
    plugin: Path | None = None,
    install_rules: bool = False,
    install_hook: bool = False,
    install_antigravity: bool = False,
) -> dict[str, Any]:
    if scope == "project":
        if project_root is None:
            raise InstallManifestError("E_SCOPE", "project scope requires a project root")
        refuse_home_project(Path(project_root), here=here)
    if scope != "project" or runtime not in {"grok", "both"}:
        install_rules = False
        install_hook = False
    tx_id = uuid.uuid4().hex
    manifest = build_manifest(
        runtime=runtime,
        scope=scope,
        project_root=project_root,
        transaction_id=tx_id,
        plugin=plugin,
        source_version=source_version,
        install_rules=install_rules,
        install_hook=install_hook,
        install_antigravity=install_antigravity,
    )
    return apply_manifest(manifest, project_root=project_root, force=force, plugin=plugin)


__all__ = [
    "CLASSES",
    "EXPECTED_IDS_BY_RUNTIME_SCOPE",
    "OPTIONAL_ARTIFACT_IDS",
    "InstallManifestError",
    "SCHEMA",
    "apply_manifest",
    "assert_expected_artifact_ids",
    "build_manifest",
    "classify_auth",
    "classify_bytes",
    "classify_path",
    "desired_artifacts",
    "inspect_install_manifest",
    "load_manifest",
    "path_is_under",
    "persist_manifest",
    "project_manifest_path",
    "refuse_home_project",
    "rollback_interrupted",
    "run_scoped_setup",
    "upsert_manifest_artifacts",
    "user_manifest_path",
    "user_store",
]
