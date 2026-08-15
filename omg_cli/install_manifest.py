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
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

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
OPTIONAL_ARTIFACT_IDS = frozenset({"user.grok.rules", "user.grok.hook"})
# Exact id set for each (runtime, scope). Optional machine-scoped grok
# rules/hook rows are extra and are subtracted before this comparison.
EXPECTED_IDS_BY_RUNTIME_SCOPE = MappingProxyType(
    {
        ("grok", "project"): frozenset({"project.agents", "project.gitignore"}),
        ("antigravity", "project"): frozenset(
            {"project.ag.projection", "project.gitignore"}
        ),
        ("both", "project"): frozenset(
            {"project.agents", "project.gitignore", "project.ag.projection"}
        ),
        ("grok", "user"): frozenset({"user.manifest.marker"}),
        ("antigravity", "user"): frozenset(
            {"user.ag.projection", "user.manifest.marker"}
        ),
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
    runtime: str, scope: str, rows: list[Mapping[str, Any]]
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
    if extras and not (scope == "project" and runtime in {"grok", "both"}):
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


def _containment_roots(
    *,
    scope: str,
    project_root: Path | None,
    runtime: str,
) -> tuple[Path, ...]:
    roots = [_install_root(scope, project_root)]
    if scope == "project" and runtime in {"grok", "both"}:
        roots.append(_machine_grok_home())
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


def _write_text_nofollow(path: Path, text: str) -> None:
    """Write a regular file atomically. Never follow a symlink to an outside path."""
    if path.is_symlink():
        path.unlink()
    tmp = path.with_name(path.name + ".tmp")
    if tmp.is_symlink() or tmp.is_file():
        tmp.unlink()
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


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
                }
            ),
        )
        target.unlink()
        return
    if target.is_file():
        data = target.read_bytes()
        if len(data) > MAX_BACKUP_BYTES:
            raise InstallManifestError(
                "E_BACKUP",
                f"refusing to overwrite {target}; file exceeds backup limit",
            )
        bak = backup_dir / f"{ident}.bak"
        bak.write_bytes(data)
        _write_text_nofollow(
            bak.with_suffix(".json"), json.dumps({"target": str(target)})
        )
        _write_text_nofollow(
            prev,
            json.dumps(
                {
                    "target": str(target),
                    "kind": "file",
                    "backup": str(bak),
                }
            ),
        )
        return
    _write_text_nofollow(
        prev, json.dumps({"target": str(target), "kind": "created"})
    )


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
) -> dict[str, Any]:
    artifacts = desired_artifacts(
        runtime=runtime,
        scope=scope,
        project_root=project_root,
        plugin=plugin,
        install_rules=install_rules,
        install_hook=install_hook,
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
            "File copy is not live verification. Antigravity projections are not "
            "an installed agy plugin. Foreign/user-owned files are preserved."
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
        marker.unlink(missing_ok=True)
        if fallback is None:
            return {"ok": True, "rolled_back": False, "note": "symlinked tx marker removed"}
        state = fallback
    elif not marker.is_file():
        if fallback is None:
            return {"ok": True, "rolled_back": False}
        state = fallback
    else:
        try:
            state = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if fallback is None:
                marker.unlink(missing_ok=True)
                return {"ok": True, "rolled_back": False, "note": "malformed tx marker removed"}
            state = fallback
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
        marker.unlink(missing_ok=True)
        return {
            "ok": True,
            "rolled_back": False,
            "note": "tx marker backup_dir rejected",
        }
    try:
        roots = _containment_roots(
            scope=scope, project_root=project_root, runtime=runtime
        )
    except InstallManifestError:
        marker.unlink(missing_ok=True)
        return {"ok": True, "rolled_back": False, "note": "tx marker root rejected"}
    allowed_targets = _writable_restore_paths(
        runtime=runtime,
        scope=scope,
        project_root=project_root,
    )
    restored = []
    created_removed = []
    for prev in backups.glob("*.prev.json"):
        try:
            meta = json.loads(prev.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        target = Path(meta.get("target") or "")
        contain = _root_for_path(target, roots)
        if contain is None:
            continue
        if target.absolute() not in allowed_targets:
            continue
        try:
            _assert_parents_not_symlink(target, contain)
        except InstallManifestError:
            continue
        kind = meta.get("kind")
        if kind == "created":
            if target.is_symlink() or target.is_file():
                target.unlink(missing_ok=True)
            created_removed.append(str(target))
        elif kind == "symlink":
            target.unlink(missing_ok=True)
            link = meta.get("link")
            if isinstance(link, str) and link:
                target.symlink_to(link)
            restored.append(str(target))
        elif kind == "file":
            bak = Path(meta.get("backup") or "")
            if (
                target.is_symlink()
                or bak.is_symlink()
                or not bak.is_file()
                or not _lexical_under(bak, backups)
            ):
                continue
            target.write_bytes(bak.read_bytes())
            restored.append(str(target))
    for backup in backups.glob("*.bak"):
        if backup.is_symlink() or not _lexical_under(backup, backups):
            continue
        meta_path = backup.with_suffix(".json")
        if not meta_path.is_file() or meta_path.is_symlink():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        target = Path(meta.get("target") or "")
        contain = _root_for_path(target, roots)
        if contain is None:
            continue
        if target.absolute() not in allowed_targets:
            continue
        try:
            _assert_parents_not_symlink(target, contain)
        except InstallManifestError:
            continue
        if any(row == str(target) for row in restored):
            continue
        if target.is_symlink():
            continue
        target.write_bytes(backup.read_bytes())
        restored.append(str(target))
    marker.unlink(missing_ok=True)
    return {
        "ok": True,
        "rolled_back": True,
        "restored": restored,
        "removed": created_removed,
    }


def _ensure_project_omg_dirs(root: Path) -> None:
    """Create the project ``.omg`` layout. POSIX uses confined mkdir; Windows falls back."""
    from omg_cli.contracts.path_keys import ContractPathError
    from omg_cli.state import OMG_PROJECT_SUBDIRS, OMG_RUN_STATE_SUBDIRS, ensure_omg_dirs

    try:
        ensure_omg_dirs(root)
        return
    except ContractPathError:
        pass
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

    if kind == "agents":
        from omg_cli.setup_fragments import merge_agents_fragment

        if project_root is None:
            raise InstallManifestError("E_SCOPE", "project.agents requires a project root")
        action = merge_agents_fragment(Path(project_root))
        label = f"AGENTS.md: {action}"
    elif kind == "gitignore":
        from omg_cli.setup_fragments import merge_gitignore_fragment

        if project_root is None:
            raise InstallManifestError(
                "E_SCOPE", "project.gitignore requires a project root"
            )
        action = merge_gitignore_fragment(Path(project_root))
        label = f".gitignore: {action}"
    elif kind == "rules":
        from omg_cli.guidance import GuidanceError, install_global_rules

        bak_sidecar = target.with_suffix(".md.bak")
        _backup_existing(backup_dir, f"{ident}.md.bak", bak_sidecar)
        try:
            rpath, raction = install_global_rules(home=_machine_grok_home())
        except GuidanceError as exc:
            raise InstallManifestError("E_TX", f"global rules install failed: {exc}") from exc
        label = f"{rpath}: {raction}"
    elif kind == "hook":
        from omg_cli.hook_install import install_global_hook

        hpath, haction = install_global_hook(home=_machine_grok_home())
        if haction.startswith("failed") or haction in {
            "quarantined-no-source",
            "skipped-no-source",
        }:
            raise InstallManifestError("E_TX", f"global hook install failed: {haction}")
        label = f"{hpath}: {haction}"
    else:
        raise InstallManifestError("E_TX", f"unknown merge kind {kind!r}")
    _seal_written_identity(row, target)
    row["action"] = label
    return label


def apply_manifest(
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
    roots = _containment_roots(
        scope=scope, project_root=project_root, runtime=runtime
    )
    rollback_interrupted(scope, project_root)
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
    try:
        if scope == "project" and project_root is not None:
            _ensure_project_omg_dirs(Path(project_root))
        for row in manifest["artifacts"]:
            target = Path(row["target"])
            body = _desired_body(row, plugin=plugin)
            klass = classify_path(target, desired=body)
            row["classification"] = klass
            contain = _root_for_path(target, roots)
            if contain is None:
                raise InstallManifestError(
                    "E_PATH", f"target escapes install roots: {target}"
                )
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
                if klass == "foreign" and not force:
                    _skip_foreign_or_malformed(row, target, klass, skipped)
                    continue
                if klass == "malformed" and not force:
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
            target.write_bytes(body)
            written.append(str(target))
            actions.append(f"{row['id']}: written")
        dest = (
            user_manifest_path()
            if scope == "user"
            else project_manifest_path(Path(project_root))  # type: ignore[arg-type]
        )
        _assert_parents_not_symlink(dest, install_root)
        _mkdir(dest.parent)
        _backup_existing(backup_dir, "omg.install.manifest", dest)
        _write_text_nofollow(
            dest,
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        )
        _write_text_nofollow(
            marker,
            json.dumps({"status": "committed", "transaction_id": tx_id}),
        )
        return {
            "ok": True,
            "verified": False,
            "observed": False,
            "healthy": False,
            "runtime": runtime,
            "scope": scope,
            "transaction_id": tx_id,
            "written": written,
            "skipped": skipped,
            "actions": actions,
            "manifest": str(dest),
            "note": "manifest written; not live-verified; not agy discovery",
        }
    except Exception as exc:
        rollback_interrupted(
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
        if isinstance(exc, InstallManifestError) and exc.code == "E_TX":
            raise
        raise InstallManifestError(
            "E_TX", f"install transaction rolled back ({type(exc).__name__})"
        ) from exc


def inspect_install_manifest(
    *,
    project_root: Path | None,
    scope: str = "project",
) -> dict[str, Any]:
    path = user_manifest_path() if scope == "user" else (
        project_manifest_path(project_root) if project_root is not None else None
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
            "drift": [
                {"id": "manifest", "class": "foreign", "target": str(path)}
            ],
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
    inspect_root = user_store() if scope == "user" else (
        Path(project_root) if project_root is not None else None
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
                "drift": [
                    {"id": "manifest", "class": "foreign", "target": str(path)}
                ],
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
        contain = _root_for_path(target, inspect_roots)
        if contain is None:
            klasses[ident] = "foreign"
            drift.append(
                {"id": row.get("id"), "class": "foreign", "target": str(target)}
            )
            continue
        try:
            _assert_parents_not_symlink(target, contain)
        except InstallManifestError:
            klasses[ident] = "foreign"
            drift.append(
                {"id": row.get("id"), "class": "foreign", "target": str(target)}
            )
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
            drift.append({"id": row.get("id"), "class": klass, "target": str(target)})
    enabled_rows = [row for row in rows if row.get("enabled") is not False]
    enabled_markers = [
        str(row["id"])
        for row in enabled_rows
        if row.get("type") == STATE_MARKER_TYPE
    ]
    enabled_runtime = [
        str(row["id"])
        for row in enabled_rows
        if row.get("type") != STATE_MARKER_TYPE
    ]
    runtime_exact = False
    for row in enabled_rows:
        if row.get("type") not in RUNTIME_ENABLED_TYPES:
            continue
        if klasses.get(str(row.get("id") or "")) == "exact":
            runtime_exact = True
            break
    return {
        "ok": not drift,
        "configured": True,
        "installed": bool(enabled_rows),
        "enabled": runtime_exact,
        "enabled_runtime": enabled_runtime,
        "enabled_markers": enabled_markers,
        "loadable": runtime_exact,
        "observed": False,
        "healthy": False,
        "verified": False,
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
    "refuse_home_project",
    "rollback_interrupted",
    "run_scoped_setup",
]
