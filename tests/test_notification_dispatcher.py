"""Optional notification adapters never become core execution authority."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from threading import Thread

import pytest

from omg_cli.notify import (
    create_notification_event,
    dispatch_notifications,
    enqueue_lifecycle_notification,
    enqueue_notification,
    format_notification,
    notification_from_lifecycle,
    process_notification_queue,
)


OWNER = {
    "owner_id": "owner",
    "generation": 1,
    "owner_nonce": "owner-nonce-123456",
}


def _event():
    return create_notification_event(
        severity="info",
        title="State",
        message="Core remains available token=private",
        created_at="2026-07-22T00:00:00Z",
        **OWNER,
    )


def test_notification_event_is_bounded_redacted_and_nonce_free():
    event = _event()
    assert event["store_kind"] == "omg_notification_event"
    assert "private" not in str(event)
    assert OWNER["owner_nonce"] not in str(event)
    assert len(event["event_id"]) == 64

    control_safe = create_notification_event(
        severity="warning",
        title="unsafe\x1b]0;title\x07",
        message="line\nnext\x9bcontrol",
        created_at="2026-07-22T00:00:00Z",
        **OWNER,
    )
    assert not any(
        ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F
        for field in (control_safe["title"], control_safe["message"])
        for char in field
    )


def test_disabled_and_failed_adapters_are_outcomes_not_exceptions():
    terminal_writes: list[str] = []
    config = {
        "store_kind": "omg_notification_config",
        "schema_version": 1,
        "enabled": True,
        "adapters": [
            {"adapter": "terminal", "enabled": False},
            {
                "adapter": "https",
                "enabled": True,
                "url_env": "OMG_NOTIFY_URL",
                "allowed_hosts": ["hooks.acme.example.net"],
                "timeout_ms": 1000,
                "header_env": {},
            },
        ],
    }
    outcomes = dispatch_notifications(
        _event(),
        config,
        owner=OWNER,
        environ={"OMG_NOTIFY_URL": "https://hooks.acme.example.net/omg"},
        terminal_writer=lambda line: terminal_writes.append(line) or True,
        https_resolver=lambda _host: (_ for _ in ()).throw(OSError("network unavailable")),
    )
    assert [row["status"] for row in outcomes] == ["skipped", "failed"]
    assert outcomes[1]["code"] == "HTTPS_DNS_FAILED"
    assert terminal_writes == []


def test_global_disabled_config_invokes_no_adapter():
    called: list[str] = []
    outcomes = dispatch_notifications(
        _event(),
        {
            "store_kind": "omg_notification_config",
            "schema_version": 1,
            "enabled": False,
            "adapters": [{"adapter": "terminal", "enabled": False}],
        },
        terminal_writer=lambda _line: called.append("terminal") or True,
    )
    assert outcomes == []
    assert called == []


def test_terminal_delivery_requires_current_pid_start_marker_tty_and_owner():
    import os

    writes: list[str] = []
    config = {
        "store_kind": "omg_notification_config",
        "schema_version": 1,
        "enabled": True,
        "adapters": [
            {
                "adapter": "terminal",
                "enabled": True,
                "pid": os.getpid(),
                "start_marker": "start",
                "tty": "ttys999",
                "stderr_dev": 42,
                "stderr_ino": 99,
            }
        ],
    }
    delivered = dispatch_notifications(
        _event(),
        config,
        owner=OWNER,
        terminal_inspector=lambda _pid: {
            "pid": os.getpid(),
            "start_marker": "start",
            "tty": "ttys999",
            "stderr_dev": 42,
            "stderr_ino": 99,
        },
        terminal_writer=lambda line: writes.append(line) or True,
    )
    assert delivered[0]["code"] == "TERMINAL_DELIVERED"
    assert writes == ["[OMG INFO] State: Core remains available token=[REDACTED]\n"]

    refused = dispatch_notifications(
        _event(),
        config,
        owner=OWNER,
        terminal_inspector=lambda _pid: {
            "pid": os.getpid(),
            "start_marker": "other",
            "tty": "ttys999",
            "stderr_dev": 42,
            "stderr_ino": 99,
        },
        terminal_writer=lambda line: writes.append(line),
    )
    assert refused[0]["code"] == "TERMINAL_IDENTITY_MISMATCH"


def test_default_terminal_inspector_binds_actual_stderr_tty_device(monkeypatch):
    import os
    from omg_cli.notify import dispatcher

    class Stderr:
        @staticmethod
        def isatty():
            return True

        @staticmethod
        def fileno():
            return 17

    monkeypatch.setattr(dispatcher.sys, "stderr", Stderr())
    monkeypatch.setattr(dispatcher.os, "ttyname", lambda fd: "/dev/ttys017")
    monkeypatch.setattr(dispatcher.os, "fstat", lambda fd: SimpleNamespace(st_dev=8, st_ino=17))
    monkeypatch.setattr(
        dispatcher.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="Wed Jul 22 00:00:00 2026\n"
        ),
    )
    assert dispatcher._inspect_terminal(os.getpid()) == {
        "pid": os.getpid(),
        "start_marker": "Wed Jul 22 00:00:00 2026",
        "tty": "/dev/ttys017",
        "stderr_dev": 8,
        "stderr_ino": 17,
    }


def test_tmux_notification_requires_env_backed_producer_nonces():
    calls: list[list[str]] = []
    config = {
        "store_kind": "omg_notification_config",
        "schema_version": 1,
        "enabled": True,
        "adapters": [
            {
                "adapter": "tmux",
                "enabled": True,
                "session_name": "team",
                "pane_id": "%9",
                "owner_nonce_env": "OMG_OWNER_NONCE",
                "worker_nonce_env": "OMG_WORKER_NONCE",
            }
        ],
    }
    outcomes = dispatch_notifications(
        _event(),
        config,
        owner=OWNER,
        environ={},
        tmux_runner=lambda argv: calls.append(list(argv)),
    )
    assert outcomes[0]["code"] == "TMUX_NONCE_MISSING"
    assert calls == []


def test_disabled_https_does_not_require_header_environment():
    config = {
        "store_kind": "omg_notification_config",
        "schema_version": 1,
        "enabled": True,
        "adapters": [
            {
                "adapter": "https",
                "enabled": False,
                "url_env": "OMG_NOTIFY_URL",
                "allowed_hosts": ["hooks.acme.example.net"],
                "timeout_ms": 1000,
                "header_env": {"authorization": "OMG_NOTIFY_AUTH"},
            }
        ],
    }
    outcomes = dispatch_notifications(_event(), config, owner=OWNER, environ={})
    assert outcomes[0]["code"] == "HTTPS_DISABLED"


def test_tmux_delivery_outcome_is_bound_to_notification_event():
    replies = iter(
        [
            {"returncode": 0, "stdout": "team\t%9", "stderr": ""},
            {"returncode": 0, "stdout": OWNER["owner_nonce"], "stderr": ""},
            {"returncode": 0, "stdout": "worker-nonce-12345", "stderr": ""},
            {"returncode": 0, "stdout": "/dev/ttys001\tteam", "stderr": ""},
            {"returncode": 0, "stdout": "", "stderr": ""},
        ]
    )
    config = {
        "store_kind": "omg_notification_config",
        "schema_version": 1,
        "enabled": True,
        "adapters": [
            {
                "adapter": "tmux",
                "enabled": True,
                "session_name": "team",
                "pane_id": "%9",
                "owner_nonce_env": "OMG_OWNER_NONCE",
                "worker_nonce_env": "OMG_WORKER_NONCE",
            }
        ],
    }
    event = _event()
    outcomes = dispatch_notifications(
        event,
        config,
        owner=OWNER,
        environ={
            "OMG_OWNER_NONCE": OWNER["owner_nonce"],
            "OMG_WORKER_NONCE": "worker-nonce-12345",
        },
        tmux_runner=lambda _argv: next(replies),
    )
    assert outcomes[0]["code"] == "TMUX_DELIVERED"
    assert outcomes[0]["event_id"] == event["event_id"]


def test_tampered_event_fails_before_terminal_or_tmux_delivery():
    import os

    calls: list[object] = []
    terminal = {
        "adapter": "terminal",
        "enabled": True,
        "pid": os.getpid(),
        "start_marker": "start",
        "tty": "/dev/ttys999",
        "stderr_dev": 42,
        "stderr_ino": 99,
    }
    tmux = {
        "adapter": "tmux",
        "enabled": True,
        "session_name": "team",
        "pane_id": "%9",
        "owner_nonce_env": "OMG_OWNER_NONCE",
        "worker_nonce_env": "OMG_WORKER_NONCE",
    }
    for changed in (
        {**_event(), "message": "tampered"},
        {**_event(), "owner": {**_event()["owner"], "generation": 2}},
    ):
        outcome = dispatch_notifications(
            changed,
            {
                "store_kind": "omg_notification_config",
                "schema_version": 1,
                "enabled": True,
                "adapters": [terminal, tmux],
            },
            owner=OWNER,
            environ={
                "OMG_OWNER_NONCE": OWNER["owner_nonce"],
                "OMG_WORKER_NONCE": "worker-nonce-12345",
            },
            terminal_writer=lambda line: calls.append(line),
            tmux_runner=lambda argv: calls.append(list(argv)),
        )
        assert outcome[0]["code"] == "NOTIFICATION_EVENT_REJECTED"
    assert calls == []


def test_lifecycle_mapping_has_stable_dedupe_and_pure_vendor_formatters():
    lifecycle = {
        "event_type": "permission_denied",
        "event_id": "permission-17",
        "observed_at": "2026-07-22T00:00:00Z",
        "payload": {"raw_command": "rm -rf /", "token": "private"},
    }
    first = notification_from_lifecycle(lifecycle, **OWNER)
    second = notification_from_lifecycle(lifecycle, **OWNER)
    assert first == second
    assert first is not None
    assert first["dedupe_key"] == second["dedupe_key"]
    assert "rm -rf" not in str(first)
    assert format_notification(first, "telegram") == {"text": "[OMG ERROR] Permission denied: An OMG operation was denied."}
    assert format_notification(first, "discord") == {"content": "[OMG ERROR] Permission denied: An OMG operation was denied."}
    assert format_notification(first, "slack") == {"text": "[OMG ERROR] Permission denied: An OMG operation was denied."}


@pytest.mark.parametrize(
    "event_type",
    [
        "session_started",
        "session_ended",
        "run_started",
        "run_completed",
        "run_failed",
        "run_cancelled",
        "blocked",
        "permission_denied",
        "subagent_completed",
        "team_degraded",
        "acceptance_passed",
        "acceptance_failed",
        "release_succeeded",
        "release_failed",
    ],
)
def test_required_lifecycle_types_have_bounded_static_templates(event_type):
    event = notification_from_lifecycle(
        {
            "event_type": event_type,
            "event_id": f"source-{event_type}",
            "observed_at": "2026-07-22T00:00:00Z",
            "payload": {"prompt": "secret prompt", "command": "secret command"},
        },
        **OWNER,
    )
    assert event is not None
    assert event["event_type"] == event_type
    assert "secret" not in str(event)


def test_command_adapter_is_argv_only_scrubbed_bounded_and_owner_fenced():
    calls: list[tuple] = []
    config = {
        "store_kind": "omg_notification_config",
        "schema_version": 1,
        "enabled": True,
        "adapters": [
            {
                "adapter": "command",
                "enabled": True,
                "argv": ["/usr/bin/logger", "literal;$(touch nope)"],
                "allowed_executables": ["/usr/bin/logger"],
                "timeout_ms": 500,
            }
        ],
    }

    def runner(argv, stdin_text, environment, timeout):
        calls.append((tuple(argv), stdin_text, dict(environment), timeout))
        return {"returncode": 0, "stdout": "", "stderr": ""}

    result = dispatch_notifications(_event(), config, owner=OWNER, local_runner=runner)
    assert result[0]["code"] == "COMMAND_DELIVERED"
    assert calls[0][0] == ("/usr/bin/logger", "literal;$(touch nope)")
    assert calls[0][2] == {"LANG": "C", "LC_ALL": "C", "PATH": "/usr/bin:/bin"}
    assert "private" not in calls[0][1]


def test_optional_adapter_and_queue_dispatch_exceptions_never_escape(tmp_path):
    config = {
        "store_kind": "omg_notification_config",
        "schema_version": 1,
        "enabled": True,
        "adapters": [
            {
                "adapter": "command",
                "enabled": True,
                "argv": ["/usr/bin/logger"],
                "allowed_executables": ["/usr/bin/logger"],
                "timeout_ms": 500,
            }
        ],
    }

    def explode(*_args, **_kwargs):
        raise KeyError("raw secret body")

    outcome = dispatch_notifications(_event(), config, owner=OWNER, local_runner=explode)
    assert outcome[0]["code"] == "COMMAND_FAILED"
    assert "raw secret" not in str(outcome)

    assert enqueue_notification(tmp_path, _event(), owner=OWNER)["queued"] is True
    processed = process_notification_queue(
        tmp_path, {}, owner=OWNER, dispatcher=explode, max_records=1
    )
    assert processed["processed"] == 1
    assert processed["failed"] == 1


def test_owner_only_queue_deduplicates_retries_dead_letters_and_advances_durably(tmp_path):
    event = _event()
    queued = enqueue_notification(
        tmp_path,
        event,
        owner=OWNER,
        enqueued_at="2026-07-22T00:00:00Z",
        max_attempts=2,
    )
    assert queued["queued"] is True
    assert enqueue_notification(tmp_path, event, owner=OWNER)["duplicate"] is True
    directory = tmp_path / ".omg" / "state" / "notifications"
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "queue-000001.jsonl").stat().st_mode & 0o777 == 0o600

    calls: list[str] = []

    def fail(event, _config, *, owner):
        calls.append(event["event_id"])
        return [{"status": "failed", "code": "TEST", "authoritative": False}]

    def now():
        return datetime(2026, 7, 22, 0, 0, 0, tzinfo=timezone.utc)
    first = process_notification_queue(
        tmp_path, {}, owner=OWNER, dispatcher=fail, now=now, sleep=lambda _seconds: None
    )
    assert first == {
        "processed": 1,
        "delivered": 0,
        "failed": 1,
        "dead_lettered": 0,
        "duplicates": 0,
        "authoritative": False,
    }
    cursor_after_first = (directory / "cursor.json").read_bytes()
    # Retry is not due until the bounded backoff elapses; cursor is unchanged.
    assert process_notification_queue(
        tmp_path, {}, owner=OWNER, dispatcher=fail, now=now, sleep=lambda _seconds: None
    )["processed"] == 0
    assert (directory / "cursor.json").read_bytes() == cursor_after_first

    def due():
        return datetime(2026, 7, 22, 0, 0, 2, tzinfo=timezone.utc)
    final = process_notification_queue(
        tmp_path, {}, owner=OWNER, dispatcher=fail, now=due, sleep=lambda _seconds: None
    )
    assert final["dead_lettered"] == 1
    assert (directory / "dead-letter-000001.jsonl").is_file()
    assert len(calls) == 2


def test_passive_lifecycle_hook_only_enqueues_and_truncated_tail_is_safe(tmp_path):
    lifecycle = {
        "event_type": "run_completed",
        "event_id": "run-done-1",
        "observed_at": "2026-07-22T00:00:00Z",
        "payload": {"prompt": "must not escape"},
    }
    result = enqueue_lifecycle_notification(tmp_path, lifecycle, owner=OWNER)
    assert result["queued"] is True
    queue = tmp_path / ".omg" / "state" / "notifications" / "queue-000001.jsonl"
    with queue.open("ab") as handle:
        handle.write(b'{"truncated":')
    other = create_notification_event(
        severity="info",
        title="Other",
        message="safe",
        created_at="2026-07-22T00:00:01Z",
        event_type="message",
        stable_source_id="other-1",
        **OWNER,
    )
    assert enqueue_notification(tmp_path, other, owner=OWNER)["queued"] is True
    assert queue.read_bytes().endswith(b"\n")


def test_notification_queue_concurrent_enqueues_are_complete_and_lock_safe(tmp_path):
    results: list[dict] = []

    def enqueue(index):
        event = create_notification_event(
            severity="info",
            title="Concurrent",
            message=f"event {index}",
            created_at=f"2026-07-22T00:00:{index:02d}Z",
            stable_source_id=f"source-{index}",
            **OWNER,
        )
        results.append(enqueue_notification(tmp_path, event, owner=OWNER))

    threads = [Thread(target=enqueue, args=(index,)) for index in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 16
    assert all(row["queued"] for row in results)
    queue = tmp_path / ".omg" / "state" / "notifications" / "queue-000001.jsonl"
    lines = queue.read_bytes().splitlines()
    assert len(lines) == 16
    assert all(line.startswith(b'{"') and line.endswith(b"}") for line in lines)


def test_notification_queue_rotates_and_fails_closed_at_bounded_capacity(monkeypatch, tmp_path):
    from omg_cli.notify import queue as queue_module

    monkeypatch.setattr(queue_module, "MAX_SEGMENT_BYTES", 1_000)
    monkeypatch.setattr(queue_module, "MAX_QUEUE_SEGMENTS", 2)
    results = []
    for index in range(3):
        event = create_notification_event(
            severity="info",
            title="Rotate",
            message="x" * 300,
            created_at=f"2026-07-22T00:00:0{index}Z",
            stable_source_id=f"rotate-{index}",
            **OWNER,
        )
        results.append(enqueue_notification(tmp_path, event, owner=OWNER))
    assert [row["code"] for row in results] == ["QUEUED", "QUEUED", "QUEUE_FULL"]
    directory = tmp_path / ".omg" / "state" / "notifications"
    assert sorted(path.name for path in directory.glob("queue-*.jsonl")) == [
        "queue-000001.jsonl",
        "queue-000002.jsonl",
    ]


def test_dead_letter_rotation_retains_full_capacity_without_early_eviction(monkeypatch, tmp_path):
    from omg_cli.notify import queue as queue_module

    monkeypatch.setattr(queue_module, "MAX_SEGMENT_RECORDS", 2)
    monkeypatch.setattr(queue_module, "MAX_QUEUE_SEGMENTS", 2)
    for index in range(4):
        event = create_notification_event(
            severity="error",
            title="Dead letter",
            message=str(index),
            created_at=f"2026-07-22T00:00:0{index}Z",
            stable_source_id=f"dead-letter-{index}",
            **OWNER,
        )
        assert enqueue_notification(tmp_path, event, owner=OWNER, max_attempts=1)["queued"]

    result = process_notification_queue(
        tmp_path,
        {},
        owner=OWNER,
        dispatcher=lambda *_args, **_kwargs: [{"status": "failed"}],
        max_records=4,
        sleep=lambda _seconds: None,
    )

    assert result["dead_lettered"] == 4
    directory = tmp_path / ".omg" / "state" / "notifications"
    dead_letters = sorted(directory.glob("dead-letter-*.jsonl"))
    assert [path.name for path in dead_letters] == [
        "dead-letter-000001.jsonl",
        "dead-letter-000002.jsonl",
    ]
    assert [len(path.read_bytes().splitlines()) for path in dead_letters] == [2, 2]


def test_notification_queue_rate_limits_multiple_deliveries(tmp_path):
    for index in range(2):
        event = create_notification_event(
            severity="info",
            title="Rate",
            message=str(index),
            created_at=f"2026-07-22T00:00:0{index}Z",
            stable_source_id=f"rate-{index}",
            **OWNER,
        )
        assert enqueue_notification(tmp_path, event, owner=OWNER)["queued"] is True
    sleeps: list[float] = []
    result = process_notification_queue(
        tmp_path,
        {},
        owner=OWNER,
        dispatcher=lambda *_args, **_kwargs: [{"status": "delivered"}],
        rate_limit_per_second=2,
        sleep=sleeps.append,
    )
    assert result["processed"] == 2
    assert result["delivered"] == 2
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 0.5


def test_notification_queue_serializes_competing_processors(tmp_path):
    import time

    event = create_notification_event(
        severity="info",
        title="Once",
        message="one delivery",
        created_at="2026-07-22T00:00:00Z",
        stable_source_id="single-worker",
        **OWNER,
    )
    assert enqueue_notification(tmp_path, event, owner=OWNER)["queued"] is True
    deliveries: list[str] = []
    results: list[dict] = []

    def dispatch(queued_event, _config, *, owner):
        deliveries.append(queued_event["event_id"])
        time.sleep(0.03)
        return [{"status": "delivered"}]

    def process():
        results.append(
            process_notification_queue(
                tmp_path, {}, owner=OWNER, dispatcher=dispatch, max_records=1
            )
        )

    workers = [Thread(target=process) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert deliveries == [event["event_id"]]
    assert sorted(row["processed"] for row in results) == [0, 1]


def test_notify_public_surface_has_no_inbound_reply_or_listener_contract():
    import omg_cli.notify as surface

    assert not any(
        word in name.lower()
        for name in surface.__all__
        for word in ("listen", "server", "reply", "inbound")
    )
