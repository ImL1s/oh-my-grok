"""Vetted executor argv adapters for ``omg team`` multi-CLI panes (D0).

Posture-aware EXECUTOR builders — distinct from advisor builders in
``omg_cli.ask.providers``.

Hard rules
----------
1. **No free-form flags** from task JSON. Only ``(provider, role, model?)``
   plus required ``prompt_file`` / ``cwd`` shape the argv; templates are fixed
   per ``(provider, posture)``.
2. **Posture is derived from role** via :func:`omg_cli.team.roles.role_posture`
   — never taken from task JSON.
3. **Model** is optional; if present, reject spaces / leading ``-`` (injection)
   and optional allowlist misses.
4. **Prompt body never inline in argv** — prefer ``--prompt-file`` / stdin /
   path-only ``-p`` so the body stays out of ``ps``.
5. **Unknown provider** → :class:`TeamProviderError` (fail-closed).
"""

from __future__ import annotations

import inspect
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Sequence

from omg_cli.team.roles import role_posture

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

EXECUTOR_PROVIDERS: Final[frozenset[str]] = frozenset(
    {"grok", "codex", "agy", "cursor", "gemini"}
)

# Optional model allowlist (None → any non-empty validated model string).
ALLOWED_EXECUTOR_MODELS: frozenset[str] | None = None

# Vetted flag tokens that may appear in fixed templates (for free-form scan).
_VETTED_FLAGS: Final[frozenset[str]] = frozenset(
    {
        # shared / meta
        "-m",
        "--model",
        "-p",
        "--prompt-file",
        "--cwd",
        "-C",
        "--cd",
        "--workspace",
        "--print",
        "--trust",
        "--mode",
        "--permission-mode",
        "--sandbox",
        "--dangerously-skip-permissions",
        "-s",
        # posture values (also appear as bare argv tokens)
        "plan",
        "bypassPermissions",
        "read-only",
        "workspace-write",
        "ask",
        # codex subcommand / stdin sentinel
        "exec",
        "-",
    }
)

# Tokens that must never appear as free-form elevation / injection.
_FREE_FORM_DENY_EXACT: Final[frozenset[str]] = frozenset(
    {
        "--yolo",
        "-y",
        "--yes",
        "--always-approve",
        "danger-full-access",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--force",
        "--auto-approve",
    }
)
_FREE_FORM_DENY_SUBSTR: Final[tuple[str, ...]] = (
    "danger-full-access",
    "dangerously-bypass-approvals",
    "always-approve",
)


class TeamProviderError(ValueError):
    """Usage / validation error for team executor providers (fail-closed)."""


class TeamProviderMissing(FileNotFoundError):
    """Executor binary not on PATH."""


@dataclass(frozen=True, slots=True)
class ExecutorSpec:
    """Static metadata for one executor provider."""

    name: str
    binary: str
    optional: bool = False
    needs_pty: bool = False


@dataclass(frozen=True, slots=True)
class ExecutorInvocation:
    """Built argv + runtime posture for one executor spawn."""

    argv: list[str]
    needs_pty: bool
    posture: str  # "read-only" | "read-write"
    provider: str


EXECUTOR_SPECS: Final[dict[str, ExecutorSpec]] = {
    "grok": ExecutorSpec(name="grok", binary="grok", optional=False, needs_pty=False),
    "codex": ExecutorSpec(name="codex", binary="codex", optional=False, needs_pty=False),
    "agy": ExecutorSpec(name="agy", binary="agy", optional=False, needs_pty=True),
    "cursor": ExecutorSpec(
        name="cursor", binary="cursor-agent", optional=False, needs_pty=False
    ),
    "gemini": ExecutorSpec(
        name="gemini", binary="gemini", optional=True, needs_pty=False
    ),
}


def normalize_executor_provider(name: str) -> str:
    """Return canonical executor provider name; raise if unknown."""
    raw = (name or "").strip().lower()
    if not raw:
        raise TeamProviderError("executor provider name required")
    if raw not in EXECUTOR_PROVIDERS:
        known = ", ".join(sorted(EXECUTOR_PROVIDERS))
        raise TeamProviderError(
            f"unknown executor provider {name!r}; expected one of: {known}"
        )
    return raw


def resolve_executor_binary(provider: str) -> str:
    """Return binary basename; raise :class:`TeamProviderMissing` if not on PATH."""
    canon = normalize_executor_provider(provider)
    spec = EXECUTOR_SPECS[canon]
    path = shutil.which(spec.binary)
    if path is None:
        raise TeamProviderMissing(
            f"executor binary not found on PATH: {spec.binary!r} "
            f"(provider={canon})"
        )
    return spec.binary


def _validate_model(model: str | None) -> str | None:
    if model is None:
        return None
    m = model.strip()
    if not m:
        return None
    # Injection floor: no spaces, no leading dash (flag smuggling).
    if m.startswith("-"):
        raise TeamProviderError(
            f"invalid model {model!r}: leading '-' is rejected (injection floor)"
        )
    if any(ch.isspace() for ch in m):
        raise TeamProviderError(
            f"invalid model {model!r}: whitespace is rejected (injection floor)"
        )
    if "\x00" in m:
        raise TeamProviderError(f"invalid model {model!r}: NUL rejected")
    if ALLOWED_EXECUTOR_MODELS is not None and m not in ALLOWED_EXECUTOR_MODELS:
        raise TeamProviderError(
            f"model {m!r} not in allowlist: {sorted(ALLOWED_EXECUTOR_MODELS)}"
        )
    return m


def _validate_path_arg(value: Path | str, *, name: str) -> str:
    s = str(value).strip() if not isinstance(value, Path) else str(value)
    if not s:
        raise TeamProviderError(f"{name} is required")
    if s.startswith("-"):
        raise TeamProviderError(
            f"invalid {name} {value!r}: leading '-' is rejected (injection floor)"
        )
    if any(ch in s for ch in ("\n", "\r", "\x00")):
        raise TeamProviderError(f"invalid {name}: control characters rejected")
    return s


def argv_has_free_form(argv: Sequence[str]) -> bool:
    """Return True if *argv* contains free-form / elevation tokens outside templates.

    Used by adversarial tests. Vetted posture flags (e.g. ``bypassPermissions``
    for grok read-write, ``--sandbox`` for agy read-only) are allowed; YOLO /
    danger-full-access / unknown ``--flag`` tokens are not.
    """
    if not argv:
        return False
    # Binary / first token may be any of the known executor binaries.
    allowed_bins = {spec.binary for spec in EXECUTOR_SPECS.values()} | set(
        EXECUTOR_PROVIDERS
    )
    for i, tok in enumerate(argv):
        if not isinstance(tok, str):
            return True
        if tok in _FREE_FORM_DENY_EXACT:
            return True
        low = tok.lower()
        for bad in _FREE_FORM_DENY_SUBSTR:
            if bad in low:
                return True
        # Flag-like token not in the vetted set (skip binary at index 0 and
        # non-flag values such as paths / model ids / posture bare values).
        if i == 0:
            if tok not in allowed_bins:
                # Unknown binary is not "free-form flags" per se; still fail-safe.
                return True
            continue
        if tok.startswith("-") and tok not in _VETTED_FLAGS:
            # Allow ``-`` stdin sentinel (already in _VETTED_FLAGS).
            return True
    return False


def _build_grok(
    *,
    posture: str,
    prompt_file: str,
    cwd: str,
    model: str | None,
) -> list[str]:
    # Verified: grok --prompt-file --cwd --permission-mode {plan|bypassPermissions}
    # (ref ~/.claude/skills/grok-cli-agent/grok-exec.sh + `grok --help`).
    argv: list[str] = [
        "grok",
        "--prompt-file",
        prompt_file,
        "--cwd",
        cwd,
    ]
    if model:
        argv.extend(["-m", model])
    if posture == "read-only":
        argv.extend(["--permission-mode", "plan"])
    else:
        argv.extend(["--permission-mode", "bypassPermissions"])
    return argv


def _build_codex(
    *,
    posture: str,
    prompt_file: str,  # noqa: ARG001 — body via stdin; path owned by caller
    cwd: str,
    model: str | None,
) -> list[str]:
    # Verified: `codex exec -C <DIR> -s {read-only|workspace-write} [-m M] -`
    # Note: real CLI uses ``-C``/``--cd``, NOT ``--cwd`` (brief misnamed).
    # Prompt via stdin sentinel ``-`` (caller feeds prompt_file contents).
    argv: list[str] = ["codex", "exec", "-C", cwd]
    if model:
        argv.extend(["-m", model])
    sandbox = "read-only" if posture == "read-only" else "workspace-write"
    argv.extend(["-s", sandbox, "-"])
    return argv


def _build_cursor(
    *,
    posture: str,
    prompt_file: str,
    cwd: str,
    model: str | None,
) -> list[str]:
    # Verified: cursor-agent --print --trust --workspace <cwd> [--model M]
    #   read-only → --mode ask; read-write → default agent mode (no --mode).
    # (ref ~/.claude/skills/cursor-cli-agent/cursor-exec.sh + `cursor-agent --help`).
    # Real CLI takes prompt as trailing positional; no --prompt-file. We pass the
    # *path* (not body) so the secret stays out of argv; D1 may expand to body
    # at spawn if the CLI requires text content.
    argv: list[str] = [
        "cursor-agent",
        "--print",
        "--trust",
        "--workspace",
        cwd,
    ]
    if model:
        argv.extend(["--model", model])
    if posture == "read-only":
        argv.extend(["--mode", "ask"])
    argv.append(prompt_file)
    return argv


def _build_agy(
    *,
    posture: str,
    prompt_file: str,
    cwd: str,  # noqa: ARG001 — agy has no --cwd; caller chdirs / PTY env
    model: str | None,
) -> list[str]:
    # Verified: agy -p <prompt> --model M --dangerously-skip-permissions [--sandbox]
    # needs_pty=True (ref ~/.claude/skills/agy-cli-agent/agy-pty.py).
    # Real CLI expects prompt *text* after -p; D0 passes path only (no body in
    # argv). Read-only → --sandbox; read-write → no sandbox.
    argv: list[str] = ["agy", "-p", prompt_file]
    if model:
        argv.extend(["--model", model])
    argv.append("--dangerously-skip-permissions")
    if posture == "read-only":
        argv.append("--sandbox")
    return argv


def _build_gemini(
    *,
    posture: str,  # noqa: ARG001 — advisor-grade; no posture flag in brief
    prompt_file: str,
    cwd: str,  # noqa: ARG001 — gemini has no fixed cwd flag in brief template
    model: str | None,
) -> list[str]:
    # Verified: gemini -p <prompt> [--model M] (advisor-grade; file path as -p).
    # Posture is recorded on ExecutorInvocation but gemini template has no
    # read-only/write switch in the D0 brief (plan/yolo exist on real CLI but
    # are intentionally NOT wired — no free-form / no guess).
    argv: list[str] = ["gemini", "-p", prompt_file]
    if model:
        argv.extend(["--model", model])
    return argv


_BUILDERS = {
    "grok": _build_grok,
    "codex": _build_codex,
    "cursor": _build_cursor,
    "agy": _build_agy,
    "gemini": _build_gemini,
}


def build_executor_argv(
    provider: str,
    role: str,
    *,
    prompt_file: Path | str,
    model: str | None = None,
    cwd: Path | str,
    check_binary: bool = False,
) -> ExecutorInvocation:
    """Build a fixed, posture-aware executor argv.

    Parameters
    ----------
    provider:
        One of :data:`EXECUTOR_PROVIDERS` (fail-closed).
    role:
        Team role; posture is **derived** via :func:`role_posture` (not an input).
    prompt_file:
        Path to the prompt file (body never inlined into argv).
    model:
        Optional model id (validated; no spaces / leading ``-``).
    cwd:
        Working directory for providers that accept it.
    check_binary:
        When True, resolve binary on PATH (raises :class:`TeamProviderMissing`).

    Notes
    -----
    There is intentionally **no** ``extra`` / free-form flags parameter.
    """
    canon = normalize_executor_provider(provider)
    # Posture from role registry only (fail-closed on unknown role).
    posture = role_posture(role)
    if posture not in ("read-only", "read-write"):
        raise TeamProviderError(f"unexpected posture {posture!r} for role {role!r}")

    pf = _validate_path_arg(prompt_file, name="prompt_file")
    workdir = _validate_path_arg(cwd, name="cwd")
    m = _validate_model(model)

    if check_binary:
        resolve_executor_binary(canon)

    builder = _BUILDERS[canon]
    argv = builder(posture=posture, prompt_file=pf, cwd=workdir, model=m)
    spec = EXECUTOR_SPECS[canon]
    inv = ExecutorInvocation(
        argv=argv,
        needs_pty=spec.needs_pty,
        posture=posture,
        provider=canon,
    )
    # Self-check: built argv must not carry free-form elevation.
    if argv_has_free_form(inv.argv):
        raise TeamProviderError(
            f"internal error: free-form tokens in vetted argv for {canon}: {inv.argv!r}"
        )
    return inv


def build_executor_argv_signature_has_free_form_param() -> bool:
    """True if :func:`build_executor_argv` ever gains a free-form passthrough param.

    Guard for the injection test — the public signature must not accept
    ``extra`` / ``flags`` / ``argv_extra`` style free-form inputs.
    """
    sig = inspect.signature(build_executor_argv)
    forbidden = {"extra", "flags", "argv_extra", "extra_args", "passthrough", "args"}
    return bool(forbidden & set(sig.parameters))


__all__ = [
    "ALLOWED_EXECUTOR_MODELS",
    "EXECUTOR_PROVIDERS",
    "EXECUTOR_SPECS",
    "ExecutorInvocation",
    "ExecutorSpec",
    "TeamProviderError",
    "TeamProviderMissing",
    "argv_has_free_form",
    "build_executor_argv",
    "build_executor_argv_signature_has_free_form_param",
    "normalize_executor_provider",
    "resolve_executor_binary",
]
