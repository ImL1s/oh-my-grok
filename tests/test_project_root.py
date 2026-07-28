"""#22 canonical project-root discovery."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omg_cli.project_root import (
    ENV_PROJECT_ROOT,
    ProjectRootError,
    clear_resolved_project_root,
    git_toplevel,
    project_root,
    resolve_project_root,
    set_resolved_project_root,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.email", "t@t")
    _git(path, "config", "user.name", "t")
    _git(path, "config", "commit.gpgsign", "false")
    (path / "README.md").write_text("x\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "i")


def test_explicit_wins_over_env_and_omg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clear_resolved_project_root()
    repo = tmp_path / "repo"
    _init_git(repo)
    (repo / ".omg").mkdir()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv(ENV_PROJECT_ROOT, str(repo))
    res = resolve_project_root(
        cwd=repo / "nested",
        explicit=other,
        env={ENV_PROJECT_ROOT: str(repo)},
    )
    assert res.root == other.resolve()
    assert res.source == "explicit"


def test_env_wins_over_omg(tmp_path: Path) -> None:
    clear_resolved_project_root()
    repo = tmp_path / "repo"
    _init_git(repo)
    (repo / ".omg").mkdir()
    override = tmp_path / "override"
    override.mkdir()
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)
    res = resolve_project_root(
        cwd=nested,
        env={ENV_PROJECT_ROOT: str(override)},
    )
    assert res.root == override.resolve()
    assert res.source == "env"


def test_nearest_omg_from_nested_child(tmp_path: Path) -> None:
    clear_resolved_project_root()
    repo = tmp_path / "repo"
    _init_git(repo)
    (repo / ".omg").mkdir()
    deep = repo / "pkg" / "app" / "src"
    deep.mkdir(parents=True)
    res = resolve_project_root(cwd=deep, env={})
    assert res.root == repo.resolve()
    assert res.source == "omg"


def test_nearest_omg_beats_farther_git_when_nested_omg(
    tmp_path: Path,
) -> None:
    """pkg/.omg wins over repo root git when cwd is under pkg."""
    clear_resolved_project_root()
    repo = tmp_path / "repo"
    _init_git(repo)
    (repo / ".omg").mkdir()
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / ".omg").mkdir()
    deep = pkg / "src"
    deep.mkdir()
    res = resolve_project_root(cwd=deep, env={})
    assert res.root == pkg.resolve()
    assert res.source == "omg"
    assert repo.resolve() in res.shadowed_omg_ancestors


def test_git_toplevel_when_no_omg(tmp_path: Path) -> None:
    clear_resolved_project_root()
    repo = tmp_path / "repo"
    _init_git(repo)
    deep = repo / "sub" / "x"
    deep.mkdir(parents=True)
    res = resolve_project_root(cwd=deep, env={})
    assert res.root == repo.resolve()
    assert res.source == "git"
    assert git_toplevel(deep) == repo.resolve()


def test_cwd_fallback_non_git(tmp_path: Path) -> None:
    clear_resolved_project_root()
    plain = tmp_path / "plain"
    plain.mkdir()
    res = resolve_project_root(cwd=plain, env={})
    assert res.root == plain.resolve()
    assert res.source == "cwd"


def test_here_forces_cwd(tmp_path: Path) -> None:
    clear_resolved_project_root()
    repo = tmp_path / "repo"
    _init_git(repo)
    (repo / ".omg").mkdir()
    deep = repo / "nested"
    deep.mkdir()
    res = resolve_project_root(cwd=deep, here=True, env={})
    assert res.root == deep.resolve()
    assert res.source == "here"


def test_invalid_explicit_fails(tmp_path: Path) -> None:
    with pytest.raises(ProjectRootError, match="does not exist"):
        resolve_project_root(cwd=tmp_path, explicit=tmp_path / "missing")


def test_invalid_env_fails(tmp_path: Path) -> None:
    with pytest.raises(ProjectRootError, match="does not exist"):
        resolve_project_root(
            cwd=tmp_path,
            env={ENV_PROJECT_ROOT: str(tmp_path / "nope")},
        )


def test_process_cache_used_by_project_root(tmp_path: Path) -> None:
    clear_resolved_project_root()
    a = tmp_path / "a"
    a.mkdir()
    res = resolve_project_root(cwd=a, env={})
    set_resolved_project_root(res)
    assert project_root() == a.resolve()
    clear_resolved_project_root()


def test_cli_nested_state_uses_same_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration: note from nested dir lands under repo .omg."""
    from omg_cli.main import main

    clear_resolved_project_root()
    repo = tmp_path / "repo"
    _init_git(repo)
    deep = repo / "packages" / "app"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    # Clear env override if host has one
    monkeypatch.delenv(ENV_PROJECT_ROOT, raising=False)
    code = main(["setup", "--no-global-rules", "--no-global-hook"])
    assert code == 0
    assert (repo / ".omg").is_dir()
    assert not (deep / ".omg").exists()

    code = main(["note", "hello-from-nested"])
    assert code == 0
    notes = list((repo / ".omg").rglob("notepad.md"))
    assert notes, "expected notepad under repo .omg"
    assert "hello-from-nested" in notes[0].read_text(encoding="utf-8")


def test_cli_project_root_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from omg_cli.main import main

    clear_resolved_project_root()
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.chdir(a)
    monkeypatch.delenv(ENV_PROJECT_ROOT, raising=False)
    code = main(
        [
            "--project-root",
            str(b),
            "setup",
            "--no-global-rules",
            "--no-global-hook",
        ]
    )
    assert code == 0
    assert (b / ".omg").is_dir()
    assert not (a / ".omg").exists()


def test_cli_bad_project_root_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omg_cli.main import main

    clear_resolved_project_root()
    monkeypatch.chdir(tmp_path)
    code = main(
        [
            "--project-root",
            str(tmp_path / "missing"),
            "state",
        ]
    )
    assert code == 2


def test_install_scoped_ignores_stale_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """install-hook must not fail solely because OMG_PROJECT_ROOT is invalid."""
    from omg_cli.main import main

    clear_resolved_project_root()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ENV_PROJECT_ROOT, str(tmp_path / "missing-root"))
    monkeypatch.setattr(
        "omg_cli.hook_install.main",
        lambda argv=None: 0,
    )
    code = main(["install-hook"])
    assert code == 0
    # Project-scoped command still fails closed on the same env.
    code2 = main(["state"])
    assert code2 == 2
