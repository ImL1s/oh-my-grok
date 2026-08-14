"""CLI: omg agents list/explain (#131)."""

from __future__ import annotations

import json
from pathlib import Path

from omg_cli.main import build_parser, main

ROOT = Path(__file__).resolve().parents[1]


def test_parser_registers_agents_list_explain() -> None:
    parser = build_parser()
    ns = parser.parse_args(["agents", "list"])
    assert ns.command == "agents"
    assert ns.agents_action == "list"
    ns = parser.parse_args(["agents", "explain", "omg-verifier"])
    assert ns.agents_action == "explain"
    assert ns.agent_or_profile == "omg-verifier"


def test_agents_list_json_stock_grok(capsys, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    rc = main(["--json", "agents", "list", "--project-root", str(tmp_path)])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["command"] == "agents.list"
    assert payload["schema_version"] == 1
    data = payload["data"]
    assert data["host_tier"] == "original_grok_build"
    ids = [row["agent_id"] for row in data["agents"]]
    assert ids == sorted(ids)
    verifier = next(row for row in data["agents"] if row["agent_id"] == "omg-verifier")
    assert verifier["baseline_mode"] == "inherit"
    assert verifier["status"] == "ready"
    assert verifier["selected_model_ref"] is None
    assert verifier["route_receipt_digest"] is None
    assert verifier["host_facts"]["medley_capability_outcome"] == "unsupported"
    assert verifier["host_facts"]["route_specific_facts"] == "unavailable"
    assert "review-primary-example" in verifier["candidate_ids"]
    blob = json.dumps(payload).lower()
    for needle in ("api_key", "sk-", "bearer ", "account_id"):
        assert needle not in blob


def test_agents_explain_human_and_json(capsys, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    rc = main(
        ["--json", "agents", "explain", "omg-orchestrator", "--project-root", str(tmp_path)]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    agent = payload["data"]["agent"]
    assert agent["agent_id"] == "omg-orchestrator"
    assert agent["baseline_mode"] == "inherit"
    assert agent["effective_route"] is None
    assert payload["data"]["resume"]["policy_digest"] == agent["policy_digest"]
    rc = main(["agents", "explain", "explore", "--project-root", str(tmp_path)])
    assert rc == 0
    human = capsys.readouterr().out
    assert "Identity" in human
    assert "Next action" in human
    assert "unsupported" in human
    assert "unavailable" in human


def test_agents_explain_unknown_is_usage(capsys, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    rc = main(
        ["--json", "agents", "explain", "nope", "--project-root", str(tmp_path)]
    )
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_code"] == "E_AGENT_NOT_FOUND"


def test_agents_list_human_has_non_color_status(capsys, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    rc = main(["agents", "list", "--project-root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Agent" in out
    assert "Status" in out
    assert "omg-verifier" in out
    assert "ready" in out
    assert "inherit" in out
    assert "review-primary-example" not in out
    assert "\x1b[" not in out
