"""Read-only Antigravity conversation-summary import contract (#74)."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import omg_cli.ag_history as ag_history
from omg_cli.ag_history import inspect_ag_history


def _summary_db(home: Path) -> Path:
    root = home / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    path = root / "conversation_summaries.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE conversation_summaries (
              conversation_id TEXT PRIMARY KEY,
              title TEXT NOT NULL DEFAULT '',
              preview TEXT NOT NULL DEFAULT '',
              step_count INTEGER NOT NULL DEFAULT 0,
              last_modified_time DATETIME NOT NULL,
              workspace_uris TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT '',
              project_id TEXT NOT NULL DEFAULT '',
              agent_name TEXT NOT NULL DEFAULT '',
              parent_conversation_id TEXT NOT NULL DEFAULT '',
              nesting_depth INTEGER NOT NULL DEFAULT 0,
              battle_id TEXT NOT NULL DEFAULT '',
              winning_conversation_id TEXT NOT NULL DEFAULT '',
              not_fully_idle NUMERIC NOT NULL DEFAULT false,
              killed NUMERIC NOT NULL DEFAULT false,
              last_user_input_time DATETIME NOT NULL,
              last_user_input_step_index INTEGER NOT NULL DEFAULT -1,
              app_data_dir TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX idx_conversation_summaries_last_modified_time
              ON conversation_summaries(last_modified_time);
            """
        )
        conn.execute(
            """INSERT INTO conversation_summaries
               (conversation_id, title, preview, step_count, last_modified_time,
                workspace_uris, status, source, project_id, agent_name,
                parent_conversation_id, nesting_depth, killed,
                last_user_input_time, last_user_input_step_index, app_data_dir)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "conversation-secret-id",
                "RAW PRIVATE TITLE",
                "RAW PRIVATE PREVIEW",
                12,
                "2026-08-31T00:00:00Z",
                json.dumps(["file:///Users/private/secret-project"]),
                "idle",
                "antigravity",
                "PRIVATE PROJECT",
                "omg-executor",
                "parent-secret-id",
                1,
                0,
                "2026-08-31T00:00:00Z",
                9,
                "/Users/private/.gemini",
            ),
        )
    return path


def test_imports_supported_summary_db_without_private_content(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    before = db.read_bytes()

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["present"] is True
    assert result["supported"] is True
    assert result["imported"] is True
    assert result["mutated"] is False
    assert result["read_only"] is True
    assert result["live_import"] is True
    assert result["pin"] == "sqlite-summary-v1"
    assert result["record_count"] == 1
    assert result["content_fields_read"] is False
    assert result["raw_content"] is False
    assert result["private_content"] is False
    row = result["records"][0]
    assert row["provider"] == "antigravity"
    assert row["step_count"] == 12
    assert row["workspace_count"] == 1
    assert row["external_id_hash"] != "conversation-secret-id"
    assert set(row) >= {"status_hash", "source_hash", "agent_name_hash"}
    assert not ({"status", "source", "agent_name"} & set(row))
    dumped = json.dumps(result)
    for private in (
        "RAW PRIVATE TITLE",
        "RAW PRIVATE PREVIEW",
        "conversation-secret-id",
        "parent-secret-id",
        "PRIVATE PROJECT",
        "/Users/private",
        str(home),
    ):
        assert private not in dumped
    assert db.read_bytes() == before
    assert not Path(f"{db}-wal").exists()
    assert not Path(f"{db}-shm").exists()


def test_unknown_schema_is_skipped_without_mutation(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 99")
    before = db.read_bytes()

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["present"] is True
    assert result["supported"] is False
    assert result["imported"] is False
    assert result["pin"] == "unknown_version"
    assert any(item["reason"] == "unsupported_schema_version" for item in result["diagnostics"])
    assert db.read_bytes() == before


def test_corrupt_summary_db_is_skipped_with_bounded_diagnostic(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    db = root / "conversation_summaries.db"
    db.write_bytes(b"not sqlite and contains RAW PRIVATE CONTENT")

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["present"] is True
    assert result["imported"] is False
    assert result["pin"] == "corrupt"
    dumped = json.dumps(result)
    assert "RAW PRIVATE CONTENT" not in dumped
    assert str(home) not in dumped
    assert any(item["reason"] == "sqlite_read_failed" for item in result["diagnostics"])


def test_summary_symlink_is_never_followed(tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    real = _summary_db(real_home)
    home = tmp_path / "home"
    root = home / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    (root / "conversation_summaries.db").symlink_to(real)

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is False
    assert result["present"] is True
    assert result["pin"] == "unsafe_path"


def test_summary_parent_symlink_is_never_followed(tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    _summary_db(real_home)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gemini").symlink_to(real_home / ".gemini", target_is_directory=True)

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is False
    assert result["present"] is True
    assert result["pin"] == "unsafe_path"


def test_live_wal_is_skipped_without_mutating_database_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            """INSERT INTO conversation_summaries
               (conversation_id, title, preview, step_count, last_modified_time,
                workspace_uris, status, source, project_id, agent_name,
                parent_conversation_id, nesting_depth, killed,
                last_user_input_time, last_user_input_step_index, app_data_dir)
               VALUES ('wal-only', '', '', 3, '2026-08-31T01:00:00Z',
                       '[]', 'idle', 'antigravity', '', '', '', 0, 0,
                       '2026-08-31T01:00:00Z', 0, '')"""
        )
        writer.commit()
        wal = Path(f"{db}-wal")
        shm = Path(f"{db}-shm")
        assert wal.is_file() and wal.stat().st_size > 0
        assert shm.is_file() and shm.stat().st_size > 0
        before = {
            path.name: (
                path.read_bytes(),
                os.stat(path).st_size,
                os.stat(path).st_mode,
                os.stat(path).st_mtime_ns,
                os.stat(path).st_ctime_ns,
            )
            for path in (db, wal, shm)
        }

        result = inspect_ag_history(tmp_path / "project", home=home)

        assert result["imported"] is False
        assert result["record_count"] == 0
        assert result["pin"] == "active_wal"
        assert result["reason"] == "ag_history_live_wal_skipped"
        assert result["diagnostics"] == [
            {
                "source": "conversation_summaries.db",
                "reason": "live_wal_readonly_unsupported",
            }
        ]
        after = {
            path.name: (
                path.read_bytes(),
                os.stat(path).st_size,
                os.stat(path).st_mode,
                os.stat(path).st_mtime_ns,
                os.stat(path).st_ctime_ns,
            )
            for path in (db, wal, shm)
        }
        assert after == before
    finally:
        writer.close()


def test_empty_wal_and_stale_shm_are_imported_without_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    wal = Path(f"{db}-wal")
    shm = Path(f"{db}-shm")
    wal.write_bytes(b"")
    shm.write_bytes(b"stale shared-memory index")
    before = {
        path.name: (
            path.read_bytes(),
            path.stat().st_mode,
            path.stat().st_mtime_ns,
            path.stat().st_ctime_ns,
        )
        for path in (db, wal, shm)
    }

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is True
    assert result["record_count"] == 1
    assert result["pin"] == "sqlite-summary-v1"
    after = {
        path.name: (
            path.read_bytes(),
            path.stat().st_mode,
            path.stat().st_mtime_ns,
            path.stat().st_ctime_ns,
        )
        for path in (db, wal, shm)
    }
    assert after == before


def test_live_rollback_journal_is_skipped_without_reading_uncommitted_data(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    writer = sqlite3.connect(db)
    try:
        writer.execute("PRAGMA journal_mode = DELETE")
        writer.execute("PRAGMA cache_size = 1")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "UPDATE conversation_summaries SET step_count = 9999999"
        )
        journal = Path(f"{db}-journal")
        assert journal.is_file() and journal.stat().st_size > 0
        before = {
            path.name: (
                path.read_bytes(),
                os.stat(path).st_size,
                os.stat(path).st_mode,
                os.stat(path).st_mtime_ns,
                os.stat(path).st_ctime_ns,
            )
            for path in (db, journal)
        }

        result = inspect_ag_history(tmp_path / "project", home=home)

        assert result["imported"] is False
        assert result["record_count"] == 0
        assert result["pin"] == "active_journal"
        assert result["reason"] == "ag_history_live_journal_skipped"
        assert result["diagnostics"] == [
            {
                "source": "conversation_summaries.db",
                "reason": "live_journal_readonly_unsupported",
            }
        ]
        after = {
            path.name: (
                path.read_bytes(),
                os.stat(path).st_size,
                os.stat(path).st_mode,
                os.stat(path).st_mtime_ns,
                os.stat(path).st_ctime_ns,
            )
            for path in (db, journal)
        }
        assert after == before
    finally:
        writer.rollback()
        writer.close()


def test_inactive_persistent_journal_is_imported_without_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode = PERSIST").fetchone()[0] == "persist"
        connection.execute("UPDATE conversation_summaries SET step_count = 13")
    journal = Path(f"{db}-journal")
    assert journal.is_file() and journal.stat().st_size > 0
    assert journal.read_bytes()[:28] == b"\0" * 28
    before = {
        path.name: (
            path.read_bytes(),
            os.stat(path).st_size,
            os.stat(path).st_mode,
            os.stat(path).st_mtime_ns,
            os.stat(path).st_ctime_ns,
        )
        for path in (db, journal)
    }

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is True
    assert result["records"][0]["step_count"] == 13
    after = {
        path.name: (
            path.read_bytes(),
            os.stat(path).st_size,
            os.stat(path).st_mode,
            os.stat(path).st_mtime_ns,
            os.stat(path).st_ctime_ns,
        )
        for path in (db, journal)
    }
    assert after == before


def test_workspace_blob_budget_rejects_oversized_cell_without_reading_it() -> None:
    class OversizedBlob:
        def __len__(self) -> int:
            return 81

        def read(self, _size: int) -> bytes:
            raise AssertionError("oversized workspace cell must not be read")

        def close(self) -> None:
            return None

    class FakeConnection:
        def blobopen(
            self, table: str, column: str, rowid: int, *, readonly: bool
        ) -> OversizedBlob:
            assert (table, column, rowid, readonly) == (
                "conversation_summaries",
                "workspace_uris",
                7,
                True,
            )
            return OversizedBlob()

    value, remaining = ag_history._read_workspace_uris(
        FakeConnection(),  # type: ignore[arg-type]
        rowid=7,
        workspace_budget=80,
    )

    assert value is None
    assert remaining == 80


def test_summary_query_requires_a_bounded_ordering_index(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    with sqlite3.connect(db) as connection:
        connection.execute("DROP INDEX idx_conversation_summaries_last_modified_time")

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is False
    assert result["record_count"] == 0
    assert result["pin"] == "unbounded_query"
    assert result["reason"] == "ag_history_unbounded_query_skipped"
    assert result["diagnostics"] == [
        {
            "source": "conversation_summaries.db",
            "reason": "unbounded_sqlite_query_plan",
        }
    ]


def test_custom_summary_labels_are_hashed_not_disclosed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    secret = "customer-acme-secret"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE conversation_summaries SET status = ?, source = ?, agent_name = ?",
            (secret, secret, secret),
        )

    result = inspect_ag_history(tmp_path / "project", home=home)

    row = result["records"][0]
    assert secret not in json.dumps(result)
    assert not ({"status", "source", "agent_name"} & set(row))
    assert row["status_hash"] != row["source_hash"]
    assert row["source_hash"] != row["agent_name_hash"]


def test_boundary_timestamp_overflow_is_omitted(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    boundary = "9999-12-31T23:59:59-23:59"
    with sqlite3.connect(db) as connection:
        connection.execute(
            """UPDATE conversation_summaries
                  SET last_modified_time = ?, last_user_input_time = ?""",
            (boundary, boundary),
        )

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is True
    assert result["records"] == []


def test_lone_surrogate_workspace_uri_is_not_hashed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE conversation_summaries SET workspace_uris = ?",
            (r'["\ud800"]',),
        )

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is True
    row = result["records"][0]
    assert row["workspace_count"] == 1
    assert row["workspace_hashes"] == []


def test_dynamic_numeric_blobs_are_rejected_inside_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    with sqlite3.connect(db) as connection:
        connection.execute(
            """UPDATE conversation_summaries
                  SET step_count = zeroblob(4096),
                      nesting_depth = zeroblob(4096),
                      killed = zeroblob(4096),
                      last_user_input_step_index = zeroblob(4096)"""
        )
    original_public_row = ag_history._public_row

    def _require_bounded_scalars(
        row: sqlite3.Row, *, workspace_uris: str | None
    ) -> dict[str, Any]:
        for name in (
            "step_count",
            "nesting_depth",
            "killed",
            "last_user_input_step_index",
        ):
            assert type(row[name]) is int
        return original_public_row(row, workspace_uris=workspace_uris)

    monkeypatch.setattr(ag_history, "_public_row", _require_bounded_scalars)

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is True
    row = result["records"][0]
    assert row["step_count"] == 0
    assert row["nesting_depth"] == 0
    assert row["killed"] is False
    assert row["last_user_input_step_index"] == -1


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_sqlite_sidecar_symlink_is_rejected(tmp_path: Path, suffix: str) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    target = tmp_path / "outside-sidecar"
    target.write_bytes(b"private")
    Path(f"{db}{suffix}").symlink_to(target)

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is False
    assert result["pin"] == "unsafe_path"
    assert result["diagnostics"] == [
        {"source": "conversation_summaries.db", "reason": "unsafe_path_skipped"}
    ]


def test_source_change_during_read_discards_all_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    original = ag_history._public_row

    def _row_then_change(
        row: sqlite3.Row, *, workspace_uris: str | None
    ) -> dict[str, Any]:
        public = original(row, workspace_uris=workspace_uris)
        info = db.stat()
        with db.open("r+b") as stream:
            stream.seek(68)
            current = stream.read(1)
            assert current
            stream.seek(68)
            stream.write(bytes([current[0] ^ 1]))
            stream.flush()
            os.fsync(stream.fileno())
        os.utime(db, ns=(info.st_atime_ns, info.st_mtime_ns))
        assert db.stat().st_size == info.st_size
        assert db.stat().st_mtime_ns == info.st_mtime_ns
        return public

    monkeypatch.setattr(ag_history, "_public_row", _row_then_change)

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is False
    assert result["record_count"] == 0
    assert result["pin"] == "unstable_source"
    assert result["reason"] == "ag_history_source_changed"
    assert result["diagnostics"] == [
        {"source": "conversation_summaries.db", "reason": "sqlite_source_changed"}
    ]


def test_rotated_database_with_active_wal_discards_all_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    with sqlite3.connect(db) as connection:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone()[0] == "wal"
    assert not Path(f"{db}-wal").exists()
    rotated = db.with_name("rotated.db")
    original = ag_history._public_row
    writers: list[sqlite3.Connection] = []

    def _row_then_rotate(
        row: sqlite3.Row, *, workspace_uris: str | None
    ) -> dict[str, Any]:
        public = original(row, workspace_uris=workspace_uris)
        db.rename(rotated)
        writer = sqlite3.connect(rotated)
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        writer.execute(
            "UPDATE conversation_summaries SET step_count = step_count + 1"
        )
        writer.commit()
        assert Path(f"{rotated}-wal").is_file()
        writers.append(writer)
        return public

    monkeypatch.setattr(ag_history, "_public_row", _row_then_rotate)
    try:
        result = inspect_ag_history(tmp_path / "project", home=home)
    finally:
        for writer in writers:
            writer.close()

    assert result["imported"] is False
    assert result["record_count"] == 0
    assert result["pin"] == "unstable_source"
    assert result["diagnostics"] == [
        {"source": "conversation_summaries.db", "reason": "sqlite_source_changed"}
    ]


def test_wal_appearing_during_read_discards_all_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    original = ag_history._public_row

    def _row_then_create_wal(
        row: sqlite3.Row, *, workspace_uris: str | None
    ) -> dict[str, Any]:
        public = original(row, workspace_uris=workspace_uris)
        Path(f"{db}-wal").write_bytes(b"active")
        return public

    monkeypatch.setattr(ag_history, "_public_row", _row_then_create_wal)

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is False
    assert result["record_count"] == 0
    assert result["pin"] == "active_wal"
    assert result["diagnostics"] == [
        {
            "source": "conversation_summaries.db",
            "reason": "live_wal_readonly_unsupported",
        }
    ]


def test_secure_descriptor_open_unavailable_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _summary_db(home)
    monkeypatch.setattr(ag_history, "_descriptor_open_ready", lambda: False)

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is False
    assert result["pin"] == "unsupported_platform"
    assert result["diagnostics"] == [
        {
            "source": "conversation_summaries.db",
            "reason": "secure_sqlite_open_unavailable",
        }
    ]


def test_legacy_version_marker_content_is_never_disclosed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    secret = "PRIVATE-AUTH-TOKEN-123456"
    (root / "VERSION").write_text(secret, encoding="utf-8")

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["present"] is True
    assert result["supported"] is False
    assert result["pin"] == "unknown_version"
    assert result["private_content"] is False
    dumped = json.dumps(result)
    assert secret not in dumped
    assert "marker-present" in result["versions"]


def test_legacy_version_marker_parent_symlink_is_never_followed(
    tmp_path: Path,
) -> None:
    real_home = tmp_path / "real-home"
    root = real_home / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    secret = "PRIVATE-EXTERNAL-MARKER"
    (root / "VERSION").write_text(secret, encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gemini").symlink_to(
        real_home / ".gemini", target_is_directory=True
    )

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["present"] is False
    assert result["supported"] is False
    assert result["versions"] == []
    assert secret not in json.dumps(result)


def test_workspace_uri_import_has_per_row_and_aggregate_bounds(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    bounded_large = json.dumps(["file:///" + ("private-segment/" * 430)])
    with sqlite3.connect(db) as conn:
        template = conn.execute(
            "SELECT * FROM conversation_summaries LIMIT 1"
        ).fetchone()
        assert template is not None
        for index in range(170):
            conn.execute(
                """INSERT INTO conversation_summaries
                   (conversation_id, title, preview, step_count, last_modified_time,
                    workspace_uris, status, source, project_id, agent_name,
                    parent_conversation_id, nesting_depth, killed,
                    last_user_input_time, last_user_input_step_index, app_data_dir)
                   VALUES (?, '', '', 1, ?, ?, 'idle', 'antigravity', '', '',
                           'parent', 0, 0, ?, 0, '')""",
                (
                    f"conversation-{index}",
                    f"2026-08-30T23:{index % 60:02d}:00Z",
                    bounded_large,
                    "2026-08-30T23:00:00Z",
                ),
            )

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["record_count"] == 171
    imported_workspace_counts = sum(
        row["workspace_count"] for row in result["records"]
    )
    assert 1 < imported_workspace_counts < 171
    assert "private-segment" not in json.dumps(result)


def test_workspace_uri_budget_is_shared_across_all_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_home = tmp_path / "project-home"
    project_db = _summary_db(project_home)
    project_root = tmp_path / "project"
    local_dir = project_root / ".antigravity"
    local_dir.mkdir(parents=True)
    local_db = local_dir / "conversation_summaries.db"
    local_db.write_bytes(project_db.read_bytes())
    home = tmp_path / "home"
    home_db = _summary_db(home)
    payload = json.dumps(["file:///" + ("x" * 48)])
    assert len(payload.encode("utf-8")) < 80
    for db in (local_db, home_db):
        with sqlite3.connect(db) as conn:
            conn.execute(
                "UPDATE conversation_summaries SET workspace_uris = ?", (payload,)
            )
    monkeypatch.setattr(ag_history, "MAX_WORKSPACE_URI_AGGREGATE_BYTES", 80)

    result = inspect_ag_history(project_root, home=home)

    assert result["record_count"] == 2
    assert sum(row["workspace_count"] for row in result["records"]) == 1


def _shadowed_rowid_summary_db(home: Path, *, alias: str) -> Path:
    root = home / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "conversation_summaries.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            f"""
            PRAGMA user_version = 1;
            CREATE TABLE conversation_summaries (
              conversation_id TEXT PRIMARY KEY,
              title TEXT NOT NULL DEFAULT '',
              preview TEXT NOT NULL DEFAULT '',
              step_count INTEGER NOT NULL DEFAULT 0,
              last_modified_time DATETIME NOT NULL,
              workspace_uris TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT '',
              project_id TEXT NOT NULL DEFAULT '',
              agent_name TEXT NOT NULL DEFAULT '',
              parent_conversation_id TEXT NOT NULL DEFAULT '',
              nesting_depth INTEGER NOT NULL DEFAULT 0,
              battle_id TEXT NOT NULL DEFAULT '',
              winning_conversation_id TEXT NOT NULL DEFAULT '',
              not_fully_idle NUMERIC NOT NULL DEFAULT false,
              killed NUMERIC NOT NULL DEFAULT false,
              last_user_input_time DATETIME NOT NULL,
              last_user_input_step_index INTEGER NOT NULL DEFAULT -1,
              app_data_dir TEXT NOT NULL DEFAULT '',
              "{alias}" TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX idx_conversation_summaries_last_modified_time_rowid
              ON conversation_summaries(last_modified_time, "{alias}");
            """
        )
        conn.execute(
            f"""INSERT INTO conversation_summaries
               (conversation_id, title, preview, step_count, last_modified_time,
                workspace_uris, status, source, project_id, agent_name,
                parent_conversation_id, nesting_depth, killed,
                last_user_input_time, last_user_input_step_index, app_data_dir,
                "{alias}")
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "conversation-secret-id",
                "RAW PRIVATE TITLE",
                "RAW PRIVATE PREVIEW",
                12,
                "2026-08-31T00:00:00Z",
                json.dumps(["file:///Users/private/secret-project"]),
                "idle",
                "antigravity",
                "PRIVATE PROJECT",
                "omg-executor",
                "parent-secret-id",
                1,
                0,
                "2026-08-31T00:00:00Z",
                9,
                "/Users/private/.gemini",
                "abc",
            ),
        )
    return path


def test_generated_rowid_column_is_rejected(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    path = root / "conversation_summaries.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA user_version = 1;
            CREATE TABLE conversation_summaries (
              conversation_id TEXT PRIMARY KEY,
              title TEXT NOT NULL DEFAULT '',
              preview TEXT NOT NULL DEFAULT '',
              step_count INTEGER NOT NULL DEFAULT 0,
              last_modified_time DATETIME NOT NULL,
              workspace_uris TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT '',
              source TEXT NOT NULL DEFAULT '',
              project_id TEXT NOT NULL DEFAULT '',
              agent_name TEXT NOT NULL DEFAULT '',
              parent_conversation_id TEXT NOT NULL DEFAULT '',
              nesting_depth INTEGER NOT NULL DEFAULT 0,
              killed NUMERIC NOT NULL DEFAULT false,
              last_user_input_time DATETIME NOT NULL,
              last_user_input_step_index INTEGER NOT NULL DEFAULT -1,
              app_data_dir TEXT NOT NULL DEFAULT '',
              rowid INTEGER GENERATED ALWAYS AS (1) VIRTUAL
            );
            CREATE INDEX idx_conversation_summaries_last_modified_time
              ON conversation_summaries(last_modified_time);
            """
        )
        conn.execute(
            """INSERT INTO conversation_summaries
               (conversation_id, title, preview, step_count, last_modified_time,
                workspace_uris, status, source, project_id, agent_name,
                parent_conversation_id, nesting_depth, killed,
                last_user_input_time, last_user_input_step_index, app_data_dir)
               VALUES ('c', '', '', 1, '2026-08-31T00:00:00Z', '[]', 'idle',
                       'antigravity', '', '', '', 0, 0, '2026-08-31T00:00:00Z', 0, '')"""
        )

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is False
    assert any(
        item["reason"] == "unsupported_schema_version" for item in result["diagnostics"]
    )


def test_impossible_canonical_timestamp_is_omitted(tmp_path: Path) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE conversation_summaries SET last_modified_time = ?",
            ("9999-99-99T99:99:99Z",),
        )

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is True
    assert result["records"] == []


@pytest.mark.parametrize("alias", ("rowid", "_rowid_", "oid", "ROWID"))
def test_declared_rowid_alias_returns_bounded_diagnostic(
    tmp_path: Path, alias: str
) -> None:
    home = tmp_path / "home"
    db = _shadowed_rowid_summary_db(home, alias=alias)
    before = db.read_bytes()

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is False
    assert result["supported"] is False
    assert result["record_count"] == 0
    assert result["records"] == []
    assert result["pin"] == "unknown_version"
    assert any(
        item["reason"] == "unsupported_schema_version" for item in result["diagnostics"]
    )
    dumped = json.dumps(result)
    assert "RAW PRIVATE TITLE" not in dumped
    assert "conversation-secret-id" not in dumped
    assert db.read_bytes() == before


def test_basic_iso_utc_timestamps_are_excluded_from_canonical_order(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    db = _summary_db(home)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE conversation_summaries SET last_modified_time = ?, step_count = ?",
            ("20260830T230000Z", 1),
        )
        conn.execute(
            """INSERT INTO conversation_summaries
               (conversation_id, title, preview, step_count, last_modified_time,
                workspace_uris, status, source, project_id, agent_name,
                parent_conversation_id, nesting_depth, killed,
                last_user_input_time, last_user_input_step_index, app_data_dir)
               VALUES ('newer-extended', '', '', 9, '2026-09-01T00:00:00Z',
                       '[]', 'idle', 'antigravity', '', '', '', 0, 0,
                       '2026-09-01T00:00:00Z', 0, '')"""
        )

    result = inspect_ag_history(tmp_path / "project", home=home)

    steps = [row["step_count"] for row in result["records"]]
    assert 9 in steps
    assert 1 not in steps


def test_workspace_pass_wal_is_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _summary_db(home)
    real = ag_history._open_summary_db
    calls = {"n": 0}

    @contextmanager
    def wrap(path: Path):
        calls["n"] += 1
        if calls["n"] > 1:
            raise ag_history.AgHistoryError(
                "active Antigravity WAL state cannot be imported without mutation",
                code="E_AG_HISTORY_WAL_ACTIVE",
            )
        with real(path) as value:
            yield value

    monkeypatch.setattr(ag_history, "_open_summary_db", wrap)
    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is False
    assert result["pin"] == "active_wal"
    assert result["records"] == []


def test_workspace_pass_rejects_changed_source_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    _summary_db(home)
    real = ag_history._open_summary_db
    calls = {"n": 0}

    @contextmanager
    def wrap(path: Path):
        calls["n"] += 1
        with real(path) as (connection, identity):
            if calls["n"] > 1:
                identity = (identity[0] + 1, *identity[1:])
            yield connection, identity

    monkeypatch.setattr(ag_history, "_open_summary_db", wrap)
    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["imported"] is False
    assert result["pin"] == "unstable_source"
    assert result["records"] == []


def test_offset_timestamps_are_excluded_from_sortable_utc_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ag_history, "MAX_AG_HISTORY_RECORDS", 1)
    home = tmp_path / "home"
    db = _summary_db(home)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE conversation_summaries SET last_modified_time = ?, step_count = ?",
            ("2026-08-31T23:30:00-02:00", 7),
        )
        conn.execute(
            """INSERT INTO conversation_summaries
               (conversation_id, title, preview, step_count, last_modified_time,
                workspace_uris, status, source, project_id, agent_name,
                parent_conversation_id, nesting_depth, killed,
                last_user_input_time, last_user_input_step_index, app_data_dir)
               VALUES ('older-utc', '', '', 3, '2026-09-01T00:30:00Z',
                       '[]', 'idle', 'antigravity', '', '', '', 0, 0,
                       '2026-09-01T00:30:00Z', 0, '')"""
        )

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["record_count"] == 1
    assert result["records"][0]["step_count"] == 3


def test_workspace_budget_is_spent_only_on_retained_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ag_history, "MAX_AG_HISTORY_RECORDS", 1)
    monkeypatch.setattr(ag_history, "MAX_WORKSPACE_URI_AGGREGATE_BYTES", 80)
    project_root = tmp_path / "project"
    local_dir = project_root / ".antigravity"
    local_dir.mkdir(parents=True)
    local_db = local_dir / "conversation_summaries.db"
    local_db.write_bytes(_summary_db(tmp_path / "project-home").read_bytes())
    large = json.dumps(["file:///" + ("x" * 48)])
    small = json.dumps(["file:///n"])
    with sqlite3.connect(local_db) as conn:
        conn.execute(
            "UPDATE conversation_summaries SET last_modified_time = ?, workspace_uris = ?",
            ("2026-01-01T00:00:00Z", large),
        )
    home = tmp_path / "home"
    home_db = _summary_db(home)
    with sqlite3.connect(home_db) as conn:
        conn.execute(
            "UPDATE conversation_summaries SET last_modified_time = ?, workspace_uris = ?, step_count = ?",
            ("2026-08-31T12:00:00Z", small, 99),
        )

    result = inspect_ag_history(project_root, home=home)

    assert result["record_count"] == 1
    assert result["records"][0]["step_count"] == 99
    assert result["records"][0]["workspace_count"] == 1


def test_newest_records_are_merged_across_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ag_history, "MAX_AG_HISTORY_RECORDS", 1)
    project_root = tmp_path / "project"
    local_dir = project_root / ".antigravity"
    local_dir.mkdir(parents=True)
    local_home = tmp_path / "project-home"
    local_db = local_dir / "conversation_summaries.db"
    local_db.write_bytes(_summary_db(local_home).read_bytes())
    with sqlite3.connect(local_db) as conn:
        conn.execute(
            "UPDATE conversation_summaries SET last_modified_time = ?, step_count = ?",
            ("2026-01-01T00:00:00Z", 1),
        )
    home = tmp_path / "home"
    home_db = _summary_db(home)
    with sqlite3.connect(home_db) as conn:
        conn.execute(
            "UPDATE conversation_summaries SET last_modified_time = ?, step_count = ?",
            ("2026-08-31T12:00:00Z", 99),
        )

    result = inspect_ag_history(project_root, home=home)

    assert result["record_count"] == 1
    assert result["records"][0]["step_count"] == 99


def test_legacy_markers_are_discovered_without_secure_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    root = home / ".gemini" / "antigravity-cli"
    root.mkdir(parents=True)
    secret = "PRIVATE-AUTH-TOKEN-123456"
    (root / "VERSION").write_text(secret, encoding="utf-8")
    monkeypatch.setattr(ag_history, "_descriptor_open_ready", lambda: False)

    result = inspect_ag_history(tmp_path / "project", home=home)

    assert result["present"] is True
    assert result["imported"] is False
    assert result["supported"] is False
    assert result["reason"] != "ag_history_absent"
    assert result["pin"] == "unknown_version"
    dumped = json.dumps(result)
    assert secret not in dumped
    assert "ag_history_absent" not in dumped


def test_project_local_db_reports_secure_open_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_root = tmp_path / "project"
    local_dir = project_root / ".antigravity"
    local_dir.mkdir(parents=True)
    local_db = local_dir / "conversation_summaries.db"
    local_db.write_bytes(_summary_db(tmp_path / "seed-home").read_bytes())
    monkeypatch.setattr(ag_history, "_descriptor_open_ready", lambda: False)

    result = inspect_ag_history(project_root, home=tmp_path / "missing-home")

    assert result["present"] is True
    assert result["imported"] is False
    assert result["pin"] == "unsupported_platform"
    assert result["reason"] == "ag_history_secure_open_unavailable"
    assert result["diagnostics"] == [
        {
            "source": "conversation_summaries.db",
            "reason": "secure_sqlite_open_unavailable",
        }
    ]
    assert result["reason"] != "ag_history_absent"
    assert "ag_history_absent" not in json.dumps(result)
