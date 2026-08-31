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
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import quote


AG_HISTORY_SCHEMA = 1
SQLITE_SUMMARY_VERSION = 1
SUPPORTED_AG_HISTORY_VERSIONS: frozenset[str] = frozenset(
    {f"sqlite-summary-v{SQLITE_SUMMARY_VERSION}"}
)
MAX_AG_HISTORY_RECORDS = 200
MAX_WORKSPACE_URI_CHARS = 8_192
MAX_WORKSPACE_URI_AGGREGATE_BYTES = 1_048_576
_MAX_WORKSPACE_URI_UTF8_BYTES = MAX_WORKSPACE_URI_CHARS * 4

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
# Declaring any of these names shadows SQLite's integer rowid aliases and
# makes ORDER BY / blobopen rowid resolve to user data instead of the key.
_ROWID_ALIAS_COLUMNS = frozenset({"rowid", "_rowid_", "oid"})
_CANONICAL_UTC = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"
)
class AgHistoryError(ValueError):
    """AG history probe failed closed without mutating anything."""

    def __init__(self, message: str, *, code: str = "E_AG_HISTORY") -> None:
        super().__init__(message)
        self.code = code


def _existing_dir(path: Path) -> Path | None:
    descriptors: list[int] = []
    try:
        # Reuse the same descriptor-relative, no-follow ancestor walk as the
        # SQLite importer.  A synthetic leaf makes ``path`` itself the pinned
        # parent directory without reading any host-private entry.
        descriptors, _directory_fd, _name = _open_summary_parent(
            path / ".omg-antigravity-directory-probe"
        )
    except (AgHistoryError, OSError):
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return path


def _candidate_dirs(project_root: Path, *, home: Path) -> list[Path]:
    """Return legacy/version-marker directories without following symlinks."""

    found: list[Path] = []
    seen: set[str] = set()

    def _add(raw: Path) -> None:
        if _descriptor_open_ready():
            existing = _existing_dir(raw)
            if existing is None:
                return
        else:
            # Marker-only layouts must still be discovered on hosts without
            # descriptor-relative opens so inspect reports present/unclassified
            # rather than ag_history_absent. The later open remains fail-closed.
            try:
                info = raw.lstat()
            except OSError:
                return
            if not stat.S_ISDIR(info.st_mode):
                return
            existing = raw
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

    # Project-local indexes must be discovered by leaf existence only. The
    # later open still requires descriptor-relative POSIX capability, so an
    # unsupported platform can emit secure_sqlite_open_unavailable instead of
    # falsely reporting ag_history_absent.
    for name in _PROJECT_MARKERS:
        _add(project_root / name / _SUMMARY_DB)
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

    descriptors: list[int] = []
    try:
        descriptors, directory_fd, _name = _open_summary_parent(
            directory / ".omg-antigravity-marker-probe"
        )
        flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
        for name in _VERSION_FILES:
            try:
                marker_fd = os.open(name, flags, dir_fd=directory_fd)
            except OSError:
                continue
            try:
                info = os.fstat(marker_fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    continue
                if info.st_size > 65_536:
                    return "oversized"
            finally:
                os.close(marker_fd)
            # Marker contents are host-private and not required for the
            # supported SQLite summary pin. Never surface or parse them.
            return "marker-present"
    except (AgHistoryError, OSError):
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return None


def _hash_descriptor(value: object, *, namespace: str) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        encoded = f"{namespace}\0{value}".encode("utf-8")
    except UnicodeEncodeError:
        return None
    digest = hashlib.sha256(encoded).hexdigest()
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
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, ValueError):
        return None


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
    row: Mapping[str, Any], *, workspace_uris: str | None
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


def _probe_sidecar_size(parent_fd: int, name: str) -> int | None:
    """Return a regular SQLite sidecar's size without following it."""

    flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
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
        return info.st_size
    finally:
        os.close(descriptor)


def _probe_rollback_journal(parent_fd: int, name: str) -> bool:
    """Reject live/ambiguous journals but allow a proven PERSIST tombstone."""

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
        if info.st_size == 0:
            return False
        header = os.read(descriptor, 28)
        if len(header) == 28 and header == b"\0" * 28:
            return False
        return True
    finally:
        os.close(descriptor)


def _assert_no_active_sqlite_sidecars(
    parent_fd: int, *, database_name: str
) -> None:
    """Reject SQLite states that immutable reads cannot safely observe."""

    if _probe_rollback_journal(parent_fd, f"{database_name}-journal"):
        raise AgHistoryError(
            "active Antigravity rollback journal cannot be imported safely",
            code="E_AG_HISTORY_JOURNAL_ACTIVE",
        )
    wal_size = _probe_sidecar_size(parent_fd, f"{database_name}-wal")
    # The shared-memory index never contains committed database content by
    # itself.  Still validate any stale file as a regular, singly-linked leaf
    # so a symlink or other unsafe object cannot hide beside the database.
    _probe_sidecar_size(parent_fd, f"{database_name}-shm")
    if wal_size not in (None, 0):
        raise AgHistoryError(
            "active Antigravity WAL state cannot be imported without mutation",
            code="E_AG_HISTORY_WAL_ACTIVE",
        )


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
def _open_summary_db(path: Path) -> Iterator[tuple[sqlite3.Connection, _FileIdentity]]:
    """Open a pinned source descriptor as immutable SQLite.

    A non-empty WAL is skipped explicitly. Native read-only SQLite may write
    reader marks into the host ``-shm`` file, while immutable mode intentionally
    ignores WAL. A stale SHM alone contains no committed database content, but
    every sidecar leaf is still validated without following links.
    """

    descriptors, parent_fd, name = _open_summary_parent(path)
    database_fd: int | None = None
    connection: sqlite3.Connection | None = None
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
        _assert_no_active_sqlite_sidecars(parent_fd, database_name=name)
        descriptor_path = _descriptor_path(database_fd)
        uri = f"file:{quote(descriptor_path, safe='/')}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        if _file_identity(os.fstat(database_fd)) != identity:
            raise AgHistoryError(
                "Antigravity summary changed while opening",
                code="E_AG_HISTORY_CHANGED",
            )
        _assert_leaf_identity(parent_fd, name, identity)
        _assert_no_active_sqlite_sidecars(parent_fd, database_name=name)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        yield connection, identity
        if _file_identity(os.fstat(database_fd)) != identity:
            raise AgHistoryError(
                "Antigravity summary changed while reading",
                code="E_AG_HISTORY_CHANGED",
            )
        _assert_leaf_identity(parent_fd, name, identity)
        _assert_no_active_sqlite_sidecars(parent_fd, database_name=name)
    finally:
        if connection is not None:
            connection.close()
        if database_fd is not None:
            os.close(database_fd)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _read_workspace_uris(
    connection: sqlite3.Connection, *, rowid: int, workspace_budget: int
) -> tuple[str | None, int]:
    """Read one TEXT cell incrementally without materializing oversized values."""

    if workspace_budget <= 0:
        return None, workspace_budget
    try:
        blob = connection.blobopen(
            "conversation_summaries", "workspace_uris", rowid, readonly=True
        )
    except sqlite3.Error:
        return None, workspace_budget
    try:
        size = len(blob)
        if size > workspace_budget or size > _MAX_WORKSPACE_URI_UTF8_BYTES:
            return None, workspace_budget
        raw = blob.read(size)
    finally:
        blob.close()
    remaining = workspace_budget - len(raw)
    if len(raw) != size:
        return None, remaining
    try:
        candidate = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, remaining
    if len(candidate) > MAX_WORKSPACE_URI_CHARS:
        return None, remaining
    return candidate, remaining


_TEXT_COLUMN_LIMITS = (
    ("conversation_id", 4096),
    ("last_modified_time", 128),
    ("status", 128),
    ("source", 128),
    ("project_id", 4096),
    ("agent_name", 128),
    ("parent_conversation_id", 4096),
    ("last_user_input_time", 128),
)


def _read_bounded_text(
    connection: sqlite3.Connection, *, column: str, rowid: int, max_chars: int
) -> str:
    """Read one TEXT/BLOB cell incrementally; oversized storage is omitted."""

    try:
        blob = connection.blobopen(
            "conversation_summaries", column, rowid, readonly=True
        )
    except sqlite3.Error:
        return ""
    try:
        size = len(blob)
        if size > max_chars * 4:
            return ""
        raw = blob.read(size)
    finally:
        blob.close()
    if len(raw) != size:
        return ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    if len(text) > max_chars:
        return ""
    return text


def _import_summary_db(
    path: Path, *, remaining: int
) -> tuple[int, list[tuple[int, dict[str, Any]]], _FileIdentity]:
    with _open_summary_db(path) as (connection, identity):
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version != SQLITE_SUMMARY_VERSION:
            raise AgHistoryError(
                f"unsupported Antigravity summary schema {user_version}",
                code="E_AG_HISTORY_VERSION",
            )
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_xinfo(conversation_summaries)")
        }
        if not _REQUIRED_SUMMARY_COLUMNS.issubset(columns):
            raise AgHistoryError(
                "unsupported Antigravity conversation_summaries shape",
                code="E_AG_HISTORY_VERSION",
            )
        if any(name.casefold() in _ROWID_ALIAS_COLUMNS for name in columns):
            raise AgHistoryError(
                "unsupported Antigravity conversation_summaries rowid alias",
                code="E_AG_HISTORY_VERSION",
            )
        order_plan = connection.execute(
            """EXPLAIN QUERY PLAN
               SELECT rowid
                 FROM conversation_summaries
             ORDER BY last_modified_time DESC, rowid DESC
                LIMIT ?""",
            (remaining,),
        ).fetchall()
        plan_details = [str(row[3]).upper() for row in order_plan]
        indexed_order = any(
            "CONVERSATION_SUMMARIES" in detail
            and (
                "USING INDEX" in detail
                or "USING COVERING INDEX" in detail
            )
            for detail in plan_details
        )
        if not indexed_order or any("TEMP B-TREE" in detail for detail in plan_details):
            raise AgHistoryError(
                "Antigravity summary ordering is not resource bounded",
                code="E_AG_HISTORY_UNBOUNDED_QUERY",
            )
        # Never add title, preview, app_data_dir, task details, or transcript
        # fields to this SELECT.  The privacy boundary is enforced at source.
        # String cells are not projected here: substr() materializes the whole
        # dynamically typed value first, so oversized TEXT/BLOB storage is
        # read incrementally through blobopen after this bounded LIMIT.
        rows = connection.execute(
            """SELECT rowid AS summary_rowid,
                      CASE WHEN typeof(step_count) = 'integer'
                           THEN step_count ELSE 0 END AS step_count,
                      CASE WHEN typeof(nesting_depth) = 'integer'
                           THEN nesting_depth ELSE 0 END AS nesting_depth,
                      CASE WHEN typeof(killed) = 'integer'
                           THEN killed ELSE 0 END AS killed,
                      CASE WHEN typeof(last_user_input_step_index) = 'integer'
                           THEN last_user_input_step_index ELSE -1 END
                        AS last_user_input_step_index
                 FROM conversation_summaries
             ORDER BY last_modified_time DESC, rowid DESC
                LIMIT ?""",
            (remaining,),
        )
        fetched = list(rows)
        imported: list[tuple[int, dict[str, Any]]] = []
        skipped = 0
        for row in fetched:
            try:
                summary_rowid = int(row["summary_rowid"])
            except (TypeError, ValueError, OverflowError) as exc:
                raise AgHistoryError(
                    "unsupported Antigravity conversation_summaries rowid",
                    code="E_AG_HISTORY_VERSION",
                ) from exc
            texts = {
                column: _read_bounded_text(
                    connection,
                    column=column,
                    rowid=summary_rowid,
                    max_chars=max_chars,
                )
                for column, max_chars in _TEXT_COLUMN_LIMITS
            }
            raw_modified = texts["last_modified_time"]
            if (
                not isinstance(raw_modified, str)
                or _CANONICAL_UTC.match(raw_modified) is None
                or _safe_timestamp(raw_modified) is None
            ):
                skipped += 1
                continue
            public = {
                "summary_rowid": summary_rowid,
                "step_count": row["step_count"],
                "nesting_depth": row["nesting_depth"],
                "killed": row["killed"],
                "last_user_input_step_index": row["last_user_input_step_index"],
                **texts,
            }
            imported.append(
                (summary_rowid, _public_row(public, workspace_uris=None))
            )
        if skipped and len(fetched) == remaining:
            raise AgHistoryError(
                "bounded newest window contains non-canonical timestamps",
                code="E_AG_HISTORY_UNBOUNDED_QUERY",
            )
    return user_version, imported, identity


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
    saw_journal = False
    saw_changed = False
    saw_platform = False
    saw_unbounded = False
    pending: list[tuple[Path, int, dict[str, Any], _FileIdentity]] = []

    for path in databases:
        location = {"kind": "sqlite_summary", "name": _SUMMARY_DB}
        locations.append(location)
        try:
            version, imported, source_identity = _import_summary_db(
                path,
                remaining=MAX_AG_HISTORY_RECORDS,
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
            elif exc.code == "E_AG_HISTORY_JOURNAL_ACTIVE":
                saw_journal = True
                diagnostics.append(
                    {
                        "source": _SUMMARY_DB,
                        "reason": "live_journal_readonly_unsupported",
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
            elif exc.code == "E_AG_HISTORY_UNBOUNDED_QUERY":
                saw_unbounded = True
                diagnostics.append(
                    {
                        "source": _SUMMARY_DB,
                        "reason": "unbounded_sqlite_query_plan",
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
        pending.extend((path, rowid, row, source_identity) for rowid, row in imported)

    pending.sort(
        key=lambda item: str(item[2].get("last_modified_time") or ""),
        reverse=True,
    )
    pending = pending[:MAX_AG_HISTORY_RECORDS]
    workspace_budget = MAX_WORKSPACE_URI_AGGREGATE_BYTES
    filled: dict[tuple[str, int], dict[str, Any]] = {}
    opened_by_path: dict[str, tuple[sqlite3.Connection, _FileIdentity]] = {}
    try:
        with ExitStack() as stack:
            for path, rowid, row, source_identity in pending:
                key = os.path.abspath(os.fspath(path))
                cached = opened_by_path.get(key)
                if cached is None:
                    connection, identity = stack.enter_context(
                        _open_summary_db(Path(key))
                    )
                    if identity != source_identity:
                        raise AgHistoryError(
                            "Antigravity summary changed while reading",
                            code="E_AG_HISTORY_CHANGED",
                        )
                    opened_by_path[key] = (connection, identity)
                else:
                    connection, identity = cached
                    if identity != source_identity:
                        raise AgHistoryError(
                            "Antigravity summary changed while reading",
                            code="E_AG_HISTORY_CHANGED",
                        )
                workspace_uris, workspace_budget = _read_workspace_uris(
                    connection,
                    rowid=rowid,
                    workspace_budget=workspace_budget,
                )
                count, hashes = _workspace_descriptors(workspace_uris)
                updated = dict(row)
                updated["workspace_count"] = count
                if hashes:
                    updated["workspace_hashes"] = hashes
                filled[(key, rowid)] = {
                    k: v for k, v in updated.items() if v is not None
                }
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
        elif exc.code == "E_AG_HISTORY_JOURNAL_ACTIVE":
            saw_journal = True
            diagnostics.append(
                {
                    "source": _SUMMARY_DB,
                    "reason": "live_journal_readonly_unsupported",
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
        records = []
    except (OSError, sqlite3.DatabaseError):
        saw_corrupt = True
        diagnostics.append({"source": _SUMMARY_DB, "reason": "sqlite_read_failed"})
        records = []
    else:
        records = [
            filled[(os.path.abspath(os.fspath(path)), rowid)]
            for path, rowid, _row, _identity in pending
        ]

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

    import_ok = opened > 0 and not (
        saw_changed
        or saw_busy
        or saw_journal
        or saw_unsafe
        or saw_corrupt
        or saw_platform
        or saw_unbounded
    )
    if not import_ok:
        records = []
    if import_ok:
        pin = f"sqlite-summary-v{SQLITE_SUMMARY_VERSION}"
        reason = "supported_summary_imported"
    elif saw_unsafe:
        pin = "unsafe_path"
        reason = "ag_history_path_rejected"
    elif saw_busy:
        pin = "active_wal"
        reason = "ag_history_live_wal_skipped"
    elif saw_journal:
        pin = "active_journal"
        reason = "ag_history_live_journal_skipped"
    elif saw_changed:
        pin = "unstable_source"
        reason = "ag_history_source_changed"
    elif saw_platform:
        pin = "unsupported_platform"
        reason = "ag_history_secure_open_unavailable"
    elif saw_unbounded:
        pin = "unbounded_query"
        reason = "ag_history_unbounded_query_skipped"
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
        "supported": import_ok,
        "imported": import_ok,
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
        "live_import": import_ok,
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
