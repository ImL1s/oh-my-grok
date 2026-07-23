"""Optional tmux display delivery requires exact ownership readback."""
from __future__ import annotations

from omg_cli.team.tmux_adapter import deliver_tmux_message


def _runner(overrides=None):
    calls: list[list[str]] = []
    stdout = [
        "team\t%9",
        "owner-nonce-123456",
        "worker-nonce-12345",
        "/dev/ttys001\tteam",
        "",
    ]
    overrides = overrides or {}

    def run(argv):
        index = len(calls)
        calls.append(list(argv))
        return {"returncode": 0, "stdout": overrides.get(index, stdout[index]), "stderr": ""}

    return run, calls


TARGET = {
    "enabled": True,
    "session_name": "team",
    "pane_id": "%9",
    "owner_nonce": "owner-nonce-123456",
    "worker_nonce": "worker-nonce-12345",
}


def test_tmux_delivery_reads_exact_session_pane_and_nonce_identity():
    run, calls = _runner()
    outcome = deliver_tmux_message("Done token=private", TARGET, runner=run)
    assert outcome["status"] == "delivered"
    assert outcome["code"] == "TMUX_DELIVERED"
    assert calls == [
        ["display-message", "-p", "-t", "%9", "#{session_name}\t#{pane_id}"],
        ["show-options", "-v", "-t", "team", "@omg_owner_nonce"],
        ["show-options", "-p", "-v", "-t", "%9", "@omg_worker_nonce"],
        ["list-clients", "-t", "team", "-F", "#{client_name}\t#{session_name}"],
        ["display-message", "-c", "/dev/ttys001", "-l", "--", "Done token=[REDACTED]"],
    ]
    assert "owner-nonce" not in str(outcome)
    assert "team" not in str(outcome)


def test_tmux_delivery_refuses_any_readback_mismatch():
    for index, value in ((0, "other\t%9"), (1, "wrong-owner"), (2, "wrong-worker")):
        run, calls = _runner({index: value})
        outcome = deliver_tmux_message("Done", TARGET, runner=run)
        assert outcome["code"] == "TMUX_IDENTITY_MISMATCH"
        assert len(calls) == 4


def test_tmux_requires_exactly_one_attached_client():
    for clients in ("", "/dev/ttys1\tteam\n/dev/ttys2\tteam", "/dev/ttys1\tother"):
        run, calls = _runner({3: clients})
        outcome = deliver_tmux_message("Done", TARGET, runner=run)
        assert outcome["code"] == "TMUX_CLIENT_BINDING_MISMATCH"
        assert len(calls) == 4


def test_tmux_literal_display_does_not_expand_format_strings():
    run, calls = _runner()
    outcome = deliver_tmux_message("#{session_name} $(touch nope)", TARGET, runner=run)
    assert outcome["code"] == "TMUX_DELIVERED"
    assert calls[-1] == [
        "display-message",
        "-c",
        "/dev/ttys001",
        "-l",
        "--",
        "#{session_name} $(touch nope)",
    ]


def test_tmux_delivery_is_disabled_without_any_subprocess():
    calls: list[list[str]] = []
    outcome = deliver_tmux_message(
        "Done",
        {**TARGET, "enabled": False},
        runner=lambda argv: calls.append(list(argv)),
    )
    assert outcome["status"] == "skipped"
    assert outcome["code"] == "TMUX_DISABLED"
    assert calls == []


def test_tmux_delivery_rejects_unbounded_message_before_readback():
    calls: list[list[str]] = []
    outcome = deliver_tmux_message(
        "x" * 4097,
        TARGET,
        runner=lambda argv: calls.append(list(argv)),
    )
    assert outcome["code"] == "TMUX_MESSAGE_REJECTED"
    assert calls == []


def test_tmux_delivery_rejects_control_characters_before_readback():
    for message in ("Done\nnext", "Done\0hidden", "Done\x7fhidden", "Done\x9bhidden"):
        calls: list[list[str]] = []
        outcome = deliver_tmux_message(
            message,
            TARGET,
            runner=lambda argv: calls.append(list(argv)),
        )
        assert outcome["code"] == "TMUX_MESSAGE_REJECTED"
        assert calls == []


def test_tmux_delivery_fails_closed_when_producer_nonces_are_absent():
    calls: list[list[str]] = []
    outcome = deliver_tmux_message(
        "Done",
        {key: value for key, value in TARGET.items() if not key.endswith("nonce")},
        runner=lambda argv: calls.append(list(argv)),
    )
    assert outcome["code"] == "TMUX_TARGET_REJECTED"
    assert calls == []
