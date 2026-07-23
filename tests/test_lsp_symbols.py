"""Registration/status-only tests for Grok-owned LSP support."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import omg_cli.lsp_tools as lsp
from omg_cli.main import main


VALID = {
    "python": {
        "command": "pyright-langserver",
        "args": ["--stdio"],
        "extensionToLanguage": {".py": "python"},
        "startupTimeout": 30000,
    }
}


def _write(root: Path, value=VALID) -> Path:
    path = root / ".lsp.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_valid_registration_is_configured_unobserved(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path)
    monkeypatch.setattr(lsp.shutil, "which", lambda _name: "/usr/bin/fake")
    status = lsp.registration_status(tmp_path)
    assert status["ok"] is True
    assert status["registered"] is True
    assert status["status"] == "configured_unobserved"
    assert status["healthy"] is False
    assert status["servers"][0]["command_available"] is True


def test_missing_registration_is_explicit(tmp_path: Path) -> None:
    status = lsp.registration_status(tmp_path)
    assert status["status"] == "missing_registration"
    assert status["registered"] is False
    assert status["configuration_valid"] is False


def test_invalid_registration_is_not_healthy(tmp_path: Path) -> None:
    _write(tmp_path, {"python": {"command": "pyright-langserver"}})
    status = lsp.registration_status(tmp_path)
    assert status["ok"] is False
    assert status["status"] == "invalid_registration"
    assert status["healthy"] is False
    assert "extensionToLanguage" in status["error"]


def test_host_observation_is_required_for_healthy(tmp_path: Path) -> None:
    _write(tmp_path)
    asserted = lsp.registration_status(
        tmp_path, host_observation={"observed": True, "healthy": True}
    )
    assert asserted["status"] == "host_observed_healthy"
    assert asserted["healthy"] is True
    unhealthy = lsp.registration_status(
        tmp_path, host_observation={"observed": True, "healthy": False}
    )
    assert unhealthy["status"] == "host_observed_unhealthy"


def test_zero_semantic_proxy_operations(tmp_path: Path) -> None:
    _write(tmp_path)
    status = lsp.registration_status(tmp_path)
    assert lsp.SEMANTIC_PROXY_OPERATIONS == ()
    assert status["semantic_proxy_operations"] == []
    assert status["semantic_proxy_count"] == 0
    for forbidden in (
        "symbols_ast",
        "diagnostics_ast",
        "symbols_pyright",
        "hover",
        "definition",
        "references",
        "rename",
    ):
        assert not hasattr(lsp, forbidden)


def test_registration_path_cannot_escape_root(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-lsp.json"
    outside.write_text(json.dumps(VALID), encoding="utf-8")
    with pytest.raises(lsp.LSPRegistrationError, match="escapes"):
        lsp.load_registration(tmp_path, config_path=outside)


def test_probe_tools_is_status_alias_without_execution(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path)
    monkeypatch.setattr(lsp.shutil, "which", lambda _name: None)
    status = lsp.probe_tools(tmp_path)
    assert status["available"] == []
    assert status["missing"] == ["python"]
    assert "configured but unobserved is not healthy" in status["honesty"]


@pytest.mark.parametrize(
    "argv",
    [
        ["lsp", "status"],
        ["lsp", "check", "sample.py"],
        ["lsp", "symbols", "sample.py"],
    ],
)
def test_lsp_cli_never_imports_removed_semantic_proxies(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(argv)
    output = json.loads(capsys.readouterr().out)
    if argv[-1] == "status":
        assert code == 0
        assert output["ownership"] == "host_owned"
    else:
        assert code == 1
        assert output["status"] == "semantic_proxy_unsupported"
        assert output["semantic_proxy_operations"] == []
