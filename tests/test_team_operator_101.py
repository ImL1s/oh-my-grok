"""Identity-fenced team pane operator control (#101)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from omg_cli.main import build_parser
from omg_cli.team import operator, plane, tmux
from omg_cli.team.io_capability import (
    E_OPERATOR_INPUT_NOT_READY,
    E_OPERATOR_INPUT_UNSUPPORTED,
    E_OPERATOR_KEY_UNSUPPORTED,
    INTERACTION_EVIDENCE_SCHEMA,
    IO_MODE_HEADLESS_STREAM,
    IO_MODE_INTERACTIVE_TTY,
    TTY_OWNER_SUPERVISOR,
)
from omg_cli.team.operator import (
    ExactPaneProof,
    OperatorError,
    STATUS_GONE,
    STATUS_LIVE,
    STATUS_MISMATCH,
    authorize_key,
    caller_shares_team_tmux_server,
    capture_worker,
    focus_worker,
    input_worker,
    key_worker,
    list_panes,
    probe_exact_worker,
    resolve_live_worker,
    tmux_socket_from_tmux_env,
    watch_worker,
)
from omg_cli.team.plane import EXPERIMENTAL_ENV, start_team, team_meta_path


def _stamp_interactive_io(root: Path, live: dict[str, Any]) -> dict[str, Any]:
    """PR1+: enable operator send path for identity/TTY/TOCTOU unit tests.

    Production supervisor panes stay headless/unsupported; tests that exercise
    ExactPaneProof/send after the capability gate must opt in explicitly.
    """
    live = dict(live)
    tasks = [dict(t) for t in live["tasks"]]
    generation = int(live.get("identity_generation") or 0)
    for i, task in enumerate(tasks):
        attempt = int(task.get("attempt") or 1)
        pane = task.get("pane_id")
        pid = task.get("pid")
        tasks[i] = {
            **task,
            "io_mode": IO_MODE_INTERACTIVE_TTY,
            "provider_tty_owner": "provider",
            "operator_input_supported": True,
            "input_ready": True,
            "interaction_evidence": {
                "schema": INTERACTION_EVIDENCE_SCHEMA,
                "attempt": attempt,
                "generation": generation,
                "ready_marker": "TUI_READY:test",
                "proven_at": "2026-01-01T00:00:00Z",
                "pane_id": pane if isinstance(pane, str) else None,
                "provider_pid": pid if isinstance(pid, int) else None,
            },
        }
    live["tasks"] = tasks
    plane._atomic_write_json(team_meta_path(root, str(live["run_id"])), live)
    return live


TASKS_ONE = [
    {
        "task_id": "w1",
        "owned_files": ["lane_a/"],
    }
]

TASKS_TWO = [
    {
        "task_id": "w1",
        "owned_files": ["lane_a/"],
    },
    {
        "task_id": "w2",
        "owned_files": ["lane_b/"],
    },
]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "omg-test@example.com")
    _git(path, "config", "user.name", "omg-test")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("base\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")


def _enable_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    monkeypatch.delenv("OMG_DISABLE_TMUX_TEAM", raising=False)


def _write_live_team(
    root: Path,
    meta: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    session_id: str = "$42",
    nonce: str = "b" * 32,
    leader_pane_id: str = "%1",
) -> dict[str, Any]:
    live = dict(meta)
    live["dry_run"] = False
    live["session"] = "omg-op-session"
    live["session_id"] = session_id
    live["leader_pane_id"] = leader_pane_id
    live["leader_pane_pid"] = 111
    live["window_id"] = "@9"
    live["view_mode"] = "detached_session"
    live["tasks"] = [
        {
            **task,
            "pane_id": f"%{index + 10}",
            "pid": 7000 + index,
            "pgid": 8000 + index,
            "pid_start": f"ps:start-{7000 + index}",
            "status": "running",
            "attempt": 1,
            "role": task.get("role") or "executor",
            "provider": task.get("provider") or "grok",
            "posture": task.get("posture") or "read-write",
            "worktree": str(root / ".omg" / "worktrees" / f"w{index}"),
            "pane_command": "python3 tests/fixtures/team_worker_fixture.py",
            # Do not set window_id on gen-0 tasks — launch receipt rows omit it
            # and identity-chain compare would fail closed.
        }
        for index, task in enumerate(meta["tasks"])
    ]
    _receipt, receipt_hash = plane._persist_team_launch_receipt(
        root,
        str(meta["run_id"]),
        session=live["session"],
        session_id=session_id,
        launch_nonce=nonce,
        tasks=live["tasks"],
        intent_nonce="c" * 32,
        window_name="omg-team-test",
        view_mode="detached_session",
        layout="tiled",
        leader_pane_id=leader_pane_id,
        leader_pane_pid=111,
        window_id="@9",
        session_owned=True,
        attach_mode="detached",
    )
    live["launch_nonce"] = nonce
    live["launch_receipt_sha256"] = receipt_hash
    live["identity_generation"] = 0
    live["identity_receipt_sha256"] = receipt_hash
    starts = {task["pid"]: task["pid_start"] for task in live["tasks"]}

    def _start_for(pid: int) -> str | None:
        return starts.get(pid) or f"ps:start-{pid}"

    monkeypatch.setattr(plane, "_pid_start_identity", _start_for)
    plane._atomic_write_json(team_meta_path(root, str(meta["run_id"])), live)
    return live


def _install_live_tmux(
    monkeypatch: pytest.MonkeyPatch,
    live: dict[str, Any],
    *,
    session_id: str = "$42",
    nonce: str | None = None,
    pane_pid_override: dict[str, int] | None = None,
    dead_panes: set[str] | None = None,
    foreign_session: str | None = None,
    pane_owner_override: dict[str, str] | None = None,
    capture_text: str = "hello secret=TOKEN\n",
    effects: list[list[str]] | None = None,
) -> list[list[str]]:
    expected_nonce = nonce if nonce is not None else str(live["launch_nonce"])
    commands = effects if effects is not None else []
    dead = dead_panes or set()
    pid_override = pane_pid_override or {}
    owner_override = pane_owner_override or {}

    def run(args: Any, **_kw: Any) -> MagicMock:
        command = list(args)
        sock = _kw.get("socket_path")
        # Strip leading tmux binary if present (_tmux_run includes it).
        if command and command[0] == "tmux":
            command = command[1:]
        recorded = list(command)
        if isinstance(sock, str) and sock:
            recorded = ["-S", sock, *command]
        commands.append(recorded)
        if len(command) >= 2 and command[0] == "-S":
            command = command[2:]
        elif isinstance(sock, str) and sock:
            # Product _tmux_run pins -S via kwarg, not argv[0].
            pass
        result = MagicMock(returncode=0, stdout="", stderr="")
        if not command:
            return result
        op = command[0]
        if op == "display-message":
            target = None
            if "-t" in command:
                target = command[command.index("-t") + 1]
            fmt = command[-1] if command else ""
            task = next(
                (t for t in live["tasks"] if t.get("pane_id") == target),
                None,
            )
            if task is not None:
                pid = pid_override.get(str(target), int(task["pid"]))
                dead_flag = "1" if target in dead else "0"
                sid = foreign_session or session_id
                fmt_s = str(fmt)
                # #102 read_exact_worker_pane_identity format
                if "#{pane_dead}" in fmt_s and (
                    "#{window_id}" in fmt_s or "omg_worker_nonce" in fmt_s
                ):
                    window_id = (
                        task.get("window_id")
                        or live.get("window_id")
                        or "@9"
                    )
                    owner = owner_override.get(
                        str(target),
                        str(task.get("pane_owner_nonce") or ""),
                    )
                    result.stdout = (
                        f"{target}\t{window_id}\t{sid}\t{pid}\t{owner}\t{dead_flag}\n"
                    )
                elif "#{pane_dead}" in fmt_s:
                    # #98 probe_worker_pane_identity
                    result.stdout = f"{target}\t{dead_flag}\t{sid}\t{pid}\n"
                else:
                    result.stdout = f"{target}\t{pid}\n"
            else:
                result.returncode = 1
                result.stderr = "can't find pane"
        elif op == "show-options":
            option = command[-1] if command else ""
            target = None
            if "-t" in command:
                target = command[command.index("-t") + 1]
            if "omg_worker_nonce" in str(option):
                task = next(
                    (t for t in live["tasks"] if t.get("pane_id") == target),
                    None,
                )
                if task is not None:
                    owner = owner_override.get(
                        str(target),
                        str(task.get("pane_owner_nonce") or ""),
                    )
                    result.stdout = owner + "\n"
                else:
                    result.returncode = 1
            else:
                result.stdout = expected_nonce + "\n"
        elif op == "list-panes":
            result.stdout = "\n".join(
                str(t["pane_id"]) for t in live["tasks"] if t.get("pane_id")
            )
            if leader := live.get("leader_pane_id"):
                result.stdout = f"{leader}\n" + result.stdout
        elif op == "capture-pane":
            result.stdout = capture_text
        elif op in {"select-pane", "send-keys"}:
            result.returncode = 0
        else:
            result.returncode = 0
        return result

    monkeypatch.setattr(tmux, "tmux_available", lambda: True)
    monkeypatch.setattr(tmux, "_tmux_run", run)
    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    # plane may call its own _tmux_run for nonce probes
    monkeypatch.setattr(plane, "_tmux_run", run)
    return commands


@pytest.fixture()
def live_team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("op fixture", TASKS_ONE, root=tmp_path, dry_run=True)
    live = _write_live_team(tmp_path, meta, monkeypatch)
    return {"root": tmp_path, "live": live, "run_id": str(live["run_id"])}


def test_exact_pane_proof_carries_tmux_socket_path() -> None:
    """Operator send/focus pin tmux -S off the proof; the field must exist."""
    proof = ExactPaneProof(
        run_id="run",
        team_id="team",
        worker_id="w1",
        attempt=1,
        generation=0,
        session="omg-op-session",
        session_id="$42",
        launch_nonce="b" * 32,
        pane_id="%10",
        window_id="@9",
        expected_pid=7000,
        expected_pid_start=None,
        pane_owner_nonce=None,
        owner_token_sha256=None,
        leader_pane_id="%1",
        role=None,
        provider=None,
        posture=None,
        worktree=None,
        state=None,
        ready=None,
        tmux_socket_path="/tmp/omg-op-test.sock",
    )
    assert proof.tmux_socket_path == "/tmp/omg-op-test.sock"


def test_caller_shares_team_tmux_server() -> None:
    assert tmux_socket_from_tmux_env("/tmp/a.sock,123,0") == "/tmp/a.sock"
    assert (
        caller_shares_team_tmux_server(
            tmux_env="/tmp/a.sock,123,0",
            team_socket_path="/tmp/a.sock",
        )
        is True
    )
    assert (
        caller_shares_team_tmux_server(
            tmux_env="/tmp/tmux-1000/default,1,0",
            team_socket_path="/tmp/a.sock",
        )
        is False
    )
    assert (
        caller_shares_team_tmux_server(
            tmux_env="/tmp/tmux-1000/default,1,0",
            team_socket_path=None,
        )
        is True
    )
    assert (
        caller_shares_team_tmux_server(
            tmux_env=None,
            team_socket_path="/tmp/a.sock",
        )
        is False
    )


def test_probe_exact_worker_threads_tmux_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_read(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 7000

    monkeypatch.setattr(operator, "tmux_available", lambda: True)
    monkeypatch.setattr(operator, "read_exact_worker_pane_identity", fake_read)
    monkeypatch.setattr(
        plane,
        "_probe_tmux_launch_nonce_for_pane",
        lambda *args, **kwargs: ("b" * 32, True),
    )
    proof = ExactPaneProof(
        run_id="run",
        team_id="team",
        worker_id="w1",
        attempt=1,
        generation=0,
        session="omg-op-session",
        session_id="$42",
        launch_nonce="b" * 32,
        pane_id="%10",
        window_id="@9",
        expected_pid=7000,
        expected_pid_start=None,
        pane_owner_nonce="owner-nonce",
        owner_token_sha256=None,
        leader_pane_id="%1",
        role=None,
        provider=None,
        posture=None,
        worktree=None,
        state=None,
        ready=None,
        tmux_socket_path="/tmp/omg-op-test.sock",
    )
    assert probe_exact_worker(proof) == STATUS_LIVE
    assert seen.get("socket_path") == "/tmp/omg-op-test.sock"


def test_parser_registers_operator_family() -> None:
    parser = build_parser()
    for action in ("panes", "capture", "focus", "key", "input", "watch"):
        ns = parser.parse_args(["team", action, "--help"]) if False else None
        # parse without help
        argv = ["team", action]
        if action != "panes" and action != "watch":
            argv.extend(["--worker", "w1"])
        if action == "key":
            argv.extend(["--key", "Enter"])
        if action == "input":
            argv.extend(["--text", "hi"])
        ns = parser.parse_args(argv)
        assert ns.team_action == action


def test_key_allowlist_rejects_injection() -> None:
    with pytest.raises(OperatorError):
        authorize_key(
            MagicMock(),  # unused — fails before probe
            "Enter;kill",
        )
    with pytest.raises(OperatorError):
        authorize_key(MagicMock(), "Enter\n")
    with pytest.raises(OperatorError):
        authorize_key(MagicMock(), "UnknownKey")


def test_tmux_send_key_and_literal_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(args: Any, **_kw: Any) -> MagicMock:
        command = list(args)
        if command and command[0] == "tmux":
            command = command[1:]
        seen.append(command)
        return MagicMock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(tmux, "tmux_available", lambda: True)
    monkeypatch.setattr(tmux, "_tmux_run", run)

    tmux.send_key("%10", "Enter")
    tmux.send_literal("%10", "hello")
    tmux.send_submit("%10")
    assert seen[0] == ["send-keys", "-t", "%10", "Enter"]
    assert seen[1] == ["send-keys", "-l", "-t", "%10", "--", "hello"]
    assert seen[2] == ["send-keys", "-l", "-t", "%10", "--", "\r"]

    with pytest.raises(tmux.TmuxTeamError):
        tmux.send_key("%10", "Enter;rm")
    with pytest.raises(tmux.TmuxTeamError):
        tmux.send_literal("%10", "x\0y")
    with pytest.raises(tmux.TmuxTeamError):
        tmux.send_literal("%10", "x" * (tmux.MAX_OPERATOR_INPUT_BYTES + 1))


def test_panes_lists_authorization_flags(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    _install_live_tmux(monkeypatch, live)

    out = list_panes(root, live_team["run_id"])
    assert out["count"] == 1
    row = out["panes"][0]
    assert row["worker_id"] == "w1"
    assert row["pane_id"] == "%10"
    assert row["liveness"] == STATUS_LIVE
    assert row["capture_allowed"] is True
    assert row["input_allowed"] is True
    assert "argv" not in row
    assert row["worktree"] == "w0"  # basename only
    assert "TOKEN" not in json.dumps(row)


def test_capture_live_redacts_and_bounds(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    # redaction should catch common secret shapes
    secret_line = "Authorization: Bearer SUPERSECRETTOKEN123\n"
    _install_live_tmux(monkeypatch, live, capture_text=secret_line)

    out = capture_worker(root, live_team["run_id"], "w1", lines=50)
    assert out["ok"] is True
    assert out["status"] == STATUS_LIVE
    assert "SUPERSECRETTOKEN123" not in (out["text"] or "")
    assert out["bytes"] <= tmux.MAX_OPERATOR_CAPTURE_BYTES


def test_capture_identity_mismatch_on_pid_reuse(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    _install_live_tmux(
        monkeypatch,
        live,
        pane_pid_override={"%10": 99999},
    )
    out = capture_worker(root, live_team["run_id"], "w1")
    assert out["ok"] is False
    assert out["status"] == STATUS_MISMATCH


def test_capture_gone_when_pane_absent(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    commands: list[list[str]] = []

    def run(args: Any, **_kw: Any) -> MagicMock:
        command = list(args)
        if command and command[0] == "tmux":
            command = command[1:]
        commands.append(command)
        result = MagicMock(returncode=0, stdout="", stderr="")
        if command and command[0] == "display-message":
            result.returncode = 1
            result.stderr = "can't find pane"
        elif command and command[0] == "list-panes":
            # Successful complete list without the worker pane → proven_absent
            result.stdout = "%1\n"
        return result

    monkeypatch.setattr(tmux, "tmux_available", lambda: True)
    monkeypatch.setattr(tmux, "_tmux_run", run)
    monkeypatch.setattr(plane, "tmux_available", lambda: True)
    monkeypatch.setattr(plane, "_tmux_run", run)

    out = capture_worker(root, live_team["run_id"], "w1")
    assert out["ok"] is False
    assert out["status"] == STATUS_GONE


def test_leader_pane_refused_as_worker(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    # Point worker pane_id at leader without rewriting immutable receipt —
    # resolve must refuse leader equality on the live meta row.
    live["tasks"][0] = dict(live["tasks"][0])
    live["tasks"][0]["pane_id"] = live["leader_pane_id"]
    # Bypass receipt continuity by stubbing the chain loader.
    monkeypatch.setattr(
        plane,
        "_load_team_identity_chain",
        lambda *_a, **_k: [
            {
                "session_name": live["session"],
                "session_id": live["session_id"],
                "launch_nonce": live["launch_nonce"],
                "tasks": [],
            }
        ],
    )
    plane._atomic_write_json(team_meta_path(root, live_team["run_id"]), live)
    with pytest.raises(OperatorError) as exc:
        resolve_live_worker(root, live_team["run_id"], "w1")
    assert exc.value.code == "E_OPERATOR_LEADER_REFUSED"


def test_focus_json_never_selects(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    effects: list[list[str]] = []
    _install_live_tmux(monkeypatch, live, effects=effects)
    out = focus_worker(
        root,
        live_team["run_id"],
        "w1",
        as_json=True,
        is_tty=True,
    )
    assert out["focused"] is False
    assert out["team_state_mutated"] is False
    assert not any(cmd and cmd[0] == "select-pane" for cmd in effects)


def test_focus_tty_inside_tmux_selects(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    effects: list[list[str]] = []
    _install_live_tmux(monkeypatch, live, effects=effects)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    out = focus_worker(
        root,
        live_team["run_id"],
        "w1",
        as_json=False,
        is_tty=True,
    )
    assert out["focused"] is True
    assert any(cmd[:2] == ["select-pane", "-t"] for cmd in effects)


def test_focus_inside_tmux_foreign_socket_does_not_claim_focus(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    sock = "/tmp/omg-team-focus.sock"
    live["tmux_socket_path"] = sock
    plane._atomic_write_json(team_meta_path(root, str(live["run_id"])), live)
    effects: list[list[str]] = []
    _install_live_tmux(monkeypatch, live, effects=effects)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    out = focus_worker(
        root,
        live_team["run_id"],
        "w1",
        as_json=False,
        is_tty=True,
    )
    assert out["focused"] is False
    assert out["mode"] == "hint"
    assert "differs from the team socket" in str(out.get("note") or "")
    assert not any(cmd[:2] == ["select-pane", "-t"] for cmd in effects)


def test_focus_inside_tmux_matching_socket_selects(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    sock = "/tmp/omg-team-focus.sock"
    live["tmux_socket_path"] = sock
    plane._atomic_write_json(team_meta_path(root, str(live["run_id"])), live)
    effects: list[list[str]] = []
    _install_live_tmux(monkeypatch, live, effects=effects)
    monkeypatch.setenv("TMUX", f"{sock},1,0")
    out = focus_worker(
        root,
        live_team["run_id"],
        "w1",
        as_json=False,
        is_tty=True,
    )
    assert out["focused"] is True
    assert out["mode"] == "select-pane"
    assert any(cmd[:2] == ["select-pane", "-t"] or cmd[:4] == ["-S", sock, "select-pane", "-t"] for cmd in effects)


def test_key_and_input_audit_no_raw_text(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = _stamp_interactive_io(root, live_team["live"])
    sock = "/tmp/omg-op-test.sock"
    live["tmux_socket_path"] = sock
    plane._atomic_write_json(team_meta_path(root, str(live["run_id"])), live)
    effects: list[list[str]] = []
    _install_live_tmux(monkeypatch, live, effects=effects)
    proof = resolve_live_worker(root, live_team["run_id"], "w1")
    assert proof.tmux_socket_path == sock

    key_worker(
        root,
        live_team["run_id"],
        "w1",
        "Enter",
        as_json=False,
        operator_override=True,
        is_tty=False,
    )
    secret = "continue-with-api-key-ABCDEF"
    out = input_worker(
        root,
        live_team["run_id"],
        "w1",
        secret,
        submit=True,
        operator_override=True,
        is_tty=False,
        as_json=False,
    )
    assert out["submitted"] is True
    assert out["submitted_to_exact_tty"] is True
    assert out["acknowledged_by_provider"] is False
    assert "delivered" not in out
    assert out["text_sha256"] == hashlib.sha256(secret.encode()).hexdigest()
    assert out["text_length"] == len(secret.encode())

    audit_path = plane.team_dir(root, live_team["run_id"]) / "operator-audit.jsonl"
    body = audit_path.read_text(encoding="utf-8")
    assert secret not in body
    assert "Enter" in body
    assert "text_sha256" in body
    assert "io_mode" in body
    # Isolated-socket teams must pin send-keys with tmux -S (not ambient TMUX).
    send = [cmd for cmd in effects if "send-keys" in cmd]
    assert send
    assert all(cmd[:2] == ["-S", sock] for cmd in send)
    # Grok submit is literal CR (possibly two pulses), never named Enter.
    cr = [cmd for cmd in send if "-l" in cmd and cmd[-1] == "\r"]
    assert len(cr) == 2
    named_enter = [cmd for cmd in send if cmd[-1] == "Enter" and "-l" not in cmd]
    assert len(named_enter) == 1  # key_worker Enter only


def test_key_requires_tty_or_operator_override(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = _stamp_interactive_io(root, live_team["live"])
    _install_live_tmux(monkeypatch, live)
    with pytest.raises(OperatorError) as exc:
        key_worker(
            root,
            live_team["run_id"],
            "w1",
            "Enter",
            is_tty=False,
            operator_override=False,
        )
    assert exc.value.code == "E_OPERATOR_KEY_POLICY"
    # Override path still submits to exact TTY (not provider ACK).
    out = key_worker(
        root,
        live_team["run_id"],
        "w1",
        "Tab",
        is_tty=False,
        operator_override=True,
    )
    assert out["submitted_to_exact_tty"] is True
    assert out["acknowledged_by_provider"] is False
    assert "delivered" not in out
    assert out["key"] == "Tab"


def test_json_refuses_key_and_input(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    _install_live_tmux(monkeypatch, live)
    with pytest.raises(OperatorError) as e1:
        key_worker(root, live_team["run_id"], "w1", "Enter", as_json=True)
    assert e1.value.code == "E_OPERATOR_JSON_NOOP"
    with pytest.raises(OperatorError) as e2:
        input_worker(
            root,
            live_team["run_id"],
            "w1",
            "hi",
            as_json=True,
            operator_override=True,
        )
    assert e2.value.code == "E_OPERATOR_JSON_NOOP"


def test_input_rejects_controls_and_oversize(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = _stamp_interactive_io(root, live_team["live"])
    _install_live_tmux(monkeypatch, live)
    with pytest.raises(OperatorError):
        input_worker(
            root,
            live_team["run_id"],
            "w1",
            "bad\nline",
            operator_override=True,
            is_tty=True,
        )
    with pytest.raises(OperatorError):
        input_worker(
            root,
            live_team["run_id"],
            "w1",
            "x" * (tmux.MAX_OPERATOR_INPUT_BYTES + 1),
            operator_override=True,
            is_tty=True,
        )


def test_toctou_blocks_input_when_identity_flips(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = _stamp_interactive_io(root, live_team["live"])
    _install_live_tmux(monkeypatch, live)
    proof = resolve_live_worker(root, live_team["run_id"], "w1")
    assert probe_exact_worker(proof) == STATUS_LIVE

    # authorize probe LIVE, then mutating re-proof MISMATCH
    states = iter([STATUS_LIVE, STATUS_MISMATCH])

    monkeypatch.setattr(
        operator,
        "probe_exact_worker",
        lambda _proof: next(states),
    )
    with pytest.raises(OperatorError) as exc:
        input_worker(
            root,
            live_team["run_id"],
            "w1",
            "hi",
            operator_override=True,
            is_tty=True,
        )
    assert exc.value.code == "E_OPERATOR_TOCTOU"
    assert exc.value.status == STATUS_MISMATCH


def test_submit_reprobes_before_enter(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--submit must re-prove identity before CR; flip after literal blocks it."""
    root = live_team["root"]
    live = _stamp_interactive_io(root, live_team["live"])
    effects: list[list[str]] = []
    _install_live_tmux(monkeypatch, live, effects=effects)

    # authorize LIVE → literal re-proof LIVE → submit re-proof MISMATCH
    states = iter([STATUS_LIVE, STATUS_LIVE, STATUS_MISMATCH])
    monkeypatch.setattr(
        operator,
        "probe_exact_worker",
        lambda _proof: next(states),
    )
    with pytest.raises(OperatorError) as exc:
        input_worker(
            root,
            live_team["run_id"],
            "w1",
            "hi",
            submit=True,
            operator_override=True,
            is_tty=True,
        )
    assert exc.value.code == "E_OPERATOR_TOCTOU"
    # Literal submitted to exact TTY; submit CR must not have been sent.
    send_cmds = [cmd for cmd in effects if cmd and cmd[0] == "send-keys"]
    assert any("-l" in cmd for cmd in send_cmds)
    assert not any(cmd[-1] == "Enter" and "-l" not in cmd for cmd in send_cmds)
    assert not any("-l" in cmd and cmd[-1] == "\r" for cmd in send_cmds)


def test_watch_without_worker_keeps_per_worker_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shared last_identity/last_text across workers must not false-trigger."""
    _init_repo(tmp_path)
    _enable_team(monkeypatch)
    meta = start_team("op multi", TASKS_TWO, root=tmp_path, dry_run=True)
    live = _write_live_team(tmp_path, meta, monkeypatch)
    run_id = str(live["run_id"])
    _install_live_tmux(monkeypatch, live)

    texts = {
        "w1": "alpha-stable\n",
        "w2": "beta-one\n",
    }
    call_n = {"n": 0}

    def fake_capture(
        _root: Any,
        _identity: Any,
        wid: str,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        call_n["n"] += 1
        # Second poll: only w2 text changes; w1 unchanged.
        if call_n["n"] > 2 and wid == "w2":
            texts["w2"] = "beta-two\n"
        body = texts[wid]
        return {
            "ok": True,
            "text": body,
            "bytes": len(body.encode()),
            "status": STATUS_LIVE,
        }

    monkeypatch.setattr(operator, "probe_exact_worker", lambda _p: STATUS_LIVE)
    monkeypatch.setattr(operator, "capture_worker", fake_capture)

    out = watch_worker(
        tmp_path,
        run_id,
        worker_id=None,  # watch all workers
        interval_s=0.5,
        max_iterations=2,
        sleep_fn=lambda _s: None,
        as_json=False,
    )
    assert out["ok"] is True
    assert out["stop_reason"] == "max_iterations"
    events = out["events"] or []
    # Two workers × two iterations
    assert len(events) == 4
    by_worker: dict[str, list[dict[str, Any]]] = {"w1": [], "w2": []}
    for ev in events:
        by_worker[str(ev["worker_id"])].append(ev)
    assert len(by_worker["w1"]) == 2
    assert len(by_worker["w2"]) == 2
    # Distinct pane identities must not raise identity_changed across workers.
    assert by_worker["w1"][0]["pane_id"] != by_worker["w2"][0]["pane_id"]
    # First poll: both changed (no prior text). Second: only w2 changed.
    assert by_worker["w1"][0]["changed"] is True
    assert by_worker["w2"][0]["changed"] is True
    assert by_worker["w1"][1]["changed"] is False
    assert by_worker["w2"][1]["changed"] is True


def test_watch_stops_on_gone(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    _install_live_tmux(monkeypatch, live)

    # watch probes once per iteration; capture_worker is stubbed (no extra probes).
    probes = iter([STATUS_LIVE, STATUS_GONE])

    monkeypatch.setattr(
        operator,
        "probe_exact_worker",
        lambda _p: next(probes, STATUS_GONE),
    )
    monkeypatch.setattr(
        operator,
        "capture_worker",
        lambda *a, **k: {
            "ok": True,
            "text": "one",
            "bytes": 3,
            "status": STATUS_LIVE,
        },
    )
    slept: list[float] = []
    out = watch_worker(
        root,
        live_team["run_id"],
        "w1",
        interval_s=0.5,
        max_iterations=5,
        sleep_fn=lambda s: slept.append(s),
        as_json=False,
    )
    assert out["ok"] is False
    assert out["stop_reason"] == "pane_gone"
    assert out["iterations"] >= 2
    assert slept


def test_stale_generation_task_still_uses_meta_generation(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    live["identity_generation"] = 0
    plane._atomic_write_json(team_meta_path(root, live_team["run_id"]), live)
    _install_live_tmux(monkeypatch, live)
    proof = resolve_live_worker(root, live_team["run_id"], "w1")
    assert proof.generation == 0
    assert proof.pane_id == "%10"


def test_foreign_session_is_mismatch(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    _install_live_tmux(monkeypatch, live, foreign_session="$999")
    out = capture_worker(root, live_team["run_id"], "w1")
    assert out["status"] == STATUS_MISMATCH


def _bind_owner_nonce_on_worker(
    root: Path,
    live: dict[str, Any],
    *,
    owner_nonce: str = "d" * 32,
    window_id: str = "@9",
) -> dict[str, Any]:
    """Attach #102 pane-owner nonce + window id without rewriting launch receipt.

    Receipt chain compare uses gen-0 launch rows (no owner nonce). Stub the
    chain loader so resolve can proceed with the strengthened meta task row.
    """
    live = dict(live)
    live["tasks"] = [dict(t) for t in live["tasks"]]
    live["tasks"][0]["pane_owner_nonce"] = owner_nonce
    live["tasks"][0]["window_id"] = window_id
    live["window_id"] = window_id
    plane._atomic_write_json(team_meta_path(root, str(live["run_id"])), live)
    return live


def test_owner_nonce_proof_live_allows_capture(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = _bind_owner_nonce_on_worker(root, live_team["live"])
    monkeypatch.setattr(
        plane,
        "_load_team_identity_chain",
        lambda *_a, **_k: [
            {
                "session_name": live["session"],
                "session_id": live["session_id"],
                "launch_nonce": live["launch_nonce"],
                "tasks": [],
            }
        ],
    )
    _install_live_tmux(monkeypatch, live)
    proof = resolve_live_worker(root, live_team["run_id"], "w1")
    assert proof.pane_owner_nonce == "d" * 32
    assert proof.window_id == "@9"
    assert probe_exact_worker(proof) == STATUS_LIVE
    out = capture_worker(root, live_team["run_id"], "w1")
    assert out["ok"] is True
    assert out["status"] == STATUS_LIVE


def test_foreign_owner_nonce_fails_closed_for_capture_key_input(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale/foreign @omg_worker_nonce must refuse capture/key/input."""
    root = live_team["root"]
    owner = "e" * 32
    live = _bind_owner_nonce_on_worker(root, live_team["live"], owner_nonce=owner)
    # Capability must pass so identity mismatch remains the refusal reason.
    live = _stamp_interactive_io(root, live)
    monkeypatch.setattr(
        plane,
        "_load_team_identity_chain",
        lambda *_a, **_k: [
            {
                "session_name": live["session"],
                "session_id": live["session_id"],
                "launch_nonce": live["launch_nonce"],
                "tasks": [],
            }
        ],
    )
    # Live pane carries a different owner nonce than the receipt/meta expects.
    _install_live_tmux(
        monkeypatch,
        live,
        pane_owner_override={"%10": "f" * 32},
    )
    proof = resolve_live_worker(root, live_team["run_id"], "w1")
    assert proof.pane_owner_nonce == owner
    assert probe_exact_worker(proof) == STATUS_MISMATCH

    cap = capture_worker(root, live_team["run_id"], "w1")
    assert cap["ok"] is False
    assert cap["status"] == STATUS_MISMATCH

    with pytest.raises(OperatorError) as key_exc:
        key_worker(
            root,
            live_team["run_id"],
            "w1",
            "Enter",
            operator_override=True,
            is_tty=False,
        )
    assert key_exc.value.status == STATUS_MISMATCH

    with pytest.raises(OperatorError) as input_exc:
        input_worker(
            root,
            live_team["run_id"],
            "w1",
            "hi",
            operator_override=True,
            is_tty=True,
        )
    assert input_exc.value.status == STATUS_MISMATCH


def test_cli_normalize_reserves_operator_actions() -> None:
    from omg_cli.team.cli import RESERVED_ACTIONS, normalize_team_argv

    for name in ("panes", "capture", "focus", "key", "input", "watch", "view", "hyperplan", "security-research"):
        assert name in RESERVED_ACTIONS
    out = normalize_team_argv(["team", "panes", "--json"])
    assert out[1] == "panes"


# ---------------------------------------------------------------------------
# PR #156: worker must not invoke leader/operator controls (zero side effects)
# ---------------------------------------------------------------------------


def test_cli_worker_operator_mutations_refused_before_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """OMG_TEAM_WORKER=1 → input/key/focus/view refuse before project_root/tmux."""
    import argparse

    from omg_cli.commands.team import cmd_team
    from omg_cli.team.plane import TEAM_WORKER_ENV

    _enable_team(monkeypatch)
    monkeypatch.setenv(TEAM_WORKER_ENV, "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "w1")

    def _boom_root() -> Path:
        raise AssertionError("project_root must not run for worker operator refuse")

    monkeypatch.setattr("omg_cli.commands.team.project_root", _boom_root)

    # If operator helpers are imported, they must never be called.
    def _boom_op(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("operator helper must not run after worker refuse")

    monkeypatch.setattr(operator, "input_worker", _boom_op)
    monkeypatch.setattr(operator, "key_worker", _boom_op)
    monkeypatch.setattr(operator, "focus_worker", _boom_op)
    monkeypatch.setattr(operator, "list_panes", _boom_op)
    monkeypatch.setattr(operator, "capture_worker", _boom_op)

    cases = (
        argparse.Namespace(
            team_action="input",
            team_identity=None,
            run_id="run-x",
            worker_id="w2",
            input_text="hi",
            input_submit=True,
            operator_override=True,
            as_json=False,
            json_output=False,
        ),
        argparse.Namespace(
            team_action="key",
            team_identity=None,
            run_id="run-x",
            worker_id="w2",
            key_name="Enter",
            operator_override=True,
            as_json=False,
            json_output=False,
        ),
        argparse.Namespace(
            team_action="focus",
            team_identity=None,
            run_id="run-x",
            worker_id="w2",
            focus_execute=True,
            as_json=False,
            json_output=False,
        ),
        argparse.Namespace(
            team_action="view",
            team_identity=None,
            run_id="run-x",
            worker_id=None,
            view_print=False,
            view_takeover=True,
            as_json=False,
            json_output=False,
        ),
    )
    for args in cases:
        code = cmd_team(args)
        assert code == 2, args.team_action
        err = capsys.readouterr().err
        assert "E_TEAM_WORKER_OPERATION_REFUSED" in err, (
            args.team_action,
            err,
        )


def test_cli_worker_resume_still_nested_launch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """resume remains E_TEAM_NESTED_LAUNCH (lifecycle), not operator refuse."""
    import argparse

    from omg_cli.commands.team import cmd_team
    from omg_cli.team.plane import TEAM_WORKER_ENV

    _enable_team(monkeypatch)
    monkeypatch.setenv(TEAM_WORKER_ENV, "1")

    def _boom_root() -> Path:
        raise AssertionError("project_root must not run for nested-launch refuse")

    monkeypatch.setattr("omg_cli.commands.team.project_root", _boom_root)

    args = argparse.Namespace(
        team_action="resume",
        team_identity=None,
        run_id="run-x",
        as_json=False,
        json_output=False,
        resume_view=True,
        view_print=False,
        view_takeover=True,
        worker_id="w1",
        provider_session=False,
    )
    code = cmd_team(args)
    assert code == 2
    err = capsys.readouterr().err
    assert "E_TEAM_NESTED_LAUNCH" in err
    assert "E_TEAM_WORKER_OPERATION_REFUSED" not in err


def test_cli_worker_read_only_reaches_operator_list_panes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-only panes remains usable under OMG_TEAM_WORKER=1."""
    import argparse

    from omg_cli.commands.team import cmd_team
    from omg_cli.team.plane import TEAM_WORKER_ENV

    _enable_team(monkeypatch)
    _init_repo(tmp_path)
    monkeypatch.setenv(TEAM_WORKER_ENV, "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "w1")
    monkeypatch.setattr(
        "omg_cli.commands.team.project_root",
        lambda: tmp_path,
    )
    called: list[tuple[Any, ...]] = []

    def _fake_list(root: Path, identity: Any, **kwargs: Any) -> dict[str, Any]:
        called.append((root, identity, kwargs))
        return {
            "ok": True,
            "run_id": "run-ro",
            "panes": [],
            "note": "hermetic",
        }

    monkeypatch.setattr(operator, "list_panes", _fake_list)
    args = argparse.Namespace(
        team_action="panes",
        team_identity=None,
        run_id="run-ro",
        as_json=True,
        json_output=True,
    )
    code = cmd_team(args)
    assert code == 0
    assert called and called[0][0] == tmp_path

# #147 PR1 — capability refuse (integration with live_team fixtures)
# ---------------------------------------------------------------------------


def test_legacy_input_refuses_and_does_not_send(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing I/O fields → unsupported; send_literal never called."""
    root = live_team["root"]
    live = live_team["live"]
    effects: list[list[str]] = []
    _install_live_tmux(monkeypatch, live, effects=effects)
    send_literal = MagicMock()
    monkeypatch.setattr(operator, "send_literal", send_literal)
    with pytest.raises(OperatorError) as exc:
        input_worker(
            root,
            live_team["run_id"],
            "w1",
            "hello",
            operator_override=True,
            is_tty=True,
        )
    assert exc.value.code == E_OPERATOR_INPUT_UNSUPPORTED
    send_literal.assert_not_called()
    assert not any(cmd and cmd[0] == "send-keys" for cmd in effects)


def test_headless_key_refuses_and_does_not_send(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    live = dict(live)
    live["tasks"] = [
        {
            **live["tasks"][0],
            "io_mode": IO_MODE_HEADLESS_STREAM,
            "provider_tty_owner": TTY_OWNER_SUPERVISOR,
            "operator_input_supported": False,
            "input_ready": False,
        }
    ]
    plane._atomic_write_json(team_meta_path(root, str(live["run_id"])), live)
    effects: list[list[str]] = []
    _install_live_tmux(monkeypatch, live, effects=effects)
    send_key = MagicMock()
    monkeypatch.setattr(operator, "send_key", send_key)
    with pytest.raises(OperatorError) as exc:
        key_worker(
            root,
            live_team["run_id"],
            "w1",
            "Enter",
            operator_override=True,
            is_tty=False,
        )
    assert exc.value.code == E_OPERATOR_KEY_UNSUPPORTED
    send_key.assert_not_called()
    assert not any(cmd and cmd[0] == "send-keys" for cmd in effects)


def test_operator_override_cannot_bypass_capability(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    _install_live_tmux(monkeypatch, live)
    send_literal = MagicMock()
    send_key = MagicMock()
    monkeypatch.setattr(operator, "send_literal", send_literal)
    monkeypatch.setattr(operator, "send_key", send_key)
    with pytest.raises(OperatorError) as e1:
        input_worker(
            root,
            live_team["run_id"],
            "w1",
            "x",
            operator_override=True,
            is_tty=False,
        )
    assert e1.value.code == E_OPERATOR_INPUT_UNSUPPORTED
    with pytest.raises(OperatorError) as e2:
        key_worker(
            root,
            live_team["run_id"],
            "w1",
            "Tab",
            operator_override=True,
            is_tty=False,
        )
    assert e2.value.code == E_OPERATOR_KEY_UNSUPPORTED
    send_literal.assert_not_called()
    send_key.assert_not_called()


def test_supported_not_ready_refuses_before_send(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    live = dict(live)
    live["tasks"] = [
        {
            **live["tasks"][0],
            "io_mode": IO_MODE_INTERACTIVE_TTY,
            "provider_tty_owner": "provider",
            "operator_input_supported": True,
            "input_ready": False,
        }
    ]
    plane._atomic_write_json(team_meta_path(root, str(live["run_id"])), live)
    _install_live_tmux(monkeypatch, live)
    send_literal = MagicMock()
    monkeypatch.setattr(operator, "send_literal", send_literal)
    with pytest.raises(OperatorError) as exc:
        input_worker(
            root,
            live_team["run_id"],
            "w1",
            "hi",
            operator_override=True,
            is_tty=True,
        )
    assert exc.value.code == E_OPERATOR_INPUT_NOT_READY
    send_literal.assert_not_called()


def test_json_noop_before_capability_and_side_effects(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--json still short-circuits before capability load side effects matter."""
    root = live_team["root"]
    live = live_team["live"]
    _install_live_tmux(monkeypatch, live)
    send_literal = MagicMock()
    monkeypatch.setattr(operator, "send_literal", send_literal)
    with pytest.raises(OperatorError) as e1:
        key_worker(root, live_team["run_id"], "w1", "Enter", as_json=True)
    assert e1.value.code == "E_OPERATOR_JSON_NOOP"
    with pytest.raises(OperatorError) as e2:
        input_worker(
            root,
            live_team["run_id"],
            "w1",
            "hi",
            as_json=True,
            operator_override=True,
        )
    assert e2.value.code == "E_OPERATOR_JSON_NOOP"
    send_literal.assert_not_called()
