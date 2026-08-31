"""#67-C: omg ask agy routes through ProviderAdapter.run (hermetic fake agy)."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
import pytest

from omg_cli.ask import AskProviderError, AskProviderMissing, run_ask, run_ask_cli
from omg_cli.ask.providers import build_provider_argv, normalize_provider

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
    for key in list(os.environ):
        if key.startswith("FAKE_AGY_RUN_") or key.startswith("FAKE_AGY_ECHO_"):
            monkeypatch.delenv(key, raising=False)
    return path


def test_normalize_agy_is_first_class_not_gemini() -> None:
    assert normalize_provider("agy") == "agy"
    assert normalize_provider("agy") != "gemini"


def test_build_provider_argv_rejects_agy_legacy_path() -> None:
    with pytest.raises(AskProviderError, match="ProviderAdapter"):
        build_provider_argv("agy", "hello", check_binary=False)


def test_ask_agy_success_uses_adapter(
    fake_agy_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.providers import antigravity as agy_mod

    calls: list[object] = []
    real_run = agy_mod.AntigravityProvider.run

    def tracking_run(self, request):  # noqa: ANN001
        calls.append(request)
        return real_run(self, request)

    monkeypatch.setattr(agy_mod.AntigravityProvider, "run", tracking_run)

    result = run_ask("agy", "hello", root=tmp_path, timeout=5.0, dry_run=False)
    assert result.exit_code == 0
    assert result.provider == "agy"
    assert result.artifact.is_file()
    body = result.artifact.read_text(encoding="utf-8")
    assert "echo:hello" in body
    assert calls, "ProviderAdapter.run must be invoked"
    assert result.argv[-1] == "--print=hello"
    assert sum(arg.startswith("--print=") for arg in result.argv) == 1
    assert "--" not in result.argv
    assert run_ask_cli("agy", "hello", root=tmp_path, timeout=5.0) == 0


def test_ask_agy_json_artifact_is_parsed_text(
    fake_agy_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_AGY_RUN_SESSION", "sess-ask-json")
    result = run_ask(
        "agy",
        "j",
        root=tmp_path,
        timeout=5.0,
        output_format="json",
    )
    assert result.exit_code == 0
    body = result.artifact.read_text(encoding="utf-8")
    # Artifact Response section must carry normalized text, not raw JSON object.
    assert '"type"' not in body.split("## Response", 1)[-1].split("## Broker", 1)[0]
    assert "echo:j" in body or "j" in body


def test_ask_agy_stream_json_final(
    fake_agy_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_AGY_RUN_SESSION", "sess-ask-stream")
    result = run_ask(
        "agy",
        "s",
        root=tmp_path,
        timeout=5.0,
        output_format="stream-json",
    )
    assert result.exit_code == 0
    body = result.artifact.read_text(encoding="utf-8")
    assert "echo:s" in body or "s" in body


def test_ask_agy_timeout_exit_4_preserves_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hang-then-partial agy → ask exit 4 with partial in artifact."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    target = bin_dir / "agy"
    py = sys.executable
    script = f"""\
#!{py}
import sys, time
sys.stdout.write("partial-ask-out\\n")
sys.stdout.flush()
time.sleep(30)
"""
    target.write_text(script, encoding="utf-8")
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)

    result = run_ask("agy", "slow", root=tmp_path, timeout=0.4, dry_run=False)
    assert result.exit_code == 4
    body = result.artifact.read_text(encoding="utf-8")
    assert "partial-ask-out" in body
    assert "timed out" in body.lower()
    assert run_ask_cli("agy", "slow", root=tmp_path, timeout=0.4) == 4


def test_ask_agy_auth_blocked_fails(
    fake_agy_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_AGY_RUN_AUTH_BLOCK", "1")
    result = run_ask("agy", "auth", root=tmp_path, timeout=5.0)
    assert result.exit_code != 0
    assert result.exit_code != 4
    body = result.artifact.read_text(encoding="utf-8")
    assert "auth_blocked" in body or "sign" in body.lower() or "log" in body.lower()
    # Must not report success exit in meta.
    if result.meta and result.meta.is_file():
        meta = json.loads(result.meta.read_text(encoding="utf-8"))
        assert meta["exit_code"] != 0
    assert run_ask_cli("agy", "auth", root=tmp_path, timeout=5.0) == 1


def test_ask_agy_missing_binary_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.delenv("OMG_AGY_BIN", raising=False)
    (tmp_path / "empty-bin").mkdir(parents=True, exist_ok=True)
    with pytest.raises(AskProviderMissing):
        run_ask("agy", "hi", root=tmp_path, dry_run=False)
    assert run_ask_cli("agy", "hi", root=tmp_path, dry_run=False) == 3


def test_ask_agy_dry_run_no_exec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = {"n": 0}

    def boom(_self, _req):  # noqa: ANN001
        ran["n"] += 1
        raise AssertionError("adapter.run must not run on dry-run")

    monkeypatch.setattr(
        "omg_cli.providers.antigravity.AntigravityProvider.run", boom
    )
    result = run_ask(
        "agy", "preview", root=tmp_path, dry_run=True, check_binary=False
    )
    assert result.dry_run is True
    assert result.exit_code == 0
    assert ran["n"] == 0
    assert result.argv[-1] == "--print=preview"
    assert sum(arg.startswith("--print=") for arg in result.argv) == 1


def test_ask_agy_prompt_is_single_print_assignment(
    fake_agy_path: Path, tmp_path: Path
) -> None:
    """Injection regression: leading-dash prompt stays one argv assignment."""
    evil = "--danger a; rm -rf / # && echo pwned"
    result = run_ask("agy", evil, root=tmp_path, timeout=5.0)
    assert result.exit_code == 0
    assert result.argv[-1] == f"--print={evil}"
    assert sum(arg.startswith("--print=") for arg in result.argv) == 1
    assert evil not in result.argv
    assert "--" not in result.argv
    assert all(isinstance(a, str) for a in result.argv)


def test_legacy_providers_unchanged_still_normalize() -> None:
    assert normalize_provider("codex") == "codex"
    assert normalize_provider("claude") == "claude"
    assert normalize_provider("gemini") == "gemini"
    assert normalize_provider("fable") == "claude"


def test_ask_agy_records_advisor_route(tmp_path: Path) -> None:
    result = run_ask(
        "agy",
        "route",
        root=tmp_path,
        dry_run=True,
        check_binary=False,
    )
    assert result.advisor_route is not None
    assert result.advisor_route["provider"] == "agy"
    assert result.advisor_route["authoritative"] is False


def test_ask_agy_propagates_broker_child_env(
    fake_agy_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Live adapter path must pass OMG_ALLOW_EXTERNAL_CLI / OMG_ASK_BROKER.

    Dry-run already prints child_env_keys; live must feed the same markers
    through ProviderRunRequest.env into the bounded child process.
    """
    from omg_cli.providers import antigravity as agy_mod

    # Parent must not already carry the allow key (broker sets it only on child).
    monkeypatch.delenv("OMG_ALLOW_EXTERNAL_CLI", raising=False)
    monkeypatch.delenv("OMG_ASK_BROKER", raising=False)
    monkeypatch.setenv(
        "FAKE_AGY_ECHO_ENV", "OMG_ALLOW_EXTERNAL_CLI,OMG_ASK_BROKER"
    )

    seen: list[object] = []
    real_run = agy_mod.AntigravityProvider.run

    def tracking_run(self, request):  # noqa: ANN001
        seen.append(request)
        return real_run(self, request)

    monkeypatch.setattr(agy_mod.AntigravityProvider, "run", tracking_run)

    result = run_ask("agy", "env-probe", root=tmp_path, timeout=5.0)
    assert result.exit_code == 0
    assert seen, "adapter.run must be called"
    req = seen[0]
    assert req.env is not None
    assert req.env.get("OMG_ALLOW_EXTERNAL_CLI") == "1"
    assert req.env.get("OMG_ASK_BROKER") == "1"
    # Markers must reach the fake child (bounded allowlist), not stay request-only.
    assert "env.OMG_ALLOW_EXTERNAL_CLI=1" in result.artifact.read_text(encoding="utf-8")
    assert "env.OMG_ASK_BROKER=1" in result.artifact.read_text(encoding="utf-8")
    # Parent unchanged.
    assert "OMG_ALLOW_EXTERNAL_CLI" not in os.environ
    assert "OMG_ASK_BROKER" not in os.environ
