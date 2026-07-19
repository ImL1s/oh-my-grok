"""Tests for process fanout (no tmux) multi-PID skeleton."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from omg_cli.fanout import (
    DEFAULT_WORKERS,
    FANOUT_PROCESS,
    build_worker_prompt,
    fanout_meta_path,
    resolve_worker_count,
    run_process_fanout,
    worker_id_label,
    workers_dir,
)
from omg_cli.state import load_active_run, load_run


def test_resolve_worker_count_defaults_and_cap():
    assert resolve_worker_count(None) == DEFAULT_WORKERS
    assert resolve_worker_count(1) == 1
    assert resolve_worker_count(4) == 4
    with pytest.raises(ValueError):
        resolve_worker_count(0)
    with pytest.raises(ValueError):
        resolve_worker_count(99)


def test_worker_id_label():
    assert worker_id_label(1) == "w01"
    assert worker_id_label(12) == "w12"


def test_build_worker_prompt_mentions_contract():
    text = build_worker_prompt(
        "ship X",
        run_id="r1",
        worker_id="w02",
        worker_index=2,
        workers=3,
    )
    assert "process fanout" in text.lower() or "Process-fanout" in text
    assert "w02" in text
    assert "2/3" in text or "2 of 3" in text
    assert "ship X" in text
    assert "verified" in text.lower()


def test_dry_run_process_fanout_skeleton(monkeypatch, tmp_path):
    """dry_run writes N argv + pid skeletons; no Popen; not verified."""
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no popen in dry_run")),
    )
    rc = run_process_fanout(
        "parallel slices",
        workers=3,
        root=tmp_path,
        dry_run=True,
    )
    assert rc == 0
    active = load_active_run(tmp_path)
    assert active is not None
    assert active["mode"] == "ulw"
    assert active.get("verified") is False
    assert active.get("fanout") == FANOUT_PROCESS or active.get("status") == "completed"
    rid = active["run_id"]
    wdir = workers_dir(tmp_path, rid)
    assert wdir.is_dir()
    for i in (1, 2, 3):
        wid = worker_id_label(i)
        argv_path = wdir / f"{wid}.argv.json"
        assert argv_path.is_file(), wid
        argv = json.loads(argv_path.read_text(encoding="utf-8"))
        assert argv[0] == "grok"
        assert "-p" in argv
        # leaders/workers keep shell unless explicitly disallowed
        assert "--disallowed-tools" not in argv
        pid_path = wdir / f"{wid}.pid.json"
        assert pid_path.is_file()
        meta = json.loads(pid_path.read_text(encoding="utf-8"))
        assert meta.get("dry_run") is True
        assert meta.get("pid") is None  # never invent live pid
    fmeta = json.loads(fanout_meta_path(tmp_path, rid).read_text(encoding="utf-8"))
    assert fmeta["workers"] == 3
    assert fmeta["fanout"] == FANOUT_PROCESS
    assert len(fmeta["records"]) == 3


def test_process_fanout_launches_n_popen(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_popen(argv, **kwargs):
        calls.append(list(argv))
        mock = MagicMock()
        mock.pid = 1000 + len(calls)
        mock.wait.return_value = 0
        return mock

    real = subprocess.Popen

    def selective(argv, *a, **k):
        # Allow real git/ps (base_sha + process_starttime); mock only grok
        if argv and argv[0] in ("git", "ps"):
            return real(argv, *a, **k)
        return fake_popen(argv, **k)

    monkeypatch.setattr(subprocess, "Popen", selective)
    # process_starttime uses subprocess.run → still hits Popen; also stub starttime
    monkeypatch.setattr(
        "omg_cli.state.process_starttime", lambda _pid: "fake-start"
    )

    rc = run_process_fanout(
        "real launch",
        workers=2,
        root=tmp_path,
        dry_run=False,
    )
    assert rc == 0
    grok = [c for c in calls if c and c[0] == "grok"]
    assert len(grok) == 2
    active = load_active_run(tmp_path)
    assert active is not None
    assert active.get("verified") is False
    rid = active["run_id"]
    wdir = workers_dir(tmp_path, rid)
    for wid in ("w01", "w02"):
        meta = json.loads((wdir / f"{wid}.pid.json").read_text(encoding="utf-8"))
        assert isinstance(meta.get("pid"), int)


def test_cli_ulw_fanout_process_requires_env_gate(tmp_path):
    """Without OMG_EXPERIMENTAL_PROCESS_FANOUT=1 → exit 2; no run created."""
    import os
    import sys

    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env.pop("OMG_EXPERIMENTAL_PROCESS_FANOUT", None)
    env["PYTHONPATH"] = str(repo) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    r = subprocess.run(
        [
            sys.executable,
            str(repo / "bin" / "omg"),
            "ulw",
            "cli fanout blocked",
            "--fanout",
            "process",
            "--workers",
            "2",
            "--dry-run",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 2, r.stderr + r.stdout
    assert "OMG_EXPERIMENTAL_PROCESS_FANOUT" in r.stderr
    assert "spawn_subagent" in r.stderr
    runs_root = tmp_path / ".omg" / "state" / "runs"
    assert not runs_root.exists() or not list(runs_root.glob("*/workers/fanout.json"))


def test_cli_ulw_fanout_process_dry_run(tmp_path):
    import os
    import sys

    repo = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["OMG_EXPERIMENTAL_PROCESS_FANOUT"] = "1"
    env["PYTHONPATH"] = str(repo) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    r = subprocess.run(
        [
            sys.executable,
            str(repo / "bin" / "omg"),
            "ulw",
            "cli fanout",
            "--fanout",
            "process",
            "--workers",
            "2",
            "--dry-run",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r.returncode == 0, r.stderr + r.stdout
    runs = list((tmp_path / ".omg" / "state" / "runs").glob("*/workers/fanout.json"))
    assert runs, "fanout.json missing"
    data = json.loads(runs[0].read_text(encoding="utf-8"))
    assert data["workers"] == 2
    assert data["fanout"] == "process"
