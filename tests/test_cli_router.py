# tests/test_cli_router.py
import json
import os
import stat
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BIN_OMG = REPO_ROOT / "bin" / "omg"
PYTHON = sys.executable


def _run_omg(*args, cwd=None, env=None, check=False):
    cmd = [PYTHON, str(BIN_OMG), *args]
    full_env = os.environ.copy()
    # Ensure package importable when invoked as script
    full_env["PYTHONPATH"] = str(REPO_ROOT) + (
        os.pathsep + full_env["PYTHONPATH"] if full_env.get("PYTHONPATH") else ""
    )
    if env:
        full_env.update(env)
    return subprocess.run(
        cmd,
        cwd=cwd or REPO_ROOT,
        env=full_env,
        capture_output=True,
        text=True,
        check=check,
    )


def test_help_exits_zero():
    r = _run_omg("--help")
    assert r.returncode == 0
    out = r.stdout + r.stderr
    assert "setup" in out
    assert "doctor" in out
    assert "state" in out
    for command in (
        "session",
        "recover",
        "memory",
        "tracker",
        "compact",
        "notify",
        "native-status",
        "workflow",
        "capabilities",
        "parity",
    ):
        assert command in out


def test_version_flag_matches_plugin_json():
    plugin = json.loads((REPO_ROOT / "plugin.json").read_text(encoding="utf-8"))
    expected = plugin["version"]
    r = _run_omg("--version")
    assert r.returncode == 0, r.stderr
    out = (r.stdout + r.stderr).strip()
    assert expected in out
    assert "omg" in out.lower() or expected in out


def test_session_router_exact_create_resume_continue_and_fork():
    parent = str(uuid.UUID(int=1))
    child = str(uuid.UUID(int=2))

    allocate = _run_omg("session", "allocate")
    assert allocate.returncode == 0, allocate.stderr
    allocation = json.loads(allocate.stdout)
    assert allocation["argv"] == ["--session-id", allocation["session_id"]]
    uuid.UUID(allocation["session_id"])

    resume = _run_omg("session", "route", "--resume", parent)
    assert resume.returncode == 0, resume.stderr
    assert json.loads(resume.stdout)["argv"] == ["--resume", parent]

    continuation = _run_omg("session", "route", "--continue")
    assert continuation.returncode == 0, continuation.stderr
    assert json.loads(continuation.stdout)["argv"] == ["--continue"]

    fork = _run_omg(
        "session",
        "route",
        "--resume",
        parent,
        "--fork-session",
        "--new-session-id",
        child,
    )
    assert fork.returncode == 0, fork.stderr
    assert json.loads(fork.stdout)["argv"] == [
        "--resume",
        parent,
        "--fork-session",
        "--session-id",
        child,
    ]

    reused = _run_omg(
        "session",
        "route",
        "--resume",
        parent,
        "--fork-session",
        "--new-session-id",
        child,
        "--existing-session-id",
        child,
    )
    assert reused.returncode == 1
    assert "already exists" in reused.stderr


def test_memory_cli_put_search_and_export(tmp_path):
    put = _run_omg("memory", "put", "architecture", "Python plugin", cwd=tmp_path)
    assert put.returncode == 0, put.stderr
    assert json.loads(put.stdout)["key"] == "architecture"

    search = _run_omg("memory", "search", "python", cwd=tmp_path)
    assert search.returncode == 0, search.stderr
    assert [row["key"] for row in json.loads(search.stdout)] == ["architecture"]

    output = tmp_path / "facts-export.json"
    export = _run_omg(
        "memory", "export", "--output", str(output), cwd=tmp_path
    )
    assert export.returncode == 0, export.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["store_kind"] == (
        "project_fact_memory"
    )


def test_recovery_tracker_compaction_and_notification_cli(tmp_path):
    source = REPO_ROOT / "tests" / "fixtures" / "recovery" / (
        "source-913-lines-broken-chain-v1.jsonl"
    )
    recovery_dir = tmp_path / "recovered"
    recovered = _run_omg(
        "recover", str(source), "--output", str(recovery_dir), cwd=tmp_path
    )
    assert recovered.returncode == 0, recovered.stderr
    recovery = json.loads(recovered.stdout)
    assert recovery["manifest"]["partial"] is True
    assert "W_BROKEN_CHAIN" in recovery["manifest"]["warnings"]
    assert "W_UNKNOWN_RECORD_TYPE" in recovery["manifest"]["warnings"]
    assert recovery["manifest"]["counters"]["physical_lines_retained"] == 900
    assert recovery["manifest"]["counters"]["unknown_records_retained"] == 3
    assert recovery["manifest"]["counters"]["complete_turns_retained"] == 124

    events = tmp_path / "events.json"
    events.write_text("[]\n", encoding="utf-8")
    projected = _run_omg(
        "tracker",
        "project",
        "--run",
        "run-1",
        "--generation",
        "0",
        "--events",
        str(events),
        cwd=tmp_path,
    )
    assert projected.returncode == 0, projected.stderr
    assert json.loads(projected.stdout)["run_id"] == "run-1"
    status = _run_omg("tracker", "status", "--run", "run-1", cwd=tmp_path)
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["event_count"] == 0

    guidance = tmp_path / "guidance.md"
    guidance.write_bytes(b"exact\x00guidance\n")
    receipts = tmp_path / "receipts.json"
    receipts.write_text("[]\n", encoding="utf-8")
    compacted = _run_omg(
        "compact",
        "create",
        "--run",
        "run-1",
        "--generation",
        "0",
        "--guidance-file",
        str(guidance),
        "--receipts",
        str(receipts),
        "--recovery-manifest",
        recovery["manifest_path"],
        cwd=tmp_path,
    )
    assert compacted.returncode == 0, compacted.stderr
    checkpoint = json.loads(compacted.stdout)["path"]
    restored = tmp_path / "restored.md"
    rendered = _run_omg(
        "compact",
        "render",
        checkpoint,
        "--guidance-out",
        str(restored),
        cwd=tmp_path,
    )
    assert rendered.returncode == 0, rendered.stderr
    assert restored.read_bytes() == guidance.read_bytes()

    notify_status = _run_omg("notify", "status", cwd=tmp_path)
    assert notify_status.returncode == 0, notify_status.stderr
    assert json.loads(notify_status.stdout)["authoritative"] is False
    queued = _run_omg(
        "notify",
        "send",
        "--owner",
        "run-1",
        "--generation",
        "1",
        "--title",
        "Done",
        "--message",
        "Checks finished",
        cwd=tmp_path,
        env={"OMG_NOTIFICATION_OWNER_NONCE": "0123456789abcdef"},
    )
    assert queued.returncode == 0, queued.stderr
    queued_status = json.loads(queued.stdout)
    assert queued_status["queued"] is True
    assert queued_status["authoritative"] is False


def test_native_and_capability_status_do_not_infer_host_health():
    native = _run_omg("native-status")
    assert native.returncode == 0, native.stderr
    native_status = json.loads(native.stdout)
    assert native_status["native_dashboard"]["status"] == "optional_unclaimed"
    assert native_status["native_workflow"]["status"] == "optional_unclaimed"
    assert native_status["headless_probe"]["attempted"] is False

    capabilities = _run_omg("capabilities")
    assert capabilities.returncode == 0, capabilities.stderr
    status = json.loads(capabilities.stdout)
    tiers = {
        "configured",
        "installed",
        "enabled",
        "loadable",
        "observed",
        "healthy",
        "verified",
    }
    assert set(status["tiers"]) == tiers
    for surface in status["surfaces"].values():
        assert tiers <= set(surface)
    assert status["surfaces"]["mcp"]["classification"] == "native_substitute"
    assert status["surfaces"]["lsp"]["classification"] == "host_owned"
    assert (
        status["surfaces"]["repository_workflow"]["classification"]
        == "native_substitute"
    )
    assert status["surfaces"]["grok_native_workflow"]["verified"] is False
    assert status["surfaces"]["native_dashboard"]["verified"] is False


def test_repository_workflow_cli_plan_and_receipt_run(tmp_path):
    from tests.test_repository_workflows import _provision_receipts

    definition = REPO_ROOT / "tests" / "fixtures" / "workflow" / (
        "production-safety-review-v1.json"
    )
    workflow_input = tmp_path / "input.json"
    workflow_input.write_text(
        json.dumps({"candidate_commit": "abc123"}) + "\n", encoding="utf-8"
    )

    install = _run_omg("workflow", "install", str(definition), cwd=tmp_path)
    assert install.returncode == 0, install.stderr
    installed = json.loads(install.stdout)
    assert installed["definition"]["name"] == "production-safety-review"

    plan_result = _run_omg(
        "workflow",
        "plan",
        "production-safety-review",
        "--version",
        "1.0.0",
        "--input",
        str(workflow_input),
        cwd=tmp_path,
    )
    assert plan_result.returncode == 0, plan_result.stderr
    plan = json.loads(plan_result.stdout)
    assert len(plan["waves"]) == 4
    assert len(plan["tasks"]) == 7

    definition_value = json.loads(definition.read_text(encoding="utf-8"))
    _expected_plan, receipt_map = _provision_receipts(
        tmp_path, definition_value, {"candidate_commit": "abc123"}
    )
    receipts = list(receipt_map.values())
    receipt_path = tmp_path / "receipts.json"
    receipt_path.write_text(json.dumps(receipts) + "\n", encoding="utf-8")
    permissions = (
        "read_repository",
        "run_declared_verification",
        "emit_declared_artifact",
    )
    run_args = [
        "workflow",
        "run",
        "production-safety-review",
        "--version",
        "1.0.0",
        "--input",
        str(workflow_input),
        "--receipts",
        str(receipt_path),
    ]
    for permission in permissions:
        run_args.extend(["--repository-permission", permission])
        run_args.extend(["--host-capability", permission])
        run_args.extend(["--launch-permission", permission])
    run = _run_omg(*run_args, cwd=tmp_path)
    assert run.returncode == 1, run.stderr + run.stdout
    summary = json.loads(run.stdout)
    assert summary["terminal"] == "no_ship"
    assert summary["review"]["verifier_approved"] is False
    assert summary["review"]["skeptic_approved"] is False
    assert summary["review"]["product_authority_verified"] is False
    assert (
        summary["review"]["authority_error"]
        == "E_WORKFLOW_PRODUCT_AUTHORITY_UNAVAILABLE"
    )

    stale_receipts = [dict(row) for row in receipts]
    stale_receipts[0]["run_generation"] = 99
    stale_path = tmp_path / "stale-receipts.json"
    stale_path.write_text(json.dumps(stale_receipts) + "\n", encoding="utf-8")
    stale_args = [
        value if value != str(receipt_path) else str(stale_path) for value in run_args
    ]
    stale = _run_omg(*stale_args, cwd=tmp_path)
    assert stale.returncode == 1
    assert "run_generation does not match launch plan" in stale.stderr

    missing = tmp_path / "missing.json"
    missing.write_text("[]\n", encoding="utf-8")
    denied_args = [
        value if value != str(receipt_path) else str(missing) for value in run_args
    ]
    blocked = _run_omg(*denied_args, cwd=tmp_path)
    assert blocked.returncode == 1
    assert "missing workflow receipts" in blocked.stderr
    assert blocked.stdout == ""


def test_parity_run_delegates_the_frozen_manifest_engine():
    delegated = _run_omg("parity", "run", "init", "--help")
    assert delegated.returncode == 0, delegated.stderr
    assert "--repository-id {OMG,OMA}" in delegated.stdout
    assert "--ownership-manifest-hash" in delegated.stdout


def test_unknown_command_fails():
    r = _run_omg("not-a-real-command")
    assert r.returncode != 0


def test_setup_on_tmp_path(tmp_path):
    grok_home = tmp_path / ".grokhome"
    env = {"GROK_HOME": str(grok_home)}
    r = _run_omg("setup", cwd=tmp_path, env=env)
    assert r.returncode == 0, r.stderr
    assert (tmp_path / ".omg" / "state" / "runs").is_dir()
    assert (tmp_path / ".omg" / "plans").is_dir()
    assert (tmp_path / ".omg" / "research").is_dir()
    assert (tmp_path / ".omg" / "handoffs").is_dir()
    assert (tmp_path / ".omg" / "artifacts").is_dir()
    assert (tmp_path / ".omg" / "ultragoal").is_dir()
    assert (tmp_path / ".omg" / "wiki").is_dir()
    agents = tmp_path / "AGENTS.md"
    assert agents.is_file()
    assert "oh-my-grok" in agents.read_text(encoding="utf-8")
    gi = tmp_path / ".gitignore"
    assert gi.is_file()
    assert ".omg/" in gi.read_text(encoding="utf-8") or ".omg/state" in gi.read_text(
        encoding="utf-8"
    )
    assert "plugin install" in r.stdout.lower() or "grok plugin" in r.stdout.lower()
    # isolation banner always printed after setup success
    assert "[compat.claude]" in r.stdout
    assert "skills = false" in r.stdout
    assert "hooks = false" in r.stdout
    # global rules installed under GROK_HOME (never real ~/.grok)
    rules = grok_home / "rules" / "omg.md"
    assert rules.is_file(), f"expected global rules at {rules}"
    assert "<!-- OMG:START -->" in rules.read_text(encoding="utf-8")


def test_setup_idempotent_agents_marker(tmp_path):
    env = {"GROK_HOME": str(tmp_path / ".grokhome")}
    r1 = _run_omg("setup", cwd=tmp_path, env=env)
    assert r1.returncode == 0
    text1 = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    r2 = _run_omg("setup", cwd=tmp_path, env=env)
    assert r2.returncode == 0
    text2 = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    # marker block should not duplicate
    marker = "<!-- OMG:START -->"
    assert text1.count(marker) == 1
    assert text2.count(marker) == 1
    # second setup with same GROK_HOME still succeeds (idempotent rules)
    rules = tmp_path / ".grokhome" / "rules" / "omg.md"
    assert rules.is_file()
    assert marker in rules.read_text(encoding="utf-8")


def test_setup_no_global_rules_skips_install(tmp_path):
    """setup --no-global-rules returns 0 and does not create rules file."""
    grok_home = tmp_path / ".grokhome"
    env = {"GROK_HOME": str(grok_home)}
    r = _run_omg("setup", "--no-global-rules", cwd=tmp_path, env=env)
    assert r.returncode == 0, r.stderr
    rules = grok_home / "rules" / "omg.md"
    assert not rules.is_file(), f"rules must not be created with --no-global-rules: {rules}"


def test_doctor_runnable():
    r = _run_omg("doctor", cwd=REPO_ROOT)
    # doctor prints OK/FAIL lines; may pass or fail on grok PATH depending on env
    out = r.stdout + r.stderr
    assert "plugin.json" in out.lower() or "OK" in out or "FAIL" in out
    # process always finishes cleanly (0 or 1), not crash
    assert r.returncode in (0, 1)
    # always runs compat.claude section + isolation banner
    assert "compat.claude" in out
    assert "skills = false" in out


def test_doctor_strict_flag_accepted():
    r = _run_omg("doctor", "--strict", cwd=REPO_ROOT)
    assert r.returncode in (0, 1)
    out = r.stdout + r.stderr
    assert "compat.claude" in out or "plugin.json" in out.lower()
    # soft-gate honesty footer always present
    assert "fail-open" in out.lower() or "soft-gate" in out.lower()
    # best-effort trust section present
    assert "trust" in out.lower() or "inventory" in out.lower()


def test_state_no_active(tmp_path):
    # setup dirs then state with no run (hermetic GROK_HOME)
    env = {"GROK_HOME": str(tmp_path / ".grokhome")}
    _run_omg("setup", cwd=tmp_path, env=env)
    r = _run_omg("state", cwd=tmp_path)
    assert r.returncode == 0
    assert "no active run" in r.stdout.lower()


def test_state_and_cancel_via_cli(tmp_path):
    from omg_cli.state import create_run, load_active_run

    run = create_run(tmp_path, mode="ralph", goal="cli cancel")
    r_state = _run_omg("state", cwd=tmp_path)
    assert r_state.returncode == 0
    assert run["run_id"] in r_state.stdout

    r_cancel = _run_omg("cancel", cwd=tmp_path)
    assert r_cancel.returncode == 0, r_cancel.stderr
    assert load_active_run(tmp_path) is None


def test_mode_launchers_dry_run(tmp_path):
    """Mode launchers: create run state without execing grok when --dry-run."""
    for mode in ("ulw", "ralph", "ralplan"):
        # ralph defaults require_acceptance → non-zero when not verified;
        # opt out for this scaffold smoke test.
        # ralplan FSM dry_run without verifier APPROVE → failed (exit 1).
        args = [mode, "do something", "--dry-run"]
        if mode == "ralph":
            args.append("--no-require-acceptance")
        r = _run_omg(*args, cwd=tmp_path)
        if mode == "ralplan":
            assert r.returncode == 1, r.stderr + r.stdout
        else:
            assert r.returncode == 0, r.stderr + r.stdout
        # active run should exist under project cwd (tmp_path)
        state = _run_omg("state", cwd=tmp_path)
        assert state.returncode == 0
        assert "do something" in state.stdout or mode in state.stdout
        if mode == "ralplan":
            assert "ralplan" in state.stdout.lower() or "failed" in state.stdout.lower()
            runs = list((tmp_path / ".omg" / "state" / "runs").glob("*/ralplan.json"))
            assert runs, "ralplan.json missing"
            data = json.loads(runs[0].read_text(encoding="utf-8"))
            assert data["status"] == "failed"
            assert data["accepted"] is False
        # cancel so next mode can create a new active cleanly
        _run_omg("cancel", cwd=tmp_path)


def test_accept_cli_freeze_and_run(tmp_path):
    """omg accept freezes prd commands and stamps CLI acceptance result."""
    from omg_cli.state import create_run

    run = create_run(tmp_path, mode="ralph", goal="accept cli")
    rid = run["run_id"]
    prd_path = tmp_path / ".omg" / "state" / "runs" / rid / "prd.json"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        json.dumps(
            {
                "version": 1,
                "goal": "accept cli",
                "stories": [
                    {"id": "s1", "title": "ok", "commands": [["true"]]}
                ],
                "global_commands": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    # non-tty subprocess requires --yes; --review prints sha/cwd/commands first
    r = _run_omg("accept", "--run", rid, "--review", "--yes", cwd=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "verified" in r.stdout.lower() or rid in r.stdout
    out = r.stdout.lower()
    assert "manifest_sha256" in out or "manifest_sha" in out
    assert "acceptance commands" in out or "true" in r.stdout
    assert rid in r.stdout or "run_id" in out
    result = tmp_path / ".omg" / "state" / "runs" / rid / "acceptance.result.json"
    assert result.is_file()
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["writer"] == "omg-cli"
    assert data["passed"] is True


def test_accept_cli_strict_v2_sets_verified(tmp_path):
    """strict-v2 accept must auto-lease and set verified (default ralph path)."""
    from omg_cli.state import create_run, load_run

    run = create_run(
        tmp_path,
        mode="ralph",
        goal="strict accept",
        force=True,
        extra={"schema_version": 2, "lifecycle_version": 2},
    )
    rid = run["run_id"]
    assert run.get("schema_version") == 2
    prd_path = tmp_path / ".omg" / "state" / "runs" / rid / "prd.json"
    prd_path.parent.mkdir(parents=True, exist_ok=True)
    prd_path.write_text(
        json.dumps(
            {
                "version": 1,
                "goal": "strict accept",
                "stories": [
                    {"id": "s1", "title": "ok", "commands": [["true"]]}
                ],
                "global_commands": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    r = _run_omg("accept", "--run", rid, "--yes", cwd=tmp_path)
    assert r.returncode == 0, r.stderr + r.stdout
    assert "verified" in r.stdout.lower() or rid in r.stdout
    assert "set_verified failed" not in (r.stderr + r.stdout).lower()
    assert "fencing" not in (r.stderr + r.stdout).lower()
    status = load_run(tmp_path, rid)
    assert status is not None
    assert status["verified"] is True
    assert status["status"] == "verified"
    result = tmp_path / ".omg" / "state" / "runs" / rid / "acceptance.result.json"
    assert result.is_file()
    data = json.loads(result.read_text(encoding="utf-8"))
    assert data["writer"] == "omg-cli"
    assert data["passed"] is True


def test_accept_cli_review_requires_yes(tmp_path):
    from omg_cli.state import create_run

    run = create_run(tmp_path, mode="ralph", goal="review gate")
    rid = run["run_id"]
    prd_path = tmp_path / ".omg" / "state" / "runs" / rid / "prd.json"
    prd_path.write_text(
        json.dumps(
            {
                "version": 1,
                "goal": "review gate",
                "stories": [
                    {"id": "s1", "title": "ok", "commands": [["true"]]}
                ],
                "global_commands": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    r = _run_omg("accept", "--run", rid, "--review", cwd=tmp_path)
    assert r.returncode == 2, r.stderr + r.stdout
    assert "yes" in (r.stderr + r.stdout).lower()


def test_accept_cli_yes_cannot_bypass_policy(tmp_path):
    """--yes skips confirmation only; python -c still rejected."""
    from omg_cli.state import create_run

    run = create_run(tmp_path, mode="ralph", goal="policy floor")
    rid = run["run_id"]
    prd_path = tmp_path / ".omg" / "state" / "runs" / rid / "prd.json"
    prd_path.write_text(
        json.dumps(
            {
                "version": 1,
                "goal": "policy floor",
                "stories": [
                    {
                        "id": "s1",
                        "title": "bad",
                        "commands": [["python3", "-c", "pass"]],
                    }
                ],
                "global_commands": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    r = _run_omg("accept", "--run", rid, "--yes", cwd=tmp_path)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "-c" in (r.stderr + r.stdout) or "policy" in (r.stderr + r.stdout).lower()


def test_safe_and_yolo_flags_accepted():
    r = _run_omg("--help")
    assert r.returncode == 0
    # flags documented or at least parseable
    r2 = _run_omg("doctor", "--safe")
    assert r2.returncode in (0, 1)
    r3 = _run_omg("doctor", "--yolo")
    assert r3.returncode in (0, 1)


def test_doctor_hooks_missing_plugin_root(monkeypatch, tmp_path):
    """Fail-path: monkeypatched empty plugin root → hooks scripts check fails."""
    import omg_cli.doctor as doctor

    monkeypatch.setattr(doctor, "plugin_root", lambda: tmp_path)
    name, ok, detail = doctor.check_hooks_scripts()
    assert name == "hooks scripts"
    assert ok is False
    assert "missing" in detail.lower()


def test_doctor_hooks_not_executable(monkeypatch, tmp_path):
    """Fail-path: hooks present but lacking +x → check fails."""
    import omg_cli.doctor as doctor

    for rel in doctor.HOOK_SCRIPTS:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# stub\n", encoding="utf-8")
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)

    monkeypatch.setattr(doctor, "plugin_root", lambda: tmp_path)
    name, ok, detail = doctor.check_hooks_scripts()
    assert name == "hooks scripts"
    assert ok is False
    assert "not executable" in detail.lower() or "+x" in detail.lower()
