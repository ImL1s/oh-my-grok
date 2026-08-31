"""Read-only Antigravity conversation-summary import contract (#74)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

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
