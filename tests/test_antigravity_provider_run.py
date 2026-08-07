"""#67-B: Antigravity headless run / parsers / failure matrix (hermetic)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "antigravity"
FAKE_AGY = FIXTURES / "fake_agy.py"


def _install_fake_agy(bin_dir: Path) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "agy"
    py = sys.executable
    script = (
        f"#!{py}\n"
        "import runpy, sys\n"
        f"sys.argv[0] = {str(target)!r}\n"
        f"raise SystemExit(runpy.run_path({str(FAKE_AGY)!r}, run_name='__main__'))\n"
    )
    target.write_text(script, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


@pytest.fixture
def fake_agy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    path = _install_fake_agy(bin_dir)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)
    # Clear run overrides between tests.
    for key in list(os.environ):
        if key.startswith("FAKE_AGY_RUN_") or key.startswith("FAKE_AGY_ECHO_"):
            monkeypatch.delenv(key, raising=False)
    return path


# ---------------------------------------------------------------------------
# Models / adapter surface
# ---------------------------------------------------------------------------


def test_run_models_are_json_serializable() -> None:
    from omg_cli.providers.models import (
        ProviderRunEvent,
        ProviderRunRequest,
        ProviderRunResult,
        ProviderUsage,
        RUN_RESULT_SCHEMA,
    )

    req = ProviderRunRequest(prompt="hi", output_format="json")
    assert "prompt_len" in req.to_dict()
    result = ProviderRunResult(
        ok=True,
        exit_class="success",
        returncode=0,
        output="hi",
        events=(ProviderRunEvent(type="result", payload={"result": "hi"}),),
        usage=ProviderUsage(input_tokens=1, output_tokens=2, total_tokens=3),
        resume_supported=True,
    )
    payload = result.to_dict()
    assert payload["schema"] == RUN_RESULT_SCHEMA
    assert "team" not in payload
    assert "pane" not in payload
    json.dumps(payload)


def test_adapter_protocol_requires_run(fake_agy_path: Path) -> None:
    from omg_cli.providers.antigravity import AntigravityProvider
    from omg_cli.providers.base import ProviderAdapter
    from omg_cli.providers.models import ProviderRunRequest

    adapter = AntigravityProvider()
    assert isinstance(adapter, ProviderAdapter)
    result = adapter.run(ProviderRunRequest(prompt="ping", timeout_s=5.0))
    assert result.ok
    assert "echo:ping" in result.output


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def test_headless_success_text(fake_agy_path: Path) -> None:
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    result = run(ProviderRunRequest(prompt="hello", timeout_s=5.0))
    assert result.ok
    assert result.exit_class == "success"
    assert result.returncode == 0
    assert "echo:hello" in result.output
    assert result.argv[1] == "--print"
    assert result.argv[2] == "hello"
    assert "--dangerously-skip-permissions" not in result.argv


def test_headless_nonzero(fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    monkeypatch.setenv("FAKE_AGY_RUN_RC", "7")
    result = run(ProviderRunRequest(prompt="x", timeout_s=5.0))
    assert not result.ok
    assert result.exit_class == "nonzero"
    assert result.returncode == 7
    assert result.retryable is True


def _install_hang_agy(
    bin_dir: Path,
    *,
    stdout: str = "partial-out\n",
    stderr: str = "",
    marker: Path | None = None,
) -> Path:
    """Minimal agy that writes immediately then sleeps (timeout/cancel tests)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / "agy"
    py = sys.executable
    marker_expr = repr(str(marker)) if marker is not None else "None"
    script = f"""\
#!{py}
import sys, time
from pathlib import Path
sys.stdout.write({stdout!r})
sys.stdout.flush()
if {stderr!r}:
    sys.stderr.write({stderr!r})
    sys.stderr.flush()
marker = {marker_expr}
if marker:
    Path(marker).write_text("ready", encoding="utf-8")
time.sleep(30)
"""
    target.write_text(script, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return target


def test_timeout_preserves_partial_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    path = _install_hang_agy(tmp_path / "bin", stdout="partial-out\n")
    monkeypatch.setenv("OMG_AGY_BIN", str(path))
    monkeypatch.setenv("PATH", str(path.parent) + os.pathsep + os.environ.get("PATH", ""))
    result = run(ProviderRunRequest(prompt="slow", timeout_s=2.0))
    assert result.timed_out
    assert result.exit_class == "timeout"
    assert result.partial_output
    assert "partial-out" in result.stdout
    assert result.ok is False


def test_cancellation_preserves_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    marker = tmp_path / "wrote.ready"
    path = _install_hang_agy(
        tmp_path / "bin", stdout="cancel-partial\n", marker=marker
    )
    monkeypatch.setenv("OMG_AGY_BIN", str(path))
    monkeypatch.setenv("PATH", str(path.parent))
    cancel = threading.Event()

    def _flip() -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if marker.exists():
                break
            time.sleep(0.02)
        # Small grace so pipe readers observe the write before killpg.
        time.sleep(0.05)
        cancel.set()

    threading.Thread(target=_flip, daemon=True).start()
    result = run(
        ProviderRunRequest(prompt="c", timeout_s=10.0, cancel_event=cancel)
    )
    assert result.cancelled
    assert result.exit_class == "cancelled"
    assert "cancel-partial" in result.stdout
    assert result.partial_output


def test_partial_stderr_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    path = _install_hang_agy(
        tmp_path / "bin", stdout="out\n", stderr="err-partial\n"
    )
    monkeypatch.setenv("OMG_AGY_BIN", str(path))
    monkeypatch.setenv("PATH", str(path.parent))
    result = run(ProviderRunRequest(prompt="e", timeout_s=0.8))
    assert result.timed_out
    assert "err-partial" in result.stderr


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def test_json_success(fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    monkeypatch.setenv("FAKE_AGY_RUN_SESSION", "sess-json-1")
    monkeypatch.setenv("FAKE_AGY_RUN_RESUME", "resume-tok-1")
    result = run(
        ProviderRunRequest(prompt="j", output_format="json", timeout_s=5.0)
    )
    assert result.ok
    assert result.exit_class == "success"
    assert "echo:j" in result.output
    assert result.session_id == "sess-json-1"
    assert result.resume_token == "resume-tok-1"
    assert result.resume_supported is True
    assert result.usage is not None
    assert result.usage.total_tokens == 10
    assert len(result.events) == 1


def test_malformed_json(fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    monkeypatch.setenv("FAKE_AGY_RUN_MALFORMED", "1")
    result = run(
        ProviderRunRequest(prompt="bad", output_format="json", timeout_s=5.0)
    )
    assert not result.ok
    assert result.exit_class == "parse_error"
    assert result.events and result.events[0].malformed


def test_empty_json_parse_error() -> None:
    from omg_cli.providers.antigravity import parse_json_result
    from omg_cli.providers.errors import ProviderRunError

    with pytest.raises(ProviderRunError, match="empty"):
        parse_json_result("   ")


def test_stream_json_success(
    fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    monkeypatch.setenv("FAKE_AGY_RUN_SESSION", "sess-stream")
    result = run(
        ProviderRunRequest(
            prompt="s", output_format="stream-json", timeout_s=5.0
        )
    )
    assert result.ok
    assert result.session_id == "sess-stream"
    assert [e.type for e in result.events] == ["message", "result"]
    assert result.events[0].index == 0
    assert result.events[1].index == 1


def test_truncated_stream_keeps_events() -> None:
    from omg_cli.providers.antigravity import parse_stream_json

    body = (
        '{"type":"message","text":"a"}\n'
        '{"type":"result","result":"done","session_id":"s1"}'
    )
    output, events, usage, meta, malformed = parse_stream_json(
        body, truncated=True
    )
    assert not malformed
    assert output == "done"
    assert meta.get("session_id") == "s1"
    assert len(events) == 2
    assert usage is None


def test_malformed_stream_event() -> None:
    from omg_cli.providers.antigravity import parse_stream_json

    body = '{"type":"message","text":"ok"}\n{not-json\n'
    output, events, usage, meta, malformed = parse_stream_json(body)
    assert malformed
    assert any(e.malformed for e in events)
    assert events[0].type == "message"
    assert events[1].type == "parse_error"


def test_stream_parser_does_not_json_loads_full_buffer() -> None:
    """Regression: must not treat multi-line NDJSON as one JSON document."""
    from omg_cli.providers.antigravity import parse_stream_json

    body = '{"type":"a","text":"1"}\n{"type":"b","text":"2"}\n'
    _, events, _, _, malformed = parse_stream_json(body)
    assert not malformed
    assert len(events) == 2


# ---------------------------------------------------------------------------
# Security / env / cwd / CJK
# ---------------------------------------------------------------------------


def test_argv_injection_stays_single_element(fake_agy_path: Path) -> None:
    from omg_cli.providers.antigravity import build_run_argv, run
    from omg_cli.providers.models import ProviderRunRequest

    evil = "hello; rm -rf / --output-format json"
    argv = build_run_argv(str(fake_agy_path), evil, output_format="text")
    assert argv[2] == evil
    assert "--output-format" not in argv  # text default omits flag
    result = run(ProviderRunRequest(prompt=evil, timeout_s=5.0))
    assert result.ok
    assert evil in result.output
    assert result.argv[2] == evil


def test_cjk_prompt_preserved(fake_agy_path: Path) -> None:
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    prompt = "你好，世界 — 繁體測試"
    result = run(ProviderRunRequest(prompt=prompt, timeout_s=5.0))
    assert result.ok
    assert prompt in result.output
    assert result.argv[2] == prompt


def test_cwd_respected(
    fake_agy_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setenv("FAKE_AGY_ECHO_CWD", "1")
    result = run(
        ProviderRunRequest(prompt="cwd", cwd=str(work), timeout_s=5.0)
    )
    assert result.ok
    assert f"cwd={work.resolve()}" in result.stdout


def test_env_allowlist_drops_secrets(
    fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    monkeypatch.setenv("SECRET_TOKEN", "should-not-leak")
    monkeypatch.setenv("FAKE_AGY_ECHO_ENV", "SECRET_TOKEN,PATH")
    result = run(
        ProviderRunRequest(
            prompt="env",
            timeout_s=5.0,
            env={"FAKE_AGY_ECHO_ENV": "SECRET_TOKEN,PATH"},
        )
    )
    assert result.ok
    assert "should-not-leak" not in result.stdout
    assert "env.SECRET_TOKEN=" in result.stdout
    # Allowlisted PATH may appear; secret key is empty because not allowlisted.
    assert "env.SECRET_TOKEN=\n" in result.stdout or "env.SECRET_TOKEN=" in result.stdout


def test_shell_false_invariant(fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess as sp

    from omg_cli.providers import process as process_mod
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    captured: dict = {}
    real_popen = sp.Popen

    class TrackingPopen(real_popen):  # type: ignore[valid-type,misc]
        def __init__(self, *args, **kwargs):
            captured["kwargs"] = dict(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(sp, "Popen", TrackingPopen)
    monkeypatch.setattr(process_mod.subprocess, "Popen", TrackingPopen)
    result = run(ProviderRunRequest(prompt="shell", timeout_s=5.0))
    assert result.ok
    assert captured["kwargs"].get("shell") is False
    assert isinstance(captured["kwargs"].get("args"), list)


def test_run_reuses_provider_process_not_second_stack(
    monkeypatch: pytest.MonkeyPatch, fake_agy_path: Path
) -> None:
    from omg_cli.providers import antigravity as ag
    from omg_cli.providers.models import ProviderRunRequest

    calls: list[str] = []
    real = ag.run_provider_process

    def _wrap(*args, **kwargs):
        calls.append(kwargs.get("mode", ""))
        return real(*args, **kwargs)

    monkeypatch.setattr(ag, "run_provider_process", _wrap)
    ag.run(ProviderRunRequest(prompt="reuse", timeout_s=5.0))
    assert calls == ["run"]


# ---------------------------------------------------------------------------
# Session metadata (no Team)
# ---------------------------------------------------------------------------


def test_session_resume_metadata_no_team_fields(
    fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    monkeypatch.setenv("FAKE_AGY_RUN_SESSION", "sess-meta")
    monkeypatch.setenv("FAKE_AGY_RUN_RESUME", "tok-meta")
    result = run(
        ProviderRunRequest(prompt="m", output_format="json", timeout_s=5.0)
    )
    payload = result.to_dict()
    assert payload["session"]["session_id"] == "sess-meta"
    assert payload["session"]["resume_token"] == "tok-meta"
    assert payload["session"]["resume_supported"] is True
    blob = json.dumps(payload)
    assert "team" not in blob.lower() or "team" not in payload
    assert "pane_id" not in payload
    assert "worker" not in payload


def test_resume_id_goes_to_conversation_flag(fake_agy_path: Path) -> None:
    from omg_cli.providers.antigravity import build_run_argv

    argv = build_run_argv(
        str(fake_agy_path),
        "p",
        resume_id="conv-123",
        session_id="sess-ignored-for-argv",
    )
    assert "--conversation" in argv
    assert argv[argv.index("--conversation") + 1] == "conv-123"
    assert "sess-ignored-for-argv" not in argv


# ---------------------------------------------------------------------------
# Process contract reuse (orphan cleanup)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="killpg is POSIX-only")
def test_run_timeout_orphan_cleanup(
    fake_agy_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Headless run keeps the same process-group cleanup contract as probes."""
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    marker = tmp_path / "grandchild.alive"
    # Use fake_agy hang via PARTIAL; additionally spawn a grandchild via a
    # custom wrapper script named agy.
    py = sys.executable
    wrapper = tmp_path / "bin" / "agy"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    wrapper.write_text(
        f"""\
#!{py}
import os, subprocess, sys, time
from pathlib import Path
marker = Path({str(marker)!r})
# Share the provider session / process group (no start_new_session).
gc = subprocess.Popen(
    [sys.executable, "-c",
     "import time; from pathlib import Path; m=Path({str(marker)!r});\\n"
     "while True:\\n"
     " m.write_text(str(time.time()), encoding='utf-8'); time.sleep(0.05)\\n"],
)
sys.stdout.write("partial\\n")
sys.stdout.flush()
time.sleep(30)
raise SystemExit(gc.wait())
""",
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | 0o111)
    monkeypatch.setenv("PATH", str(wrapper.parent))
    monkeypatch.setenv("OMG_AGY_BIN", str(wrapper))

    result = run(ProviderRunRequest(prompt="orphan", timeout_s=0.4))
    assert result.timed_out
    # Grandchild must die with the process group.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if not marker.exists():
            break
        # If marker stops updating, process is dead.
        try:
            t1 = marker.read_text(encoding="utf-8")
            time.sleep(0.2)
            t2 = marker.read_text(encoding="utf-8")
            if t1 == t2:
                break
        except OSError:
            break
    else:
        pytest.fail("orphan grandchild still updating marker after timeout")


def test_auth_block_not_success(
    fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    monkeypatch.setenv("FAKE_AGY_RUN_AUTH_BLOCK", "1")
    result = run(ProviderRunRequest(prompt="auth", timeout_s=5.0))
    assert not result.ok
    assert result.exit_class == "auth_blocked"


def test_prompt_file(
    fake_agy_path: Path, tmp_path: Path
) -> None:
    from omg_cli.providers.antigravity import run
    from omg_cli.providers.models import ProviderRunRequest

    path = tmp_path / "prompt.txt"
    path.write_text("from-file", encoding="utf-8")
    result = run(
        ProviderRunRequest(prompt="", prompt_file=str(path), timeout_s=5.0)
    )
    assert result.ok
    assert "echo:from-file" in result.output


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def test_cli_run_json_envelope(
    fake_agy_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMG_AGY_BIN", str(fake_agy_path))
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omg_cli.main",
            "provider",
            "antigravity",
            "run",
            "--prompt",
            "cli-ok",
            "--run-json",
            "--timeout",
            "5",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    data = json.loads(proc.stdout)
    assert data["ok"] is True
    assert data["command"] == "provider.antigravity.run"
    assert data["result"]["ok"] is True
    assert "echo:cli-ok" in data["result"]["output"]


def test_cli_run_requires_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "omg_cli.main",
            "provider",
            "antigravity",
            "run",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )
    assert proc.returncode == 2
    data = json.loads(proc.stdout)
    assert data["ok"] is False
    assert data["error"]["code"] == "E_PROVIDER_RUN"
