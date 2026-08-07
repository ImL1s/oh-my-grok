#!/usr/bin/env python3
"""Hermetic fake ``agy`` binary for Antigravity provider tests (#67-A/B).

Responds to ``--version`` / ``--help`` / headless ``--print`` via argv only
(no network). Version and help text are overridable via env for compat-range
fixtures:

- ``FAKE_AGY_VERSION`` — printed for ``--version`` (default: fixture version.txt)
- ``FAKE_AGY_HELP`` — if set, path to help text; else sibling ``help.txt``
- ``FAKE_AGY_VERSION_RC`` — exit code for ``--version`` (default 0)
- ``FAKE_AGY_HELP_RC`` — exit code for ``--help`` (default 0)
- ``FAKE_AGY_HELP_EMPTY`` — when truthy, ``--help`` prints nothing
- ``FAKE_AGY_VERSION_TEXT`` — override version stdout/stderr body (may embed
  semver while failing via ``FAKE_AGY_VERSION_RC``)

Headless run (#67-B):

- ``FAKE_AGY_RUN_RC`` — exit code for ``--print`` / ``-p`` / ``--prompt``
- ``FAKE_AGY_RUN_SLEEP`` — seconds to sleep before emitting (timeout tests)
- ``FAKE_AGY_RUN_STDOUT`` — raw stdout override (else synthesize from format)
- ``FAKE_AGY_RUN_STDERR`` — stderr body
- ``FAKE_AGY_RUN_PARTIAL`` — when truthy, emit partial then sleep forever
- ``FAKE_AGY_RUN_AUTH_BLOCK`` — when truthy, print auth prompt + exit 1
- ``FAKE_AGY_RUN_SESSION`` — session_id embedded in json/stream-json
- ``FAKE_AGY_RUN_RESUME`` — resume token embedded when set
- ``FAKE_AGY_RUN_MALFORMED`` — force malformed json / bad stream line
- ``FAKE_AGY_RUN_TRUNCATE_STREAM`` — omit final newline on last stream event
- ``FAKE_AGY_ECHO_CWD`` — when truthy, prefix stdout with ``cwd=<getcwd()>``
- ``FAKE_AGY_ECHO_ENV`` — comma-separated env keys to echo as ``env.KEY=…``
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_DEFAULT_VERSION = (_HERE / "version.txt").read_text(encoding="utf-8").strip()
_DEFAULT_HELP = (_HERE / "help.txt").read_text(encoding="utf-8")


def _truthy(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int = 0) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _float_env(name: str, default: float = 0.0) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _flag_value(args: list[str], *names: str) -> str | None:
    """Return the argv value following the first matching flag name.

    Stops at end-of-options ``--`` or the first non-flag positional.
    """
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            break
        if not tok.startswith("-"):
            break
        if tok in names and i + 1 < len(args):
            return args[i + 1]
        for name in names:
            prefix = name + "="
            if tok.startswith(prefix):
                return tok[len(prefix) :]
        # Known value-taking flags consume the next token when present.
        if tok in {
            "--output-format",
            "--model",
            "--effort",
            "--mode",
            "--conversation",
            "--agent",
            "--project",
            "--json-schema",
            "--log-file",
            "--print-timeout",
            "--add-dir",
        }:
            i += 2
            continue
        # Boolean / presence flags (including --print / -p / --prompt).
        i += 1
    return None


def _has_flag(args: list[str], *names: str) -> bool:
    for tok in args:
        if tok == "--":
            break
        if not tok.startswith("-"):
            break
        if tok in names or any(tok.startswith(n + "=") for n in names):
            return True
    return False


def _prompt_positional(args: list[str]) -> str | None:
    """Return the prompt after ``--`` (preferred) or the trailing positional.

    Enforces flags-before-prompt / end-of-options: after ``--``, the next
    token is the prompt even when it starts with ``-``.
    """
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            if i + 1 >= len(args):
                sys.stderr.write("fake_agy: missing prompt after end-of-options '--'\n")
                return None
            prompt = args[i + 1]
            if len(args) != i + 2:
                extra = args[i + 2 :]
                sys.stderr.write(
                    f"fake_agy: extra args after prompt {extra!r} "
                    f"(only one positional after '--')\n"
                )
                return None
            return prompt
        if not tok.startswith("-"):
            prompt = tok
            for later in args[i + 1 :]:
                if later.startswith("-"):
                    sys.stderr.write(
                        f"fake_agy: flag {later!r} after prompt positional "
                        f"(flags must precede prompt; use '--' for dash prompts)\n"
                    )
                    return None
            return prompt
        if tok in {
            "--output-format",
            "--model",
            "--effort",
            "--mode",
            "--conversation",
            "--agent",
            "--project",
            "--json-schema",
            "--log-file",
            "--print-timeout",
            "--add-dir",
        }:
            i += 2
            continue
        # --print / -p / --prompt are boolean mode flags (prompt is positional).
        i += 1
    sys.stderr.write("fake_agy: missing prompt positional after flags\n")
    return None


def _synthesize_run_stdout(*, prompt: str, output_format: str) -> str:
    session = (os.environ.get("FAKE_AGY_RUN_SESSION") or "sess-fake-001").strip()
    resume = (os.environ.get("FAKE_AGY_RUN_RESUME") or "").strip()
    usage = {"input_tokens": 3, "output_tokens": 7, "total_tokens": 10}
    result_text = f"echo:{prompt}"

    if _truthy("FAKE_AGY_RUN_MALFORMED"):
        if output_format == "stream-json":
            return '{"type":"message","text":"ok"}\n{not-json\n'
        return "{not-json"

    if output_format == "json":
        payload = {
            "type": "result",
            "result": result_text,
            "session_id": session,
            "usage": usage,
        }
        if resume:
            payload["resume_token"] = resume
        return json.dumps(payload, ensure_ascii=False) + "\n"

    if output_format == "stream-json":
        events = [
            {"type": "message", "text": "partial"},
            {
                "type": "result",
                "result": result_text,
                "session_id": session,
                "usage": usage,
            },
        ]
        if resume:
            events[-1]["resume_token"] = resume
        lines = [json.dumps(e, ensure_ascii=False) for e in events]
        body = "\n".join(lines)
        if _truthy("FAKE_AGY_RUN_TRUNCATE_STREAM"):
            return body  # no trailing newline → last line still parseable; tests can truncate further
        return body + "\n"

    return result_text + "\n"


def _handle_print(args: list[str]) -> int:
    if not _has_flag(args, "--print", "-p", "--prompt"):
        sys.stderr.write("fake_agy: missing --print/-p/--prompt mode flag\n")
        return 2

    prompt = _prompt_positional(args)
    if prompt is None:
        # Distinguish missing prompt vs flag-after-prompt (stderr already set).
        if any(
            (not t.startswith("-")) for t in args
        ) and any(t.startswith("-") for t in args[1:]):
            return 2
        sys.stderr.write("fake_agy: missing prompt positional after flags\n")
        return 2

    output_format = (_flag_value(args, "--output-format") or "text").strip().lower()
    if output_format not in {"text", "json", "stream-json"}:
        sys.stderr.write(f"fake_agy: unsupported output-format {output_format!r}\n")
        return 2

    if _truthy("FAKE_AGY_RUN_AUTH_BLOCK"):
        sys.stderr.write("Please sign in to continue (auth required)\n")
        # Default non-zero; FAKE_AGY_RUN_AUTH_EXIT0 forces exit 0 false-green shape.
        if _truthy("FAKE_AGY_RUN_AUTH_EXIT0"):
            sys.stdout.write("Please sign in to continue (auth required)\n")
            return 0
        return 1

    sleep_s = _float_env("FAKE_AGY_RUN_SLEEP", 0.0)
    if sleep_s > 0 and not _truthy("FAKE_AGY_RUN_PARTIAL"):
        time.sleep(sleep_s)

    prefixes: list[str] = []
    if _truthy("FAKE_AGY_ECHO_CWD"):
        prefixes.append(f"cwd={os.getcwd()}")
    echo_env = (os.environ.get("FAKE_AGY_ECHO_ENV") or "").strip()
    if echo_env:
        for key in echo_env.split(","):
            key = key.strip()
            if not key:
                continue
            prefixes.append(f"env.{key}={os.environ.get(key, '')}")

    stdout_override = os.environ.get("FAKE_AGY_RUN_STDOUT")
    if stdout_override is not None:
        body = stdout_override
    else:
        body = _synthesize_run_stdout(prompt=prompt, output_format=output_format)

    if prefixes:
        prefix_block = "\n".join(prefixes) + "\n"
        body = prefix_block + body

    stderr_body = os.environ.get("FAKE_AGY_RUN_STDERR")
    if stderr_body:
        sys.stderr.write(
            stderr_body if stderr_body.endswith("\n") else stderr_body + "\n"
        )

    if _truthy("FAKE_AGY_RUN_PARTIAL"):
        # Emit something then hang so timeout/cancel keep partial stdout.
        sys.stdout.write(body if body.endswith("\n") else body + "\n")
        sys.stdout.flush()
        time.sleep(_float_env("FAKE_AGY_RUN_SLEEP", 30.0) or 30.0)
        return 0

    sys.stdout.write(body if body.endswith("\n") else body + "\n")
    return _int_env("FAKE_AGY_RUN_RC", 0)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    version = os.environ.get("FAKE_AGY_VERSION", _DEFAULT_VERSION).strip() or _DEFAULT_VERSION
    version_text = os.environ.get("FAKE_AGY_VERSION_TEXT")
    help_path = os.environ.get("FAKE_AGY_HELP")
    help_text = (
        Path(help_path).read_text(encoding="utf-8")
        if help_path
        else _DEFAULT_HELP
    )

    if "--version" in args or "-V" in args:
        body = version_text if version_text is not None else version
        stream = sys.stderr if _int_env("FAKE_AGY_VERSION_RC") != 0 else sys.stdout
        if body:
            stream.write(body if body.endswith("\n") else body + "\n")
        return _int_env("FAKE_AGY_VERSION_RC", 0)
    if _has_flag(args, "--print", "-p", "--prompt"):
        return _handle_print(args)
    if "--help" in args or "-h" in args or not args:
        if _truthy("FAKE_AGY_HELP_EMPTY"):
            return _int_env("FAKE_AGY_HELP_RC", 0)
        sys.stdout.write(help_text if help_text.endswith("\n") else help_text + "\n")
        return _int_env("FAKE_AGY_HELP_RC", 0)

    # Unknown subcommands stay hermetic: no TTY / network.
    sys.stderr.write(f"fake_agy: unsupported argv {args!r} (hermetic stub)\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
