"""Fail-closed team role taxonomy (Workstream F / D3 routing floors)."""

from __future__ import annotations

import pytest

from omg_cli.team.roles import (
    CANONICAL_ROLES,
    UnknownRoleError,
    is_reviewer_or_verifier,
    normalize_role,
    role_class,
    role_meta,
    role_posture,
)


# --- expected taxonomy (brief) ------------------------------------------------

_READ_ONLY_REVIEWER = frozenset({"code-reviewer", "critic", "security-reviewer"})
_READ_ONLY_VERIFIER = frozenset({"verifier"})
_READ_ONLY_PLANNER = frozenset({"analyst", "architect", "planner"})
_READ_WRITE_EXECUTOR = frozenset(
    {
        "executor",
        "debugger",
        "designer",
        "writer",
        "test-engineer",
        "qa-tester",
    }
)
_ORCHESTRATOR = frozenset({"orchestrator"})


def test_canonical_roles_cover_existing_plus_five_new() -> None:
    existing = {
        "analyst",
        "architect",
        "code-reviewer",
        "critic",
        "executor",
        "orchestrator",
        "qa-tester",
        "verifier",
    }
    new_five = {
        "debugger",
        "designer",
        "writer",
        "security-reviewer",
        "test-engineer",
    }
    # planner is taxonomy-only (no agent md required) but must be registered
    assert existing | new_five | {"planner"} == CANONICAL_ROLES


@pytest.mark.parametrize("role", sorted(_READ_ONLY_REVIEWER))
def test_read_only_reviewer(role: str) -> None:
    assert role_posture(role) == "read-only"
    assert role_class(role) == "reviewer"
    assert is_reviewer_or_verifier(role) is True


@pytest.mark.parametrize("role", sorted(_READ_ONLY_VERIFIER))
def test_read_only_verifier(role: str) -> None:
    assert role_posture(role) == "read-only"
    assert role_class(role) == "verifier"
    assert is_reviewer_or_verifier(role) is True


@pytest.mark.parametrize("role", sorted(_READ_ONLY_PLANNER))
def test_read_only_planner_class(role: str) -> None:
    assert role_posture(role) == "read-only"
    assert role_class(role) == "planner"
    assert is_reviewer_or_verifier(role) is False


@pytest.mark.parametrize("role", sorted(_READ_WRITE_EXECUTOR))
def test_read_write_executor(role: str) -> None:
    assert role_posture(role) == "read-write"
    assert role_class(role) == "executor"
    assert is_reviewer_or_verifier(role) is False


def test_orchestrator_pinned() -> None:
    assert role_posture("orchestrator") == "read-write"
    assert role_class("orchestrator") == "orchestrator"
    assert is_reviewer_or_verifier("orchestrator") is False


def test_every_canonical_role_has_posture_and_class() -> None:
    for role in sorted(CANONICAL_ROLES):
        meta = role_meta(role)
        assert meta.posture in ("read-only", "read-write")
        assert meta.role_class in (
            "reviewer",
            "verifier",
            "executor",
            "planner",
            "orchestrator",
        )
        assert role_posture(role) == meta.posture
        assert role_class(role) == meta.role_class


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("executor", "executor"),
        ("omg-executor", "executor"),
        ("OMG-Code-Reviewer", "code-reviewer"),
        ("  security_reviewer  ", "security-reviewer"),
        ("omg-test-engineer", "test-engineer"),
    ],
)
def test_normalize_role(raw: str, expected: str) -> None:
    assert normalize_role(raw) == expected
    assert role_posture(raw) == role_posture(expected)


@pytest.mark.parametrize(
    "unknown",
    ["", "not-a-role", "cursor", "general-purpose", "explore", "omg-unknown"],
)
def test_unknown_roles_fail_closed(unknown: str) -> None:
    with pytest.raises(UnknownRoleError) as ei:
        role_posture(unknown)
    assert "unknown team role" in str(ei.value).lower() or "unknown" in str(
        ei.value
    ).lower()

    with pytest.raises(UnknownRoleError):
        role_class(unknown)

    with pytest.raises(UnknownRoleError):
        is_reviewer_or_verifier(unknown)

    with pytest.raises(UnknownRoleError):
        role_meta(unknown)


def test_unknown_role_error_lists_expected() -> None:
    with pytest.raises(UnknownRoleError) as ei:
        role_meta("nope")
    msg = str(ei.value)
    assert "executor" in msg
    assert "security-reviewer" in msg
