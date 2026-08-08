"""Reusable fake-agy installer for hermetic Antigravity tests (#67/#68)."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "antigravity"
FAKE_AGY = FIXTURES / "fake_agy.py"


def install_fake_agy(
    bin_dir: Path,
    *,
    version: str | None = None,
    name: str = "agy",
) -> Path:
    """Install a PATH-visible ``agy`` shim that runs the hermetic fixture."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / name
    py = sys.executable
    script = (
        f"#!{py}\n"
        "import runpy, sys\n"
        f"sys.argv[0] = {str(target)!r}\n"
        f"raise SystemExit(runpy.run_path({str(FAKE_AGY)!r}, run_name='__main__'))\n"
    )
    target.write_text(script, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if version is not None:
        # Caller sets FAKE_AGY_VERSION in env; this helper just documents it.
        pass
    return target


def clear_fake_agy_run_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("FAKE_AGY_RUN_") or key.startswith("FAKE_AGY_ECHO_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def fake_agy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    path = install_fake_agy(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)
    clear_fake_agy_run_env(monkeypatch)
    return path


__all__ = [
    "FAKE_AGY",
    "FIXTURES",
    "clear_fake_agy_run_env",
    "fake_agy_path",
    "install_fake_agy",
]
