"""Hermetic tests for offline omg ask list-advisors / explain (#138 Slice A)."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from pathlib import Path

import pytest

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
