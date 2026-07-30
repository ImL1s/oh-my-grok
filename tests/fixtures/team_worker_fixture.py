#!/usr/bin/env python3
"""Hermetic team worker fixture: ACK leader via ``omg team api``, then hold.

Not Grok live parity — replaces pane_command so split-pane transport can be
proved without the grok binary or API quota.

Requires ``OMG_EXPERIMENTAL_TMUX_TEAM=1`` and identity env from pane ``-e``:
``OMG_TEAM_WORKER_ID``, ``OMG_TEAM_RUN_ID``, ``OMG_TEAM_ID``,
``OMG_TEAM_LEADER_ROOT``.

Usage (from repo root)::

    PYTHONPATH=. python tests/fixtures/team_worker_fixture.py
    PYTHONPATH=. python -m tests.fixtures.team_worker_fixture
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

EXPERIMENTAL_ENV = "OMG_EXPERIMENTAL_TMUX_TEAM"
_DEFAULT_HOLD_S = 30.0
_ACK_DEADLINE_S = 20.0
_ACK_POLL_S = 0.25


def _require_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise SystemExit(f"team_worker_fixture: missing required env {name}")
    return value


def _send_ack(*, leader_root: Path, worker_id: str) -> int:
    """Call ``omg team api send-message`` with body ACK (CLI path)."""
    payload = {
        "from_worker": worker_id,
        "to_worker": "leader-fixed",
        "body": "ACK",
    }
    env = os.environ.copy()
    env[EXPERIMENTAL_ENV] = "1"
    env["PYTHONPATH"] = str(_REPO_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    # Strip accidental non-team spawn markers so API gate stays open.
    for key in ("OMG_PROCESS_FANOUT_WORKER", "OMG_SPAWNED_WORKER"):
        env.pop(key, None)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omg_cli.main",
            "team",
            "api",
            "send-message",
            "--input",
            json.dumps(payload, ensure_ascii=False),
        ],
        cwd=str(leader_root),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout or "")
        sys.stderr.write(proc.stderr or "")
    return int(proc.returncode)


def main() -> int:
    # Team plane default-on; kill switch or legacy EXPERIMENTAL=0 disables.
    disable = (os.environ.get("OMG_DISABLE_TMUX_TEAM") or "").strip().lower()
    exp = (os.environ.get(EXPERIMENTAL_ENV) or "").strip().lower()
    if disable in ("1", "true", "yes", "on") or exp in ("0", "false", "no", "off"):
        print(
            "team_worker_fixture: team plane disabled "
            f"(OMG_DISABLE_TMUX_TEAM or {EXPERIMENTAL_ENV}=0)",
            file=sys.stderr,
        )
        return 2

    worker_id = _require_env("OMG_TEAM_WORKER_ID")
    _require_env("OMG_TEAM_RUN_ID")
    _require_env("OMG_TEAM_ID")
    leader_root = Path(_require_env("OMG_TEAM_LEADER_ROOT")).resolve()
    if not leader_root.is_dir():
        print(
            f"team_worker_fixture: leader root missing: {leader_root}",
            file=sys.stderr,
        )
        return 2

    # Control plane (team.json) is written after panes spawn — retry ACK.
    deadline = time.monotonic() + _ACK_DEADLINE_S
    last_code = 1
    while time.monotonic() < deadline:
        last_code = _send_ack(leader_root=leader_root, worker_id=worker_id)
        if last_code == 0:
            break
        time.sleep(_ACK_POLL_S)
    if last_code != 0:
        print(
            f"team_worker_fixture: ACK failed after retries (exit {last_code})",
            file=sys.stderr,
        )
        return last_code or 1

    hold_s = float(os.environ.get("OMG_TEAM_FIXTURE_HOLD_S") or _DEFAULT_HOLD_S)
    if hold_s > 0:
        time.sleep(hold_s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
