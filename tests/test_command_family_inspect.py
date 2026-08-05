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


def _subparser_choices(parser, dest_cmd: str) -> set[str]:
    import argparse

    for act in parser._actions:
        if isinstance(act, argparse._SubParsersAction):
            top = act.choices
            if dest_cmd in top:
                for a2 in top[dest_cmd]._actions:
                    if isinstance(a2, argparse._SubParsersAction):
                        return set(a2.choices.keys())
    return set()


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
    assert "refresh" in _subparser_choices(parser, "parity")
    ns = parser.parse_args(
        ["parity", "refresh", "--source", "OMC", "--pin", "a" * 40]
    )
    assert ns.func is inspect_cmds.cmd_parity
    assert ns.parity_action == "refresh"


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


def test_parity_check_uses_global_json_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(["parity", "check", "--strict", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["schema_version"] == 1
    assert payload["command"] == "parity.check"
    assert payload["data"]["ok"] is True
    assert payload["data"]["schema_version"] == 2
    assert payload["data"]["strict"] is True
    assert payload["data"]["completion_claims_allowed"] is False

    code = main(["parity", "gaps", "--priority", "P0", "--json"])
    assert code == 0
    gaps_payload = json.loads(capsys.readouterr().out)
    assert gaps_payload["ok"] is True
    assert gaps_payload["command"] == "parity.gaps"
    assert gaps_payload["data"]["open_only"] is True
    assert gaps_payload["data"]["include_all"] is False
    assert all(gap["status"] == "open" for gap in gaps_payload["data"]["gaps"])
    issues = {
        issue
        for gap in gaps_payload["data"]["gaps"]
        for issue in gap["issues"]
    }
    assert {"#67", "#68", "#69", "#78"} <= issues


def test_parity_check_strict_invokes_shared_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI --strict must call shared check_parity_inventory(strict=True)."""
    calls: list[dict[str, bool]] = []
    import omg_cli.parity_check as parity_check

    real = parity_check.check_parity_inventory

    def wrapped(*, inventory_path, repo_root, strict=False, release=False):
        calls.append({"strict": bool(strict), "release": bool(release)})
        return real(
            inventory_path=inventory_path,
            repo_root=repo_root,
            strict=strict,
            release=release,
        )

    monkeypatch.setattr(parity_check, "check_parity_inventory", wrapped)

    code = main(["parity", "check", "--strict", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["strict"] is True
    assert payload["data"]["release"] is False
    assert calls == [{"strict": True, "release": False}]

    calls.clear()
    code = main(["parity", "check", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["strict"] is False
    assert payload["data"]["release"] is False
    assert calls == [{"strict": False, "release": False}]


def test_parity_check_release_invokes_shared_gate(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI --release must call shared check_parity_inventory(release=True)."""
    calls: list[dict[str, bool]] = []
    import omg_cli.parity_check as parity_check

    real = parity_check.check_parity_inventory

    def wrapped(*, inventory_path, repo_root, strict=False, release=False):
        calls.append({"strict": bool(strict), "release": bool(release)})
        return real(
            inventory_path=inventory_path,
            repo_root=repo_root,
            strict=strict,
            release=release,
        )

    monkeypatch.setattr(parity_check, "check_parity_inventory", wrapped)

    code = main(["parity", "check", "--release", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["release"] is True
    assert payload["data"]["strict"] is True
    assert calls == [{"strict": False, "release": True}]


def test_parity_gaps_defaults_open_only(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["parity", "gaps", "--json"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["data"]["open_only"] is True
    assert payload["data"]["include_all"] is False
    assert all(gap["status"] == "open" for gap in payload["data"]["gaps"])

    code = main(["parity", "gaps", "--all", "--json"])
    assert code == 0
    all_payload = json.loads(capsys.readouterr().out)
    assert all_payload["data"]["include_all"] is True
    assert all_payload["data"]["open_only"] is False
