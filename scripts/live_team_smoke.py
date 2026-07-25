#!/usr/bin/env python3
"""Live / transport smoke for OMX-like ``omg team N[:role]`` (experimental).

Default is **dry-run** proof of shorthand + state seed. Pass ``--live`` only when
``OMG_EXPERIMENTAL_TMUX_TEAM=1``, tmux, and grok are available — live mode still
does not claim promotion until ACK/claim/commit evidence is collected.

Usage:
  python3 scripts/live_team_smoke.py --workers 2 --goal "1. a\\n2. b"
  OMG_EXPERIMENTAL_TMUX_TEAM=1 python3 scripts/live_team_smoke.py --live ...
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
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
    args = parser.parse_args()

    env = os.environ.copy()
    env["OMG_EXPERIMENTAL_TMUX_TEAM"] = "1"
    env["PYTHONPATH"] = str(ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )

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
