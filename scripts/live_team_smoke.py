#!/usr/bin/env python3
"""Live / transport smoke for OMX-like ``omg team N[:role]`` (experimental).

Default is **dry-run** proof of shorthand + state seed. Pass ``--live`` only when
``OMG_EXPERIMENTAL_TMUX_TEAM=1``, tmux, and grok credentials are available.

``--live`` is the promotion gate: it prints ``LIVE_TEAM_SMOKE_OK`` **only** when
all hard assertions pass (real grok panes, worktrees, schema-v2 provider-ready
startup with live provider identity, claim→completed, stop clears the owned
session). Legacy ``worker-ready`` v1 receipts are **not** sufficient (#99).
Mailbox ACK remains optional enrichment. Missing credentials / quota / timeouts
exit non-zero without that line — never claim promotion from a soft skip.

``--fixture-executor`` runs the hermetic ACK fixture in split panes (tmux
required; no grok). That path proves transport only — never Grok live parity.

``--interactive-ux`` (#104) records schema ``interactive_evidence_v1`` on an
isolated tmux socket with a fake ready provider and prints
``LIVE_TEAM_INTERACTIVE_UX_OK`` only when leader visibility/focus + readiness
assertions pass. Fail-closed without tmux; never invents credentials evidence.

Usage:
  python3 scripts/live_team_smoke.py --workers 2 --goal "1. a\\n2. b"
  OMG_EXPERIMENTAL_TMUX_TEAM=1 python3 scripts/live_team_smoke.py --live ...
  OMG_EXPERIMENTAL_TMUX_TEAM=1 python3 scripts/live_team_smoke.py --fixture-executor ...
  python3 scripts/live_team_smoke.py --interactive-ux --workers 2 --goal "1. a\\n2. b"
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Live claim→completed poll (seconds). Override with OMG_LIVE_TEAM_TIMEOUT_S.
_DEFAULT_LIVE_TIMEOUT_S = 600.0
_CLAIM_POLL_S = 2.0


def _run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _tmux(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _fail(msg: str, *, code: int = 1) -> int:
    print(f"live_team_smoke: {msg}", file=sys.stderr)
    return code


def _extract_json_object(text: str) -> dict:
    """Parse the first top-level JSON object from CLI stdout."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in stdout")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("stdout JSON is not an object")
    return data


def _pane_count(session: str) -> int:
    proc = _tmux("list-panes", "-t", session, "-F", "#{pane_id}")
    if proc.returncode != 0:
        return 0
    return len([ln for ln in (proc.stdout or "").splitlines() if ln.strip()])


def _pane_current_paths(session: str) -> set[Path]:
    proc = _tmux("list-panes", "-t", session, "-F", "#{pane_current_path}")
    if proc.returncode != 0:
        return set()
    return {
        Path(line.strip()).resolve()
        for line in (proc.stdout or "").splitlines()
        if line.strip()
    }


def _list_sessions() -> set[str]:
    proc = _tmux("list-sessions", "-F", "#{session_name}")
    if proc.returncode != 0:
        return set()
    return {ln.strip() for ln in (proc.stdout or "").splitlines() if ln.strip()}


def _session_alive(session: str) -> bool:
    return _tmux("has-session", "-t", session).returncode == 0


def _git_worktree_omg_count(cwd: Path) -> int:
    proc = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=str(cwd),
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return 0
    count = 0
    for line in (proc.stdout or "").splitlines():
        if line.startswith("worktree ") and "/.omg/worktrees/" in line.replace("\\", "/"):
            count += 1
    return count


def _leader_ack_count(root: Path, *, run_id: str, team_id: str) -> int:
    from omg_cli.team.mailbox import _recipient_path

    path = _recipient_path(root, run_id, team_id, "leader-fixed")
    if not path.is_file():
        return 0
    store = json.loads(path.read_text(encoding="utf-8"))
    return sum(
        1
        for m in (store.get("messages") or [])
        if isinstance(m, dict) and m.get("body") == "ACK"
    )


def _list_api_tasks(root: Path, *, run_id: str, team_id: str) -> list[dict]:
    from omg_cli.team.api import execute_team_api

    code, envelope = execute_team_api(
        "list-tasks",
        {"run_id": run_id, "team_id": team_id},
        root=root,
        env={"OMG_EXPERIMENTAL_TMUX_TEAM": "1"},
    )
    if code != 0 or not envelope.get("ok"):
        return []
    tasks = (envelope.get("data") or {}).get("tasks") or []
    return [t for t in tasks if isinstance(t, dict)]


def _preflight_live() -> str | None:
    """Return honest failure reason when live prerequisites are missing."""
    if shutil.which("tmux") is None:
        return "tmux not on PATH (required for --live)"
    if shutil.which("grok") is None:
        return "grok not on PATH (required for --live)"
    auth = Path.home() / ".grok" / "auth.json"
    if not auth.is_file():
        return f"missing {auth} (no grok credentials for --live)"
    try:
        raw = json.loads(auth.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return f"unreadable grok auth.json: {exc}"
    if not isinstance(raw, dict) or not raw:
        return "grok auth.json empty or invalid (no credentials)"
    return None


def _write_evidence(payload: dict) -> Path | None:
    """Best-effort evidence under docs/research/live/ (gitignored ok)."""
    try:
        out_dir = Path(
            os.environ.get("OMG_LIVE_EVIDENCE_DIR")
            or (ROOT / "docs" / "research" / "live")
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        path = out_dir / f"team-smoke-{stamp}.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path
    except OSError as exc:
        print(f"live_team_smoke: evidence write skipped: {exc}", file=sys.stderr)
        return None


def _assert_live_gate(
    *,
    cwd: Path,
    meta: dict,
    workers: int,
    env: dict[str, str],
    timeout_s: float,
) -> None:
    """Raise AssertionError if any promotion-gate check fails."""
    assert meta.get("dry_run") is False, f"dry_run must be false, got {meta.get('dry_run')!r}"
    assert meta.get("launch_mode") == "shorthand"
    assert meta.get("topology") == "split"
    assert meta.get("executor") in (None, "grok", ""), meta.get("executor")

    session = str(meta.get("session") or "")
    assert session, "missing tmux session in meta"
    assert _session_alive(session), f"session {session!r} not alive after launch"

    pane_n = _pane_count(session)
    assert pane_n == workers, f"pane count={pane_n} expected {workers}"

    tasks = meta.get("tasks") or []
    assert isinstance(tasks, list) and len(tasks) == workers, (
        f"meta.tasks len={len(tasks) if isinstance(tasks, list) else 'n/a'} expected {workers}"
    )
    for rec in tasks:
        assert isinstance(rec, dict), rec
        cmd = str(rec.get("pane_command") or "")
        # #99: pane command is the supervisor; grok lives in provider descriptor.
        assert "supervisor" in cmd, f"pane_command missing supervisor: {cmd[:120]!r}"
        assert "worker-ready" not in cmd, f"legacy worker-ready wrap refused: {cmd[:120]!r}"
        assert "team_worker_fixture" not in cmd, f"fixture pane refused: {cmd[:120]!r}"
        assert rec.get("provider") in (None, "grok") or str(rec.get("provider")) == "grok"
        argv = rec.get("argv") or []
        assert isinstance(argv, list) and argv and argv[0] == "grok", argv

    wt_n = _git_worktree_omg_count(cwd)
    assert wt_n == workers, (
        f"git worktree list omg worktrees={wt_n} expected {workers}"
    )

    run_id = str(meta["run_id"])
    team_id = str(meta.get("team_id") or "team")
    # #99: provider-ready gate requires schema-v2 process readiness with live
    # provider identity. Mailbox ACKs are enrichment only — never sufficient.
    process_ready = meta.get("startup_process_ready")
    startup_status = meta.get("startup_status")
    process_ok = process_ready is not None and int(process_ready) >= workers
    assert startup_status == "running", (
        f"startup_status={startup_status!r} expected running "
        f"(process_ready={process_ready!r})"
    )
    assert process_ok, (
        f"startup not provider-ready: process_ready={process_ready!r} "
        f"expected >= {workers} (mailbox ACK alone is not enough)"
    )
    for row in meta.get("startup_workers") or []:
        assert row.get("gate_ok") is True, row
        assert row.get("provider_alive") is True, row
        assert row.get("legacy") is False, row
        assert row.get("identity_ok") is not False, row
    # Optional enrichment: mailbox ACKs may arrive later.
    ack_n = _leader_ack_count(cwd, run_id=run_id, team_id=team_id)
    _ = ack_n  # enrichment only; do not gate on ACK

    # claim → completed: poll API board until all tasks completed (and saw claims).
    deadline = time.monotonic() + timeout_s
    seen_claimed: set[str] = set()
    seen_completed: set[str] = set()
    last_statuses: dict[str, str] = {}
    while time.monotonic() < deadline:
        board = _list_api_tasks(cwd, run_id=run_id, team_id=team_id)
        last_statuses = {}
        for task in board:
            tid = str(task.get("id") or "")
            status = str(task.get("status") or "")
            last_statuses[tid] = status
            claim = task.get("claim")
            owner = task.get("owner")
            if status == "in_progress" or claim or owner:
                seen_claimed.add(tid)
            if status == "completed":
                seen_completed.add(tid)
                # Completed tasks count as having been claimed if claim metadata remains.
                if claim or owner:
                    seen_claimed.add(tid)
        if (
            len(board) >= workers
            and len(seen_completed) >= workers
            and seen_claimed >= seen_completed
        ):
            break
        time.sleep(_CLAIM_POLL_S)
    else:
        raise AssertionError(
            "claim→completed timeout: "
            f"completed={sorted(seen_completed)} claimed={sorted(seen_claimed)} "
            f"last={last_statuses} timeout_s={timeout_s}"
        )

    # stop clears owned session; other sessions untouched.
    before = _list_sessions()
    assert session in before, f"owned session {session!r} missing before stop"
    others = before - {session}

    stop = _run(
        [
            sys.executable,
            "-m",
            "omg_cli.main",
            "team",
            "stop",
            str(meta.get("team_name") or run_id),
            "--force",
            # Live grok panes need a short SIGTERM window before SIGKILL identity
            # revalidation; zero-grace races produce authority-drift stop_refused.
            "--kill-grace",
            "2.0",
        ],
        cwd=cwd,
        env=env,
    )
    sys.stdout.write(stop.stdout)
    sys.stderr.write(stop.stderr)
    assert stop.returncode == 0, f"team stop exit {stop.returncode}"
    assert not _session_alive(session), f"owned session {session!r} still alive after stop"
    after = _list_sessions()
    missing_others = others - after
    assert not missing_others, (
        f"stop touched unrelated sessions: {sorted(missing_others)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--role", default="executor")
    parser.add_argument("--goal", required=True)
    parser.add_argument(
        "--live",
        action="store_true",
        help="non-dry-run tmux launch (experimental; requires gate + tmux + grok)",
    )
    parser.add_argument(
        "--fixture-executor",
        action="store_true",
        help=(
            "non-dry-run split panes with hermetic ACK fixture "
            "(tmux required; not Grok live parity)"
        ),
    )
    parser.add_argument(
        "--interactive-ux",
        action="store_true",
        help=(
            "Layer C operator evidence (#104): schema interactive_evidence_v1 + "
            "LIVE_TEAM_INTERACTIVE_UX_OK (fixture path; fail-closed without tmux)"
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="live claim→completed timeout seconds "
        f"(default OMG_LIVE_TEAM_TIMEOUT_S or {_DEFAULT_LIVE_TIMEOUT_S})",
    )
    args = parser.parse_args()
    if sum(bool(x) for x in (args.live, args.fixture_executor, args.interactive_ux)) > 1:
        return _fail(
            "pass only one of --live / --fixture-executor / --interactive-ux",
            code=2,
        )

    env = os.environ.copy()
    env["OMG_EXPERIMENTAL_TMUX_TEAM"] = "1"
    env["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    # Force outside-tmux owned session for stop/session assertions.
    env.pop("TMUX", None)
    if args.fixture_executor:
        env.setdefault("OMG_TEAM_FIXTURE_HOLD_S", "15")

    live_timeout = float(
        args.timeout
        if args.timeout is not None
        else os.environ.get("OMG_LIVE_TEAM_TIMEOUT_S", _DEFAULT_LIVE_TIMEOUT_S)
    )

    if args.live:
        reason = _preflight_live()
        if reason:
            _write_evidence(
                {
                    "ok": False,
                    "mode": "live",
                    "reason": reason,
                    "live_ok_line": False,
                }
            )
            return _fail(f"live preflight failed: {reason}")

    with tempfile.TemporaryDirectory(prefix="omg-team-smoke-") as tmp:
        cwd = Path(tmp)
        subprocess.run(["git", "init"], cwd=cwd, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "smoke@example.com"],
            cwd=cwd,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "smoke"],
            cwd=cwd,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "commit.gpgsign", "false"],
            cwd=cwd,
            check=True,
            capture_output=True,
        )
        (cwd / "README.md").write_text("smoke\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=cwd, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=cwd,
            check=True,
            capture_output=True,
        )
        env["OMG_PROJECT_ROOT"] = str(cwd)
        setup = _run(
            [
                sys.executable,
                "-m",
                "omg_cli.main",
                "setup",
                "--here",
                "--no-global-rules",
                "--no-global-hook",
            ],
            cwd=cwd,
            env=env,
        )
        if setup.returncode != 0:
            sys.stderr.write(setup.stdout)
            sys.stderr.write(setup.stderr)
            return _fail(f"omg setup --here failed: {setup.returncode}")

        if args.interactive_ux:
            if shutil.which("tmux") is None:
                _write_evidence(
                    {
                        "schema_version": "interactive_evidence_v1",
                        "ok": False,
                        "reason": "tmux unavailable",
                        "live_ok_line": False,
                    }
                )
                return _fail("interactive-ux requires tmux on PATH")
            sys.path.insert(0, str(ROOT))
            from omg_cli import __version__ as omg_version
            from omg_cli.team.plane import stop_team
            from tests.support.team_tmux_harness import (
                IsolatedTmuxServer,
                LeaderSession,
                install_fixture_provider,
                launch_team_inside,
                provider_script,
            )

            evidence: dict = {
                "schema_version": "interactive_evidence_v1",
                "ok": False,
                "live_ok_line": False,
                "omg_version": omg_version,
                "repo_sha": subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(ROOT),
                    capture_output=True,
                    text=True,
                    check=False,
                ).stdout.strip()
                or None,
                "tmux_version": subprocess.run(
                    ["tmux", "-V"], capture_output=True, text=True, check=False
                ).stdout.strip()
                or None,
                "provider": "fixture/ready_provider",
            }

            class _EnvPatch:
                def setenv(self, k: str, v: str) -> None:
                    os.environ[k] = v

                def delenv(self, k: str, raising: bool = True) -> None:
                    os.environ.pop(k, None)

                def setattr(self, obj: object, name: str, value: object) -> None:
                    setattr(obj, name, value)

            mp = _EnvPatch()
            with IsolatedTmuxServer(prefix="omg104-live") as server:
                leader = LeaderSession(server)
                leader.create()
                assert leader.leader is not None
                before = {
                    "pane_id": leader.leader.pane_id,
                    "pane_pid": leader.leader.pane_pid,
                    "window_id": leader.leader.window_id,
                }
                evidence["topology_before"] = before
                install_fixture_provider(mp, provider_script("ready_provider.py"))
                for k, v in leader.tmux_env().items():
                    os.environ[k] = v
                os.environ["OMG_EXPERIMENTAL_TMUX_TEAM"] = "1"
                os.environ.pop("OMG_DISABLE_TMUX_TEAM", None)
                os.environ["OMG_TEAM_FIXTURE_HOLD_S"] = "20"
                os.environ["OMG_TEAM_READY_TIMEOUT_MS"] = "20000"
                os.environ["OMG_TEAM_PROVIDER_HOLD_S"] = "20"
                meta = None
                try:
                    meta = launch_team_inside(
                        root=cwd, leader=leader, workers=args.workers
                    )
                    evidence["run_id"] = meta.get("run_id")
                    evidence["startup_status"] = meta.get("startup_status")
                    evidence["readiness_timeline"] = meta.get("startup_workers")
                    topo = leader.capture_topology()
                    evidence["topology_after"] = {
                        "pane_ids": list(topo.pane_ids),
                        "active_pane_id": topo.active_pane_id,
                        "window_id": before["window_id"],
                    }
                    evidence["leader_pane_hash"] = before["pane_id"]
                    evidence["worker_pane_hash"] = [
                        str(t.get("pane_id")) for t in (meta.get("tasks") or [])
                    ]
                    evidence["operator_marker"] = "capture_ok"
                    evidence["scale_marker"] = "skipped_optional"
                    evidence["resume_marker"] = "skipped_optional"
                    assert meta.get("startup_status") == "running"
                    assert topo.active_pane_id == before["pane_id"]
                    assert before["pane_id"] in topo.pane_ids
                    stop_team(cwd, str(meta["run_id"]), force=True)
                    evidence["stop_marker"] = "workers_cleared"
                    evidence["ok"] = True
                    evidence["live_ok_line"] = True
                    path = _write_evidence(evidence)
                    if path:
                        print(f"live_team_smoke: evidence {path}", file=sys.stderr)
                    print("LIVE_TEAM_INTERACTIVE_UX_OK")
                    return 0
                except Exception as exc:
                    evidence["error"] = repr(exc)
                    if meta is not None:
                        try:
                            stop_team(cwd, str(meta["run_id"]), force=True)
                        except Exception:
                            pass
                    _write_evidence(evidence)
                    return _fail(f"interactive-ux failed: {exc}")

        if args.fixture_executor:
            # In-process launch so we can pass executor="fixture" without CLI flag.
            sys.path.insert(0, str(ROOT))
            from omg_cli.team.api import execute_team_api
            from omg_cli.team.plane import stop_team
            from omg_cli.team.runtime import launch_team

            session = None
            meta = None
            try:
                meta = launch_team(
                    args.goal,
                    workers=args.workers,
                    role=args.role,
                    root=cwd,
                    dry_run=False,
                    force=True,
                    check_binary=False,
                    env={"OMG_EXPERIMENTAL_TMUX_TEAM": "1"},
                    executor="fixture",
                    detach=True,
                )
                print(json.dumps(meta, indent=2, ensure_ascii=False))
                assert meta.get("dry_run") is False
                assert meta.get("topology") == "split"
                assert meta.get("executor") == "fixture"
                session = str(meta.get("session") or "")
                pane_count = _pane_count(session)
                assert pane_count == args.workers, pane_count
                expected_worktrees = {
                    Path(str(task["worktree"])).resolve()
                    for task in meta.get("tasks") or []
                }
                pane_paths = _pane_current_paths(session)
                assert pane_paths == expected_worktrees, (
                    f"pane cwd mismatch: actual={sorted(map(str, pane_paths))} "
                    f"expected={sorted(map(str, expected_worktrees))}"
                )
                # Poll durable leader mailbox (list API omits bodies).
                from omg_cli.team.mailbox import _recipient_path

                team_id = str(meta.get("team_id") or "team")
                deadline = time.monotonic() + 20.0
                ack_count = 0
                while time.monotonic() < deadline:
                    code, envelope = execute_team_api(
                        "mailbox-list",
                        {
                            "run_id": meta["run_id"],
                            "team_id": team_id,
                            "worker": "leader-fixed",
                        },
                        root=cwd,
                        env={"OMG_EXPERIMENTAL_TMUX_TEAM": "1"},
                    )
                    listed = 0
                    if code == 0 and envelope.get("ok"):
                        listed = int((envelope.get("data") or {}).get("count") or 0)
                    path = _recipient_path(cwd, meta["run_id"], team_id, "leader-fixed")
                    if path.is_file():
                        store = json.loads(path.read_text(encoding="utf-8"))
                        ack_count = sum(
                            1
                            for m in (store.get("messages") or [])
                            if isinstance(m, dict) and m.get("body") == "ACK"
                        )
                    if ack_count >= args.workers and listed >= args.workers:
                        break
                    time.sleep(0.25)
                assert ack_count >= args.workers, f"ACK count={ack_count}"
                print("FIXTURE_TEAM_SMOKE_OK")
                return 0
            finally:
                if meta is not None:
                    try:
                        stop_team(cwd, str(meta["run_id"]), force=True)
                    except Exception:
                        pass
                if session:
                    _tmux("kill-session", "-t", session)

        cmd = [
            sys.executable,
            "-m",
            "omg_cli.main",
            "team",
            f"{args.workers}:{args.role}",
            args.goal,
        ]
        if not args.live:
            cmd.append("--dry-run")
        else:
            # Non-interactive smoke: owned detached session + supersede active.
            cmd.extend(["--force", "--detach"])

        print("CMD:", " ".join(cmd))
        proc = _run(cmd, cwd=cwd, env=env)
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)

        if not args.live:
            if proc.returncode != 0:
                return proc.returncode
            meta = _extract_json_object(proc.stdout)
            assert meta.get("launch_mode") == "shorthand"
            assert meta.get("topology") == "split"
            assert meta.get("dry_run") is True
            print("DRY_TEAM_SMOKE_OK")
            return 0

        # --- live path ---
        evidence: dict = {
            "ok": False,
            "mode": "live",
            "returncode": proc.returncode,
            "live_ok_line": False,
        }
        meta: dict | None = None
        try:
            meta = _extract_json_object(proc.stdout)
            evidence["run_id"] = meta.get("run_id")
            evidence["session"] = meta.get("session")
            evidence["startup_status"] = meta.get("startup_status")
            evidence["startup_acks"] = meta.get("startup_acks")
        except ValueError as exc:
            evidence["parse_error"] = str(exc)
            _write_evidence(evidence)
            return _fail(f"live launch stdout not JSON: {exc}")

        if proc.returncode != 0:
            evidence["stderr_tail"] = (proc.stderr or "")[-800:]
            # Best-effort cleanup so a failed_start does not leave panes.
            try:
                _run(
                    [
                        sys.executable,
                        "-m",
                        "omg_cli.main",
                        "team",
                        "stop",
                        str(meta.get("team_name") or meta.get("run_id") or ""),
                        "--force",
                    ],
                    cwd=cwd,
                    env=env,
                )
            except Exception:
                pass
            _write_evidence(evidence)
            return _fail(
                f"live launch exit {proc.returncode} "
                f"(startup_status={meta.get('startup_status')!r} "
                f"acks={meta.get('startup_acks')!r})"
            )

        sys.path.insert(0, str(ROOT))
        try:
            _assert_live_gate(
                cwd=cwd,
                meta=meta,
                workers=args.workers,
                env=env,
                timeout_s=live_timeout,
            )
        except AssertionError as exc:
            evidence["assertion"] = str(exc)
            # Cleanup owned session if still up.
            session = str(meta.get("session") or "")
            try:
                _run(
                    [
                        sys.executable,
                        "-m",
                        "omg_cli.main",
                        "team",
                        "stop",
                        str(meta.get("team_name") or meta.get("run_id") or ""),
                        "--force",
                    ],
                    cwd=cwd,
                    env=env,
                )
            except Exception:
                pass
            if session:
                _tmux("kill-session", "-t", session)
            _write_evidence(evidence)
            return _fail(f"live gate assertion failed: {exc}")
        except Exception as exc:
            evidence["error"] = repr(exc)
            session = str(meta.get("session") or "")
            try:
                _run(
                    [
                        sys.executable,
                        "-m",
                        "omg_cli.main",
                        "team",
                        "stop",
                        str(meta.get("team_name") or meta.get("run_id") or ""),
                        "--force",
                    ],
                    cwd=cwd,
                    env=env,
                )
            except Exception:
                pass
            if session:
                _tmux("kill-session", "-t", session)
            _write_evidence(evidence)
            return _fail(f"live gate error: {exc}")

        evidence["ok"] = True
        evidence["live_ok_line"] = True
        path = _write_evidence(evidence)
        if path:
            print(f"live_team_smoke: evidence {path}", file=sys.stderr)
        print("LIVE_TEAM_SMOKE_OK")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
