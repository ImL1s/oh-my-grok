"""Identity-fenced team pane operator control (#101)."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from omg_cli.evidence import CLI_WRITER
from omg_cli.main import build_parser
from omg_cli.team import operator, plane, tmux
from omg_cli.team.operator import (
    OperatorError,
    STATUS_GONE,
    STATUS_LIVE,
    STATUS_MISMATCH,
    STATUS_UNKNOWN,
    authorize_key,
    capture_worker,
    focus_worker,
    input_worker,
    key_worker,
    list_panes,
    probe_exact_worker,
    resolve_live_worker,
    watch_worker,
)
from omg_cli.team.plane import EXPERIMENTAL_ENV, start_team, team_meta_path


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
    capture_text: str = "hello secret=TOKEN\n",
    effects: list[list[str]] | None = None,
) -> list[list[str]]:
    expected_nonce = nonce if nonce is not None else str(live["launch_nonce"])
    commands = effects if effects is not None else []
    dead = dead_panes or set()
    pid_override = pane_pid_override or {}

    def run(args: Any, **_kw: Any) -> MagicMock:
        command = list(args)
        # Strip leading tmux binary if present (_tmux_run includes it).
        if command and command[0] == "tmux":
            command = command[1:]
        commands.append(command)
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
                if "#{pane_dead}" in str(fmt):
                    result.stdout = f"{target}\t{dead_flag}\t{sid}\t{pid}\n"
                else:
                    result.stdout = f"{target}\t{pid}\n"
            else:
                result.returncode = 1
                result.stderr = "can't find pane"
        elif op == "show-options":
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
    assert seen[0] == ["send-keys", "-t", "%10", "Enter"]
    assert seen[1] == ["send-keys", "-l", "-t", "%10", "--", "hello"]

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
    live = live_team["live"]
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
        operator,
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


def test_key_and_input_audit_no_raw_text(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
    effects: list[list[str]] = []
    _install_live_tmux(monkeypatch, live, effects=effects)

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
    assert out["text_sha256"] == hashlib.sha256(secret.encode()).hexdigest()
    assert out["text_length"] == len(secret.encode())

    audit_path = plane.team_dir(root, live_team["run_id"]) / "operator-audit.jsonl"
    body = audit_path.read_text(encoding="utf-8")
    assert secret not in body
    assert "Enter" in body
    assert "text_sha256" in body
    # send-keys effects present
    assert any(cmd[:1] == ["send-keys"] for cmd in effects)


def test_key_requires_tty_or_operator_override(
    live_team: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    root = live_team["root"]
    live = live_team["live"]
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
    # Override path still delivers.
    out = key_worker(
        root,
        live_team["run_id"],
        "w1",
        "Tab",
        is_tty=False,
        operator_override=True,
    )
    assert out["delivered"] is True
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
    live = live_team["live"]
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
    live = live_team["live"]
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
    """--submit must re-prove identity before Enter; flip after literal blocks it."""
    root = live_team["root"]
    live = live_team["live"]
    effects: list[list[str]] = []
    _install_live_tmux(monkeypatch, live, effects=effects)

    # authorize LIVE → literal re-proof LIVE → Enter re-proof MISMATCH
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
    # Literal delivered; Enter must not have been sent.
    send_cmds = [cmd for cmd in effects if cmd and cmd[0] == "send-keys"]
    assert any("-l" in cmd for cmd in send_cmds)
    assert not any(cmd[-1] == "Enter" and "-l" not in cmd for cmd in send_cmds)


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


def test_cli_normalize_reserves_operator_actions() -> None:
    from omg_cli.team.cli import RESERVED_ACTIONS, normalize_team_argv

    for name in ("panes", "capture", "focus", "key", "input", "watch"):
        assert name in RESERVED_ACTIONS
    out = normalize_team_argv(["team", "panes", "--json"])
    assert out[1] == "panes"
