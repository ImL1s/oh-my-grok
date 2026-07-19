"""Trusted user-invoked broker for external advisor CLIs.

Security invariants (S3/S5/S6):
- OMG_ALLOW_EXTERNAL_CLI=1 ONLY in child process env (never parent os.environ)
- Fixed argv, shell=False
- Capture stdout/stderr → artifact only; never apply patches / set verified
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from omg_cli.ask.providers import (
    AskProviderError,
    AskProviderMissing,
    build_provider_argv,
    normalize_provider,
)

DEFAULT_TIMEOUT: float = 600.0
DEFAULT_MAX_BYTES: int = 512 * 1024


@dataclass
class AskResult:
    provider: str
    exit_code: int
    artifact: Path
    meta: Path | None
    duration_s: float
    argv: list[str]
    truncated: bool
    dry_run: bool = False


def child_env_for_ask(base: dict[str, str] | None = None) -> dict[str, str]:
    """Build child env with OMG_ALLOW_EXTERNAL_CLI=1.

    Parent process must never set this key on os.environ via this function.
    """
    env = dict(base if base is not None else os.environ)
    env["OMG_ALLOW_EXTERNAL_CLI"] = "1"
    env["OMG_ASK_BROKER"] = "1"
    return env


def _utc_ts_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_artifact_path(root: Path, provider: str, ts: str | None = None) -> Path:
    slug = ts or _utc_ts_slug()
    return Path(root) / ".omg" / "artifacts" / f"ask-{slug}-{provider}.md"


def _truncate(data: bytes, max_bytes: int) -> tuple[str, bool]:
    truncated = len(data) > max_bytes
    chunk = data[:max_bytes] if truncated else data
    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        text = chunk.decode("latin-1", errors="replace")
    if truncated:
        text += f"\n\n… [truncated: captured {len(data)} bytes, max_bytes={max_bytes}]\n"
    return text, truncated


def _redact_argv_for_display(argv: list[str]) -> list[str]:
    """Return argv copy safe for logs (prompt may be long; keep as-is for audit)."""
    return list(argv)


def write_ask_artifact(
    path: Path,
    *,
    provider: str,
    prompt: str,
    response: str,
    argv: list[str],
    cwd: Path,
    exit_code: int,
    duration_s: float,
    run_id: str | None,
    truncated: bool,
    dry_run: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f"""# omg ask — {provider}

- ts: {_utc_iso()}
- provider: {provider}
- cwd: {cwd}
- exit_code: {exit_code}
- argv: {json.dumps(_redact_argv_for_display(argv), ensure_ascii=False)}
- duration_s: {duration_s:.3f}
- run_id: {run_id or ""}
- dry_run: {bool(dry_run)}
- truncated: {bool(truncated)}

## Prompt

```text
{prompt}
```

## Response

```text
{response}
```

## Broker notes

- Advisory only. Does not set omg verified/passes.
- Not an executor. Product changes require omg ulw/ralph/pipeline implement stages.
- OMG_ALLOW_EXTERNAL_CLI was set only in the child process env (if executed).
"""
    path.write_text(body, encoding="utf-8")


def write_ask_meta(
    path: Path,
    *,
    provider: str,
    argv: list[str],
    cwd: Path,
    exit_code: int,
    duration_s: float,
    artifact: Path,
    run_id: str | None,
    truncated: bool,
    bytes_captured: int,
    dry_run: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": 1,
        "writer": "omg-cli",
        "kind": "ask",
        "provider": provider,
        "ts": _utc_iso(),
        "cwd": str(cwd),
        "exit_code": exit_code,
        "duration_s": round(duration_s, 3),
        "argv": _redact_argv_for_display(argv),
        "artifact": str(artifact),
        "run_id": run_id,
        "truncated": truncated,
        "bytes_captured": bytes_captured,
        "dry_run": bool(dry_run),
    }
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _link_into_run(
    root: Path, run_id: str, artifact: Path, meta: Path | None
) -> None:
    """Copy/link ask artifact under runs/<id>/artifacts/ when run exists."""
    run_dir = Path(root) / ".omg" / "state" / "runs" / run_id
    if not run_dir.is_dir():
        return
    dest_dir = run_dir / "artifacts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / artifact.name
    try:
        if not dest.exists():
            dest.write_bytes(artifact.read_bytes())
    except OSError:
        pass
    if meta is not None and meta.is_file():
        mdest = dest_dir / meta.name
        try:
            if not mdest.exists():
                mdest.write_bytes(meta.read_bytes())
        except OSError:
            pass
    # Best-effort status extra without changing verified
    try:
        from omg_cli.state import load_run, write_status

        current = load_run(root, run_id)
        if current is None:
            return
        st = str(current.get("status") or "running")
        write_status(
            root,
            run_id,
            st,
            extra={
                "last_ask": {
                    "provider": artifact.name,
                    "artifact": str(artifact),
                    "ts": _utc_iso(),
                }
            },
        )
    except Exception:
        pass


def run_ask(
    provider: str,
    prompt: str,
    *,
    root: Path | str | None = None,
    cwd: Path | str | None = None,
    timeout: float | None = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    out: Path | str | None = None,
    run_id: str | None = None,
    dry_run: bool = False,
    model: str | None = None,
    extra: Sequence[str] | None = None,
    write_json: bool = True,
    files: Sequence[Path | str] | None = None,
    check_binary: bool | None = None,
) -> AskResult:
    """User-invoked trusted broker. Sets OMG_ALLOW_EXTERNAL_CLI only in child env.

    Returns AskResult. Raises AskProviderError (usage), AskProviderMissing (binary).
    Does not set verified. Does not mutate parent os.environ for allow key.
    """
    root_path = Path(root) if root is not None else Path.cwd().resolve()
    cwd_path = Path(cwd) if cwd is not None else root_path
    prompt = (prompt or "").strip()
    if not prompt:
        raise AskProviderError("prompt required")

    # Inline optional context files into prompt
    if files:
        chunks = [prompt, "", "## Attached files"]
        for f in files:
            fp = Path(f)
            chunks.append(f"### {fp}")
            try:
                chunks.append(fp.read_text(encoding="utf-8"))
            except OSError as exc:
                chunks.append(f"(unreadable: {exc})")
        prompt = "\n".join(chunks)

    canon = normalize_provider(provider)
    if check_binary is None:
        check_binary = not dry_run

    argv = build_provider_argv(
        canon,
        prompt,
        model=model,
        extra=extra,
        check_binary=check_binary,
    )

    ts = _utc_ts_slug()
    artifact = Path(out) if out is not None else default_artifact_path(root_path, canon, ts)
    meta_path = artifact.with_suffix(artifact.suffix + ".meta.json") if write_json else None
    # Prefer ask-*.meta.json alongside .md
    if write_json:
        meta_path = artifact.parent / (artifact.stem + ".meta.json")

    parent_had_allow = os.environ.get("OMG_ALLOW_EXTERNAL_CLI")

    if dry_run:
        # Print argv + child env keys + out path; no exec
        child_keys = sorted(child_env_for_ask().keys())
        print(f"omg ask dry-run provider={canon}")
        print(f"argv: {json.dumps(argv, ensure_ascii=False)}")
        print(f"out: {artifact}")
        print(f"cwd: {cwd_path}")
        print(f"child_env_keys: {json.dumps(child_keys)}")
        print("note: OMG_ALLOW_EXTERNAL_CLI set only in child env on real run")
        write_ask_artifact(
            artifact,
            provider=canon,
            prompt=prompt,
            response="(dry-run: provider not executed)",
            argv=argv,
            cwd=cwd_path,
            exit_code=0,
            duration_s=0.0,
            run_id=run_id,
            truncated=False,
            dry_run=True,
        )
        if meta_path is not None:
            write_ask_meta(
                meta_path,
                provider=canon,
                argv=argv,
                cwd=cwd_path,
                exit_code=0,
                duration_s=0.0,
                artifact=artifact,
                run_id=run_id,
                truncated=False,
                bytes_captured=0,
                dry_run=True,
            )
        # Parent env must be unchanged for allow key
        if os.environ.get("OMG_ALLOW_EXTERNAL_CLI") != parent_had_allow:
            # Restore if something mutated (defensive)
            if parent_had_allow is None:
                os.environ.pop("OMG_ALLOW_EXTERNAL_CLI", None)
            else:
                os.environ["OMG_ALLOW_EXTERNAL_CLI"] = parent_had_allow
        return AskResult(
            provider=canon,
            exit_code=0,
            artifact=artifact,
            meta=meta_path,
            duration_s=0.0,
            argv=argv,
            truncated=False,
            dry_run=True,
        )

    # Real launch
    child_env = child_env_for_ask()
    # Resolve timeout: None → DEFAULT; 0 → unlimited
    if timeout is None:
        eff_timeout: float | None = float(DEFAULT_TIMEOUT)
    elif timeout == 0 or timeout == 0.0:
        eff_timeout = None
    else:
        eff_timeout = float(timeout)

    t0 = time.monotonic()
    exit_code = 1
    captured = b""
    timed_out = False
    launch_os_error: OSError | None = None

    popen_kwargs: dict[str, Any] = {
        "cwd": str(cwd_path),
        "env": child_env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "shell": False,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    try:
        proc = subprocess.Popen(argv, **popen_kwargs)
    except OSError as exc:
        launch_os_error = exc
        proc = None  # type: ignore[assignment]
        exit_code = 127
    else:
        try:
            out_b, _ = proc.communicate(timeout=eff_timeout)
            captured = out_b or b""
            exit_code = int(proc.returncode if proc.returncode is not None else 1)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except (ProcessLookupError, OSError):
                try:
                    proc.kill()
                except (ProcessLookupError, OSError):
                    pass
            try:
                out_b, _ = proc.communicate(timeout=5)
                captured = out_b or b""
            except Exception:
                captured = b""
            exit_code = 4

    duration_s = time.monotonic() - t0

    # Ensure parent never retains allow from us
    if os.environ.get("OMG_ALLOW_EXTERNAL_CLI") != parent_had_allow:
        if parent_had_allow is None:
            os.environ.pop("OMG_ALLOW_EXTERNAL_CLI", None)
        else:
            os.environ["OMG_ALLOW_EXTERNAL_CLI"] = parent_had_allow

    if launch_os_error is not None:
        response = f"(launch OSError: {launch_os_error})"
        truncated = False
        bytes_captured = 0
    else:
        response, truncated = _truncate(captured, int(max_bytes))
        bytes_captured = len(captured)
        if timed_out:
            response = (response + "\n\n(broker: timed out; process group killed)\n").lstrip()

    write_ask_artifact(
        artifact,
        provider=canon,
        prompt=prompt,
        response=response,
        argv=argv,
        cwd=cwd_path,
        exit_code=exit_code,
        duration_s=duration_s,
        run_id=run_id,
        truncated=truncated,
        dry_run=False,
    )
    if meta_path is not None:
        write_ask_meta(
            meta_path,
            provider=canon,
            argv=argv,
            cwd=cwd_path,
            exit_code=exit_code,
            duration_s=duration_s,
            artifact=artifact,
            run_id=run_id,
            truncated=truncated,
            bytes_captured=bytes_captured,
            dry_run=False,
        )

    if run_id:
        _link_into_run(root_path, run_id, artifact, meta_path)

    print(f"omg ask: provider={canon} exit={exit_code} artifact={artifact}")
    return AskResult(
        provider=canon,
        exit_code=exit_code,
        artifact=artifact,
        meta=meta_path,
        duration_s=duration_s,
        argv=argv,
        truncated=truncated,
        dry_run=False,
    )


def ask_exit_code(result: AskResult) -> int:
    """Map AskResult to process exit code per design §2.7."""
    if result.dry_run:
        return 0
    if result.exit_code == 4:
        return 4
    if result.exit_code == 127:
        return 127
    if result.exit_code == 0:
        return 0
    return 1


def run_ask_cli(
    provider: str,
    prompt: str,
    **kwargs: Any,
) -> int:
    """CLI wrapper: catches provider errors → exit codes."""
    try:
        result = run_ask(provider, prompt, **kwargs)
    except AskProviderError as exc:
        print(f"omg ask: {exc}", file=sys.stderr)
        return 2
    except AskProviderMissing as exc:
        print(f"omg ask: {exc}", file=sys.stderr)
        return 3
    return ask_exit_code(result)


__all__ = [
    "AskResult",
    "DEFAULT_MAX_BYTES",
    "DEFAULT_TIMEOUT",
    "ask_exit_code",
    "child_env_for_ask",
    "default_artifact_path",
    "run_ask",
    "run_ask_cli",
]
