"""Fixed argv templates for omg ask external advisors.

Never free-form shell. Prefer read-only / non-elevated provider flags.

By default prompts are fed via **stdin** (``prompt_mode="stdin"``) so the
full prompt body never appears in process argv. Set ``OMG_ASK_STDIN=0`` to
fall back to argv embedding (legacy).
"""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

# Providers supported in v0.2.1
PROVIDERS = frozenset({"codex", "claude", "gemini"})
ALIASES: dict[str, str] = {
    "fable": "claude",
    "agy": "gemini",
}

PromptMode = Literal["stdin", "argv", "file"]

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


def extras_allowed() -> bool:
    """True only when ``OMG_ASK_ALLOW_EXTRA=1`` (default: freeform extras denied)."""
    return os.environ.get("OMG_ASK_ALLOW_EXTRA", "").strip() == "1"


def default_prompt_mode() -> PromptMode:
    """Default prompt transport. ``OMG_ASK_STDIN=0`` → argv (legacy)."""
    val = os.environ.get("OMG_ASK_STDIN", "1").strip().lower()
    if val in ("0", "false", "no", "off"):
        return "argv"
    return "stdin"


def validate_extra(extra: Sequence[str] | None) -> list[str]:
    """Validate --extra passthrough; reject elevation / write flags.

    When ``OMG_ASK_ALLOW_EXTRA`` is not ``1``, any non-empty extra is rejected.
    """
    if not extra:
        return []
    if not extras_allowed():
        raise AskProviderError(
            "freeform --extra is disabled by default; "
            "set OMG_ASK_ALLOW_EXTRA=1 to enable validated passthrough"
        )
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


def write_prompt_temp(
    prompt: str,
    *,
    root: Path | str | None = None,
) -> Path:
    """Write prompt to a 0600 temp file under ``.omg/artifacts/.ask-prompt-*``."""
    base = Path(root) if root is not None else Path.cwd()
    art = base / ".omg" / "artifacts"
    art.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=".ask-prompt-",
        suffix=".txt",
        dir=str(art),
    )
    path = Path(name)
    try:
        os.write(fd, prompt.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except OSError:
        pass
    return path


def argv_codex(
    prompt: str,
    *,
    model: str | None = None,
    extra: Sequence[str] | None = None,
    prompt_mode: PromptMode | None = None,
    prompt_file: Path | str | None = None,
) -> list[str]:
    """codex exec -s read-only [ -m MODEL ] [PROMPT|stdin|-]."""
    m = _check_model(model, ALLOWED_CODEX_MODELS)
    mode = prompt_mode or default_prompt_mode()
    argv: list[str] = ["codex", "exec", "-s", "read-only"]
    if m:
        argv.extend(["-m", m])
    if mode == "stdin":
        # Read prompt from stdin; do not embed body in argv
        argv.append("-")
    elif mode == "file":
        if prompt_file is None:
            raise AskProviderError("prompt_mode=file requires prompt_file")
        argv.extend(["--", str(prompt_file)])
    else:
        argv.append(prompt)
    argv.extend(validate_extra(extra))
    return argv


def argv_claude(
    prompt: str,
    *,
    model: str | None = None,
    extra: Sequence[str] | None = None,
    prompt_mode: PromptMode | None = None,
    prompt_file: Path | str | None = None,
) -> list[str]:
    """claude [ -p PROMPT | stdin ] [ --model MODEL ] [extra…]. Never skip-permissions."""
    m = _check_model(model, ALLOWED_CLAUDE_MODELS)
    mode = prompt_mode or default_prompt_mode()
    argv: list[str] = ["claude"]
    if mode == "stdin":
        # No -p; broker feeds stdin
        pass
    elif mode == "file":
        if prompt_file is None:
            raise AskProviderError("prompt_mode=file requires prompt_file")
        argv.extend(["-p", str(prompt_file)])
    else:
        argv.extend(["-p", prompt])
    if m:
        argv.extend(["--model", m])
    argv.extend(validate_extra(extra))
    return argv


def argv_gemini(
    prompt: str,
    *,
    model: str | None = None,
    extra: Sequence[str] | None = None,
    prompt_mode: PromptMode | None = None,
    prompt_file: Path | str | None = None,
) -> list[str]:
    """gemini [ -p PROMPT | stdin ] [ --model MODEL ] [extra…]. Optional provider."""
    m = _check_model(model, ALLOWED_GEMINI_MODELS)
    mode = prompt_mode or default_prompt_mode()
    argv: list[str] = ["gemini"]
    if mode == "stdin":
        pass
    elif mode == "file":
        if prompt_file is None:
            raise AskProviderError("prompt_mode=file requires prompt_file")
        argv.extend(["-p", str(prompt_file)])
    else:
        argv.extend(["-p", prompt])
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
    prompt_mode: PromptMode | None = None,
    prompt_file: Path | str | None = None,
) -> list[str]:
    """Build fixed argv for provider. Optionally verify binary on PATH.

    When ``prompt_mode`` is ``stdin`` (default), the prompt body is **not**
    included in argv.
    """
    canon = normalize_provider(provider)
    if check_binary:
        resolve_binary(canon)
    mode = prompt_mode or default_prompt_mode()
    kwargs = dict(model=model, extra=extra, prompt_mode=mode, prompt_file=prompt_file)
    if canon == "codex":
        return argv_codex(prompt, **kwargs)  # type: ignore[arg-type]
    if canon == "claude":
        return argv_claude(prompt, **kwargs)  # type: ignore[arg-type]
    if canon == "gemini":
        return argv_gemini(prompt, **kwargs)  # type: ignore[arg-type]
    raise AskProviderError(f"unknown provider {provider!r}")


def argv_contains_prompt(argv: Sequence[str], prompt: str) -> bool:
    """True if the full prompt body appears as an argv element."""
    if not prompt:
        return False
    return any(prompt == a for a in argv)


__all__ = [
    "ALIASES",
    "AskProviderError",
    "AskProviderMissing",
    "PROVIDERS",
    "SPECS",
    "argv_claude",
    "argv_codex",
    "argv_contains_prompt",
    "argv_gemini",
    "build_provider_argv",
    "default_prompt_mode",
    "extras_allowed",
    "normalize_provider",
    "resolve_binary",
    "validate_extra",
    "write_prompt_temp",
]
