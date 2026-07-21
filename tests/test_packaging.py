"""Hermetic packaging contract for editable pipx / pip install -e .

stdlib tomllib only — no build tooling required at test time.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import omg_cli.ask  # noqa: F401 — packages.find must bundle omg_cli.ask
import omg_cli.main

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_scripts_and_dynamic_version() -> None:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["omg"] == "omg_cli.main:main"
    assert (
        data["tool"]["setuptools"]["dynamic"]["version"]["attr"]
        == "omg_cli.__version__"
    )


def test_main_entry_callable() -> None:
    assert callable(omg_cli.main.main)


def test_ask_subpackage_importable() -> None:
    assert omg_cli.ask is not None
