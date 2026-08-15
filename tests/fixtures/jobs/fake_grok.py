#!/usr/bin/env python3
"""Hermetic fake ``grok`` binary for job-provider tests (#69).

Responds to ``--version`` and headless ``--prompt-file`` via argv only.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _int_env(name: str, default: int = 0) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _flag_value(args: list[str], name: str) -> str | None:
    i = 0
    while i < len(args):
        if args[i] == name and i + 1 < len(args):
            return args[i + 1]
        i += 1
    return None


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--version" in args:
        print(os.environ.get("FAKE_GROK_VERSION", "1.0.4"))
        return _int_env("FAKE_GROK_VERSION_RC", 0)
    prompt_file = _flag_value(args, "--prompt-file")
    prompt = ""
    if prompt_file:
        prompt = Path(prompt_file).read_text(encoding="utf-8")
    override = os.environ.get("FAKE_GROK_RUN_STDOUT")
    if override is not None:
        sys.stdout.write(override)
    else:
        sys.stdout.write(f"grok:ok prompt_len={len(prompt)}\n")
        if (os.environ.get("FAKE_GROK_ECHO_CWD") or "").strip() in {"1", "true", "yes"}:
            sys.stdout.write(f"cwd={os.getcwd()}\n")
        if (os.environ.get("FAKE_GROK_ECHO_PROMPT") or "").strip() in {"1", "true", "yes"}:
            sys.stdout.write(prompt)
            if prompt and not prompt.endswith("\n"):
                sys.stdout.write("\n")
    err = os.environ.get("FAKE_GROK_RUN_STDERR")
    if err:
        sys.stderr.write(err)
    return _int_env("FAKE_GROK_RUN_RC", 0)


if __name__ == "__main__":
    raise SystemExit(main())
