"""Read-only Antigravity conversation-summary importer (#74).

Only Antigravity's bounded ``conversation_summaries`` index is opened.  Per-
conversation databases, titles, previews, prompts, responses, tool output, and
application data directories are deliberately never queried.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
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
        "status_hash": _hash_descriptor(row["status"], namespace="ag-status"),
        "source_hash": _hash_descriptor(row["source"], namespace="ag-source"),
        "agent_name_hash": _hash_descriptor(
            row["agent_name"], namespace="ag-agent-name"
        ),
        "killed": bool(row["killed"]),
        "workspace_count": workspace_count,
        "workspace_hashes": workspace_hashes,
    }
    return {key: value for key, value in result.items() if value is not None}


_FileIdentity = tuple[int, int, int, int, int, int, int]


def _file_identity(info: os.stat_result) -> _FileIdentity:
    return (
        int(info.st_dev),
        int(info.st_ino),
        stat.S_IFMT(info.st_mode),
        int(info.st_nlink),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _descriptor_open_ready() -> bool:
    return (
        os.name == "posix"
        and hasattr(os, "O_NOFOLLOW")
        and bool(os.supports_dir_fd)
        and os.open in os.supports_dir_fd
    )


def _open_summary_parent(path: Path) -> tuple[list[int], int, str]:
    """Pin every POSIX ancestor with ``openat`` and ``O_NOFOLLOW``.

    Python's sqlite3 API accepts paths, not Windows handles.  Rather than
    validate a Windows junction and then race a second pathname open, the
    importer fails closed on hosts without POSIX descriptor-relative opens.
    """

    if not _descriptor_open_ready():
        raise AgHistoryError(
            "secure Antigravity SQLite open is unavailable",
            code="E_AG_HISTORY_PLATFORM",
        )
    absolute = Path(os.path.abspath(os.fspath(path)))
    parts = absolute.parts
    if len(parts) < 2 or not absolute.name:
        raise AgHistoryError(
            "unsafe Antigravity summary path", code="E_AG_HISTORY_PATH"
        )
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptors: list[int] = []
    try:
        current = os.open(absolute.anchor, directory_flags)
        descriptors.append(current)
        for part in parts[1:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
        return descriptors, current, parts[-1]
    except OSError as exc:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
        raise AgHistoryError(
            "unsafe Antigravity summary path", code="E_AG_HISTORY_PATH"
        ) from exc


def _probe_sidecar(parent_fd: int, name: str) -> bool:
    """Return whether a regular SQLite sidecar exists without following it."""

    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AgHistoryError(
            "unsafe Antigravity summary sidecar", code="E_AG_HISTORY_PATH"
        ) from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise AgHistoryError(
                "unsafe Antigravity summary sidecar", code="E_AG_HISTORY_PATH"
            )
        return True
    finally:
        os.close(descriptor)


def _assert_leaf_identity(parent_fd: int, name: str, expected: _FileIdentity) -> None:
    """Prove the pinned inode is still published at its original leaf name."""

    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise AgHistoryError(
            "Antigravity summary path changed while reading",
            code="E_AG_HISTORY_CHANGED",
        ) from exc
    try:
        if _file_identity(os.fstat(descriptor)) != expected:
            raise AgHistoryError(
                "Antigravity summary path changed while reading",
                code="E_AG_HISTORY_CHANGED",
            )
    finally:
        os.close(descriptor)


def _descriptor_path(descriptor: int) -> str:
    for root in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(root):
            return f"{root}/{descriptor}"
    raise AgHistoryError(
        "secure Antigravity SQLite descriptor path is unavailable",
        code="E_AG_HISTORY_PLATFORM",
    )


@contextmanager
def _open_summary_db(path: Path) -> Iterator[sqlite3.Connection]:
    """Open a pinned source descriptor as immutable SQLite.

    Active WAL/SHM state is skipped explicitly. Native read-only SQLite may
    write reader marks into the host ``-shm`` file, while immutable mode
    intentionally ignores WAL.  Failing closed is therefore the only stdlib
    sqlite3 option that is both honest and source-non-mutating.
    """

    descriptors, parent_fd, name = _open_summary_parent(path)
    database_fd: int | None = None
    connection: sqlite3.Connection | None = None
    wal_name = f"{name}-wal"
    shm_name = f"{name}-shm"
    try:
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        try:
            database_fd = os.open(name, flags, dir_fd=parent_fd)
        except OSError as exc:
            raise AgHistoryError(
                "unsafe Antigravity summary path", code="E_AG_HISTORY_PATH"
            ) from exc
        before = os.fstat(database_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise AgHistoryError(
                "unsafe Antigravity summary path", code="E_AG_HISTORY_PATH"
            )
        identity = _file_identity(before)
        if _probe_sidecar(parent_fd, wal_name) or _probe_sidecar(parent_fd, shm_name):
            raise AgHistoryError(
                "active Antigravity WAL state cannot be imported without mutation",
                code="E_AG_HISTORY_WAL_ACTIVE",
            )
        descriptor_path = _descriptor_path(database_fd)
        uri = f"file:{quote(descriptor_path, safe='/')}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        if _file_identity(os.fstat(database_fd)) != identity:
            raise AgHistoryError(
                "Antigravity summary changed while opening",
                code="E_AG_HISTORY_CHANGED",
            )
        sidecar_present = _probe_sidecar(parent_fd, wal_name) or _probe_sidecar(
            parent_fd, shm_name
        )
        _assert_leaf_identity(parent_fd, name, identity)
        if sidecar_present:
            raise AgHistoryError(
                "active Antigravity WAL state cannot be imported without mutation",
                code="E_AG_HISTORY_WAL_ACTIVE",
            )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        yield connection
        if _file_identity(os.fstat(database_fd)) != identity:
            raise AgHistoryError(
                "Antigravity summary changed while reading",
                code="E_AG_HISTORY_CHANGED",
            )
        sidecar_present = _probe_sidecar(parent_fd, wal_name) or _probe_sidecar(
            parent_fd, shm_name
        )
        _assert_leaf_identity(parent_fd, name, identity)
        if sidecar_present:
            raise AgHistoryError(
                "active Antigravity WAL state cannot be imported without mutation",
                code="E_AG_HISTORY_WAL_ACTIVE",
            )
    finally:
        if connection is not None:
            connection.close()
        if database_fd is not None:
            os.close(database_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


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
            """SELECT rowid AS summary_rowid,
                      substr(conversation_id, 1, 4096) AS conversation_id,
                      step_count,
                      substr(last_modified_time, 1, 128) AS last_modified_time,
                      length(workspace_uris) AS workspace_uris_chars,
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
            (remaining,),
        )
        imported: list[dict[str, Any]] = []
        for row in rows:
            workspace_chars = _bounded_int(
                row["workspace_uris_chars"],
                minimum=0,
                maximum=MAX_WORKSPACE_URI_CHARS + 1,
                fallback=MAX_WORKSPACE_URI_CHARS + 1,
            )
            workspace_bytes = _bounded_int(
                row["workspace_uris_bytes"],
                minimum=0,
                maximum=MAX_WORKSPACE_URI_AGGREGATE_BYTES + 1,
                fallback=MAX_WORKSPACE_URI_AGGREGATE_BYTES + 1,
            )
            workspace_uris: str | None = None
            if (
                workspace_budget > 0
                and workspace_chars <= MAX_WORKSPACE_URI_CHARS
                and workspace_bytes <= workspace_budget
            ):
                workspace_row = connection.execute(
                    """SELECT workspace_uris
                         FROM conversation_summaries
                        WHERE rowid = ?""",
                    (row["summary_rowid"],),
                ).fetchone()
                candidate = workspace_row[0] if workspace_row is not None else None
                if (
                    isinstance(candidate, str)
                    and len(candidate) <= MAX_WORKSPACE_URI_CHARS
                    and len(candidate.encode("utf-8")) <= workspace_budget
                ):
                    workspace_uris = candidate
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
    saw_busy = False
    saw_changed = False
    saw_platform = False
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
            elif exc.code == "E_AG_HISTORY_WAL_ACTIVE":
                saw_busy = True
                diagnostics.append(
                    {
                        "source": _SUMMARY_DB,
                        "reason": "live_wal_readonly_unsupported",
                    }
                )
            elif exc.code == "E_AG_HISTORY_CHANGED":
                saw_changed = True
                diagnostics.append(
                    {"source": _SUMMARY_DB, "reason": "sqlite_source_changed"}
                )
            elif exc.code == "E_AG_HISTORY_PLATFORM":
                saw_platform = True
                diagnostics.append(
                    {
                        "source": _SUMMARY_DB,
                        "reason": "secure_sqlite_open_unavailable",
                    }
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
    elif saw_busy:
        pin = "active_wal"
        reason = "ag_history_live_wal_skipped"
    elif saw_changed:
        pin = "unstable_source"
        reason = "ag_history_source_changed"
    elif saw_platform:
        pin = "unsupported_platform"
        reason = "ag_history_secure_open_unavailable"
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
