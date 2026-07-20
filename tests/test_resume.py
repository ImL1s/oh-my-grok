"""omg resume + RESUME.md routing."""
from __future__ import annotations

import json

from omg_cli.resume import (
    build_resume_pack,
    clear_resume_md,
    recommend_commands,
    resume_md_path,
    route_resume,
    write_resume_md,
)
from omg_cli.state import create_run, write_status


def test_resume_pack_no_active(tmp_path):
    pack = build_resume_pack(tmp_path)
    assert pack["ok"] is False
    assert pack["reason"] == "no_active_run"


def test_resume_routes_pipeline_and_writes_md(tmp_path):
    run = create_run(tmp_path, mode="pipeline", goal="ship resume")
    rid = run["run_id"]
    write_status(tmp_path, rid, "running", extra={"stage": "implement"})
    code, pack = route_resume(tmp_path, run_id=rid)
    assert code == 0
    assert pack["resumable"] is True
    assert pack["mode"] == "pipeline"
    cmds = pack["commands"]
    assert any(f"omg pipeline --resume {rid}" in c for c in cmds)
    path = resume_md_path(tmp_path)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert rid in text
    assert "pipeline --resume" in text
    assert clear_resume_md(tmp_path) is True
    assert not path.is_file()


def test_resume_terminal_not_resumable(tmp_path):
    run = create_run(tmp_path, mode="ralph", goal="done goal")
    rid = run["run_id"]
    st = write_status(tmp_path, rid, "completed")
    pack = build_resume_pack(tmp_path, rid)
    assert st.get("status") == "completed"
    assert pack.get("terminal") is True
    assert pack.get("resumable") is False


def test_recommend_ralph_includes_session(tmp_path):
    status = {
        "run_id": "r1",
        "mode": "ralph",
        "status": "running",
        "grok_session_id": "11111111-1111-1111-1111-111111111111",
    }
    cmds = recommend_commands(status)
    assert any("omg ralph --resume r1" in c for c in cmds)
    assert any("grok --resume" in c for c in cmds)


def test_cli_resume_json(tmp_path, monkeypatch):
    from omg_cli.main import main

    monkeypatch.chdir(tmp_path)
    create_run(tmp_path, mode="ulw", goal="fanout")
    rc = main(["resume", "--json", "--no-write"])
    assert rc == 0
