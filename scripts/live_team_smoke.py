#!/usr/bin/env python3
"""Live / transport smoke for OMX-like ``omg team N[:role]`` (experimental).

Default is **dry-run** proof of shorthand + state seed. Pass ``--live`` only when
``OMG_EXPERIMENTAL_TMUX_TEAM=1``, tmux, and grok are available — live mode still
does not claim promotion until ACK/claim/commit evidence is collected.

``--fixture-executor`` runs the hermetic ACK fixture in split panes (tmux
required; no grok). That path proves transport only — never Grok live parity.

Usage:
  python3 scripts/live_team_smoke.py --workers 2 --goal "1. a\\n2. b"
  OMG_EXPERIMENTAL_TMUX_TEAM=1 python3 scripts/live_team_smoke.py --live ...
  OMG_EXPERIMENTAL_TMUX_TEAM=1 python3 scripts/live_team_smoke.py --fixture-executor ...
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    args = parser.parse_args()
    if args.live and args.fixture_executor:
        print(
            "live_team_smoke: pass only one of --live / --fixture-executor",
            file=sys.stderr,
        )
        return 2

    env = os.environ.copy()
    env["OMG_EXPERIMENTAL_TMUX_TEAM"] = "1"
    env["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    if args.fixture_executor:
        env.setdefault("OMG_TEAM_FIXTURE_HOLD_S", "15")

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
                )
                print(json.dumps(meta, indent=2, ensure_ascii=False))
                assert meta.get("dry_run") is False
                assert meta.get("topology") == "split"
                assert meta.get("executor") == "fixture"
                session = str(meta.get("session") or "")
                panes = _tmux("list-panes", "-t", session, "-F", "#{pane_id}")
                pane_count = len(
                    [ln for ln in (panes.stdout or "").splitlines() if ln.strip()]
                )
                assert pane_count == args.workers, pane_count
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
                        stop_team(cwd, str(meta["run_id"]))
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
            cmd.append("--force")

        print("CMD:", " ".join(cmd))
        proc = _run(cmd, cwd=cwd, env=env)
        sys.stdout.write(proc.stdout)
        sys.stderr.write(proc.stderr)
        if proc.returncode != 0:
            return proc.returncode
        meta = json.loads(proc.stdout)
        assert meta.get("launch_mode") == "shorthand"
        assert meta.get("topology") == "split"
        if not args.live:
            assert meta.get("dry_run") is True
        else:
            assert meta.get("dry_run") is False
            # Best-effort stop so we don't leave panes behind.
            stop = _run(
                [
                    sys.executable,
                    "-m",
                    "omg_cli.main",
                    "team",
                    "stop",
                    str(meta.get("team_name") or meta["run_id"]),
                ],
                cwd=cwd,
                env=env,
            )
            sys.stdout.write(stop.stdout)
            sys.stderr.write(stop.stderr)
        print("LIVE_TEAM_SMOKE_OK" if args.live else "DRY_TEAM_SMOKE_OK")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
