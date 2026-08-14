"""#146 PR3: isolated installed-hook + bare ``omg team`` CLI smoke.

Proves the generated hook installed into a temporary Grok home, plus the
real OMG CLI on PATH (basename ``omg``, not an absolute rewrite), agree:

- leader PreToolUse allows bare / path-prefixed first-party Team;
- foreign CLIs stay denied;
- worker nested launch is refused by the runtime with zero side effects;
- identity-bound ``omg team api`` is not labeled an external agent CLI.

No paid provider is invoked. Dry-run / catalog is enough to prove routing.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from omg_cli.hook_install import (
    STANDALONE_BASENAME,
    committed_standalone,
    install_global_hook,
    render_hook_json,
)
from omg_cli.team.plane import EXPERIMENTAL_ENV, TEAM_WORKER_ENV

ROOT = Path(__file__).resolve().parents[1]


def _payload(command: str, *, tool_name: str = "run_terminal_command") -> str:
    return json.dumps({"tool_name": tool_name, "tool_input": {"command": command}})


def _run_installed(
    script: Path,
    payload: str,
    *,
    env_extra: dict[str, str] | None = None,
) -> tuple[int, dict]:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(
        [sys.executable, "-I", "-S", str(script)],
        input=payload,
        capture_output=True,
        text=True,
        cwd=os.path.dirname(script) if script.parent.is_dir() else None,
        env=env,
        timeout=15,
    )
    body = json.loads((proc.stdout or "").strip() or "{}")
    return proc.returncode, body


def _install_hook(grok_home: Path) -> Path:
    """Publish the committed standalone into an isolated Grok home.

    Prefer the real installer (stage → python3 smoke → publish). When ``python3``
    is absent (Windows contributors), copy the same committed bytes the installer
    would publish and smoke them with this interpreter — the payload under test
    is still the generated standalone, not a hand-authored hook.
    """
    _json_path, action = install_global_hook(home=grok_home)
    script = grok_home / "hooks" / STANDALONE_BASENAME
    if action in ("created", "updated", "repaired", "unchanged", "migrated") and script.is_file():
        return script
    src = committed_standalone()
    hooks = grok_home / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    script.write_bytes(src.read_bytes())
    (hooks / "omg-pretool-deny.json").write_text(
        render_hook_json(script), encoding="utf-8"
    )
    rc, body = _run_installed(script, _payload("ls"))
    assert rc == 0 and body.get("decision") == "allow", (action, body)
    rc, body = _run_installed(script, _payload("claude -p x"))
    assert rc == 0 and body.get("decision") == "deny", (action, body)
    return script


def _omg_cmd(bin_dir: Path, env: dict[str, str]) -> str:
    found = shutil.which("omg", path=env.get("PATH"))
    if found:
        return found
    if os.name == "nt" and (bin_dir / "omg.cmd").is_file():
        return str(bin_dir / "omg.cmd")
    return str(bin_dir / "omg")


def _write_path_omg(bin_dir: Path) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "omg"
    script.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import sys",
                f"sys.path.insert(0, {str(ROOT)!r})",
                "from omg_cli.main import main",
                "raise SystemExit(main())",
                "",
            ]
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    if os.name == "nt":
        (bin_dir / "omg.cmd").write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )


def _git_init(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "omg-test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "omg-test"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=path,
        check=True,
        capture_output=True,
    )


def _cli_env(
    *,
    bin_dir: Path,
    grok_home: Path,
    home: Path,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    env["GROK_HOME"] = str(grok_home)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(ROOT)
    env[EXPERIMENTAL_ENV] = "1"
    env.pop("OMG_DISABLE_TMUX_TEAM", None)
    env.pop("OMG_ALLOW_EXTERNAL_CLI", None)
    env.pop(TEAM_WORKER_ENV, None)
    env.pop("OMG_TEAM_WORKER_ID", None)
    if extra:
        env.update(extra)
    return env


def test_installed_hook_allows_bare_omg_team_and_denies_foreign(tmp_path: Path) -> None:
    grok_home = tmp_path / "grok"
    script = _install_hook(grok_home)

    allow_cmds = (
        'omg team 2:executor "fix tests"',
        "omg team launch --workers 2 --goal x",
        "/opt/omg/bin/omg team status r",
        "command env /opt/omg/bin/omg team api get-summary",
        "bash -lc 'omg team launch --goal x'",
    )
    for cmd in allow_cmds:
        rc, body = _run_installed(script, _payload(cmd))
        assert rc == 0, cmd
        assert body.get("decision") == "allow", (cmd, body)

    deny_cmds = (
        "claude -p hi",
        "codex exec y",
        "omc team 2:codex x",
        "/usr/bin/omc team x",
        "agy --version",
        "cursor-agent -p x",
        "kimi --version",
    )
    for cmd in deny_cmds:
        rc, body = _run_installed(script, _payload(cmd))
        assert rc == 0, cmd
        assert body.get("decision") == "deny", (cmd, body)

    rc, body = _run_installed(
        script,
        _payload("omg team launch --goal x"),
        env_extra={TEAM_WORKER_ENV: "1"},
    )
    assert rc == 0
    assert body.get("decision") == "deny"
    assert "E_TEAM_NESTED_LAUNCH" in str(body.get("reason") or "")
    assert "omg ask" not in str(body.get("reason") or "")

    rc, body = _run_installed(
        script,
        _payload("omg team api claim-task"),
        env_extra={TEAM_WORKER_ENV: "1", "OMG_TEAM_WORKER_ID": "w1"},
    )
    assert rc == 0
    assert body.get("decision") == "allow", body


def test_bare_path_omg_reaches_team_parser_without_absolute_rewrite(
    tmp_path: Path,
) -> None:
    grok_home = tmp_path / "grok"
    _install_hook(grok_home)
    bin_dir = tmp_path / "bin"
    _write_path_omg(bin_dir)
    repo = tmp_path / "proj"
    _git_init(repo)

    env = _cli_env(bin_dir=bin_dir, grok_home=grok_home, home=tmp_path)
    omg = _omg_cmd(bin_dir, env)
    # Basename-only invocation — the original live failure mode was that the
    # model had to substitute an absolute ``omg`` after PreToolUse denied this.
    proc = subprocess.run(
        [omg, "team", "--help"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "external agent CLI" not in combined
    assert "team" in combined.lower()

    dry = subprocess.run(
        [
            omg,
            "team",
            "2:executor",
            "map ownership",
            "--dry-run",
            "--project-root",
            str(repo),
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    combined = (dry.stdout or "") + (dry.stderr or "")
    assert "external agent CLI" not in combined
    if os.name == "nt":
        # Team launch writes a managed store that requires POSIX dir_fd/O_NOFOLLOW.
        # PATH routing is already proven by ``omg team --help`` above.
        return
    assert dry.returncode == 0, combined
    assert "dry_run" in combined or "dry-run" in combined.lower()


def test_worker_nested_launch_runtime_zero_side_effects(tmp_path: Path) -> None:
    grok_home = tmp_path / "grok"
    _install_hook(grok_home)
    bin_dir = tmp_path / "bin"
    _write_path_omg(bin_dir)
    repo = tmp_path / "proj"
    _git_init(repo)
    before = {p.as_posix() for p in repo.rglob("*") if p.is_file()}

    env = _cli_env(
        bin_dir=bin_dir,
        grok_home=grok_home,
        home=tmp_path,
        extra={TEAM_WORKER_ENV: "1", "OMG_TEAM_WORKER_ID": "w1"},
    )
    omg = _omg_cmd(bin_dir, env)
    proc = subprocess.run(
        [
            omg,
            "team",
            "launch",
            "--workers",
            "1",
            "--goal",
            "x",
            "--dry-run",
            "--project-root",
            str(repo),
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode != 0, combined
    assert "E_TEAM_NESTED_LAUNCH" in combined
    assert "external agent CLI" not in combined
    after = {p.as_posix() for p in repo.rglob("*") if p.is_file()}
    assert after == before
    assert not (repo / ".omg" / "state" / "runs").exists()


def test_worker_api_not_mislabeled_external_cli(tmp_path: Path) -> None:
    grok_home = tmp_path / "grok"
    _install_hook(grok_home)
    bin_dir = tmp_path / "bin"
    _write_path_omg(bin_dir)
    repo = tmp_path / "proj"
    _git_init(repo)

    env = _cli_env(
        bin_dir=bin_dir,
        grok_home=grok_home,
        home=tmp_path,
        extra={TEAM_WORKER_ENV: "1", "OMG_TEAM_WORKER_ID": "w1"},
    )
    omg = _omg_cmd(bin_dir, env)
    proc = subprocess.run(
        [omg, "team", "api", "catalog", "--project-root", str(repo)],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    assert "external agent CLI" not in combined
    assert "omg ask" not in combined.lower() or "E_TEAM" in combined
    # Reaching runtime (catalog JSON or a typed Team error) is the gate;
    # identity-bound API must not be classified as an advisor CLI.
    assert proc.returncode in (0, 1, 2)
    if proc.returncode == 0:
        assert "schema" in combined.lower() or "catalog" in combined.lower() or "{" in combined
