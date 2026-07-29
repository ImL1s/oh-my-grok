"""#29 Phase 2: inspect family handlers live under omg_cli.commands.inspect."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omg_cli.commands import inspect as inspect_cmds
from omg_cli.main import (
    build_parser,
    cmd_capabilities,
    cmd_hud,
    cmd_lsp,
    cmd_native_status,
    cmd_notify,
    cmd_parity,
    cmd_wiki,
    main,
)


INSPECT_CMDS = (
    "wiki",
    "hud",
    "lsp",
    "notify",
    "native-status",
    "capabilities",
    "parity",
)


def test_main_reexports_inspect_handlers() -> None:
    assert cmd_wiki is inspect_cmds.cmd_wiki
    assert cmd_hud is inspect_cmds.cmd_hud
    assert cmd_lsp is inspect_cmds.cmd_lsp
    assert cmd_notify is inspect_cmds.cmd_notify
    assert cmd_native_status is inspect_cmds.cmd_native_status
    assert cmd_capabilities is inspect_cmds.cmd_capabilities
    assert cmd_parity is inspect_cmds.cmd_parity
    assert inspect_cmds.register_inspect_parsers is not None


def test_parser_wires_inspect_handlers() -> None:
    parser = build_parser()
    samples = {
        "wiki": ["wiki", "list"],
        "hud": ["hud", "--json"],
        "lsp": ["lsp", "status"],
        "notify": ["notify", "status"],
        "native-status": ["native-status"],
        "capabilities": ["capabilities"],
        "parity": ["parity", "release-readback", "--manifest", "x.json"],
    }
    for name in INSPECT_CMDS:
        ns = parser.parse_args(samples[name])
        assert callable(getattr(ns, "func", None))
        # Must be the inspect-module implementation
        assert ns.func.__module__ == "omg_cli.commands.inspect", name


def test_inspect_help_lists_primary_commands() -> None:
    parser = build_parser()
    help_text = parser.format_help()
    for name in INSPECT_CMDS:
        assert name in help_text


def test_register_inspect_parsers_phase_all_matches_build() -> None:
    """phase=all wires the same handlers as main's early+late calls."""
    import argparse

    from omg_cli.commands.inspect import register_inspect_parsers

    root = argparse.ArgumentParser()
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", dest="json_output", action="store_true")
    sub = root.add_subparsers(dest="command")
    register_inspect_parsers(sub, common, phase="all")
    ns = root.parse_args(["wiki", "list"])
    assert ns.func.__module__ == "omg_cli.commands.inspect"
    ns = root.parse_args(["lsp", "status"])
    assert ns.func is inspect_cmds.cmd_lsp


def test_lsp_status_via_main_still_works(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["lsp", "status"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ownership"] == "host_owned"


def test_hud_json_via_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(["hud", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, dict)
