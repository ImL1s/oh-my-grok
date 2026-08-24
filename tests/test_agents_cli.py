"""CLI: omg agents list/explain (#131)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omg_cli.main import build_parser, main
from omg_cli.medley_inspect import INSPECT_SCHEMA

ROOT = Path(__file__).resolve().parents[1]
_SKIP_CATALOG_PIN = pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_NOFOLLOW"),
    reason="agent catalog pin requires POSIX O_NOFOLLOW/dir_fd",
)


def test_parser_registers_agents_list_explain() -> None:
    parser = build_parser()
    ns = parser.parse_args(["agents", "list"])
    assert ns.command == "agents"
    assert ns.agents_action == "list"
    ns = parser.parse_args(["agents", "explain", "omg-verifier"])
    assert ns.agents_action == "explain"
    assert ns.agent_or_profile == "omg-verifier"
    ns = parser.parse_args(
        ["agents", "list", "--host-inspect", "inspect.json"]
    )
    assert ns.host_inspect == "inspect.json"
    ns = parser.parse_args(
        ["agents", "explain", "omg-verifier", "--host-inspect", "inspect.json"]
    )
    assert ns.host_inspect == "inspect.json"


@_SKIP_CATALOG_PIN
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
    assert data["inspect_source"] == "absent"
    ids = [row["agent_id"] for row in data["agents"]]
    assert ids == sorted(ids)
    verifier = next(row for row in data["agents"] if row["agent_id"] == "omg-verifier")
    assert verifier["baseline_mode"] == "inherit"
    assert verifier["status"] == "ready"
    assert verifier["selected_model_ref"] is None
    assert verifier["route_receipt_digest"] is None
    assert verifier["attempt"] is None
    assert verifier["inspect_source"] == "absent"
    assert verifier["host_facts"]["medley_capability_outcome"] == "unsupported"
    assert verifier["host_facts"]["route_specific_facts"] == "unavailable"
    assert "review-primary-example" in verifier["candidate_ids"]
    assert verifier["selected_model_ref"] not in verifier["candidate_ids"]
    for row in data["agents"]:
        assert row["inspect_source"] == "absent"
        assert row["attempt"] is None
        assert row["route_receipt_digest"] is None
        assert row["effective_route"] is None
    blob = json.dumps(payload).lower()
    for needle in ("api_key", "sk-", "bearer ", "account_id"):
        assert needle not in blob


@_SKIP_CATALOG_PIN
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
    assert agent["inspect_source"] == "absent"
    assert agent["attempt"] is None
    assert payload["data"]["inspect_source"] == "absent"
    assert payload["data"]["resume"]["policy_digest"] == agent["policy_digest"]
    assert payload["data"]["resume"]["attempt"] is None
    assert payload["data"]["resume"]["route_receipt_digest"] is None
    rc = main(["agents", "explain", "explore", "--project-root", str(tmp_path)])
    assert rc == 0
    human = capsys.readouterr().out
    assert "Identity" in human
    assert "Next action" in human
    assert "unsupported" in human
    assert "unavailable" in human
    assert "inspect_source: absent" in human
    assert "not attempted" in human
    assert "omg agents list --host-inspect PATH" in human


@_SKIP_CATALOG_PIN
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


@_SKIP_CATALOG_PIN
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


@_SKIP_CATALOG_PIN
def test_agents_list_missing_inspect_file_fail_closes(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    missing = tmp_path / "missing-inspect.json"
    rc = main(
        [
            "--json",
            "agents",
            "list",
            "--host-inspect",
            str(missing),
            "--project-root",
            str(tmp_path),
        ]
    )
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error_code"] == "E_MEDLEY_INSPECT_PATH"


@_SKIP_CATALOG_PIN
def test_agents_explain_json_stock_has_null_attempt(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    rc = main(
        [
            "--json",
            "agents",
            "explain",
            "omg-verifier",
            "--project-root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    agent = payload["data"]["agent"]
    assert agent["inspect_source"] == "absent"
    assert agent["attempt"] is None
    assert agent["route_receipt_digest"] is None
    assert any(r["code"] == "E_MEDLEY_INSPECT_ABSENT" for r in agent["reasons"])


@_SKIP_CATALOG_PIN
def test_agents_list_json_inspect_document_sets_source(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".omg").mkdir()
    inspect = tmp_path / "inspect.json"
    inspect.write_text(
        json.dumps(
            {
                "schema": INSPECT_SCHEMA,
                "schemaVersion": 1,
                "host": "medley",
                "capabilities": [
                    {
                        "capability_id": "medley.native-route-receipt.v1",
                        "state": "unsupported",
                    }
                ],
                "receipts": [],
            }
        ),
        encoding="utf-8",
    )
    rc = main(
        [
            "--json",
            "agents",
            "list",
            "--host-inspect",
            str(inspect),
            "--project-root",
            str(tmp_path),
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    data = payload["data"]
    assert data["inspect_source"] == "document"
    verifier = next(row for row in data["agents"] if row["agent_id"] == "omg-verifier")
    assert verifier["inspect_source"] == "document"
    assert verifier["attempt"] is None
    assert verifier["route_receipt_digest"] is None
    assert not any(
        r["code"] == "E_MEDLEY_INSPECT_ABSENT" for r in verifier["reasons"]
    )
