"""#29 Phase 2: team family + interview/goal under commands/."""

from __future__ import annotations

from omg_cli.commands import team as team_cmds
from omg_cli.commands import workflow as workflow_cmds
from omg_cli.main import (
    build_parser,
    cmd_accept,
    cmd_goal,
    cmd_integrate,
    cmd_interview,
    cmd_team,
    cmd_worker,
    cmd_workflow,
)


def test_main_reexports_team_and_workflow_extras() -> None:
    assert cmd_accept is team_cmds.cmd_accept
    assert cmd_integrate is team_cmds.cmd_integrate
    assert cmd_team is team_cmds.cmd_team
    assert cmd_worker is team_cmds.cmd_worker
    assert cmd_interview is workflow_cmds.cmd_interview
    assert cmd_goal is workflow_cmds.cmd_goal
    assert cmd_workflow is workflow_cmds.cmd_workflow


def test_parser_wires_team_handlers() -> None:
    parser = build_parser()
    samples = {
        "accept": (["accept", "--run", "r1", "--yes"], team_cmds.cmd_accept),
        "integrate": (["integrate", "--run", "r1"], team_cmds.cmd_integrate),
        "team": (["team", "status", "--run", "r1"], team_cmds.cmd_team),
        "worker": (
            [
                "worker",
                "own",
                "--run",
                "r1",
                "--tasks-json",
                '[{"task_id":"t1","owned_files":["a.py"]}]',
            ],
            team_cmds.cmd_worker,
        ),
        "interview": (["interview", "status", "--run", "r1"], workflow_cmds.cmd_interview),
        "goal": (["goal", "status"], workflow_cmds.cmd_goal),
    }
    for name, (argv, expected) in samples.items():
        ns = parser.parse_args(argv)
        assert ns.func is expected, name
        assert ns.func.__module__.startswith("omg_cli.commands."), name


def test_no_handler_defs_left_in_main() -> None:
    """Phase 2 complete: main should not define cmd_* handlers."""
    import omg_cli.main as main_mod
    import inspect

    for name, obj in vars(main_mod).items():
        if not name.startswith("cmd_"):
            continue
        # re-exports are fine; definitions would have main_mod as module
        if inspect.isfunction(obj):
            assert obj.__module__ != "omg_cli.main", name
