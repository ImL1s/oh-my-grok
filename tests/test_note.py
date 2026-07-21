"""Tests for omg_cli.note — compaction-resistant project notepad."""
from __future__ import annotations

from pathlib import Path

import pytest

from omg_cli.note import add_note, notepad_path, read_notes, run_note


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
