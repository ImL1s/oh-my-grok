"""wiki / hud / lsp surfaces."""
from __future__ import annotations

from omg_cli.hud import hud_line, hud_pack
from omg_cli.lsp_tools import probe_tools
from omg_cli.state import create_run
from omg_cli.wiki import ingest, list_pages, query


def test_wiki_ingest_list_query(tmp_path):
    r = ingest(
        tmp_path,
        title="Auth Notes",
        body="Use OAuth PKCE for mobile.",
        tags=["auth", "mobile"],
    )
    assert "auth-notes" in r["slug"]
    pages = list_pages(tmp_path)
    assert any(p["slug"] == "auth-notes" for p in pages)
    hits = query(tmp_path, "PKCE")
    assert hits and "PKCE" in hits[0]["snippet"]


def test_hud_no_run(tmp_path):
    assert "no-active-run" in hud_line(tmp_path)


def test_hud_with_run(tmp_path):
    run = create_run(tmp_path, mode="autopilot", goal="x")
    line = hud_line(tmp_path, run["run_id"])
    assert "autopilot" in line
    assert "omg-hud:" in line
    pack = hud_pack(tmp_path, run["run_id"])
    assert pack["line"].startswith("omg-hud:")


def test_lsp_probe_structure():
    data = probe_tools()
    assert "available" in data
    assert "honesty" in data
    assert isinstance(data["available"], list)
