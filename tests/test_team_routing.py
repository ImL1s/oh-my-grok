"""Hermetic tests for D3 team routing floors + resolved-once snapshot.

No live tmux / no process exec of external CLIs.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omg_cli.team.providers import build_executor_argv
from omg_cli.team.roles import UnknownRoleError, role_posture
from omg_cli.team.routing import (
    DEFAULT_PROVIDER,
    STRUCTURED_VERDICT_PROVIDERS,
    ResolvedRouting,
    RoutingError,
    resolve_routing,
)

# All executor providers treated as present (no PATH dependency).
_ALL = frozenset({"grok", "codex", "agy", "cursor", "gemini"})


def test_structured_verdict_excludes_cursor() -> None:
    assert "cursor" not in STRUCTURED_VERDICT_PROVIDERS
    assert STRUCTURED_VERDICT_PROVIDERS == frozenset(
        {"grok", "codex", "claude", "gemini"}
    )


# ---------------------------------------------------------------------------
# FLOOR 1 — cursor (and non-structured) on reviewer/verifier → RoutingError
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role",
    ["code-reviewer", "critic", "security-reviewer", "verifier"],
)
def test_floor1_cursor_on_reviewer_verifier_raises(role: str) -> None:
    with pytest.raises(RoutingError, match="FLOOR 1|structured-verdict|cursor"):
        resolve_routing(
            {role: {"provider": "cursor"}},
            available_providers=_ALL,
            check_binary=True,
        )


def test_floor1_agy_on_reviewer_raises() -> None:
    with pytest.raises(RoutingError, match="FLOOR 1|structured-verdict"):
        resolve_routing(
            {"code-reviewer": {"provider": "agy"}},
            available_providers=_ALL,
        )


def test_floor1_codex_on_reviewer_ok() -> None:
    snap = resolve_routing(
        {"code-reviewer": {"provider": "codex"}},
        available_providers=_ALL,
    )
    route = snap.for_role("code-reviewer")
    assert route.provider == "codex"
    assert route.posture == "read-only"
    assert route.needs_pty is False


# ---------------------------------------------------------------------------
# FLOOR 2 — UnknownRoleError propagates (never swallowed)
# ---------------------------------------------------------------------------


def test_floor2_unknown_role_propagates_not_swallowed() -> None:
    with pytest.raises(UnknownRoleError) as ei:
        resolve_routing(
            {"not-a-real-role": {"provider": "grok"}},
            available_providers=_ALL,
        )
    assert ei.value.role == "not-a-real-role" or "not-a-real-role" in str(ei.value)
    # Must be UnknownRoleError, not RoutingError wrapping it away
    assert type(ei.value) is UnknownRoleError


def test_floor2_unknown_role_in_roles_needed() -> None:
    with pytest.raises(UnknownRoleError):
        resolve_routing(
            {},
            roles_needed=["totally-unknown-zzz"],
            available_providers=_ALL,
        )


def test_floor2_no_try_except_swallow_in_resolve() -> None:
    """Adversarial: ensure UnknownRoleError is not converted to allow."""
    try:
        resolve_routing(
            {"ghost-role": {"provider": "codex"}},
            available_providers=_ALL,
        )
    except UnknownRoleError:
        return
    except Exception as exc:  # pragma: no cover
        pytest.fail(f"expected UnknownRoleError, got {type(exc).__name__}: {exc}")
    pytest.fail("unknown role was silently accepted")


# ---------------------------------------------------------------------------
# FLOOR 3 / posture — derived from role; reviewer never write
# ---------------------------------------------------------------------------


def test_floor3_reviewer_codex_read_only_argv() -> None:
    snap = resolve_routing(
        {"code-reviewer": {"provider": "codex"}},
        available_providers=_ALL,
    )
    route = snap.for_role("code-reviewer")
    assert route.posture == "read-only"
    assert role_posture("code-reviewer") == "read-only"
    inv = build_executor_argv(
        route.provider,
        route.role,
        prompt_file=Path("/tmp/p.md"),
        cwd=Path("/tmp/wt"),
        model=route.model,
    )
    assert inv.posture == "read-only"
    assert "-s" in inv.argv
    assert inv.argv[inv.argv.index("-s") + 1] == "read-only"
    assert "workspace-write" not in inv.argv


def test_floor3_executor_codex_read_write_argv() -> None:
    snap = resolve_routing(
        {"executor": {"provider": "codex"}},
        available_providers=_ALL,
    )
    route = snap.for_role("executor")
    assert route.posture == "read-write"
    inv = build_executor_argv(
        route.provider,
        route.role,
        prompt_file=Path("/tmp/p.md"),
        cwd=Path("/tmp/wt"),
    )
    assert inv.argv[inv.argv.index("-s") + 1] == "workspace-write"
    assert "read-only" not in inv.argv  # bare posture token not as sandbox value


def test_floor3_config_cannot_override_posture() -> None:
    """Even if config smuggles a posture field, role_posture wins."""
    snap = resolve_routing(
        {
            "code-reviewer": {
                "provider": "codex",
                "posture": "read-write",  # ignored / not a routing field
            }
        },
        available_providers=_ALL,
    )
    assert snap.for_role("code-reviewer").posture == "read-only"


# ---------------------------------------------------------------------------
# Loud fallback
# ---------------------------------------------------------------------------


def test_loud_fallback_records_warning_and_uses_grok() -> None:
    warnings: list[str] = []

    def capture(msg: str) -> None:
        warnings.append(msg)

    snap = resolve_routing(
        {"executor": {"provider": "codex"}},
        available_providers=frozenset({"grok"}),  # codex absent
        warn=capture,
    )
    route = snap.for_role("executor")
    assert route.provider == DEFAULT_PROVIDER == "grok"
    assert route.fallback_from == "codex"
    assert route.warning
    assert "codex" in route.warning
    assert warnings, "warning must be emitted (not silent)"
    assert any("codex" in w for w in warnings)
    assert snap.warnings
    assert route.posture == "read-write"  # same posture after fallback


def test_no_silent_fallback_when_provider_present() -> None:
    snap = resolve_routing(
        {"executor": {"provider": "codex"}},
        available_providers=_ALL,
    )
    route = snap.for_role("executor")
    assert route.provider == "codex"
    assert route.fallback_from is None
    assert route.warning is None
    assert snap.warnings == ()


# ---------------------------------------------------------------------------
# Resolved-once immutability + serialization
# ---------------------------------------------------------------------------


def test_resolved_snapshot_immutable_and_serializable() -> None:
    snap = resolve_routing(
        {
            "executor": {"provider": "agy", "model": "m1"},
            "code-reviewer": {"provider": "gemini"},
        },
        roles_needed=["executor", "code-reviewer", "verifier"],
        available_providers=_ALL,
    )
    assert isinstance(snap, ResolvedRouting)
    assert snap.by_role["executor"].needs_pty is True
    assert snap.by_role["executor"].model == "m1"
    assert snap.by_role["verifier"].provider == "grok"  # default
    d = snap.to_dict()
    # Round-trip shape for team.json
    assert d["default_provider"] == "grok"
    assert set(d["by_role"]) == {"code-reviewer", "executor", "verifier"}
    blob = json.dumps(d, sort_keys=True)
    again = json.loads(blob)
    assert again["by_role"]["executor"]["provider"] == "agy"
    assert again["by_role"]["executor"]["needs_pty"] is True


def test_default_provider_for_needed_roles() -> None:
    snap = resolve_routing(
        None,
        roles_needed=["executor"],
        available_providers=_ALL,
    )
    assert snap.for_role("executor").provider == "grok"
