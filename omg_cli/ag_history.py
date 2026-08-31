"""Read-only Antigravity conversation-summary importer (#74).

Only Antigravity's bounded ``conversation_summaries`` index is opened.  Per-
conversation databases, titles, previews, prompts, responses, tool output, and
application data directories are deliberately never queried.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


AG_HISTORY_SCHEMA = 1
SQLITE_SUMMARY_VERSION = 1
SUPPORTED_AG_HISTORY_VERSIONS: frozenset[str] = frozenset(
    {f"sqlite-summary-v{SQLITE_SUMMARY_VERSION}"}
)
MAX_AG_HISTORY_RECORDS = 200
MAX_WORKSPACE_URI_CHARS = 8_192
MAX_WORKSPACE_URI_AGGREGATE_BYTES = 1_048_576

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
_SUMMARY_DB = "conversation_summaries.db"
_REQUIRED_SUMMARY_COLUMNS = frozenset(
    {
        "conversation_id",
        "title",
        "preview",
        "step_count",
        "last_modified_time",
        "workspace_uris",
        "status",
        "source",
        "project_id",
        "agent_name",
        "parent_conversation_id",
        "nesting_depth",
        "killed",
        "last_user_input_time",
        "last_user_input_step_index",
        "app_data_dir",
    }
)
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:/-]{1,64}$")


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
    """Return legacy/version-marker directories without following symlinks."""

    found: list[Path] = []
    seen: set[str] = set()

    def _add(raw: Path) -> None:
        existing = _existing_dir(raw)
        if existing is None:
            return
        key = os.path.abspath(os.fspath(existing))
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
        candidate = Path(env_dir).expanduser()
        if candidate.name != _SUMMARY_DB:
            _add(candidate)
    # The explicit ``session ag-history`` command may inspect this one pinned,
    # documented host index.  No other home directory is scanned.
    _add(home / ".gemini" / "antigravity-cli")
    return found


def _candidate_summary_dbs(project_root: Path, *, home: Path) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()

    def _add(raw: Path) -> None:
        key = os.path.abspath(os.fspath(raw))
        if key in seen:
            return
        try:
            raw.lstat()
        except OSError:
            return
        seen.add(key)
        found.append(raw)

    for directory in _candidate_dirs(project_root, home=home):
        _add(directory / _SUMMARY_DB)
    env_dir = (
        os.environ.get("ANTIGRAVITY_HISTORY") or os.environ.get("AG_HISTORY_DIR") or ""
    ).strip()
    if env_dir:
        candidate = Path(env_dir).expanduser()
        _add(candidate if candidate.name == _SUMMARY_DB else candidate / _SUMMARY_DB)
    _add(home / ".gemini" / "antigravity-cli" / _SUMMARY_DB)
    return found


def _read_version_label(directory: Path) -> str | None:
    """Classify legacy marker layouts without reading marker contents."""

    for name in _VERSION_FILES:
        path = directory / name
        try:
            if not path.is_file() or path.is_symlink():
                continue
            if path.stat().st_size > 65_536:
                return "oversized"
        except OSError:
            continue
        # Marker contents are host-private and not required for the supported
        # SQLite summary pin. Never surface or parse them as public metadata.
        return "marker-present"
    return None


def _hash_descriptor(value: object, *, namespace: str) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    digest = hashlib.sha256(f"{namespace}\0{value}".encode("utf-8")).hexdigest()
    return digest[:20]


def _bounded_int(value: Any, *, minimum: int, maximum: int, fallback: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    return max(minimum, min(parsed, maximum))


def _safe_label(value: object) -> str | None:
    if not isinstance(value, str) or not _SAFE_LABEL.fullmatch(value):
        return None
    return value


def _safe_timestamp(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _workspace_descriptors(value: object) -> tuple[int, list[str]]:
    if not isinstance(value, str) or len(value) > MAX_WORKSPACE_URI_CHARS:
        return 0, []
    try:
        parsed = json.loads(value)
    except (json.JSONDecodeError, RecursionError):
        parsed = [value] if value else []
    if not isinstance(parsed, list):
        return 0, []
    fingerprints = [
        digest
        for item in parsed[:32]
        if (digest := _hash_descriptor(item, namespace="ag-workspace")) is not None
    ]
    return min(len(parsed), 32), fingerprints


def _public_row(
    row: sqlite3.Row, *, workspace_uris: str | None
) -> dict[str, Any]:
    workspace_count, workspace_hashes = _workspace_descriptors(workspace_uris)
    result: dict[str, Any] = {
        "provider": "antigravity",
        "provenance": "antigravity_conversation_summaries",
        "external_id_hash": _hash_descriptor(
            row["conversation_id"], namespace="ag-conversation"
        ),
        "parent_id_hash": _hash_descriptor(
            row["parent_conversation_id"], namespace="ag-conversation"
        ),
        "project_id_hash": _hash_descriptor(row["project_id"], namespace="ag-project"),
        "step_count": _bounded_int(
            row["step_count"], minimum=0, maximum=10_000_000, fallback=0
        ),
        "nesting_depth": _bounded_int(
            row["nesting_depth"], minimum=0, maximum=1_000, fallback=0
        ),
        "last_user_input_step_index": _bounded_int(
            row["last_user_input_step_index"],
            minimum=-1,
            maximum=10_000_000,
            fallback=-1,
        ),
        "last_modified_time": _safe_timestamp(row["last_modified_time"]),
        "last_user_input_time": _safe_timestamp(row["last_user_input_time"]),
        "status": _safe_label(row["status"]),
        "source": _safe_label(row["source"]),
        "agent_name": _safe_label(row["agent_name"]),
        "killed": bool(row["killed"]),
        "workspace_count": workspace_count,
        "workspace_hashes": workspace_hashes,
    }
    return {key: value for key, value in result.items() if value is not None}


def _safe_path_identity(path: Path) -> tuple[tuple[int, int, int], ...]:
    """Validate every existing path component and return a stable identity chain."""

    absolute = Path(os.path.abspath(os.fspath(path)))
    current = Path(absolute.anchor)
    identities: list[tuple[int, int, int]] = []
    try:
        for part in absolute.parts[1:]:
            current /= part
            info = current.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise AgHistoryError(
                    "unsafe Antigravity summary path", code="E_AG_HISTORY_PATH"
                )
            identities.append(
                (int(info.st_dev), int(info.st_ino), stat.S_IFMT(info.st_mode))
            )
    except AgHistoryError:
        raise
    except OSError as exc:
        raise AgHistoryError(
            "unsafe Antigravity summary path", code="E_AG_HISTORY_PATH"
        ) from exc
    if not identities or not stat.S_ISREG(identities[-1][2]):
        raise AgHistoryError("unsafe Antigravity summary path", code="E_AG_HISTORY_PATH")
    return tuple(identities)


def _validate_optional_sqlite_sidecar(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AgHistoryError(
            "unsafe Antigravity summary sidecar", code="E_AG_HISTORY_PATH"
        ) from exc
    _safe_path_identity(path)


def _open_summary_db(path: Path) -> sqlite3.Connection:
    """Open a component-safe SQLite database in native read-only mode.

    Unlike immutable mode, mode=ro participates in SQLite's normal WAL
    snapshot handling and cannot silently ignore committed rows that still
    live in the host's WAL file.
    """

    before = _safe_path_identity(path)
    _validate_optional_sqlite_sidecar(Path(f"{path}-wal"))
    _validate_optional_sqlite_sidecar(Path(f"{path}-shm"))
    uri = f"file:{quote(os.path.abspath(os.fspath(path)), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    if _safe_path_identity(path) != before:
        connection.close()
        raise AgHistoryError("unsafe Antigravity summary path", code="E_AG_HISTORY_PATH")
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection


def _import_summary_db(
    path: Path, *, remaining: int, workspace_budget: int
) -> tuple[int, list[dict[str, Any]], int]:
    with _open_summary_db(path) as connection:
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version != SQLITE_SUMMARY_VERSION:
            raise AgHistoryError(
                f"unsupported Antigravity summary schema {user_version}",
                code="E_AG_HISTORY_VERSION",
            )
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(conversation_summaries)")
        }
        if not _REQUIRED_SUMMARY_COLUMNS.issubset(columns):
            raise AgHistoryError(
                "unsupported Antigravity conversation_summaries shape",
                code="E_AG_HISTORY_VERSION",
            )
        # Never add title, preview, app_data_dir, task details, or transcript
        # fields to this SELECT.  The privacy boundary is enforced at source.
        rows = connection.execute(
            """SELECT substr(conversation_id, 1, 4096) AS conversation_id,
                      step_count,
                      substr(last_modified_time, 1, 128) AS last_modified_time,
                      substr(workspace_uris, 1, ?) AS workspace_uris_prefix,
                      length(CAST(workspace_uris AS BLOB)) AS workspace_uris_bytes,
                      substr(status, 1, 128) AS status,
                      substr(source, 1, 128) AS source,
                      substr(project_id, 1, 4096) AS project_id,
                      substr(agent_name, 1, 128) AS agent_name,
                      substr(parent_conversation_id, 1, 4096)
                        AS parent_conversation_id,
                      nesting_depth, killed,
                      substr(last_user_input_time, 1, 128)
                        AS last_user_input_time,
                      last_user_input_step_index
                 FROM conversation_summaries
             ORDER BY last_modified_time DESC, conversation_id ASC
                LIMIT ?""",
            (MAX_WORKSPACE_URI_CHARS + 1, remaining),
        )
        imported: list[dict[str, Any]] = []
        for row in rows:
            raw_workspace = row["workspace_uris_prefix"]
            workspace_bytes = _bounded_int(
                row["workspace_uris_bytes"],
                minimum=0,
                maximum=MAX_WORKSPACE_URI_AGGREGATE_BYTES + 1,
                fallback=MAX_WORKSPACE_URI_AGGREGATE_BYTES + 1,
            )
            workspace_uris = (
                raw_workspace
                if isinstance(raw_workspace, str)
                and len(raw_workspace) <= MAX_WORKSPACE_URI_CHARS
                and workspace_bytes <= workspace_budget
                else None
            )
            if workspace_uris is not None:
                workspace_budget -= workspace_bytes
            imported.append(_public_row(row, workspace_uris=workspace_uris))
    return user_version, imported, workspace_budget


def _empty_result() -> dict[str, Any]:
    return {
        "schema_version": AG_HISTORY_SCHEMA,
        "present": False,
        "supported": False,
        "imported": False,
        "mutated": False,
        "read_only": True,
        "pin": "unsupported",
        "reason": "ag_history_absent",
        "locations": [],
        "versions": [],
        "record_count": 0,
        "records": [],
        "diagnostics": [],
        "content_fields_read": False,
        "raw_content": False,
        "private_content": False,
        "live_import": False,
    }


def inspect_ag_history(
    project_root: Path | str,
    *,
    home: Path | str | None = None,
) -> dict[str, Any]:
    """Import bounded descriptors from supported AG summary indexes, read-only."""

    root = Path(project_root)
    home_path = Path(home) if home is not None else Path.home()
    databases = _candidate_summary_dbs(root, home=home_path)
    legacy_locations = _candidate_dirs(root, home=home_path)
    if not databases and not legacy_locations:
        return _empty_result()

    records: list[dict[str, Any]] = []
    locations: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    versions: list[str] = []
    opened = 0
    saw_corrupt = False
    saw_unsafe = False
    saw_unknown = False
    workspace_budget = MAX_WORKSPACE_URI_AGGREGATE_BYTES

    for path in databases:
        location = {"kind": "sqlite_summary", "name": _SUMMARY_DB}
        locations.append(location)
        try:
            version, imported, workspace_budget = _import_summary_db(
                path,
                remaining=max(0, MAX_AG_HISTORY_RECORDS - len(records)),
                workspace_budget=workspace_budget,
            )
        except AgHistoryError as exc:
            if exc.code == "E_AG_HISTORY_PATH":
                saw_unsafe = True
                diagnostics.append(
                    {"source": _SUMMARY_DB, "reason": "unsafe_path_skipped"}
                )
            else:
                saw_unknown = True
                diagnostics.append(
                    {
                        "source": _SUMMARY_DB,
                        "reason": "unsupported_schema_version",
                    }
                )
            continue
        except (OSError, sqlite3.DatabaseError):
            saw_corrupt = True
            diagnostics.append({"source": _SUMMARY_DB, "reason": "sqlite_read_failed"})
            continue
        opened += 1
        version_label = f"sqlite-summary-v{version}"
        versions.append(version_label)
        location["version"] = version_label
        records.extend(imported)

    # Preserve classification for older marker-only layouts without reading
    # transcript-like files inside them.
    database_parents = {os.path.abspath(os.fspath(path.parent)) for path in databases}
    for directory in legacy_locations:
        if os.path.abspath(os.fspath(directory)) in database_parents:
            continue
        label = _read_version_label(directory)
        locations.append({"kind": "directory", "name": directory.name, "version": label})
        if label:
            versions.append(label)
        saw_unknown = True

    if opened:
        pin = f"sqlite-summary-v{SQLITE_SUMMARY_VERSION}"
        reason = "supported_summary_imported"
    elif saw_unsafe:
        pin = "unsafe_path"
        reason = "ag_history_path_rejected"
    elif saw_corrupt:
        pin = "corrupt"
        reason = "ag_history_corrupt_skipped"
    elif saw_unknown:
        pin = "unknown_version"
        reason = "ag_history_version_unclassified"
    else:
        pin = "unsupported"
        reason = "ag_history_absent"

    return {
        "schema_version": AG_HISTORY_SCHEMA,
        "present": bool(databases or legacy_locations),
        "supported": opened > 0,
        "imported": opened > 0,
        "mutated": False,
        "read_only": True,
        "pin": pin,
        "reason": reason,
        "versions": list(dict.fromkeys(versions)),
        "locations": locations,
        "record_count": len(records),
        "records": records,
        "diagnostics": diagnostics,
        "content_fields_read": False,
        "raw_content": False,
        "private_content": False,
        "live_import": opened > 0,
    }


__all__ = [
    "AG_HISTORY_SCHEMA",
    "AgHistoryError",
    "MAX_AG_HISTORY_RECORDS",
    "MAX_WORKSPACE_URI_AGGREGATE_BYTES",
    "MAX_WORKSPACE_URI_CHARS",
    "SUPPORTED_AG_HISTORY_VERSIONS",
    "inspect_ag_history",
]
