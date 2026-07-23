"""Hermetic packaging contract for editable pipx / pip install -e .

stdlib tomllib only — no build tooling required at test time.
"""
from __future__ import annotations

import json
import tomllib
from pathlib import Path

import omg_cli
import omg_cli.ask  # noqa: F401 — packages.find must bundle omg_cli.ask
import omg_cli.contracts
import omg_cli.main
import omg_cli.notify
import omg_cli.workflows

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


def test_product_subpackages_importable() -> None:
    assert omg_cli.contracts is not None
    assert omg_cli.notify is not None
    assert omg_cli.workflows is not None


def test_import_safe_version_matches_plugin_manifest() -> None:
    plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    assert omg_cli.__version__ == plugin["version"] == "0.6.0"


def test_grok_plugin_mcp_and_lsp_manifests() -> None:
    mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
    server = mcp["mcpServers"]["omg"]
    assert server["command"] == "python3"
    assert server["args"] == ["${GROK_PLUGIN_ROOT}/bin/omg", "mcp-server"]

    lsp = json.loads((ROOT / ".lsp.json").read_text(encoding="utf-8"))
    assert lsp["pyright"]["command"] == "pyright-langserver"
    assert lsp["pyright"]["args"] == ["--stdio"]
    assert lsp["pyright"]["extensionToLanguage"] == {".py": "python"}
