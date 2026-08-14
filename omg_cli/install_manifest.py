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
    if path.is_symlink():
        return "foreign"
    if not path.is_file():
        return "missing"
    try:
        actual = path.read_bytes()
    except OSError:
        return "malformed"
    return classify_bytes(desired=desired, actual=actual)


def _mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _write_text_nofollow(path: Path, text: str) -> None:
    """Write a regular file. Never follow a symlink to an outside path."""
    if path.is_symlink():
        path.unlink()
    path.write_text(text, encoding="utf-8")


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
        if len(data) <= MAX_BACKUP_BYTES:
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


def desired_artifacts(
    *,
    runtime: str,
    scope: str,
    project_root: Path | None,
    plugin: Path | None = None,
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
                "type": "state marker",
                "target": str(user_store() / "README.md"),
                "ownership": "OMG-managed",
                "enabled": True,
                "mergeable": False,
            }
        )
    for row in rows:
        target = Path(row["target"])
        body = _desired_body(row, plugin=plugin)
        row["content_hash"] = _sha256_bytes(body) if body is not None else None
        row["classification"] = classify_path(target, desired=body)
    return rows


def _desired_body(row: Mapping[str, Any], *, plugin: Path) -> bytes | None:
    ident = row["id"]
    if ident == "project.agents":
        return None  # mergeable; existing setup_cmd owns the bytes
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


def build_manifest(
    *,
    runtime: str,
    scope: str,
    project_root: Path | None,
    transaction_id: str,
    plugin: Path | None = None,
    source_version: str | None = None,
) -> dict[str, Any]:
    artifacts = desired_artifacts(
        runtime=runtime,
        scope=scope,
        project_root=project_root,
        plugin=plugin,
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


def rollback_interrupted(scope: str, project_root: Path | None) -> dict[str, Any]:
    tx_root = _tx_dir(scope, project_root)
    marker = tx_root / "current.json"
    if not marker.is_file():
        return {"ok": True, "rolled_back": False}
    try:
        state = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        marker.unlink(missing_ok=True)
        return {"ok": True, "rolled_back": False, "note": "malformed tx marker removed"}
    if state.get("status") == "committed":
        return {"ok": True, "rolled_back": False}
    backups = Path(state.get("backup_dir") or "")
    restored = []
    created_removed = []
    if backups.is_dir():
        for prev in backups.glob("*.prev.json"):
            try:
                meta = json.loads(prev.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            target = Path(meta.get("target") or "")
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
                if bak.is_file():
                    target.write_bytes(bak.read_bytes())
                    restored.append(str(target))
        for backup in backups.glob("*.bak"):
            meta_path = backup.with_suffix(".json")
            if not meta_path.is_file():
                continue
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            target = Path(meta["target"])
            if any(row == str(target) for row in restored):
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
    rollback_interrupted(scope, project_root)
    tx_root = _tx_dir(scope, project_root)
    backup_dir = tx_root / tx_id
    _mkdir(backup_dir)
    marker = tx_root / "current.json"
    _write_text_nofollow(
        marker,
        json.dumps(
            {
                "status": "committing",
                "transaction_id": tx_id,
                "backup_dir": str(backup_dir),
            }
        ),
    )
    written: list[str] = []
    skipped: list[dict[str, str]] = []
    try:
        for row in manifest["artifacts"]:
            target = Path(row["target"])
            body = _desired_body(row, plugin=plugin)
            if body is None:
                _seal_observed_identity(row, target)
                continue
            klass = classify_path(target, desired=body)
            row["classification"] = klass
            if klass in {"user_owned", "user_owned_conflict", "foreign"} and not force:
                skipped.append({"target": str(target), "class": klass})
                continue
            if klass == "malformed" and not force:
                skipped.append({"target": str(target), "class": klass})
                continue
            _mkdir(target.parent)
            _backup_existing(backup_dir, str(row["id"]), target)
            target.write_bytes(body)
            written.append(str(target))
        dest = (
            user_manifest_path()
            if scope == "user"
            else project_manifest_path(Path(project_root))  # type: ignore[arg-type]
        )
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
            "manifest": str(dest),
            "note": "manifest written; not live-verified; not agy discovery",
        }
    except Exception as exc:
        rollback_interrupted(scope, project_root)
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
    drift = []
    plugin = plugin_root()
    for row in raw.get("artifacts") or []:
        if not isinstance(row, dict):
            continue
        target = Path(row.get("target") or "")
        claimed = row.get("content_hash")
        if target.is_symlink():
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
        if klass in {"stale", "missing", "malformed", "foreign"}:
            drift.append({"id": row.get("id"), "class": klass, "target": str(target)})
    return {
        "ok": not drift,
        "configured": True,
        "installed": True,
        "enabled": True,
        "loadable": True,
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
) -> dict[str, Any]:
    if scope == "project":
        if project_root is None:
            raise InstallManifestError("E_SCOPE", "project scope requires a project root")
        refuse_home_project(Path(project_root), here=here)
    tx_id = uuid.uuid4().hex
    manifest = build_manifest(
        runtime=runtime,
        scope=scope,
        project_root=project_root,
        transaction_id=tx_id,
        plugin=plugin,
        source_version=source_version,
    )
    return apply_manifest(manifest, project_root=project_root, force=force, plugin=plugin)


__all__ = [
    "CLASSES",
    "InstallManifestError",
    "SCHEMA",
    "apply_manifest",
    "build_manifest",
    "classify_auth",
    "classify_bytes",
    "classify_path",
    "inspect_install_manifest",
    "refuse_home_project",
    "rollback_interrupted",
    "run_scoped_setup",
]
