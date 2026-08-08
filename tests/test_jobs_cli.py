"""CLI surface for durable jobs (#68 PR1) — JSON envelope stability."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omg_cli.cli_envelope import SCHEMA_VERSION
from omg_cli.main import build_parser, main

pytest_plugins = ["tests.jobs_testutil"]


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from tests.jobs_testutil import register_project_root

    (tmp_path / ".omg").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    register_project_root(tmp_path)
    prompt = tmp_path / "task.md"
    prompt.write_text("hermetic job prompt", encoding="utf-8")
    return tmp_path


def _out(capsys: pytest.CaptureFixture[str]) -> dict:
    raw = capsys.readouterr().out
    assert raw.strip(), "expected JSON on stdout"
    return json.loads(raw)


def test_job_subcommands_registered() -> None:
    parser = build_parser()
    choices = None
    for act in parser._actions:
        if getattr(act, "dest", None) == "command" and hasattr(act, "choices"):
            choices = act.choices
            break
    assert choices is not None
    assert "job" in choices


def test_cli_start_status_wait_collect_json(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prompt = project / "task.md"
    rc = main(
        [
            "--json",
            "job",
            "start",
            "--provider",
            "fake",
            "--role",
            "researcher",
            "--prompt-file",
            str(prompt),
            "--sleep",
            "0.05",
        ]
    )
    assert rc == 0
    start = _out(capsys)
    assert start["ok"] is True
    assert start["schema_version"] == SCHEMA_VERSION
    assert start["command"] == "job.start"
    job_id = start["job_id"]
    assert start["state"] == "running"
    assert start["pid"]

    rc = main(["--json", "job", "status", job_id])
    assert rc == 0
    status = _out(capsys)
    assert status["ok"] is True
    assert status["command"] == "job.status"
    assert status["job"]["job_id"] == job_id

    rc = main(["--json", "job", "wait", job_id, "--timeout", "15"])
    assert rc == 0
    waited = _out(capsys)
    assert waited["ok"] is True
    assert waited["command"] == "job.wait"
    assert waited["timed_out"] is False
    assert waited["job"]["state"] == "succeeded"

    rc = main(["--json", "job", "collect", job_id])
    assert rc == 0
    coll = _out(capsys)
    assert coll["ok"] is True
    assert coll["command"] == "job.collect"
    assert coll["collect"]["state"] == "succeeded"

    # Idempotent collect
    rc = main(["--json", "job", "collect", job_id])
    assert rc == 0
    coll2 = _out(capsys)
    assert coll2["collect"] == coll["collect"]


def test_cli_wait_timeout_envelope(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prompt = project / "task.md"
    rc = main(
        [
            "--json",
            "job",
            "start",
            "--provider",
            "fake",
            "--prompt-file",
            str(prompt),
            "--sleep",
            "1.5",
        ]
    )
    assert rc == 0
    job_id = _out(capsys)["job_id"]

    rc = main(["--json", "job", "wait", job_id, "--timeout", "0.15"])
    assert rc == 1
    payload = _out(capsys)
    assert payload["ok"] is False
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "job.wait"
    err = payload.get("error") or {}
    assert err.get("code") == "E_JOB_TIMEOUT" or payload.get("error_code") == (
        "E_JOB_TIMEOUT"
    )
    details = err.get("details") or {}
    assert details.get("timed_out") is True
    assert (details.get("job") or {}).get("state") == "running"

    # Cleanup
    main(["--json", "job", "cancel", job_id])
    _out(capsys)


def test_cli_antigravity_refused(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prompt = project / "task.md"
    rc = main(
        [
            "--json",
            "job",
            "start",
            "--provider",
            "antigravity",
            "--prompt-file",
            str(prompt),
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert payload["ok"] is False
    err = payload.get("error") or {}
    assert err.get("code") == "E_JOB_PROVIDER" or payload.get("error_code") == (
        "E_JOB_PROVIDER"
    )


def test_cli_unknown_job(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--json", "job", "status", "20990101T000000Z-deadbeef"])
    assert rc == 1
    payload = _out(capsys)
    assert payload["ok"] is False
    err = payload.get("error") or {}
    assert err.get("code") == "E_JOB_UNKNOWN" or payload.get("error_code") == (
        "E_JOB_UNKNOWN"
    )


def test_cli_list_and_cancel(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prompt = project / "task.md"
    rc = main(
        [
            "--json",
            "job",
            "start",
            "--provider",
            "fake",
            "--prompt-file",
            str(prompt),
            "--sleep",
            "2",
            "--run",
            "run-x",
        ]
    )
    assert rc == 0
    job_id = _out(capsys)["job_id"]

    rc = main(["--json", "job", "list", "--provider", "fake", "--state", "running"])
    assert rc == 0
    listed = _out(capsys)
    assert listed["ok"] is True
    assert listed["command"] == "job.list"
    assert listed["count"] >= 1
    assert any(j["job_id"] == job_id for j in listed["jobs"])

    rc = main(["--json", "job", "cancel", job_id, "--reason", "cli-test"])
    assert rc == 0
    cancelled = _out(capsys)
    assert cancelled["ok"] is True
    assert cancelled["job"]["state"] == "cancelled"
    assert cancelled["job"]["cancel_reason"] == "cli-test"

    # Idempotent cancel
    rc = main(["--json", "job", "cancel", job_id])
    assert rc == 0
    again = _out(capsys)
    assert again["job"]["state"] == "cancelled"


def test_cli_large_output_descriptors(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prompt = project / "task.md"
    rc = main(
        [
            "--json",
            "job",
            "start",
            "--provider",
            "fake",
            "--prompt-file",
            str(prompt),
            "--sleep",
            "0.05",
            "--large-output",
        ]
    )
    assert rc == 0
    job_id = _out(capsys)["job_id"]
    assert main(["--json", "job", "wait", job_id, "--timeout", "15"]) == 0
    _out(capsys)
    assert main(["--json", "job", "collect", job_id]) == 0
    coll = _out(capsys)["collect"]
    assert coll["result"] == "artifacts/result.md"
    raw = json.dumps(coll)
    assert "x" * 500 not in raw
