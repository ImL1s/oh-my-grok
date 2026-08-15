"""Reusable fake-grok installer for hermetic job provider tests (#69)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jobs"
FAKE_GROK = FIXTURES / "fake_grok.py"


def install_fake_grok(
    bin_dir: Path,
    *,
    name: str = "grok",
) -> Path:
    """Install a PATH-visible ``grok`` shim that runs the hermetic fixture."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / name
    py = sys.executable
    script = (
        f"#!{py}\n"
        "import runpy, sys\n"
        f"sys.argv[0] = {str(target)!r}\n"
        f"raise SystemExit(runpy.run_path({str(FAKE_GROK)!r}, run_name='__main__'))\n"
    )
    target.write_text(script, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


@pytest.fixture
def fake_grok_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    path = install_fake_grok(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("OMG_GROK_BIN", str(path))
    for key in (
        "FAKE_GROK_VERSION",
        "FAKE_GROK_VERSION_RC",
        "FAKE_GROK_RUN_RC",
        "FAKE_GROK_RUN_STDOUT",
        "FAKE_GROK_RUN_STDERR",
        "FAKE_GROK_ECHO_CWD",
        "FAKE_GROK_ECHO_PROMPT",
    ):
        monkeypatch.delenv(key, raising=False)
    return path


__all__ = ["FAKE_GROK", "FIXTURES", "fake_grok_path", "install_fake_grok"]
