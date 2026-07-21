"""Tests for omg_cli.note — compaction-resistant project notepad."""
from __future__ import annotations

from pathlib import Path

import pytest

from omg_cli.note import add_note, notepad_path, prune_notes, read_notes, run_note


def test_add_note_creates_header_and_7d_line(tmp_path: Path) -> None:
    path = add_note(tmp_path, "remember the lease fence")
    assert path == notepad_path(tmp_path)
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# OMG notepad\n\n")
    assert "- [7d] " in text
    assert "remember the lease fence" in text


def test_priority_yields_permanent_ttl(tmp_path: Path) -> None:
    path = add_note(tmp_path, "always keep this", priority=True)
    text = path.read_text(encoding="utf-8")
    assert "- [permanent] " in text
    assert "always keep this" in text
    assert "- [7d] " not in text


def test_read_notes_returns_content_or_empty(tmp_path: Path) -> None:
    assert read_notes(tmp_path) == ""
    add_note(tmp_path, "hello notepad")
    content = read_notes(tmp_path)
    assert "# OMG notepad" in content
    assert "hello notepad" in content


def test_run_note_show_prints(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    add_note(tmp_path, "visible note")
    code = run_note("", root=tmp_path, show=True)
    assert code == 0
    out = capsys.readouterr().out
    assert "visible note" in out
    assert "# OMG notepad" in out


def test_run_note_appends_and_prints_ack(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run_note("new durable fact", root=tmp_path, priority=False, show=False)
    assert code == 0
    out = capsys.readouterr().out
    assert "noted" in out.lower()
    assert "new durable fact" in read_notes(tmp_path)


def test_run_note_empty_text_shows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    add_note(tmp_path, "only show path")
    code = run_note("", root=tmp_path)
    assert code == 0
    assert "only show path" in capsys.readouterr().out


def _write_notepad(root: Path, body: str) -> Path:
    path = notepad_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_prune_notes_removes_old_7d_keeps_fresh_and_permanent(tmp_path: Path) -> None:
    old_ts = "2026-07-01T12:00:00+00:00"
    fresh_ts = "2026-07-08T12:00:00+00:00"
    perm_ts = "2026-06-01T00:00:00+00:00"
    now_iso = "2026-07-09T12:00:00+00:00"  # 8 days after old_ts

    body = (
        "# OMG notepad\n\n"
        f"- [7d] {old_ts} old seven-day note\n"
        f"- [7d] {fresh_ts} fresh seven-day note\n"
        f"- [permanent] {perm_ts} permanent forever\n"
    )
    path = _write_notepad(tmp_path, body)

    kept, removed = prune_notes(tmp_path, now_iso=now_iso)
    assert removed == 1
    assert kept >= 2  # at least fresh + permanent (header lines also kept)

    text = path.read_text(encoding="utf-8")
    assert "old seven-day note" not in text
    assert "fresh seven-day note" in text
    assert "permanent forever" in text
    assert text.startswith("# OMG notepad")
    assert f"- [7d] {fresh_ts} fresh seven-day note" in text
    assert f"- [permanent] {perm_ts} permanent forever" in text


def test_prune_notes_keeps_unparseable_7d_timestamp(tmp_path: Path) -> None:
    body = (
        "# OMG notepad\n\n"
        "- [7d] not-a-timestamp keep me anyway\n"
        "- [7d] 2020-01-01T00:00:00+00:00 definitely expired\n"
    )
    path = _write_notepad(tmp_path, body)

    kept, removed = prune_notes(tmp_path, now_iso="2026-07-21T00:00:00+00:00")
    assert removed == 1
    text = path.read_text(encoding="utf-8")
    assert "keep me anyway" in text
    assert "definitely expired" not in text
    assert kept >= 1


def test_run_note_prune_prints_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=8)).isoformat()
    fresh = (now - timedelta(days=1)).isoformat()
    _write_notepad(
        tmp_path,
        (
            "# OMG notepad\n\n"
            f"- [7d] {old} drop me\n"
            f"- [7d] {fresh} keep me\n"
            f"- [permanent] {now.isoformat()} stay\n"
        ),
    )

    code = run_note("", root=tmp_path, prune=True)
    assert code == 0
    out = capsys.readouterr().out
    assert "pruned: removed 1, kept" in out
    text = read_notes(tmp_path)
    assert "drop me" not in text
    assert "keep me" in text
    assert "stay" in text


def test_omg_note_prune_cli_exits_zero(tmp_path: Path) -> None:
    """`omg note --prune` via CLI returns 0."""
    import os
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    bin_omg = repo / "bin" / "omg"
    (tmp_path / ".omg").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".omg" / "notepad.md").write_text(
        "# OMG notepad\n\n- [permanent] 2026-01-01T00:00:00+00:00 x\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(repo) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    r = subprocess.run(
        [sys.executable, str(bin_omg), "note", "--prune"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "pruned:" in r.stdout
