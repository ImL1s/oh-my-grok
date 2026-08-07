"""#67-D: Antigravity Team launch envelope (adapter-owned; supervisor still spawns).

Hermetic only — no live agy, no tmux. Golden argv/env/needs_pty/prompt_delivery
plus Team cutover that consumes the adapter envelope (never Adapter.run).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omg_cli.providers.antigravity import (
    AntigravityProvider,
    build_launch_envelope,
    build_team_argv,
    get_adapter,
)
from omg_cli.providers.base import ProviderAdapter
from omg_cli.providers.errors import ProviderRunError
from omg_cli.providers.models import (
    LAUNCH_ENVELOPE_SCHEMA,
    ProviderLaunchEnvelope,
    ProviderLaunchRequest,
)
from omg_cli.team.providers import (
    PROMPT_DELIVERY_POSITIONAL_TEXT,
    build_executor_argv,
)
from omg_cli.team.supervisor import (
    DESCRIPTOR_SCHEMA_VERSION,
    load_provider_descriptor,
    write_provider_descriptor,
)

_PROMPT = Path("/tmp/omg-team-prompt.txt")
_CWD = Path("/tmp/omg-team-cwd")
_MODEL = "test-model-xyz"
_REVIEWER_ROLE = "code-reviewer"  # → read-only
_EXECUTOR_ROLE = "executor"  # → read-write


def _team_request(**kwargs: Any) -> ProviderLaunchRequest:
    base: dict[str, Any] = {
        "provider": "antigravity",
        "launch_kind": "team",
        "prompt_file": str(_PROMPT),
        "cwd": str(_CWD),
        "model": _MODEL,
        "posture": "read-write",
        "needs_pty": True,
    }
    base.update(kwargs)
    return ProviderLaunchRequest(**base)


# ---------------------------------------------------------------------------
# Models / protocol
# ---------------------------------------------------------------------------


def test_launch_models_json_serializable() -> None:
    req = _team_request()
    assert "prompt_file" in req.to_dict()
    env = build_launch_envelope(req)
    payload = env.to_dict()
    assert payload["schema"] == LAUNCH_ENVELOPE_SCHEMA
    assert isinstance(payload["argv"], list)
    assert all(isinstance(x, str) for x in payload["argv"])
    # Never a shell string field.
    assert "command" not in payload
    assert "shell" not in payload
    json.dumps(payload)


def test_adapter_protocol_requires_build_launch_envelope() -> None:
    adapter = AntigravityProvider()
    assert isinstance(adapter, ProviderAdapter)
    env = adapter.build_launch_envelope(_team_request())
    assert isinstance(env, ProviderLaunchEnvelope)
    assert env.needs_pty is True


# ---------------------------------------------------------------------------
# Golden envelope
# ---------------------------------------------------------------------------


def test_envelope_golden_read_write() -> None:
    env = build_launch_envelope(_team_request(posture="read-write"))
    assert env.argv == (
        "agy",
        "-p",
        str(_PROMPT),
        "--model",
        _MODEL,
        "--dangerously-skip-permissions",
    )
    assert "--sandbox" not in env.argv
    assert env.needs_pty is True
    assert env.prompt_delivery == "positional-text"
    assert env.cwd == str(_CWD)
    assert env.posture == "read-write"
    assert "agy" in env.identity_basenames
    assert env.startup_strategy == "supervisor"
    assert env.provider_strategy == "antigravity-team-interactive"
    # Env is allowlisted (may be empty in hermetic hosts without PATH extras).
    assert isinstance(dict(env.env), dict)
    assert "SHELL" not in env.env  # fail-closed allowlist


def test_envelope_golden_read_only_sandbox() -> None:
    env = build_launch_envelope(_team_request(posture="read-only"))
    assert "--sandbox" in env.argv
    assert "--dangerously-skip-permissions" in env.argv
    assert env.argv[0:3] == ("agy", "-p", str(_PROMPT))
    assert env.needs_pty is True
    assert env.prompt_delivery == "positional-text"


def test_envelope_preserves_cwd_and_env_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/fake/bin")
    monkeypatch.setenv("HOME", "/fake/home")
    monkeypatch.setenv("SECRET_TOKEN", "should-not-leak")
    env = build_launch_envelope(
        _team_request(env={"PATH": "/override/bin", "SECRET_TOKEN": "nope"})
    )
    assert env.cwd == str(_CWD)
    assert env.env.get("PATH") == "/override/bin"
    assert "SECRET_TOKEN" not in env.env
    assert "HOME" in env.env


def test_team_argv_matches_build_team_argv() -> None:
    for posture in ("read-only", "read-write"):
        req = _team_request(posture=posture)
        env = build_launch_envelope(req)
        direct = build_team_argv(
            "agy", str(_PROMPT), posture=posture, model=_MODEL
        )
        assert list(env.argv) == direct


def test_envelope_rejects_needs_pty_false() -> None:
    with pytest.raises(ProviderRunError, match="needs_pty"):
        build_launch_envelope(_team_request(needs_pty=False))


def test_envelope_rejects_leading_dash_prompt_file() -> None:
    with pytest.raises(ProviderRunError, match="leading"):
        build_launch_envelope(_team_request(prompt_file="-evil"))


def test_envelope_does_not_call_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Team path must never invoke Adapter.run / run_provider_process."""
    import omg_cli.providers.antigravity as ag

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("run must not be called for Team envelope")

    monkeypatch.setattr(ag, "run", _boom)
    monkeypatch.setattr(ag, "run_provider_process", _boom)
    env = get_adapter().build_launch_envelope(_team_request())
    assert env.needs_pty is True
    assert env.argv[0] == "agy"


# ---------------------------------------------------------------------------
# Team uses adapter envelope
# ---------------------------------------------------------------------------


def test_team_agy_uses_adapter_envelope_argv() -> None:
    inv = build_executor_argv(
        "agy", _EXECUTOR_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    env = build_launch_envelope(
        _team_request(posture="read-write", model=_MODEL)
    )
    assert inv.argv == list(env.argv)
    assert inv.needs_pty is env.needs_pty is True
    assert inv.prompt_delivery == env.prompt_delivery == PROMPT_DELIVERY_POSITIONAL_TEXT
    assert inv.identity_basenames == env.identity_basenames
    assert inv.provider_strategy == env.provider_strategy
    assert inv.startup_strategy == env.startup_strategy
    assert inv.provider == "agy"  # Team canonical name


def test_team_agy_read_only_matches_envelope() -> None:
    inv = build_executor_argv(
        "agy", _REVIEWER_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    env = build_launch_envelope(_team_request(posture="read-only"))
    assert inv.argv == list(env.argv)
    assert "--sandbox" in inv.argv
    assert inv.needs_pty is True


def test_descriptor_propagates_envelope_identity(tmp_path: Path) -> None:
    inv = build_executor_argv(
        "agy", _EXECUTOR_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    path = tmp_path / "agy.provider.json"
    write_provider_descriptor(
        path,
        provider=inv.provider,
        argv=inv.argv,
        prompt_delivery=inv.prompt_delivery,
        prompt_file=_PROMPT,
        needs_pty=inv.needs_pty,
        cwd=_CWD,
        identity_basenames=inv.identity_basenames,
        provider_strategy=inv.provider_strategy,
        startup_strategy=inv.startup_strategy,
    )
    desc = load_provider_descriptor(path)
    assert desc["schema_version"] == DESCRIPTOR_SCHEMA_VERSION
    assert desc["argv"] == inv.argv
    assert desc["needs_pty"] is True
    assert desc["prompt_delivery"] == PROMPT_DELIVERY_POSITIONAL_TEXT
    assert desc["identity_basenames"] == list(inv.identity_basenames)
    assert desc["provider_strategy"] == "antigravity-team-interactive"
    assert desc["startup_strategy"] == "supervisor"
    assert desc["cwd"] == str(_CWD)


def test_descriptor_schema_stays_v1_additive(tmp_path: Path) -> None:
    """#67-D adds optional fields without bumping descriptor schema (resume-safe)."""
    path = tmp_path / "legacy.provider.json"
    write_provider_descriptor(
        path,
        provider="agy",
        argv=["agy", "-p", "x"],
        needs_pty=True,
    )
    desc = load_provider_descriptor(path)
    assert desc["schema_version"] == 1
    assert "identity_basenames" not in desc
    assert "provider_strategy" not in desc
