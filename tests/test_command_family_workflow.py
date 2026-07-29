"""#29 Phase 2: workflow family under omg_cli.commands.workflow."""

from __future__ import annotations

import pytest

from omg_cli.commands import workflow as workflow_cmds
from omg_cli.main import build_parser, cmd_workflow


def test_main_reexports_workflow_handler() -> None:
    assert cmd_workflow is workflow_cmds.cmd_workflow
    assert callable(workflow_cmds.register_workflow_parsers)


def test_parser_wires_workflow_handlers() -> None:
    parser = build_parser()
    for argv in (
        ["workflow", "list"],
        ["workflow", "show", "demo"],
        ["workflow", "install", "wf.json"],
        ["workflow", "plan", "demo", "--input", "in.json"],
        [
            "workflow",
            "run",
            "demo",
            "--input",
            "in.json",
            "--receipts",
            "r.json",
        ],
    ):
        ns = parser.parse_args(argv)
        assert ns.func is workflow_cmds.cmd_workflow
        assert ns.func.__module__ == "omg_cli.commands.workflow"


def test_workflow_receipts_array_and_map() -> None:
    arr = workflow_cmds.workflow_receipts(
        [{"task_id": "a", "ok": True}, {"task_id": "b", "ok": False}]
    )
    assert set(arr) == {"a", "b"}
    mapped = workflow_cmds.workflow_receipts(
        {"a": {"task_id": "a"}, "b": {"task_id": "b"}}
    )
    assert set(mapped) == {"a", "b"}
    with pytest.raises(ValueError, match="duplicated"):
        workflow_cmds.workflow_receipts(
            [{"task_id": "a"}, {"task_id": "a"}]
        )


def test_workflow_in_root_help() -> None:
    assert "workflow" in build_parser().format_help()
