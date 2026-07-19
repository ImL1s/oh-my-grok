"""omg ask — trusted user-invoked broker for external advisor CLIs."""
from __future__ import annotations

from omg_cli.ask.broker import (
    AskResult,
    DEFAULT_MAX_BYTES,
    DEFAULT_TIMEOUT,
    ask_exit_code,
    child_env_for_ask,
    run_ask,
    run_ask_cli,
)
from omg_cli.ask.providers import (
    AskProviderError,
    AskProviderMissing,
    PROVIDERS,
    build_provider_argv,
    normalize_provider,
)

__all__ = [
    "AskProviderError",
    "AskProviderMissing",
    "AskResult",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT",
    "PROVIDERS",
    "ask_exit_code",
    "build_provider_argv",
    "child_env_for_ask",
    "normalize_provider",
    "run_ask",
    "run_ask_cli",
]
