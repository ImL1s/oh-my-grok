"""Red regression locks for the approved strict-v2 lifecycle plan.

These tests intentionally describe contracts that are not present at the
5a6e232 baseline.  Keep them isolated from the frozen v1 suite so implementers
can prove that an expected red is a product gap rather than baseline damage.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from omg_cli.integrate import IntegrateError, integrate_results, load_envelopes
from omg_cli.main import _print_state_human, build_parser
from omg_cli.modes import run_mode
from omg_cli.pipeline import run_pipeline
from omg_cli.ralplan import (
    load_ralplan_state,
    run_ralplan,
    stage_artifact_json_path,
)
from omg_cli.state import (
    cancel_run,
    create_run,
    execution_lease,
    load_active_run,
    load_run,
    set_verified,
    write_status,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_OMG = REPO_ROOT / "bin" / "omg"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _strict_run(root: Path, *, mode: str = "autopilot", goal: str = "strict") -> dict:
    return create_run(
        root,
        mode=mode,
        goal=goal,
        extra={"schema_version": 2, "lifecycle_version": 2},
    )


def _foreign_envelope(root: Path, *, run_id: str, task_id: str) -> dict:
    return {
        "writer": "omg-cli",
        "run_id": run_id,
        "task_id": task_id,
        "base_sha": "a" * 40,
        "head_sha": "b" * 40,
        "worktree_path": str(root),
        "status": "failed",
        "changed_files": ["foreign.txt"],
        "evidence": "must never be consumed by a different strict run",
    }


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def _flag_value(argv: list[str], flag: str) -> str:
    assert argv.count(flag) == 1, f"expected exactly one {flag!r}: {argv!r}"
    index = argv.index(flag)
    assert index + 1 < len(argv), f"missing value after {flag!r}: {argv!r}"
    return argv[index + 1]


def _run_omg(*args: str, cwd: Path, env: dict[str, str] | None = None):
    process_env = os.environ.copy()
    process_env.pop("OMG_ALLOW_EXTERNAL_CLI", None)
    process_env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + process_env["PYTHONPATH"]
        if process_env.get("PYTHONPATH")
        else ""
    )
    if env:
        process_env.update(env)
    return subprocess.run(
        [sys.executable, str(BIN_OMG), *args],
        cwd=cwd,
        env=process_env,
        capture_output=True,
        text=True,
    )


# U-01: one shared, two-layer evidence parser; authoritative files are not host
# proposals merely because they self-declare writer=omg-cli.


def test_evidence_parses_host_envelope_before_nested_payload() -> None:
    evidence = importlib.import_module("omg_cli.evidence")
    parse_host = getattr(evidence, "parse_host_envelope")
    parse_payload = getattr(evidence, "parse_structured_payload")
    raw = json.loads(
        (FIXTURES / "evidence" / "valid-host-valid-payload.json").read_text(
            encoding="utf-8"
        )
    )

    host = parse_host(raw)
    assert host is not None
    payload = parse_payload(raw["text"])
    assert payload["schema_version"] == 2
    assert payload["verdict"] == "APPROVE"


@pytest.mark.parametrize(
    ("fixture", "layer"),
    [
        ("malformed-host.json", "host"),
        ("valid-host-malformed-payload.json", "payload"),
        ("forged-authoritative-result.json", "host"),
    ],
)
def test_evidence_layers_fail_closed_independently(fixture: str, layer: str) -> None:
    evidence = importlib.import_module("omg_cli.evidence")
    parse_host = getattr(evidence, "parse_host_envelope")
    parse_payload = getattr(evidence, "parse_structured_payload")
    raw = json.loads((FIXTURES / "evidence" / fixture).read_text(encoding="utf-8"))

    if layer == "host":
        with pytest.raises((TypeError, ValueError), match="host|session|stop|text"):
            parse_host(raw)
        return

    parse_host(raw)
    with pytest.raises((TypeError, ValueError), match="payload|JSON|schema"):
        parse_payload(raw["text"])


# U-02/U-03: supervised environments and envelope sets fail closed.


@pytest.mark.parametrize(
    "foreign_relative_path",
    [
        Path("legacy-global.json"),
        Path("run-b") / "cross-run.json",
    ],
)
def test_v2_integrate_never_reads_global_or_cross_run_envelope(
    tmp_path: Path, foreign_relative_path: Path
) -> None:
    run = _strict_run(tmp_path, mode="ulw", goal="scope envelopes")
    run_id = run["run_id"]
    legacy_root = tmp_path / ".omg" / "artifacts" / "ulw-results"
    _write_json(
        legacy_root / foreign_relative_path,
        _foreign_envelope(tmp_path, run_id="run-b", task_id="foreign"),
    )

    result = integrate_results(tmp_path, run_id, dry_run=True)

    expected_root = legacy_root / run_id
    assert Path(result["envelopes_dir"]) == expected_root
    assert result["status"] == "missing"
    assert result["applied"] == []


def test_mixed_valid_and_invalid_envelope_siblings_fail_whole_set(tmp_path: Path) -> None:
    envelope_dir = tmp_path / "envelopes" / "run-v2"
    _write_json(
        envelope_dir / "valid.json",
        _foreign_envelope(tmp_path, run_id="run-v2", task_id="valid"),
    )
    envelope_dir.joinpath("invalid.json").write_text("{not-json\n", encoding="utf-8")

    with pytest.raises(IntegrateError, match="invalid.json|parse|valid envelopes"):
        load_envelopes(envelope_dir)


def test_supervised_pipeline_rejects_parent_allow_env_before_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMG_ALLOW_EXTERNAL_CLI", "1")

    blocked = False
    try:
        rc = run_pipeline(
            "must contain advisor escape",
            root=tmp_path,
            dry_run=True,
            plan_only=True,
            require_acceptance=False,
        )
    except RuntimeError:
        blocked = True
    else:
        blocked = rc != 0

    status_files = list((tmp_path / ".omg" / "state" / "runs").glob("*/status.json"))
    assert blocked, "OMG_ALLOW_EXTERNAL_CLI=1 flowed through supervised pipeline"
    assert status_files == [], "guard must run before create_run or stage mutation"


# U-03/U-06/U-11: schema selection is exact and pre-mutation.


def _schema_label(result: object) -> str:
    if isinstance(result, dict):
        return str(result.get("classification"))
    value = getattr(result, "value", result)
    return str(value)


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        ("absent.json", "legacy-v1"),
        ("v1.json", "legacy-v1"),
        ("v2.json", "strict-v2"),
    ],
)
def test_schema_dispatch_accepts_only_frozen_v1_or_strict_v2(
    fixture: str, expected: str
) -> None:
    state = importlib.import_module("omg_cli.state")
    classifier = getattr(state, "classify_run_schema", None)
    assert callable(classifier), "state.py must own one schema classifier"
    raw = json.loads((FIXTURES / "schema_dispatch" / fixture).read_text(encoding="utf-8"))
    assert _schema_label(classifier(raw)) == expected


@pytest.mark.parametrize(
    "fixture",
    [
        "malformed-string.json",
        "malformed-null.json",
        "malformed-object.json",
        "malformed-negative.json",
        "future.json",
    ],
)
def test_schema_dispatch_rejects_malformed_and_future_versions(fixture: str) -> None:
    state = importlib.import_module("omg_cli.state")
    classifier = getattr(state, "classify_run_schema", None)
    assert callable(classifier), "state.py must own one schema classifier"
    raw = json.loads((FIXTURES / "schema_dispatch" / fixture).read_text(encoding="utf-8"))
    with pytest.raises((TypeError, ValueError), match="schema|version|unsupported"):
        classifier(raw)


# U-04: real CLI resume syntax, truthful state UX, and one durable Grok session.


def test_ralph_parser_supports_active_and_explicit_resume_contract() -> None:
    parser = build_parser()
    active = parser.parse_args(["ralph", "--resume"])
    explicit = parser.parse_args(["ralph", "--resume", "run-123"])

    assert getattr(active, "resume", None) is not None
    assert explicit.resume == "run-123"


def test_ralph_human_state_prints_an_executable_resume_command(capsys) -> None:
    _print_state_human(
        {
            "run_id": "run-123",
            "mode": "ralph",
            "status": "blocked",
            "verified": False,
            "goal": "resume safely",
            "grok_session_id": "019f7e08-417a-7080-853e-29aef82bd168",
        }
    )
    output = capsys.readouterr().out

    assert "omg ralph --resume run-123" in output
    assert "--resume /" not in output


def test_ralph_iteration_two_resumes_the_first_iteration_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    modes = importlib.import_module("omg_cli.modes")
    launches: list[list[str]] = []

    def fake_launch(argv, **_kwargs):
        launches.append(list(argv))
        return 0

    monkeypatch.setattr(modes, "_launch_grok", fake_launch)
    monkeypatch.setattr(modes, "_try_acceptance_and_verify", lambda *_a, **_k: False)
    monkeypatch.setattr(modes, "_try_set_verified", lambda *_a, **_k: False)

    rc = run_mode(
        "ralph",
        "keep one conversation",
        root=tmp_path,
        max_iter=2,
        require_acceptance=False,
    )

    assert rc == 0
    assert len(launches) == 2
    first_session = _flag_value(launches[0], "--session-id")
    assert "--resume" not in launches[0]
    assert "--session-id" not in launches[1]
    assert _flag_value(launches[1], "--resume") == first_session


def test_ralph_new_process_resume_reuses_persisted_session(tmp_path: Path) -> None:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    invocation_log = tmp_path / "grok-invocations.jsonl"
    fake_grok = fake_bin / "grok"
    fake_grok.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['FAKE_GROK_LOG'], 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    fake_grok.chmod(0o755)
    env = {
        "PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
        "FAKE_GROK_LOG": str(invocation_log),
    }

    first = _run_omg(
        "ralph",
        "resume across processes",
        "--max-iter",
        "1",
        "--no-require-acceptance",
        cwd=tmp_path,
        env=env,
    )
    assert first.returncode == 0, first.stderr + first.stdout
    run = load_active_run(tmp_path)
    assert run is not None
    run_id = run["run_id"]

    resumed = _run_omg(
        "ralph",
        "--resume",
        run_id,
        "--max-iter",
        "2",
        "--no-require-acceptance",
        cwd=tmp_path,
        env=env,
    )
    assert resumed.returncode == 0, resumed.stderr + resumed.stdout

    invocations = [
        json.loads(line)
        for line in invocation_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(invocations) == 2, "resume ceiling is cumulative, not two new iterations"
    session_id = _flag_value(invocations[0], "--session-id")
    assert _flag_value(invocations[1], "--resume") == session_id
    final = load_run(tmp_path, run_id)
    assert final is not None
    assert final["grok_session_id"] == session_id
    assert final["iteration"] == 2


# U-06: v2 consensus is Planner -> Architect -> Critic; verifier-only is v1.


def _ralplan_executor(seen: list[str], verdicts: dict[str, str]):
    def execute(stage: str, **kwargs) -> int:
        seen.append(stage)
        verdict = verdicts.get(stage)
        if verdict:
            artifact = stage_artifact_json_path(
                Path(kwargs["root"]),
                kwargs["run_id"],
                stage,
                kwargs["round_n"],
            )
            _write_json(
                artifact,
                {
                    "schema_version": 2,
                    "run_id": kwargs["run_id"],
                    "stage": stage,
                    "role": stage,
                    "round": kwargs["round_n"],
                    "verdict": verdict,
                    "steelman": "strongest viable interpretation",
                    "tradeoff": "safety before breadth",
                    "synthesis": "use the strict lifecycle",
                },
            )
        return 0

    return execute


def test_ralplan_v2_architect_rejection_prevents_critic_launch(tmp_path: Path) -> None:
    run = _strict_run(tmp_path, mode="ralplan", goal="ordered consensus")
    seen: list[str] = []

    rc = run_ralplan(
        "ordered consensus",
        root=tmp_path,
        existing_run_id=run["run_id"],
        max_rounds=1,
        dry_run=True,
        stage_executor=_ralplan_executor(seen, {"architect": "REQUEST CHANGES"}),
    )

    assert rc != 0
    assert seen[:2] == ["planner", "architect"]
    assert "critic" not in seen


def test_ralplan_v2_legacy_verifier_only_approval_cannot_accept(tmp_path: Path) -> None:
    run = _strict_run(tmp_path, mode="ralplan", goal="no verifier shortcut")
    seen: list[str] = []

    rc = run_ralplan(
        "no verifier shortcut",
        root=tmp_path,
        existing_run_id=run["run_id"],
        max_rounds=1,
        dry_run=True,
        stage_executor=_ralplan_executor(seen, {"verifier": "APPROVE"}),
    )

    state = load_ralplan_state(tmp_path, run["run_id"])
    assert rc != 0
    assert state is not None and state.get("accepted") is False


# U-04/U-11: deterministic first-committer barriers for cancellation.


def test_request_first_barrier_prevents_late_non_cancel_status(tmp_path: Path) -> None:
    run = _strict_run(tmp_path, mode="ralph", goal="cancel wins")
    run_id = run["run_id"]
    cancelled = threading.Event()
    writer_errors: list[BaseException] = []

    def delayed_writer() -> None:
        assert cancelled.wait(timeout=5), "cancel barrier was not released"
        try:
            write_status(tmp_path, run_id, "running", extra={"iteration": 99})
        except (PermissionError, RuntimeError) as exc:
            writer_errors.append(exc)

    thread = threading.Thread(target=delayed_writer)
    thread.start()
    cancel_run(tmp_path, run_id)
    cancelled.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert writer_errors, "a committed cancellation did not fence the stale writer"
    final = load_run(tmp_path, run_id)
    assert final is not None
    assert final["status"] == "cancelled"
    assert final.get("iteration") != 99


def test_verified_first_barrier_refuses_late_cancel(tmp_path: Path) -> None:
    run = _strict_run(tmp_path, mode="autopilot", goal="verified wins")
    run_id = run["run_id"]
    with execution_lease(tmp_path, run_id, intent="test-verified-first") as lease:
        set_verified(tmp_path, run_id, force=True, lease=lease)

    result = cancel_run(tmp_path, run_id)

    final = load_run(tmp_path, run_id)
    assert result["status"] == "verified"
    assert final is not None and final["status"] == "verified"
    assert final["verified"] is True
    assert not (tmp_path / ".omg" / "state" / "runs" / run_id / "cancel.request.json").exists()
