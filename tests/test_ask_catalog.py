"""Hermetic tests for offline omg ask list-advisors / explain (#138 Slice A)."""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

from omg_cli.ask.catalog_usage import (
    ASK_EXECUTION_OPTION_STRINGS,
    CATALOG_USAGE_CODE,
    catalog_forbidden_supplied,
    catalog_verb_from_argv,
)
from omg_cli.ask.registry import CANONICAL_HARNESS_IDS
from omg_cli.ask.views import (
    CATALOG_FACT_KEYS,
    explain_advisor_catalog,
    list_advisor_catalog,
)
from omg_cli.cli_envelope import SCHEMA_VERSION
from omg_cli.commands.modes import cmd_ask
from omg_cli.main import build_parser, main


_SUPPORT_KEYS = (
    "supports_advisor",
    "supports_executor",
    "supports_background",
    "supports_structured_output",
    "supports_resume",
)


@pytest.fixture
def catalog_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".omg").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    return tmp_path


def _stdout_json(capsys: pytest.CaptureFixture[str]) -> dict:
    captured = capsys.readouterr()
    raw = captured.out
    assert raw.strip(), "expected one JSON document on stdout"
    payload = json.loads(raw)
    assert isinstance(payload, dict)
    return payload


def _parse_human_facts(text: str) -> dict:
    facts: dict = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        key = key.strip()
        rest = rest.strip()
        try:
            facts[key] = json.loads(rest)
        except json.JSONDecodeError:
            continue
    return facts


def test_json_list_advisors_unproven_canonical_order(
    catalog_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--json", "ask", "list-advisors"])
    assert code == 0
    payload = _stdout_json(capsys)
    assert payload["ok"] is True
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "ask.list-advisors"
    advisors = payload["advisors"]
    assert [row["harness_id"] for row in advisors] == list(CANONICAL_HARNESS_IDS)
    assert len(advisors) == 6
    for row in advisors:
        assert list(row) == list(CATALOG_FACT_KEYS)
        assert row["advisor_read_only"] == "unproven"
        assert row["binary_presence"] == "not_probed"
        assert row["observed_version"] is None
        assert row["tested_versions"] is None
        assert row["platforms"] == []
        assert row["identity_probe"] == "none"
        assert row["version_probe"] == "none"
        assert row["runtime_kind"] == "external_cli"
        assert row["purpose"] == "advisory"
        assert row["worker_eligible"] is False
        assert row["authoritative"] is False
        assert row["auto_apply"] is False
        for key in _SUPPORT_KEYS:
            assert row[key] is False


def test_human_list_advisors_matches_facts_without_qualification(
    catalog_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["ask", "list-advisors"])
    assert code == 0
    captured = capsys.readouterr()
    out = captured.out
    assert captured.err == ""
    for harness_id in CANONICAL_HARNESS_IDS:
        assert harness_id in out
    assert "unproven" in out
    assert "not_probed" in out
    assert "PATH" not in out
    lowered = out.lower()
    idx = 0
    while True:
        pos = lowered.find("qualified", idx)
        if pos < 0:
            break
        window = lowered[max(0, pos - 40) : pos + len("qualified")]
        assert "do not treat as qualified" in window
        idx = pos + 1


def test_json_explain_fable_resolves_to_claude_cli(
    catalog_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--json", "ask", "explain", "fable"])
    assert code == 0
    payload = _stdout_json(capsys)
    assert payload["ok"] is True
    assert payload["command"] == "ask.explain"
    row = payload["advisor"]
    assert row["harness_id"] == "claude-cli"
    assert row["resolved_from"] == "fable"
    assert row["advisor_read_only"] == "unproven"
    assert row["binary_presence"] == "not_probed"


def test_json_explain_agy_is_antigravity_not_gemini(
    catalog_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--json", "ask", "explain", "agy"])
    assert code == 0
    payload = _stdout_json(capsys)
    row = payload["advisor"]
    assert row["harness_id"] == "antigravity-cli"
    assert row["harness_id"] != "gemini-cli"
    assert row["resolved_from"] == "agy"


def test_json_explain_unknown_is_not_found(
    catalog_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--json", "ask", "explain", "nope"])
    assert code == 1
    payload = _stdout_json(capsys)
    assert payload["ok"] is False
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "ask.explain"
    assert payload.get("error_code") == "E_ADVISOR_NOT_FOUND"
    err = payload.get("error")
    assert isinstance(err, dict)
    assert err.get("code") == "E_ADVISOR_NOT_FOUND"
    blob = json.dumps(payload)
    assert "PATH" not in blob


def test_explain_without_id_is_usage(
    catalog_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["ask", "explain"])
    assert code == 2
    captured = capsys.readouterr()
    assert "omg ask explain: advisor id required" in captured.err
    assert captured.out == ""


def test_list_advisors_extra_arg_is_usage(
    catalog_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["ask", "list-advisors", "extra"])
    assert code == 2
    captured = capsys.readouterr()
    assert "omg ask list-advisors: unexpected arguments" in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("token", ["顾问", "顧問", "a" * 129, "fable\x00", "fable\n"])
def test_explain_cjk_overlong_control_not_found(
    catalog_root: Path, capsys: pytest.CaptureFixture[str], token: str
) -> None:
    code = main(["--json", "ask", "explain", token])
    assert code == 1
    payload = _stdout_json(capsys)
    assert payload["ok"] is False
    assert payload.get("error_code") == "E_ADVISOR_NOT_FOUND"
    err = payload.get("error")
    assert isinstance(err, dict)
    assert err.get("code") == "E_ADVISOR_NOT_FOUND"
    assert "PATH" not in json.dumps(payload)


def test_catalog_does_not_probe_path_or_network(
    catalog_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("catalog must not probe PATH, spawn, or network")

    monkeypatch.setattr(shutil, "which", explode)
    monkeypatch.setattr(subprocess, "run", explode)
    monkeypatch.setattr(subprocess, "Popen", explode)
    monkeypatch.setattr(socket, "create_connection", explode)

    assert main(["--json", "ask", "list-advisors"]) == 0
    list_payload = _stdout_json(capsys)
    assert list_payload["command"] == "ask.list-advisors"
    assert len(list_payload["advisors"]) == 6

    assert main(["--json", "ask", "explain", "fable"]) == 0
    explain_payload = _stdout_json(capsys)
    assert explain_payload["advisor"]["harness_id"] == "claude-cli"


def test_human_and_json_explain_facts_equal(
    catalog_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["--json", "ask", "explain", "claude-cli"]) == 0
    json_row = _stdout_json(capsys)["advisor"]
    assert main(["ask", "explain", "claude-cli"]) == 0
    human_row = _parse_human_facts(capsys.readouterr().out)
    assert json_row["harness_id"] == "claude-cli"
    assert json_row["resolved_from"] == "claude-cli"
    for key, value in json_row.items():
        assert key in human_row, key
        assert human_row[key] == value


def test_ask_codex_still_routes_to_cmd_ask() -> None:
    ns = build_parser().parse_args(["ask", "codex", "hello"])
    assert ns.func is cmd_ask
    assert ns.provider == "codex"
    assert ns.prompt == ["hello"]


_EXECUTION_FLAG_ARGV: tuple[tuple[str, list[str]], ...] = (
    ("--prompt-file", ["--prompt-file", "prompt.txt"]),
    ("--file", ["--file", "ctx.txt"]),
    ("--cwd", ["--cwd", "."]),
    ("--timeout", ["--timeout", "600"]),
    ("--timeout", ["--timeout=600"]),
    ("--max-bytes", ["--max-bytes", "524288"]),
    ("--out", ["--out", "out.md"]),
    ("--run", ["--run", "run-1"]),
    ("--dry-run", ["--dry-run"]),
    ("--model", ["--model", "x"]),
    ("--extra", ["--extra", "passthrough"]),
    ("--background", ["--background"]),
    ("--attempt-budget", ["--attempt-budget", "1"]),
    ("--role", ["--role", "researcher"]),
)


def _assert_catalog_usage(
    capsys: pytest.CaptureFixture[str], code: int, command: str, *, json_mode: bool
) -> dict | str:
    assert code == 2
    captured = capsys.readouterr()
    if json_mode:
        payload = json.loads(captured.out)
        assert isinstance(payload, dict)
        assert payload["ok"] is False
        assert payload["schema_version"] == SCHEMA_VERSION
        assert payload["command"] == command
        assert payload.get("error_code") == CATALOG_USAGE_CODE
        err = payload.get("error")
        assert isinstance(err, dict)
        assert err.get("code") == CATALOG_USAGE_CODE
        assert captured.err == ""
        return payload
    assert captured.out == ""
    assert f"omg {command.replace('.', ' ')}:" in captured.err
    return captured.err


@pytest.mark.parametrize("option,flag_argv", _EXECUTION_FLAG_ARGV)
@pytest.mark.parametrize("verb_argv,command", [
    (["list-advisors"], "ask.list-advisors"),
    (["explain", "fable"], "ask.explain"),
])
@pytest.mark.parametrize("flag_first", [True, False])
def test_catalog_rejects_execution_options_by_presence(
    catalog_root: Path,
    capsys: pytest.CaptureFixture[str],
    option: str,
    flag_argv: list[str],
    verb_argv: list[str],
    command: str,
    flag_first: bool,
) -> None:
    assert option in ASK_EXECUTION_OPTION_STRINGS
    body = [*flag_argv, *verb_argv] if flag_first else [*verb_argv, *flag_argv]
    human = _assert_catalog_usage(
        capsys, main(["ask", *body]), command, json_mode=False
    )
    assert isinstance(human, str)
    assert option in human
    payload = _assert_catalog_usage(
        capsys, main(["--json", "ask", *body]), command, json_mode=True
    )
    assert isinstance(payload, dict)
    assert option in str(payload.get("message", ""))


def test_catalog_rejects_double_dash_extras(
    catalog_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["ask", "list-advisors", "--"])
    _assert_catalog_usage(capsys, code, "ask.list-advisors", json_mode=False)
    code = main(["--json", "ask", "explain", "--", "fable"])
    payload = _assert_catalog_usage(capsys, code, "ask.explain", json_mode=True)
    assert isinstance(payload, dict)
    assert "--" in str(payload.get("message", ""))


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "ask", "list-advisors"],
        ["ask", "--json", "list-advisors"],
        ["ask", "list-advisors", "--json"],
        ["--json", "ask", "explain", "fable"],
        ["ask", "--json", "explain", "fable"],
        ["ask", "explain", "fable", "--json"],
        ["ask", "explain", "--json", "fable"],
    ],
)
def test_catalog_json_all_supported_positions_one_document(
    catalog_root: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    code = main(argv)
    assert code == 0
    payload = _stdout_json(capsys)
    assert payload["ok"] is True
    if "list-advisors" in argv:
        assert payload["command"] == "ask.list-advisors"
        assert len(payload["advisors"]) == 6
    else:
        assert payload["command"] == "ask.explain"
        assert payload["advisor"]["harness_id"] == "claude-cli"


def test_json_catalog_usage_is_envelope_exit_2(
    catalog_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["--json", "ask", "explain"])
    payload = _assert_catalog_usage(capsys, code, "ask.explain", json_mode=True)
    assert isinstance(payload, dict)
    assert "advisor id required" in str(payload.get("message", ""))
    code = main(["--json", "ask", "list-advisors", "extra"])
    extra = _assert_catalog_usage(capsys, code, "ask.list-advisors", json_mode=True)
    assert isinstance(extra, dict)
    assert "unexpected arguments" in str(extra.get("message", ""))


def test_human_unknown_includes_advisor_not_found(
    catalog_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["ask", "explain", "nope"])
    assert code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "E_ADVISOR_NOT_FOUND" in captured.err
    assert "nope" in captured.err


def test_catalog_rejects_prompt_file_before_read(
    catalog_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = catalog_root / "missing-prompt.txt"
    assert not missing.exists()
    code = main(["ask", "list-advisors", "--prompt-file", str(missing)])
    _assert_catalog_usage(capsys, code, "ask.list-advisors", json_mode=False)
    captured = capsys.readouterr()
    del captured
    code = main(["ask", "explain", "fable", "--prompt-file", str(missing)])
    err = capsys.readouterr()
    assert code == 2
    assert "cannot read" not in err.err
    assert "--prompt-file" in err.err


def test_catalog_forbidden_detector_is_presence_not_defaults() -> None:
    assert catalog_forbidden_supplied(["ask", "list-advisors"]) == ()
    assert catalog_forbidden_supplied(
        ["ask", "--timeout", "600", "list-advisors"]
    ) == ("--timeout",)
    assert catalog_forbidden_supplied(
        ["ask", "explain", "fable", "--role=researcher"]
    ) == ("--role",)
    assert catalog_forbidden_supplied(["ask", "list-advisors", "--"]) == ("--",)
    assert "--json" not in catalog_forbidden_supplied(
        ["--json", "ask", "list-advisors"]
    )
    assert "--project-root" not in catalog_forbidden_supplied(
        ["ask", "--project-root", ".", "list-advisors"]
    )


_PREFIX_FLAG_ARGV: tuple[tuple[str, list[str]], ...] = (
    ("--dry", ["--dry"]),
    ("--back", ["--back"]),
    ("--prompt", ["--prompt", "prompt.txt"]),
    ("--mod", ["--mod", "x"]),
    ("--rol", ["--rol", "researcher"]),
)


@pytest.mark.parametrize("option,flag_argv", _PREFIX_FLAG_ARGV)
def test_catalog_rejects_unique_option_prefixes(
    catalog_root: Path,
    capsys: pytest.CaptureFixture[str],
    option: str,
    flag_argv: list[str],
) -> None:
    human = _assert_catalog_usage(
        capsys,
        main(["ask", "list-advisors", *flag_argv]),
        "ask.list-advisors",
        json_mode=False,
    )
    assert isinstance(human, str)
    assert option in human
    payload = _assert_catalog_usage(
        capsys,
        main(["--json", "ask", "explain", "fable", *flag_argv]),
        "ask.explain",
        json_mode=True,
    )
    assert isinstance(payload, dict)
    assert option in str(payload.get("message", ""))


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "ask", "list-advisors", "--timeout"],
        ["--json", "ask", "list-advisors", "--timeout=nope"],
        ["--json", "ask", "list-advisors", "--attempt-budget=x"],
        ["--json", "ask", "explain", "fable", "--timeout"],
    ],
)
def test_json_catalog_malformed_execution_flag_is_envelope(
    catalog_root: Path, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    try:
        code = main(argv)
    except SystemExit as exc:
        raise AssertionError(f"argparse SystemExit leaked: {exc}") from exc
    command = (
        "ask.list-advisors" if "list-advisors" in argv else "ask.explain"
    )
    payload = _assert_catalog_usage(capsys, code, command, json_mode=True)
    assert isinstance(payload, dict)


def test_ask_parser_execution_options_match_detector() -> None:
    parser = build_parser()
    ask = None
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            ask = action.choices.get("ask")
            if ask is not None:
                break
    assert ask is not None
    owned: set[str] = set()
    global_flags = {"--json", "--project-root", "--safe", "--yolo", "--help"}
    for action in ask._actions:
        for option in action.option_strings:
            if option in global_flags or not option.startswith("--"):
                continue
            owned.add(option)
    assert owned == set(ASK_EXECUTION_OPTION_STRINGS)


def test_provider_option_abbreviation_still_parses() -> None:
    ns = build_parser().parse_args(["ask", "codex", "hello", "--mod", "x"])
    assert ns.func is cmd_ask
    assert ns.provider == "codex"
    assert ns.model == "x"
    assert catalog_verb_from_argv(["ask", "codex", "hello", "--mod", "x"]) is None


def test_catalog_allows_global_project_root_and_json(
    catalog_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["ask", "--project-root", str(catalog_root), "list-advisors", "--json"]
    )
    assert code == 0
    payload = _stdout_json(capsys)
    assert payload["command"] == "ask.list-advisors"
    assert len(payload["advisors"]) == 6


def test_views_share_immutable_facts_and_resolve_aliases() -> None:
    rows = list_advisor_catalog()
    assert [row["harness_id"] for row in rows] == list(CANONICAL_HARNESS_IDS)
    fable = explain_advisor_catalog("fable")
    assert fable["harness_id"] == "claude-cli"
    assert fable["resolved_from"] == "fable"
    assert fable["aliases"] == ["claude", "fable"]
    agy = explain_advisor_catalog("agy")
    assert agy["harness_id"] == "antigravity-cli"
    assert agy["resolved_from"] == "agy"
