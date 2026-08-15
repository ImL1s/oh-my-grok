"""Read-only Antigravity (AG) history importer stub (#74).

Never mutates AG-owned files. Never fabricates a live import. Absence pins
``unsupported``; an unknown schema pins ``unknown_version``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


AG_HISTORY_SCHEMA = 1
SUPPORTED_AG_HISTORY_VERSIONS: frozenset[str] = frozenset()

_PROJECT_MARKERS = (
    ".antigravity",
    ".agent",
    ".gemini",
    "antigravity-history",
)
_VERSION_FILES = (
    "version",
    "VERSION",
    "history-version.json",
    "manifest.json",
    "package.json",
)


class AgHistoryError(ValueError):
    """AG history probe failed closed without mutating anything."""

    def __init__(self, message: str, *, code: str = "E_AG_HISTORY") -> None:
        super().__init__(message)
        self.code = code


def _existing_dir(path: Path) -> Path | None:
    try:
        if path.is_symlink():
            return None
        if path.is_dir():
            return path
    except OSError:
        return None
    return None


def _candidate_dirs(project_root: Path, *, home: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()

    def _add(raw: Path) -> None:
        existing = _existing_dir(raw)
        if existing is None:
            return
        try:
            key = existing.resolve()
        except OSError:
            return
        if key in seen:
            return
        seen.add(key)
        found.append(existing)

    for name in _PROJECT_MARKERS:
        _add(project_root / name)
    env_dir = (
        os.environ.get("ANTIGRAVITY_HISTORY") or os.environ.get("AG_HISTORY_DIR") or ""
    ).strip()
    if env_dir:
        _add(Path(env_dir).expanduser())
    # Home profile dirs are not scanned unless pointed at via env — a project
    # CLI must not inspect AG-owned files outside the project by default.
    _ = home
    return found


def _read_version_label(directory: Path) -> str | None:
    for name in _VERSION_FILES:
        path = directory / name
        try:
            if not path.is_file() or path.is_symlink():
                continue
            if path.stat().st_size > 65_536:
                return "oversized"
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        if name.endswith(".json"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                return "unparseable"
            if isinstance(parsed, dict):
                for key in ("history_version", "schema_version", "version"):
                    value = parsed.get(key)
                    if value is not None:
                        return str(value)
        return text.splitlines()[0][:128]
    return None


def inspect_ag_history(
    project_root: Path | str,
    *,
    home: Path | str | None = None,
) -> dict[str, Any]:
    """Probe AG history locations without reading conversation bodies."""

    root = Path(project_root)
    home_path = Path(home) if home is not None else Path.home()
    locations = _candidate_dirs(root, home=home_path)
    if not locations:
        return {
            "schema_version": AG_HISTORY_SCHEMA,
            "present": False,
            "supported": False,
            "imported": False,
            "mutated": False,
            "pin": "unsupported",
            "reason": "ag_history_absent",
            "locations": [],
            "live_import": False,
        }

    versions: list[str] = []
    public_locations: list[dict[str, Any]] = []
    for directory in locations:
        label = _read_version_label(directory)
        if label:
            versions.append(label)
        public_locations.append(
            {
                "kind": "directory",
                "name": directory.name,
                "version": label,
            }
        )

    unique_versions = tuple(dict.fromkeys(versions))
    known = bool(unique_versions) and all(
        item in SUPPORTED_AG_HISTORY_VERSIONS for item in unique_versions
    )
    if known:
        pin = "unsupported"
        reason = "import_not_implemented"
    elif unique_versions:
        pin = "unknown_version"
        reason = "ag_history_version_unclassified"
    else:
        pin = "unknown_version"
        reason = "ag_history_present_unversioned"

    return {
        "schema_version": AG_HISTORY_SCHEMA,
        "present": True,
        "supported": False,
        "imported": False,
        "mutated": False,
        "pin": pin,
        "reason": reason,
        "versions": list(unique_versions),
        "locations": public_locations,
        "live_import": False,
    }


__all__ = [
    "AG_HISTORY_SCHEMA",
    "AgHistoryError",
    "SUPPORTED_AG_HISTORY_VERSIONS",
    "inspect_ag_history",
]
