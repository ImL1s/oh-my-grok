from __future__ import annotations

import json
from pathlib import Path

import pytest

from omg_cli.team.mailbox import (
    MailboxError,
    ack_message,
    list_messages,
    read_message,
    send_message,
)
from omg_cli.team.notify import NotifyError, mark_notified, notify_cursor_path


RUN = "run-mailbox"
TEAM = "team-mailbox"
RECIPIENT = "worker-1"
STAMP = "2026-07-22T00:00:00Z"


def _send(root: Path, key: str, *, generation: int = 0, body: object | None = None):
    return send_message(
        root,
        run_id=RUN,
        team_id=TEAM,
        sender_id="leader",
        recipient_id=RECIPIENT,
        generation=generation,
        kind="instruction",
        body={"value": key} if body is None else body,
        dedupe_key=key,
        sent_at=STAMP,
    )


def test_mailbox_orders_lists_reads_and_acks_without_read_side_effect(
    tmp_path: Path,
) -> None:
    first = _send(tmp_path, "d1")
    second = _send(tmp_path, "d2")
    assert (first["sequence"], second["sequence"]) == (0, 1)

    listing = list_messages(
        tmp_path,
        run_id=RUN,
        team_id=TEAM,
        recipient_id=RECIPIENT,
    )
    assert [item["message_id"] for item in listing["messages"]] == [
        first["message_id"],
        second["message_id"],
    ]
    assert listing["ack_cursor"] == "start"
    assert read_message(
        tmp_path,
        run_id=RUN,
        team_id=TEAM,
        recipient_id=RECIPIENT,
        message_id=first["message_id"],
        generation=0,
    )["body"] == {"value": "d1"}
    assert (
        list_messages(
            tmp_path,
            run_id=RUN,
            team_id=TEAM,
            recipient_id=RECIPIENT,
        )["ack_cursor"]
        == "start"
    )

    ack = ack_message(
        tmp_path,
        run_id=RUN,
        team_id=TEAM,
        recipient_id=RECIPIENT,
        message_id=first["message_id"],
        expected_cursor="start",
        generation=0,
    )
    assert ack == {
        "message_id": first["message_id"],
        "ack_cursor": "m000000000000",
        "duplicate": False,
    }
    retry = ack_message(
        tmp_path,
        run_id=RUN,
        team_id=TEAM,
        recipient_id=RECIPIENT,
        message_id=first["message_id"],
        expected_cursor="start",
        generation=0,
    )
    assert retry["duplicate"] is True


def test_mailbox_dedupe_conflict_skip_and_generation_fences(tmp_path: Path) -> None:
    first = _send(tmp_path, "same")
    assert _send(tmp_path, "same")["duplicate"] is True
    with pytest.raises(MailboxError, match="different content"):
        _send(tmp_path, "same", body={"changed": True})

    second = _send(tmp_path, "next", generation=1)
    rollover = ack_message(
        tmp_path,
        run_id=RUN,
        team_id=TEAM,
        recipient_id=RECIPIENT,
        message_id=second["message_id"],
        expected_cursor="start",
        generation=1,
    )
    assert rollover["ack_cursor"] == "m000000000001"
    assert rollover["superseded"] == 1
    with pytest.raises(MailboxError, match="stale generation"):
        read_message(
            tmp_path,
            run_id=RUN,
            team_id=TEAM,
            recipient_id=RECIPIENT,
            message_id=first["message_id"],
            generation=1,
        )


def test_mailbox_generation_filter_cursor_recovery_and_redaction(
    tmp_path: Path,
) -> None:
    secret_message = _send(
        tmp_path,
        "secret",
        body={"token": "raw-secret", "safe": "Authorization: Bearer abc"},
    )
    newest = _send(tmp_path, "g1", generation=1)
    filtered = list_messages(
        tmp_path,
        run_id=RUN,
        team_id=TEAM,
        recipient_id=RECIPIENT,
        generation=1,
        limit=1,
    )
    assert [item["message_id"] for item in filtered["messages"]] == [
        newest["message_id"]
    ]
    assert filtered["has_more"] is False

    secret = read_message(
        tmp_path,
        run_id=RUN,
        team_id=TEAM,
        recipient_id=RECIPIENT,
        message_id=secret_message["message_id"],
    )
    encoded = json.dumps(secret)
    assert "raw-secret" not in encoded
    assert "Bearer abc" not in encoded
    assert "[REDACTED]" in encoded

    resumed = list_messages(
        tmp_path,
        run_id=RUN,
        team_id=TEAM,
        recipient_id=RECIPIENT,
        after="m000000000000",
    )
    assert [row["sequence"] for row in resumed["messages"]] == [1]
    with pytest.raises(MailboxError, match="future"):
        list_messages(
            tmp_path,
            run_id=RUN,
            team_id=TEAM,
            recipient_id=RECIPIENT,
            after="m999999999999",
        )
    with pytest.raises(MailboxError, match="12 digits"):
        list_messages(
            tmp_path,
            run_id=RUN,
            team_id=TEAM,
            recipient_id=RECIPIENT,
            after="m+00000000000",
        )


def test_generation_rollover_exposes_ordered_recoverable_ack_path(
    tmp_path: Path,
) -> None:
    stale = _send(tmp_path, "old", generation=0)
    current = _send(tmp_path, "current", generation=1)
    listing = list_messages(
        tmp_path,
        run_id=RUN,
        team_id=TEAM,
        recipient_id=RECIPIENT,
        generation=1,
    )
    assert [item["message_id"] for item in listing["messages"]] == [
        current["message_id"]
    ]
    assert [item["message_id"] for item in listing["ack_path"]] == [
        stale["message_id"],
        current["message_id"],
    ]
    ack = ack_message(
        tmp_path,
        run_id=RUN,
        team_id=TEAM,
        recipient_id=RECIPIENT,
        message_id=current["message_id"],
        expected_cursor="start",
        generation=1,
    )
    assert ack["superseded"] == 1
    assert ack["ack_cursor"] == current["cursor"]
    retry = ack_message(
        tmp_path,
        run_id=RUN,
        team_id=TEAM,
        recipient_id=RECIPIENT,
        message_id=current["message_id"],
        expected_cursor="start",
        generation=1,
    )
    assert retry["duplicate"] is True
    assert retry["ack_cursor"] == current["cursor"]


def test_mailbox_rollover_never_skips_same_generation_message(tmp_path: Path) -> None:
    first = _send(tmp_path, "g1-first", generation=1)
    second = _send(tmp_path, "g1-second", generation=1)
    with pytest.raises(MailboxError, match="skip"):
        ack_message(
            tmp_path,
            run_id=RUN,
            team_id=TEAM,
            recipient_id=RECIPIENT,
            message_id=second["message_id"],
            expected_cursor="start",
            generation=1,
        )
    assert first["sequence"] == 0


_MAILBOX_V1_KEYS = {
    "store_kind",
    "schema_version",
    "writer",
    "run_id",
    "team_id",
    "recipient_id",
    "next_sequence",
    "ack_cursor",
    "messages",
    "dedupe",
}


def _mailbox_doc(root: Path) -> tuple[Path, dict]:
    for path in root.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(data, dict) and data.get("store_kind") == "team_mailbox":
            return path, data
    raise AssertionError("mailbox v1 document missing")


def test_mark_notified_uses_separate_store_and_keeps_mailbox_v1_keys(
    tmp_path: Path,
) -> None:
    first = _send(tmp_path, "n1")
    second = _send(tmp_path, "n2")
    path, before = _mailbox_doc(tmp_path)
    assert set(before) == _MAILBOX_V1_KEYS
    with pytest.raises(NotifyError, match="skip"):
        mark_notified(
            tmp_path,
            run_id=RUN,
            team_id=TEAM,
            recipient_id=RECIPIENT,
            message_id=second["message_id"],
        )
    marked = mark_notified(
        tmp_path,
        run_id=RUN,
        team_id=TEAM,
        recipient_id=RECIPIENT,
        message_id=first["message_id"],
    )
    assert marked["duplicate"] is False
    assert marked["sequence"] == 0
    after = json.loads(path.read_text(encoding="utf-8"))
    assert set(after) == _MAILBOX_V1_KEYS
    assert after["ack_cursor"] == before["ack_cursor"]
    cursor_path = notify_cursor_path(tmp_path, RUN, TEAM, RECIPIENT)
    assert cursor_path.is_file()
    notify_doc = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert notify_doc["store_kind"] == "team_mailbox_notify"
    assert notify_doc["notify_cursor"] == 0
    retry = mark_notified(
        tmp_path,
        run_id=RUN,
        team_id=TEAM,
        recipient_id=RECIPIENT,
        message_id=first["message_id"],
    )
    assert retry["duplicate"] is True
    sequential = mark_notified(
        tmp_path,
        run_id=RUN,
        team_id=TEAM,
        recipient_id=RECIPIENT,
        message_id=second["message_id"],
    )
    assert sequential["sequence"] == 1
    assert sequential["duplicate"] is False

