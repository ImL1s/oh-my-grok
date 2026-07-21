"""Hermetic tests for team executor argv adapters (D0).

Pure argv building — no process exec. Covers posture × provider templates,
injection rejection, role-derived posture, and the ask agy mislabel fix.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from omg_cli.ask.providers import AskProviderError, normalize_provider
from omg_cli.team.providers import (
    EXECUTOR_PROVIDERS,
    EXECUTOR_SPECS,
    TeamProviderError,
    argv_has_free_form,
    build_executor_argv,
    build_executor_argv_signature_has_free_form_param,
    normalize_executor_provider,
)
from omg_cli.team.roles import UnknownRoleError, role_posture

# Roles that derive each posture (from roles.py taxonomy).
_REVIEWER_ROLE = "code-reviewer"  # → read-only
_EXECUTOR_ROLE = "executor"  # → read-write

_PROMPT = Path("/tmp/omg-team-prompt.txt")
_CWD = Path("/tmp/omg-team-cwd")
_MODEL = "test-model-xyz"


# ---------------------------------------------------------------------------
# Spec / registry
# ---------------------------------------------------------------------------


def test_executor_providers_match_specs() -> None:
    assert EXECUTOR_PROVIDERS == frozenset(EXECUTOR_SPECS)
    assert EXECUTOR_SPECS["agy"].needs_pty is True
    assert EXECUTOR_SPECS["agy"].binary == "agy"
    assert EXECUTOR_SPECS["cursor"].binary == "cursor-agent"
    for name in ("grok", "codex", "cursor", "gemini"):
        assert EXECUTOR_SPECS[name].needs_pty is False


def test_agy_needs_pty_on_invocation() -> None:
    inv = build_executor_argv(
        "agy", _EXECUTOR_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    assert inv.needs_pty is True
    assert inv.provider == "agy"


# ---------------------------------------------------------------------------
# Provider × posture templates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,posture",
    [
        (_REVIEWER_ROLE, "read-only"),
        (_EXECUTOR_ROLE, "read-write"),
    ],
)
def test_posture_derived_from_role(role: str, posture: str) -> None:
    assert role_posture(role) == posture
    inv = build_executor_argv(
        "grok", role, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    assert inv.posture == posture


def test_grok_read_only_uses_plan() -> None:
    inv = build_executor_argv(
        "grok", _REVIEWER_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    assert inv.argv[0] == "grok"
    assert "--prompt-file" in inv.argv
    assert str(_PROMPT) in inv.argv
    assert "--cwd" in inv.argv
    assert str(_CWD) in inv.argv
    assert inv.argv[inv.argv.index("-m") + 1] == _MODEL
    assert "--permission-mode" in inv.argv
    assert inv.argv[inv.argv.index("--permission-mode") + 1] == "plan"
    assert "bypassPermissions" not in inv.argv
    assert inv.posture == "read-only"
    assert inv.needs_pty is False
    assert not argv_has_free_form(inv.argv)


def test_grok_read_write_uses_bypass() -> None:
    inv = build_executor_argv(
        "grok", _EXECUTOR_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    assert inv.argv[inv.argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert "plan" not in inv.argv  # posture value only as mode arg value when RO
    # Ensure we didn't use plan as permission mode
    perm_idx = inv.argv.index("--permission-mode")
    assert inv.argv[perm_idx + 1] != "plan"
    assert inv.posture == "read-write"
    assert not argv_has_free_form(inv.argv)


def test_codex_read_only_sandbox() -> None:
    inv = build_executor_argv(
        "codex", _REVIEWER_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    assert inv.argv[:2] == ["codex", "exec"]
    assert "-C" in inv.argv
    assert str(_CWD) in inv.argv
    assert "-s" in inv.argv
    assert inv.argv[inv.argv.index("-s") + 1] == "read-only"
    assert "workspace-write" not in inv.argv
    assert inv.argv[-1] == "-"  # stdin sentinel; body not in argv
    assert str(_PROMPT) not in inv.argv  # body path not required in argv
    assert not argv_has_free_form(inv.argv)


def test_codex_read_write_workspace_write() -> None:
    inv = build_executor_argv(
        "codex", _EXECUTOR_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    assert inv.argv[inv.argv.index("-s") + 1] == "workspace-write"
    assert "read-only" not in inv.argv
    assert inv.argv[-1] == "-"
    assert not argv_has_free_form(inv.argv)


def test_cursor_read_only_mode_ask() -> None:
    inv = build_executor_argv(
        "cursor", _REVIEWER_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    assert inv.argv[0] == "cursor-agent"
    assert "--print" in inv.argv
    assert "--trust" in inv.argv
    assert "--workspace" in inv.argv
    assert str(_CWD) in inv.argv
    assert "--mode" in inv.argv
    assert inv.argv[inv.argv.index("--mode") + 1] == "ask"
    assert inv.argv[inv.argv.index("--model") + 1] == _MODEL
    # prompt path as trailing (body not inlined)
    assert inv.argv[-1] == str(_PROMPT)
    assert not argv_has_free_form(inv.argv)


def test_cursor_read_write_default_agent_mode() -> None:
    inv = build_executor_argv(
        "cursor", _EXECUTOR_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    assert "--mode" not in inv.argv  # default agent = write
    assert "ask" not in inv.argv
    assert "--print" in inv.argv and "--trust" in inv.argv
    assert not argv_has_free_form(inv.argv)


def test_agy_read_only_sandbox() -> None:
    inv = build_executor_argv(
        "agy", _REVIEWER_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    assert inv.argv[0] == "agy"
    assert "-p" in inv.argv
    assert "--dangerously-skip-permissions" in inv.argv
    assert "--sandbox" in inv.argv
    assert inv.needs_pty is True
    assert inv.argv[inv.argv.index("--model") + 1] == _MODEL
    assert not argv_has_free_form(inv.argv)


def test_agy_read_write_no_sandbox() -> None:
    inv = build_executor_argv(
        "agy", _EXECUTOR_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    assert "--sandbox" not in inv.argv
    assert "--dangerously-skip-permissions" in inv.argv
    assert inv.needs_pty is True
    assert not argv_has_free_form(inv.argv)


def test_gemini_file_prompt_both_postures() -> None:
    for role in (_REVIEWER_ROLE, _EXECUTOR_ROLE):
        inv = build_executor_argv(
            "gemini", role, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
        )
        assert inv.argv[0] == "gemini"
        assert inv.argv[1:3] == ["-p", str(_PROMPT)]
        assert inv.argv[inv.argv.index("--model") + 1] == _MODEL
        # No free-form elevation (yolo / approval-mode not wired)
        assert "--yolo" not in inv.argv
        assert "--approval-mode" not in inv.argv
        assert not argv_has_free_form(inv.argv)


# ---------------------------------------------------------------------------
# Reviewer never gets write/bypass flags
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", sorted(EXECUTOR_PROVIDERS))
def test_reviewer_role_never_emits_write_bypass(provider: str) -> None:
    inv = build_executor_argv(
        provider, _REVIEWER_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    assert inv.posture == "read-only"
    joined = " ".join(inv.argv)
    # Write / elevation posture flags must not appear for reviewer.
    assert "bypassPermissions" not in inv.argv
    assert "workspace-write" not in inv.argv
    # cursor agent mode would omit --mode ask; we require ask for RO
    if provider == "cursor":
        assert "--mode" in inv.argv and "ask" in inv.argv
    if provider == "agy":
        assert "--sandbox" in inv.argv
    if provider == "grok":
        assert "plan" in inv.argv
    if provider == "codex":
        assert "read-only" in inv.argv
    # Never YOLO / danger-full-access
    assert "--yolo" not in joined
    assert "danger-full-access" not in joined


# ---------------------------------------------------------------------------
# Injection / free-form rejection
# ---------------------------------------------------------------------------


def test_model_space_rejected() -> None:
    with pytest.raises(TeamProviderError, match="whitespace|injection"):
        build_executor_argv(
            "grok",
            _EXECUTOR_ROLE,
            prompt_file=_PROMPT,
            cwd=_CWD,
            model="evil model --yolo",
        )


def test_model_leading_dash_rejected() -> None:
    with pytest.raises(TeamProviderError, match="leading|injection"):
        build_executor_argv(
            "grok",
            _EXECUTOR_ROLE,
            prompt_file=_PROMPT,
            cwd=_CWD,
            model="--permission-mode=bypassPermissions",
        )


def test_no_free_form_param_on_signature() -> None:
    """HARD RULE: no free-form flags parameter on build_executor_argv."""
    assert build_executor_argv_signature_has_free_form_param() is False
    sig = inspect.signature(build_executor_argv)
    assert "extra" not in sig.parameters
    assert "flags" not in sig.parameters
    assert "argv_extra" not in sig.parameters
    # Only the vetted kwargs.
    params = set(sig.parameters)
    assert "provider" in params and "role" in params
    assert "prompt_file" in params and "cwd" in params and "model" in params
    # No *args / **kwargs catch-all for free-form passthrough.
    for p in sig.parameters.values():
        assert p.kind != inspect.Parameter.VAR_KEYWORD
        assert p.kind != inspect.Parameter.VAR_POSITIONAL


def test_argv_has_free_form_detects_yolo() -> None:
    assert argv_has_free_form(["grok", "--yolo"]) is True
    assert argv_has_free_form(["codex", "exec", "-s", "danger-full-access"]) is True
    # Clean vetted argv is clean
    inv = build_executor_argv(
        "grok", _EXECUTOR_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
    )
    assert argv_has_free_form(inv.argv) is False


def test_prompt_body_not_in_argv() -> None:
    secret = "TOP_SECRET_PROMPT_BODY_MUST_NOT_APPEAR"
    # Even if the path *name* is mundane, body text must not be an argv element.
    inv = build_executor_argv(
        "grok",
        _EXECUTOR_ROLE,
        prompt_file=_PROMPT,
        cwd=_CWD,
        model=_MODEL,
    )
    assert secret not in inv.argv
    assert all(secret not in a for a in inv.argv)


# ---------------------------------------------------------------------------
# Fail-closed: unknown provider / role
# ---------------------------------------------------------------------------


def test_unknown_provider_fail_closed() -> None:
    with pytest.raises(TeamProviderError, match="unknown executor provider"):
        build_executor_argv(
            "not-a-cli", _EXECUTOR_ROLE, prompt_file=_PROMPT, cwd=_CWD
        )
    with pytest.raises(TeamProviderError):
        normalize_executor_provider("claude")  # advisor-only, not executor


def test_unknown_role_fail_closed() -> None:
    with pytest.raises(UnknownRoleError):
        build_executor_argv(
            "grok", "not-a-role", prompt_file=_PROMPT, cwd=_CWD
        )


def test_model_optional_omitted() -> None:
    inv = build_executor_argv(
        "grok", _REVIEWER_ROLE, prompt_file=_PROMPT, cwd=_CWD, model=None
    )
    assert "-m" not in inv.argv
    assert "--model" not in inv.argv


# ---------------------------------------------------------------------------
# ask mislabel: agy must NOT silently map to gemini; fable still → claude
# ---------------------------------------------------------------------------


def test_ask_agy_no_longer_aliases_to_gemini() -> None:
    with pytest.raises(AskProviderError, match="unknown provider"):
        normalize_provider("agy")


def test_ask_fable_still_aliases_to_claude() -> None:
    assert normalize_provider("fable") == "claude"


def test_all_providers_both_postures_no_free_form() -> None:
    for provider in sorted(EXECUTOR_PROVIDERS):
        for role in (_REVIEWER_ROLE, _EXECUTOR_ROLE):
            inv = build_executor_argv(
                provider, role, prompt_file=_PROMPT, cwd=_CWD, model=_MODEL
            )
            assert inv.provider == provider
            assert inv.posture == role_posture(role)
            assert not argv_has_free_form(inv.argv)
            assert all(isinstance(a, str) for a in inv.argv)
            # Prompt body never appears; secret-looking content not smuggled.
            assert "TOP_SECRET" not in " ".join(inv.argv)
