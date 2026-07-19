"""Fixed argv templates for omg ask external advisors.

Never free-form shell. Prefer read-only / non-elevated provider flags.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Sequence

# Providers supported in v0.2.1
PROVIDERS = frozenset({"codex", "claude", "gemini"})
ALIASES: dict[str, str] = {
    "fable": "claude",
    "agy": "gemini",
}

# Optional model allowlists (empty = any non-empty model string accepted with caution)
ALLOWED_CODEX_MODELS: frozenset[str] | None = None  # None → accept any
ALLOWED_CLAUDE_MODELS: frozenset[str] | None = None
ALLOWED_GEMINI_MODELS: frozenset[str] | None = None

# --extra reject patterns (elevation / write / shell injection)
_EXTRA_DENY_EXACT = frozenset(
    {
        "--dangerously-skip-permissions",
        "--always-approve",
        "bypassPermissions",
        "--yolo",
        "--yes",
        "-y",
    }
)
_EXTRA_DENY_SUBSTR = (
    "dangerously-skip-permissions",
    "bypassPermissions",
    "workspace-write",
    "danger-full-access",
)


class AskProviderError(ValueError):
    """Usage / validation error for ask (maps to exit 2)."""


class AskProviderMissing(FileNotFoundError):
    """Provider binary not on PATH (maps to exit 3)."""


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    binary: str
    optional: bool = False


SPECS: dict[str, ProviderSpec] = {
    "codex": ProviderSpec(name="codex", binary="codex", optional=False),
    "claude": ProviderSpec(name="claude", binary="claude", optional=False),
    "gemini": ProviderSpec(name="gemini", binary="gemini", optional=True),
}


def normalize_provider(name: str) -> str:
    """Resolve alias → canonical provider name. Raises AskProviderError if unknown."""
    raw = (name or "").strip().lower()
    if not raw:
        raise AskProviderError("provider name required")
    canon = ALIASES.get(raw, raw)
    if canon not in PROVIDERS:
        known = ", ".join(sorted(PROVIDERS | set(ALIASES)))
        raise AskProviderError(f"unknown provider {name!r}; expected one of: {known}")
    return canon


def resolve_binary(provider: str) -> str:
    """Return binary name; raise AskProviderMissing if not on PATH."""
    provider = normalize_provider(provider)
    spec = SPECS[provider]
    path = shutil.which(spec.binary)
    if path is None:
        # gemini optional still exits 3 at ask time (doctor may WARN)
        raise AskProviderMissing(
            f"provider binary not found on PATH: {spec.binary!r} "
            f"(provider={provider})"
        )
    return spec.binary


def validate_extra(extra: Sequence[str] | None) -> list[str]:
    """Validate --extra passthrough; reject elevation / write flags."""
    if not extra:
        return []
    out: list[str] = []
    i = 0
    items = list(extra)
    while i < len(items):
        arg = items[i]
        if not isinstance(arg, str):
            raise AskProviderError(f"invalid --extra arg type: {type(arg)!r}")
        if arg in _EXTRA_DENY_EXACT:
            raise AskProviderError(f"rejected --extra elevation flag: {arg!r}")
        low = arg.lower()
        for bad in _EXTRA_DENY_SUBSTR:
            if bad.lower() in low:
                raise AskProviderError(
                    f"rejected --extra flag matching deny policy: {arg!r}"
                )
        # Reject -s workspace-write style pairs
        if arg in ("-s", "--sandbox") and i + 1 < len(items):
            nxt = items[i + 1]
            if "write" in nxt.lower() or "danger" in nxt.lower():
                raise AskProviderError(
                    f"rejected --extra sandbox elevation: {arg} {nxt!r}"
                )
        # Reject @-file shell-ish injection as free extra
        if arg.startswith("@") and len(arg) > 1:
            raise AskProviderError(f"rejected --extra @-file injection: {arg!r}")
        out.append(arg)
        i += 1
    return out


def _check_model(model: str | None, allowlist: frozenset[str] | None) -> str | None:
    if model is None:
        return None
    m = model.strip()
    if not m:
        return None
    if allowlist is not None and m not in allowlist:
        raise AskProviderError(
            f"model {m!r} not in allowlist: {sorted(allowlist)}"
        )
    return m


def argv_codex(
    prompt: str,
    *,
    model: str | None = None,
    extra: Sequence[str] | None = None,
) -> list[str]:
    """codex exec -s read-only [ -m MODEL ] PROMPT [extra…]."""
    m = _check_model(model, ALLOWED_CODEX_MODELS)
    argv: list[str] = ["codex", "exec", "-s", "read-only"]
    if m:
        argv.extend(["-m", m])
    argv.append(prompt)
    argv.extend(validate_extra(extra))
    return argv


def argv_claude(
    prompt: str,
    *,
    model: str | None = None,
    extra: Sequence[str] | None = None,
) -> list[str]:
    """claude -p PROMPT [ --model MODEL ] [extra…]. Never skip-permissions."""
    m = _check_model(model, ALLOWED_CLAUDE_MODELS)
    argv: list[str] = ["claude", "-p", prompt]
    if m:
        argv.extend(["--model", m])
    argv.extend(validate_extra(extra))
    return argv


def argv_gemini(
    prompt: str,
    *,
    model: str | None = None,
    extra: Sequence[str] | None = None,
) -> list[str]:
    """gemini -p PROMPT [ --model MODEL ] [extra…]. Optional provider."""
    m = _check_model(model, ALLOWED_GEMINI_MODELS)
    argv: list[str] = ["gemini", "-p", prompt]
    if m:
        argv.extend(["--model", m])
    argv.extend(validate_extra(extra))
    return argv


def build_provider_argv(
    provider: str,
    prompt: str,
    *,
    model: str | None = None,
    extra: Sequence[str] | None = None,
    check_binary: bool = True,
) -> list[str]:
    """Build fixed argv for provider. Optionally verify binary on PATH."""
    canon = normalize_provider(provider)
    if check_binary:
        resolve_binary(canon)
    if canon == "codex":
        return argv_codex(prompt, model=model, extra=extra)
    if canon == "claude":
        return argv_claude(prompt, model=model, extra=extra)
    if canon == "gemini":
        return argv_gemini(prompt, model=model, extra=extra)
    raise AskProviderError(f"unknown provider {provider!r}")


__all__ = [
    "ALIASES",
    "AskProviderError",
    "AskProviderMissing",
    "PROVIDERS",
    "SPECS",
    "argv_claude",
    "argv_codex",
    "argv_gemini",
    "build_provider_argv",
    "normalize_provider",
    "resolve_binary",
    "validate_extra",
]
