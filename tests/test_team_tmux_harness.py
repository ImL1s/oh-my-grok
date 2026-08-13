"""Hermetic proof that the real-tmux fixture adapter forwards authority.

Live CI (gh runs 31614886395 / 31616070102) failed because
``install_fixture_provider`` patched ``build_fixture_pane_command`` with a
``descriptor_path``-only wrapper. Production ``start_team`` now passes
leader_root / run_id / team_id / worker_id / owner_token / publish_authority
(after 2c2283d). This file does not require tmux.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path
from typing import Any

import pytest

from omg_cli.team import plane as plane_mod
from tests.support.team_tmux_harness import install_fixture_provider


def test_fixture_adapter_keyword_params_cover_production(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Adapter must accept every production keyword — extras must TypeError."""
    prod_params = inspect.signature(plane_mod.build_fixture_pane_command).parameters
    script = tmp_path / "ready.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    install_fixture_provider(monkeypatch, script)
    adapter_params = inspect.signature(plane_mod.build_fixture_pane_command).parameters
    missing = set(prod_params) - set(adapter_params)
    assert not missing, f"fixture adapter dropped production kwargs: {sorted(missing)}"
    assert adapter_params.get("kwargs") is None
    assert not any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in adapter_params.values()
    )


def test_fixture_adapter_forwards_supervisor_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    def _materialize(**kwargs: Any) -> str:
        captured.update(kwargs)
        return "supervisor-cmd"

    monkeypatch.setattr(plane_mod, "materialize_supervisor_pane_command", _materialize)
    script = tmp_path / "ready.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    install_fixture_provider(monkeypatch, script, provider="fixture", needs_pty=True)

    desc = tmp_path / "w1.provider.json"
    root = tmp_path / "leader"
    root.mkdir()
    out = plane_mod.build_fixture_pane_command(
        descriptor_path=desc,
        leader_root=root,
        run_id="run-1",
        team_id="team",
        worker_id="w1",
        owner_token="tok",
        authority_generation=2,
        authority_attempt=3,
        publish_authority=True,
    )
    assert out == "supervisor-cmd"
    assert captured["descriptor_path"] == desc
    assert captured["leader_root"] == root
    assert captured["run_id"] == "run-1"
    assert captured["team_id"] == "team"
    assert captured["worker_id"] == "w1"
    assert captured["owner_token"] == "tok"
    assert captured["authority_generation"] == 2
    assert captured["authority_attempt"] == 3
    assert captured["publish_authority"] is True
    assert captured["provider"] == "fixture"
    assert captured["needs_pty"] is True
    assert captured["prompt_delivery"] == "prompt-file"
    assert captured["argv"] == [sys.executable, str(script)]


def test_fixture_adapter_rejects_unknown_kwargs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "ready.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    install_fixture_provider(monkeypatch, script)
    adapter = plane_mod.build_fixture_pane_command
    with pytest.raises(TypeError, match="unexpected keyword"):
        adapter(
            descriptor_path=tmp_path / "w1.provider.json",
            leader_root=tmp_path,
            swallowed="must-not-be-accepted",  # type: ignore[call-arg]
        )
