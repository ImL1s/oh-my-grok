"""#146 / PR #156: parsed-argv Team worker preflight before project-root / git.

Hermetic: refused worker surfaces must not call resolver, git, tmux, or
state writers. Legal supervisor / api / read-only must still reach the
intended resolution path.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from omg_cli.deny import _TEAM_NESTED_LAUNCH_OPS, is_first_party_team_nested_launch
from omg_cli.main import main
from omg_cli.project_root import ProjectRootResolution
from omg_cli.team.cli import RESERVED_ACTIONS
from omg_cli.team.plane import (
    EXPERIMENTAL_ENV,
    LEADER_ONLY_COMPOSITION_ACTIONS,
    LEADER_ONLY_OPERATOR_ACTIONS,
    NESTED_LAUNCH_ACTIONS,
    TEAM_WORKER_ENV,
    TeamGateError,
    preflight_team_worker_parsed_argv,
)

MISSING_ROOT = "/definitely-missing-omg-146-preflight"
_GENERIC_ROOT_ERR = ("--project-root does not exist", "does not exist")

_LEGAL_CONTINUE = frozenset(
    {
        "status",
        "panes",
        "capture",
        "api",
        "supervisor",
        "watch",
        "worker-ready",
        "hyperplan",
        "security-research",
        "help",
        "-h",
        "--help",
    }
)


def _enable_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(EXPERIMENTAL_ENV, "1")
    monkeypatch.delenv("OMG_DISABLE_TMUX_TEAM", raising=False)


def _worker_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_team(monkeypatch)
    monkeypatch.setenv(TEAM_WORKER_ENV, "1")
    monkeypatch.setenv("OMG_TEAM_WORKER_ID", "w1")
    monkeypatch.delenv("OMG_PROJECT_ROOT", raising=False)


def _boom(*_a: Any, **_k: Any) -> Any:
    raise AssertionError("resolver/git/tmux/state must not run after worker preflight")


def _patch_refused_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omg_cli.project_root.resolve_project_root", _boom)
    monkeypatch.setattr("omg_cli.project_root.git_toplevel", _boom)
    monkeypatch.setattr("omg_cli.project_root.clear_resolved_project_root", _boom)
    monkeypatch.setattr(
        "omg_cli.team.bootstrap.resolve_supervisor_project_root", _boom
    )
    monkeypatch.setattr("omg_cli.team.operator.input_worker", _boom)
    monkeypatch.setattr("omg_cli.team.operator.key_worker", _boom)
    monkeypatch.setattr("omg_cli.team.operator.focus_worker", _boom)
    monkeypatch.setattr("omg_cli.team.plane.start_team", _boom)
    monkeypatch.setattr("omg_cli.team.plane.stop_team", _boom)
    monkeypatch.setattr("omg_cli.state.write_status", _boom)
    _orig_run = subprocess.run

    def _boom_git_or_tmux_run(argv: Any, *a: Any, **k: Any) -> Any:
        tokens = [str(x) for x in list(argv or ())]
        head = tokens[0] if tokens else ""
        is_git = head == "git" or head.endswith("/git") or head.endswith("\\git")
        is_tmux = head == "tmux" or head.endswith("/tmux") or head.endswith("\\tmux")
        if (is_git and "rev-parse" in tokens) or is_tmux:
            raise AssertionError(
                "git rev-parse / tmux must not run after worker preflight"
            )
        return _orig_run(argv, *a, **k)

    # project_root / plane / tmux `import subprocess` — same module object.
    monkeypatch.setattr("omg_cli.project_root.subprocess.run", _boom_git_or_tmux_run)
    monkeypatch.setattr("omg_cli.team.plane.subprocess.run", _boom_git_or_tmux_run)
    monkeypatch.setattr("omg_cli.team.tmux.subprocess.run", _boom_git_or_tmux_run)
    monkeypatch.setattr("omg_cli.team.plane._tmux_run", _boom)
    monkeypatch.setattr("omg_cli.team.tmux._tmux_run", _boom)
    monkeypatch.setattr("omg_cli.team.plane.os.killpg", _boom)
    monkeypatch.setattr("omg_cli.team.plane.os.kill", _boom)
    monkeypatch.setattr("omg_cli.team.tmux.os.killpg", _boom)
    monkeypatch.setattr("omg_cli.team.tmux.os.kill", _boom)
    monkeypatch.setattr("omg_cli.team.plane.mutate_team_meta", _boom)
    monkeypatch.setattr("omg_cli.state.write_pid_metadata", _boom)
    monkeypatch.setattr("omg_cli.state.prepare_leader_spawn", _boom)
    monkeypatch.setattr("omg_cli.team.runtime.launch_team", _boom)

    def _boom_popen(*_a: Any, **_k: Any) -> Any:
        raise AssertionError(
            "subprocess.Popen / _SYSTEM_POPEN must not run after worker preflight"
        )

    monkeypatch.setattr("subprocess.Popen", _boom_popen)
    monkeypatch.setattr("omg_cli.state.subprocess.Popen", _boom_popen)
    monkeypatch.setattr("omg_cli.team.plane.subprocess.Popen", _boom_popen)
    monkeypatch.setattr("omg_cli.team.tmux.subprocess.Popen", _boom_popen)
    monkeypatch.setattr("omg_cli.state._SYSTEM_POPEN", _boom_popen)


def _dummy_resolution(root: Path) -> ProjectRootResolution:
    return ProjectRootResolution(root=root, source="cwd", cwd=root)


def _assert_typed(err: str, code: str) -> None:
    assert code in err
    for generic in _GENERIC_ROOT_ERR:
        assert generic not in err, err


# ---------------------------------------------------------------------------
# Vocab lock + unit preflight
# ---------------------------------------------------------------------------


def test_nested_launch_actions_vocab_lock() -> None:
    assert NESTED_LAUNCH_ACTIONS == frozenset(
        {
            "launch",
            "start",
            "run",
            "scale",
            "resume",
            "stop",
            "collect",
            "shutdown",
        }
    )
    assert NESTED_LAUNCH_ACTIONS.isdisjoint(LEADER_ONLY_OPERATOR_ACTIONS)
    assert LEADER_ONLY_COMPOSITION_ACTIONS.isdisjoint(NESTED_LAUNCH_ACTIONS)
    assert LEADER_ONLY_COMPOSITION_ACTIONS.isdisjoint(LEADER_ONLY_OPERATOR_ACTIONS)
    assert "supervisor" not in NESTED_LAUNCH_ACTIONS
    assert NESTED_LAUNCH_ACTIONS <= _TEAM_NESTED_LAUNCH_OPS
    leftover = RESERVED_ACTIONS - (
        NESTED_LAUNCH_ACTIONS | LEADER_ONLY_OPERATOR_ACTIONS | _LEGAL_CONTINUE
    )
    assert leftover == frozenset(), f"undocumented reserved ops: {sorted(leftover)}"


def test_preflight_skips_non_team_command(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TEAM_WORKER_ENV, "1")
    preflight_team_worker_parsed_argv("launch", command="doctor")
    preflight_team_worker_parsed_argv(None, command="state")


def test_preflight_empty_action_is_launch_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEAM_WORKER_ENV, "1")
    for action in (None, "", "   "):
        with pytest.raises(TeamGateError) as ei:
            preflight_team_worker_parsed_argv(action)
        assert ei.value.code == "E_TEAM_NESTED_LAUNCH"


def test_preflight_process_env_only_not_command_text() -> None:
    """Command-text mappings never authorize; only the env argument / process env."""
    with pytest.raises(TeamGateError) as ei:
        preflight_team_worker_parsed_argv(
            "launch",
            env={TEAM_WORKER_ENV: "1"},
        )
    assert ei.value.code == "E_TEAM_NESTED_LAUNCH"
    # Explicit non-worker mapping is a no-op even if a second unused mapping
    # would look like an assignment in argv.
    preflight_team_worker_parsed_argv(
        "launch",
        env={TEAM_WORKER_ENV: "0", "PATH": "/bin"},
    )


def test_preflight_legal_actions_pass_in_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEAM_WORKER_ENV, "1")
    for action in (
        "api",
        "supervisor",
        "status",
        "panes",
        "capture",
        "watch",
        "worker-ready",
        "hyperplan",
        "security-research",
        "help",
    ):
        preflight_team_worker_parsed_argv(action, command="team")


_LEADER_COMPOSITION_ACTIONS = (
    "materialize",
    "validate-decision",
    "validate-report",
    "produce-decision",
    "produce-report",
    "admit-tasks",
    "collect-tasks",
)


@pytest.mark.parametrize("action", ("hyperplan", "security-research"))
@pytest.mark.parametrize("composition_action", _LEADER_COMPOSITION_ACTIONS)
def test_preflight_worker_refuses_leader_composition_publication(
    action: str,
    composition_action: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEAM_WORKER_ENV, "1")
    with pytest.raises(TeamGateError) as ei:
        preflight_team_worker_parsed_argv(
            action,
            command="team",
            composition_action=composition_action,
        )
    assert ei.value.code == "E_TEAM_WORKER_OPERATION_REFUSED"
    assert "leader-owned composition publication/decision" in str(ei.value)


@pytest.mark.parametrize("action", ("hyperplan", "security-research"))
@pytest.mark.parametrize(
    "composition_action",
    (None, "", "   ", "plan", "claim-lane", "submit-lane-result", "unknown-sub"),
)
def test_preflight_worker_allows_non_leader_composition_subactions(
    action: str,
    composition_action: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TEAM_WORKER_ENV, "1")
    preflight_team_worker_parsed_argv(
        action,
        command="team",
        composition_action=composition_action,
    )


# ---------------------------------------------------------------------------
# main() refused classes — no resolver / git / tmux / state
# ---------------------------------------------------------------------------


def test_main_worker_launch_missing_project_root_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    rc = main(
        [
            "team",
            "launch",
            "--workers",
            "1",
            "--goal",
            "x",
            "--project-root",
            MISSING_ROOT,
        ]
    )
    assert rc == 2
    _assert_typed(capsys.readouterr().err, "E_TEAM_NESTED_LAUNCH")


def test_main_worker_launch_prefix_project_root_typed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    rc = main(
        [
            "--project-root",
            MISSING_ROOT,
            "team",
            "launch",
            "--workers",
            "1",
            "--goal",
            "x",
        ]
    )
    assert rc == 2
    _assert_typed(capsys.readouterr().err, "E_TEAM_NESTED_LAUNCH")


def test_main_worker_bare_team_nested_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    rc = main(["team"])
    assert rc == 2
    _assert_typed(capsys.readouterr().err, "E_TEAM_NESTED_LAUNCH")


@pytest.mark.parametrize(
    "marker",
    ["OMG_PROCESS_FANOUT_WORKER", "OMG_SPAWNED_WORKER"],
)
def test_main_other_worker_markers_refuse_launch_before_root(
    marker: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _enable_team(monkeypatch)
    monkeypatch.setenv(marker, "1")
    monkeypatch.delenv(TEAM_WORKER_ENV, raising=False)
    monkeypatch.delenv("OMG_TEAM_WORKER_ID", raising=False)
    monkeypatch.delenv("OMG_PROJECT_ROOT", raising=False)
    _patch_refused_side_effects(monkeypatch)
    rc = main(
        [
            "--project-root",
            MISSING_ROOT,
            "team",
            "launch",
            "--workers",
            "1",
            "--goal",
            "x",
        ]
    )
    assert rc == 2
    _assert_typed(capsys.readouterr().err, "E_TEAM_NESTED_LAUNCH")


def test_main_worker_launch_without_project_root_no_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    rc = main(["team", "launch", "--workers", "1", "--goal", "x"])
    assert rc == 2
    _assert_typed(capsys.readouterr().err, "E_TEAM_NESTED_LAUNCH")


def test_main_worker_goal_shorthand_nested_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    rc = main(["team", "fix the flaky tests"])
    assert rc == 2
    _assert_typed(capsys.readouterr().err, "E_TEAM_NESTED_LAUNCH")


def test_main_worker_numeric_shorthand_nested_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    rc = main(["team", "3:executor", "fix it"])
    assert rc == 2
    _assert_typed(capsys.readouterr().err, "E_TEAM_NESTED_LAUNCH")


def test_main_worker_prefix_project_root_form_b_shorthand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--project-root /missing team fix …`` is Form B, not argparse SystemExit."""
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    rc = main(["--project-root", MISSING_ROOT, "team", "fix the flaky tests"])
    assert rc == 2
    _assert_typed(capsys.readouterr().err, "E_TEAM_NESTED_LAUNCH")


@pytest.mark.parametrize(
    "prefix",
    [
        ["--json"],
        ["--safe"],
        ["--yolo"],
        ["--json", "--safe"],
        ["--project-root", MISSING_ROOT, "--json"],
    ],
)
def test_main_worker_leading_globals_form_a_shorthand(
    prefix: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Leading globals + Form A must gate before argparse SystemExit / side effects."""
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    rc = main([*prefix, "team", "3:executor", "fix the flaky tests"])
    assert rc == 2
    _assert_typed(capsys.readouterr().err, "E_TEAM_NESTED_LAUNCH")


def test_main_worker_shutdown_alias_nested_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    rc = main(["team", "shutdown"])
    assert rc == 2
    err = capsys.readouterr().err
    _assert_typed(err, "E_TEAM_NESTED_LAUNCH")
    assert "E_TEAM_WORKER_OPERATION_REFUSED" not in err


@pytest.mark.parametrize(
    "argv",
    [
        ["team", "start", "--goal", "x", "--tasks-json", "[]"],
        ["team", "run", "--goal", "x"],
        ["team", "scale", "--run", "r1", "--add", "1"],
        ["team", "resume", "--run", "r1"],
        ["team", "stop", "--run", "r1"],
        ["team", "collect", "--run", "r1"],
    ],
)
def test_main_worker_lifecycle_verbs_nested_launch(
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    rc = main(argv)
    assert rc == 2
    err = capsys.readouterr().err
    _assert_typed(err, "E_TEAM_NESTED_LAUNCH")
    assert "E_TEAM_WORKER_OPERATION_REFUSED" not in err


@pytest.mark.parametrize(
    "argv",
    [
        ["team", "input", "--text", "hi", "--worker", "w2", "--operator-override"],
        ["team", "key", "--key", "Enter", "--worker", "w2", "--operator-override"],
        ["team", "focus", "--worker", "w2", "--execute"],
        ["team", "view", "--takeover"],
    ],
)
def test_main_worker_operator_refused_not_nested(
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    rc = main(argv)
    assert rc == 2
    err = capsys.readouterr().err
    _assert_typed(err, "E_TEAM_WORKER_OPERATION_REFUSED")
    assert "E_TEAM_NESTED_LAUNCH" not in err


def test_main_worker_command_text_assignment_does_not_authorize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Process env stays worker=1; argv-looking assignment is just a goal string."""
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    rc = main(
        [
            "team",
            "launch",
            "--workers",
            "1",
            "--goal",
            "OMG_TEAM_WORKER=0",
        ]
    )
    assert rc == 2
    _assert_typed(capsys.readouterr().err, "E_TEAM_NESTED_LAUNCH")


def test_path_prefixed_omg_team_launch_still_deny_nested() -> None:
    """Path wrappers are deny-only; main() sees post-binary argv."""
    assert is_first_party_team_nested_launch("/opt/omg/bin/omg team launch") is True
    assert is_first_party_team_nested_launch("/usr/local/bin/omg team start") is True
    assert is_first_party_team_nested_launch("/opt/omg/bin/omg team api catalog") is False
    assert is_first_party_team_nested_launch("env omg team launch") is True
    assert is_first_party_team_nested_launch("env omg team api catalog") is False


# ---------------------------------------------------------------------------
# Legal forms still reach intended resolution
# ---------------------------------------------------------------------------


def test_main_worker_api_catalog_skips_root_and_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    monkeypatch.setattr("omg_cli.project_root.resolve_project_root", _boom)
    monkeypatch.setattr("omg_cli.project_root.git_toplevel", _boom)
    monkeypatch.setattr(
        "omg_cli.team.bootstrap.resolve_supervisor_project_root", _boom
    )
    rc = main(["team", "api", "catalog"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "E_TEAM_NESTED_LAUNCH" not in captured.err
    assert "E_TEAM_WORKER_OPERATION_REFUSED" not in captured.err
    doc = json.loads(captured.out)
    assert doc.get("kind") == "omg.team.operation_catalog"


def test_main_worker_status_reaches_status_helper_not_preflight_refuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    called: list[str] = []

    def _spy_resolve(**kwargs: Any) -> ProjectRootResolution:
        called.append("resolve_project_root")
        return _dummy_resolution(tmp_path)

    def _spy_status(*_a: Any, **_k: Any) -> dict[str, Any]:
        called.append("status_for_identity")
        return {"run_id": "r1", "tasks": []}

    monkeypatch.setattr("omg_cli.project_root.resolve_project_root", _spy_resolve)
    monkeypatch.setattr("omg_cli.team.runtime.status_for_identity", _spy_status)
    rc = main(["team", "status", "--json"])
    assert called == ["resolve_project_root", "status_for_identity"]
    err = capsys.readouterr().err
    assert "E_TEAM_NESTED_LAUNCH" not in err
    assert "E_TEAM_WORKER_OPERATION_REFUSED" not in err
    assert rc == 0


def test_main_worker_supervisor_reaches_supervisor_root_not_nested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    called: list[str] = []

    def _spy_supervisor(*_a: Any, **_k: Any) -> ProjectRootResolution:
        called.append("resolve_supervisor_project_root")
        return _dummy_resolution(tmp_path)

    def _boom_generic(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("generic resolve_project_root must not run for supervisor")

    monkeypatch.setattr(
        "omg_cli.team.bootstrap.resolve_supervisor_project_root", _spy_supervisor
    )
    monkeypatch.setattr("omg_cli.project_root.resolve_project_root", _boom_generic)
    rc = main(["team", "supervisor", "--descriptor", "/nope.json"])
    assert called == ["resolve_supervisor_project_root"]
    err = capsys.readouterr().err
    assert "E_TEAM_NESTED_LAUNCH" not in err
    assert "E_TEAM_WORKER_OPERATION_REFUSED" not in err
    assert rc != 0  # missing descriptor / later bootstrap fail is OK


def test_main_worker_panes_reaches_list_panes_not_operator_refuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    called: list[str] = []

    def _spy_resolve(**kwargs: Any) -> ProjectRootResolution:
        called.append("resolve_project_root")
        return _dummy_resolution(tmp_path)

    def _spy_panes(*_a: Any, **_k: Any) -> dict[str, Any]:
        called.append("list_panes")
        return {"run_id": "r1", "command": "team.panes", "panes": [], "count": 0}

    monkeypatch.setattr("omg_cli.project_root.resolve_project_root", _spy_resolve)
    monkeypatch.setattr("omg_cli.team.operator.list_panes", _spy_panes)
    rc = main(["team", "panes", "--json"])
    assert called == ["resolve_project_root", "list_panes"]
    err = capsys.readouterr().err
    assert "E_TEAM_NESTED_LAUNCH" not in err
    assert "E_TEAM_WORKER_OPERATION_REFUSED" not in err
    assert rc == 0


def test_main_worker_capture_reaches_capture_worker_not_preflight_refuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    called: list[str] = []

    def _spy_resolve(**kwargs: Any) -> ProjectRootResolution:
        called.append("resolve_project_root")
        return _dummy_resolution(tmp_path)

    def _spy_capture(*_a: Any, **_k: Any) -> dict[str, Any]:
        called.append("capture_worker")
        return {
            "ok": True,
            "command": "team.capture",
            "status": "live",
            "text": "",
            "bytes": 0,
        }

    monkeypatch.setattr("omg_cli.project_root.resolve_project_root", _spy_resolve)
    monkeypatch.setattr("omg_cli.team.operator.capture_worker", _spy_capture)
    rc = main(["team", "capture", "--worker", "w1", "--json"])
    assert called == ["resolve_project_root", "capture_worker"]
    err = capsys.readouterr().err
    assert "E_TEAM_NESTED_LAUNCH" not in err
    assert "E_TEAM_WORKER_OPERATION_REFUSED" not in err
    assert rc == 0


def test_main_worker_api_mailbox_list_reaches_execute_api_not_preflight_refuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    called: list[str] = []

    def _spy_resolve(**kwargs: Any) -> ProjectRootResolution:
        called.append("resolve_project_root")
        return _dummy_resolution(tmp_path)

    def _spy_api(*_a: Any, **_k: Any) -> tuple[int, dict[str, Any]]:
        called.append("execute_team_api")
        return 0, {"ok": True, "operation": "mailbox-list"}

    monkeypatch.setattr("omg_cli.project_root.resolve_project_root", _spy_resolve)
    monkeypatch.setattr(
        "omg_cli.team.api.resolve_team_api_cli_root",
        lambda *_a, **_k: tmp_path,
    )
    monkeypatch.setattr("omg_cli.team.api.execute_team_api", _spy_api)
    rc = main(
        [
            "team",
            "api",
            "mailbox-list",
            "--input",
            '{"run_id":"r1"}',
            "--json",
        ]
    )
    assert called == ["resolve_project_root", "execute_team_api"]
    err = capsys.readouterr().err
    assert "E_TEAM_NESTED_LAUNCH" not in err
    assert "E_TEAM_WORKER_OPERATION_REFUSED" not in err
    assert rc == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["team", "hyperplan", "materialize", "--spec", "x.json", "--run", "r1"],
        [
            "team",
            "hyperplan",
            "validate-decision",
            "--run",
            "r1",
            "--input",
            "d.json",
        ],
        [
            "team",
            "hyperplan",
            "produce-decision",
            "--run",
            "r1",
            "--input",
            "b.json",
        ],
        ["team", "hyperplan", "admit-tasks", "--run", "r1", "--team-id", "t"],
        ["team", "hyperplan", "collect-tasks", "--run", "r1", "--team-id", "t"],
        [
            "team",
            "security-research",
            "materialize",
            "--spec",
            "x.json",
            "--run",
            "r1",
        ],
        [
            "team",
            "security-research",
            "validate-report",
            "--run",
            "r1",
            "--input",
            "d.json",
        ],
        [
            "team",
            "security-research",
            "produce-report",
            "--run",
            "r1",
            "--input",
            "b.json",
        ],
        [
            "team",
            "security-research",
            "admit-tasks",
            "--run",
            "r1",
            "--team-id",
            "t",
        ],
        [
            "team",
            "security-research",
            "collect-tasks",
            "--run",
            "r1",
            "--team-id",
            "t",
        ],
    ],
)
def test_main_worker_composition_publication_refused_before_side_effects(
    argv: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    rc = main(argv)
    assert rc == 2
    err = capsys.readouterr().err
    _assert_typed(err, "E_TEAM_WORKER_OPERATION_REFUSED")
    assert "E_TEAM_NESTED_LAUNCH" not in err
    assert "leader-owned composition publication/decision" in err


def test_cmd_team_hyperplan_materialize_did_before_project_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Direct cmd_team(hyperplan materialize) refuses before project_root."""
    from omg_cli.commands.team import cmd_team

    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    monkeypatch.setattr("omg_cli.commands.team.project_root", _boom)
    args = argparse.Namespace(
        team_action="hyperplan",
        hyperplan_action="materialize",
        hyperplan_spec="x.json",
        run_id="r1",
        as_json=False,
        json_output=False,
    )
    rc = cmd_team(args)
    assert rc == 2
    _assert_typed(capsys.readouterr().err, "E_TEAM_WORKER_OPERATION_REFUSED")


def test_cmd_team_launch_did_nested_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Direct cmd_team(launch) refuses before project_root / launch_team."""
    from omg_cli.commands.team import cmd_team

    monkeypatch.chdir(tmp_path)
    _worker_env(monkeypatch)
    _patch_refused_side_effects(monkeypatch)
    monkeypatch.setattr("omg_cli.commands.team.project_root", _boom)
    args = argparse.Namespace(
        team_action="launch",
        goal="x",
        workers=1,
        role="executor",
        routing=None,
        plan_only=False,
        dry_run=False,
        materialize_only=False,
        detach=False,
        dedicated_window=False,
        force=False,
        yolo=False,
        safe=False,
        run_id=None,
        worker_topology=None,
        as_json=False,
        json_output=False,
    )
    rc = cmd_team(args)
    assert rc == 2
    _assert_typed(capsys.readouterr().err, "E_TEAM_NESTED_LAUNCH")


def test_leading_globals_do_not_host_launch_bypass() -> None:
    """Supported CLI forms stay on the typed gate; they are not host-launch."""
    from omg_cli.command_registry import KNOWN_SUBCOMMANDS
    from omg_cli.host_launcher import should_host_launch

    assert (
        should_host_launch(
            ["--json", "team", "3:executor", "fix"], KNOWN_SUBCOMMANDS
        )
        is False
    )
    assert (
        should_host_launch(
            ["--project-root", MISSING_ROOT, "team", "fix it"],
            KNOWN_SUBCOMMANDS,
        )
        is False
    )
    assert (
        should_host_launch(["--safe", "team", "3:executor", "x"], KNOWN_SUBCOMMANDS)
        is False
    )
    assert should_host_launch(["--yolo", "team", "fix it"], KNOWN_SUBCOMMANDS) is False
