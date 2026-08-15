"""#74 PR1 — canonical state-root contract (pure resolver, no writer cutover)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from omg_cli.project_root import (
    ENV_PROJECT_ROOT,
    clear_resolved_project_root,
    resolve_project_root,
)
from omg_cli.state_root import (
    ENV_DISABLE_WORKSPACE_MARKER,
    ENV_STATE_DIR,
    ENV_WORKSPACE_MARKER,
    STATE_ROOT_SCHEMA_VERSION,
    StateRootError,
    reject_marker_stat,
    resolve_state_root,
)

SECRET = "sekrit-TOKEN-xyz74"


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


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return home.resolve()


def _fingerprint(root: Path) -> list[tuple[str, int, int, int, int]]:
    rows: list[tuple[str, int, int, int, int]] = []
    for path in sorted(root.rglob("*")):
        st = path.lstat()
        rows.append(
            (
                str(path.relative_to(root)),
                st.st_ino,
                st.st_mtime_ns,
                st.st_size,
                st.st_nlink,
            )
        )
    return rows


def _public_text(res) -> str:
    return res.serialize().decode("utf-8") + json.dumps(res.to_public_dict())


def test_default_per_worktree_from_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    nested = repo / "pkg" / "src"
    nested.mkdir(parents=True)
    res = resolve_state_root(cwd=nested, env={}, home=_home(tmp_path))
    assert res.scope == "per_worktree"
    assert res.source == "project_derived"
    assert res.project_root == repo.resolve()
    assert res.state_dir == repo.resolve() / ".omg"
    assert res.diagnostics["project_root_source"] == "git"
    assert res.diagnostics["authority"] == "none"
    assert res.schema_version == STATE_ROOT_SCHEMA_VERSION


def test_nearest_omg_beats_git_for_project_identity(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    (repo / ".omg").mkdir()
    pkg = repo / "pkg"
    pkg.mkdir()
    (pkg / ".omg").mkdir()
    src = pkg / "src"
    src.mkdir()
    res = resolve_state_root(cwd=src, env={}, home=_home(tmp_path))
    assert res.project_root == pkg.resolve()
    assert res.diagnostics["project_root_source"] == "omg"
    assert res.scope == "per_worktree"


def test_non_git_cwd_fallback(tmp_path: Path) -> None:
    plain = tmp_path / "plain" / "nested"
    plain.mkdir(parents=True)
    res = resolve_state_root(cwd=plain, env={}, home=_home(tmp_path))
    assert res.project_root == plain.resolve()
    assert res.diagnostics["project_root_source"] == "cwd"
    assert res.state_dir == plain.resolve() / ".omg"


def test_explicit_and_env_project_root_preserved(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    (repo / ".omg").mkdir()
    other = tmp_path / "other"
    other.mkdir()
    nested = repo / "nested"
    nested.mkdir()
    home = _home(tmp_path)
    explicit = resolve_state_root(
        cwd=nested,
        explicit_project_root=other,
        env={ENV_PROJECT_ROOT: str(repo)},
        home=home,
    )
    assert explicit.project_root == other.resolve()
    assert explicit.diagnostics["project_root_source"] == "explicit"
    env_res = resolve_state_root(
        cwd=nested,
        env={ENV_PROJECT_ROOT: str(other)},
        home=home,
    )
    assert env_res.project_root == other.resolve()
    assert env_res.diagnostics["project_root_source"] == "env"


def test_relocated_nested_cwd_same_key(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    deep = repo / "a" / "b"
    deep.mkdir(parents=True)
    home = _home(tmp_path)
    a = resolve_state_root(cwd=repo, env={}, home=home)
    b = resolve_state_root(cwd=deep, env={}, home=home)
    assert a.project_key == b.project_key
    assert a.project_root == b.project_root == repo.resolve()
    assert a.serialize() == b.serialize()


def test_marker_disabled_by_default_even_when_present(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".omg-workspace").write_text('{"version":1}', encoding="utf-8")
    repo = ws / "repo"
    repo.mkdir()
    res = resolve_state_root(cwd=repo, env={}, home=_home(tmp_path))
    assert res.scope == "per_worktree"
    assert res.diagnostics["marker"] == "disabled"
    assert res.state_dir == repo.resolve() / ".omg"


def test_marker_enabled_nearest_wins(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "inner"
    repo = inner / "repo"
    repo.mkdir(parents=True)
    (outer / ".omg-workspace").write_text('{"version":1}', encoding="utf-8")
    (inner / ".omg-workspace").write_text('{"version":1}', encoding="utf-8")
    res = resolve_state_root(
        cwd=repo,
        env={ENV_WORKSPACE_MARKER: "1"},
        home=_home(tmp_path),
    )
    assert res.scope == "workspace_shared"
    assert res.source == "workspace_marker"
    assert res.state_dir == inner.resolve() / ".omg"
    assert res.diagnostics["marker"] == "used"
    assert res.diagnostics["identity_kind"] == "workspace"


def test_marker_kill_switch_wins_over_env_and_api(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    repo = ws / "repo"
    repo.mkdir(parents=True)
    (ws / ".omg-workspace").write_text('{"version":1}', encoding="utf-8")
    home = _home(tmp_path)
    killed = resolve_state_root(
        cwd=repo,
        env={ENV_WORKSPACE_MARKER: "1", ENV_DISABLE_WORKSPACE_MARKER: "1"},
        home=home,
    )
    assert killed.scope == "per_worktree"
    assert killed.diagnostics["marker"] == "killed"
    api_killed = resolve_state_root(
        cwd=repo,
        env={ENV_DISABLE_WORKSPACE_MARKER: "true"},
        enable_workspace_marker=True,
        home=home,
    )
    assert api_killed.scope == "per_worktree"


def test_centralized_env_wins_over_marker(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    repo = ws / "repo"
    repo.mkdir(parents=True)
    (ws / ".omg-workspace").write_text('{"version":1}', encoding="utf-8")
    central = tmp_path / "central"
    central.mkdir()
    res = resolve_state_root(
        cwd=repo,
        env={ENV_STATE_DIR: str(central), ENV_WORKSPACE_MARKER: "1"},
        home=_home(tmp_path),
    )
    assert res.scope == "centralized"
    assert res.source == "centralized_env"
    assert res.project_root == repo.resolve()
    assert res.state_dir.parent == central.resolve()
    assert res.state_dir.name == res.project_key


def test_centralized_sibling_projects_isolated(tmp_path: Path) -> None:
    central = tmp_path / "central"
    central.mkdir()
    a = tmp_path / "proj-a"
    b = tmp_path / "proj-b"
    a.mkdir()
    b.mkdir()
    home = _home(tmp_path)
    ra = resolve_state_root(cwd=a, env={ENV_STATE_DIR: str(central)}, home=home)
    rb = resolve_state_root(cwd=b, env={ENV_STATE_DIR: str(central)}, home=home)
    assert ra.project_key != rb.project_key
    assert ra.state_dir != rb.state_dir
    assert ra.state_dir.parent == rb.state_dir.parent == central.resolve()


def test_centralized_nested_projects_under_one_git_repo_stay_isolated(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    a = repo / "pkg-a"
    b = repo / "pkg-b"
    for project in (a, b):
        project.mkdir()
        (project / ".omg").mkdir()
    central = tmp_path / "central"
    central.mkdir()
    env = {ENV_STATE_DIR: str(central)}
    home = _home(tmp_path)

    discovered_a = resolve_state_root(cwd=a, env=env, home=home)
    discovered_b = resolve_state_root(cwd=b, env=env, home=home)
    assert discovered_a.project_root == a.resolve()
    assert discovered_b.project_root == b.resolve()
    assert discovered_a.diagnostics["identity_kind"] == "project_root"
    assert discovered_b.diagnostics["identity_kind"] == "project_root"
    assert discovered_a.project_key != discovered_b.project_key
    assert discovered_a.state_dir != discovered_b.state_dir

    here_a = resolve_state_root(cwd=a, env=env, home=home, here=True)
    here_b = resolve_state_root(cwd=b, env=env, home=home, here=True)
    assert here_a.diagnostics["identity_kind"] == "project_root"
    assert here_b.diagnostics["identity_kind"] == "project_root"
    assert here_a.project_key != here_b.project_key


def test_linked_worktrees_distinct_per_worktree_shared_when_central(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main"
    _init_git(main)
    wt = tmp_path / "linked"
    _git(main, "worktree", "add", str(wt), "-b", "feat")
    home = _home(tmp_path)
    per_a = resolve_state_root(cwd=main, env={}, home=home)
    per_b = resolve_state_root(cwd=wt, env={}, home=home)
    assert per_a.project_root != per_b.project_root
    assert per_a.project_key != per_b.project_key
    assert per_a.state_dir == main.resolve() / ".omg"
    assert per_b.state_dir == wt.resolve() / ".omg"

    central = tmp_path / "central"
    central.mkdir()
    env = {ENV_STATE_DIR: str(central)}
    ca = resolve_state_root(cwd=main, env=env, home=home)
    cb = resolve_state_root(cwd=wt, env=env, home=home)
    assert ca.scope == cb.scope == "centralized"
    assert ca.diagnostics["identity_kind"] == "git_common"
    assert ca.project_key == cb.project_key
    assert ca.state_dir == cb.state_dir
    assert ca.project_root != cb.project_root


def test_home_and_filesystem_root_not_implicit_state(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with pytest.raises(StateRootError, match="implicit"):
        resolve_state_root(cwd=home, env={}, home=home)
    nested = home / "nested"
    nested.mkdir()
    (home / ".omg").mkdir()
    with pytest.raises(StateRootError, match="implicit"):
        resolve_state_root(cwd=nested, env={}, home=home)
    with pytest.raises(StateRootError, match="implicit"):
        resolve_state_root(cwd=Path("/"), env={}, home=home)


def test_explicit_home_override_allowed_and_redacted(tmp_path: Path) -> None:
    home = tmp_path / f"home-{SECRET}"
    home.mkdir()
    start = tmp_path / "start"
    start.mkdir()
    res = resolve_state_root(
        cwd=start,
        explicit_project_root=home,
        env={},
        home=home,
    )
    assert res.project_root == home.resolve()
    assert res.diagnostics["home_scope"] == "explicit_override"
    public = _public_text(res)
    assert SECRET not in public
    assert str(home.resolve()) not in public
    assert str(home) not in public


def test_marker_malformed_future_bool_duplicate_unknown_escape(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    repo = ws / "repo"
    repo.mkdir(parents=True)
    marker = ws / ".omg-workspace"
    home = _home(tmp_path)
    env = {ENV_WORKSPACE_MARKER: "1"}

    marker.write_text("not-json", encoding="utf-8")
    with pytest.raises(StateRootError, match="malformed"):
        resolve_state_root(cwd=repo, env=env, home=home)

    marker.write_text('{"version":2}', encoding="utf-8")
    with pytest.raises(StateRootError, match="unsupported"):
        resolve_state_root(cwd=repo, env=env, home=home)

    marker.write_text('{"version":true}', encoding="utf-8")
    with pytest.raises(StateRootError, match="integer"):
        resolve_state_root(cwd=repo, env=env, home=home)

    marker.write_text('{"version":1,"version":1}', encoding="utf-8")
    with pytest.raises(StateRootError, match="duplicate"):
        resolve_state_root(cwd=repo, env=env, home=home)

    marker.write_text('{"version":1,"root":"/tmp/evil"}', encoding="utf-8")
    with pytest.raises(StateRootError, match="unknown"):
        resolve_state_root(cwd=repo, env=env, home=home)

    marker.write_text('{"version":1,"root":"../../etc"}', encoding="utf-8")
    with pytest.raises(StateRootError, match="unknown"):
        resolve_state_root(cwd=repo, env=env, home=home)


def test_marker_symlink_hardlink_fifo_socket(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    repo = ws / "repo"
    repo.mkdir(parents=True)
    home = _home(tmp_path)
    env = {ENV_WORKSPACE_MARKER: "1"}
    marker = ws / ".omg-workspace"

    target = tmp_path / "target.json"
    target.write_text('{"version":1}', encoding="utf-8")
    marker.symlink_to(target)
    with pytest.raises(StateRootError, match="symlink"):
        resolve_state_root(cwd=repo, env=env, home=home)
    marker.unlink()

    real = tmp_path / "real.json"
    real.write_text('{"version":1}', encoding="utf-8")
    os.link(real, marker)
    with pytest.raises(StateRootError, match="hardlink"):
        resolve_state_root(cwd=repo, env=env, home=home)
    marker.unlink()

    os.mkfifo(marker)
    with pytest.raises(StateRootError, match="FIFO"):
        resolve_state_root(cwd=repo, env=env, home=home)
    marker.unlink()

    marker.mkdir()
    with pytest.raises(StateRootError, match="regular file"):
        resolve_state_root(cwd=repo, env=env, home=home)


def test_workspace_state_directory_symlink_is_rejected(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    repo = ws / "repo"
    repo.mkdir(parents=True)
    (ws / ".omg-workspace").write_text('{"version":1}', encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (ws / ".omg").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StateRootError, match="workspace state directory.*symlink"):
        resolve_state_root(
            cwd=repo,
            env={ENV_WORKSPACE_MARKER: "1"},
            home=_home(tmp_path),
        )


def test_reject_marker_device_and_socket_stat() -> None:
    device = os.stat_result((stat.S_IFCHR | 0o666, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    with pytest.raises(StateRootError, match="device"):
        reject_marker_stat(device)
    sock = os.stat_result((stat.S_IFSOCK | 0o666, 0, 0, 1, 0, 0, 0, 0, 0, 0))
    with pytest.raises(StateRootError, match="socket"):
        reject_marker_stat(sock)


def test_marker_must_not_select_home(tmp_path: Path) -> None:
    home = _home(tmp_path)
    nested = home / "nested"
    nested.mkdir()
    (home / ".omg-workspace").write_text('{"version":1}', encoding="utf-8")
    with pytest.raises(StateRootError, match="HOME or filesystem root"):
        resolve_state_root(
            cwd=nested,
            env={ENV_WORKSPACE_MARKER: "1"},
            home=home,
        )


def test_central_symlink_leaf_and_ancestor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = _home(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    leaf = tmp_path / "central-leaf"
    leaf.symlink_to(target)
    with pytest.raises(StateRootError, match="leaf may not be a symlink"):
        resolve_state_root(cwd=repo, env={ENV_STATE_DIR: str(leaf)}, home=home)

    outside = tmp_path / "outside"
    outside.mkdir()
    parent = tmp_path / "parent-link"
    parent.symlink_to(outside)
    planned = parent / "state"
    with pytest.raises(StateRootError, match="ancestor may not be a symlink"):
        resolve_state_root(cwd=repo, env={ENV_STATE_DIR: str(planned)}, home=home)


def test_no_filesystem_mutation_or_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    nested = repo / "nested"
    nested.mkdir()
    home = _home(tmp_path)
    central = tmp_path / "does-not-exist-central"
    before = _fingerprint(tmp_path)

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("subprocess forbidden in state-root resolver")

    monkeypatch.setattr("subprocess.run", boom)
    monkeypatch.setattr("subprocess.Popen", boom)
    monkeypatch.setattr("omg_cli.project_root.subprocess.run", boom)

    orig_mkdir = os.mkdir

    def no_mkdir(*_a: object, **_k: object) -> None:
        raise AssertionError("mkdir forbidden")

    monkeypatch.setattr(os, "mkdir", no_mkdir)
    monkeypatch.setattr(os, "makedirs", no_mkdir)

    resolve_state_root(cwd=nested, env={}, home=home)
    resolve_state_root(
        cwd=nested, env={ENV_STATE_DIR: str(central)}, home=home
    )
    resolve_state_root(
        cwd=nested,
        env={ENV_WORKSPACE_MARKER: "1"},
        home=home,
    )
    assert _fingerprint(tmp_path) == before
    monkeypatch.setattr(os, "mkdir", orig_mkdir)


def test_no_network_sockets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    repo = tmp_path / "repo"
    repo.mkdir()
    home = _home(tmp_path)
    central = tmp_path / "central"
    central.mkdir()

    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("network forbidden in state-root resolver")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)

    resolve_state_root(cwd=repo, env={}, home=home)
    resolve_state_root(cwd=repo, env={ENV_STATE_DIR: str(central)}, home=home)


def test_opaque_keys_and_deterministic_public_bytes(tmp_path: Path) -> None:
    repo = tmp_path / f"repo-{SECRET}"
    _init_git(repo)
    home = tmp_path / f"home-{SECRET}"
    home.mkdir()
    central = tmp_path / f"central-{SECRET}"
    central.mkdir()
    env = {ENV_STATE_DIR: str(central), "API_TOKEN": SECRET}
    a = resolve_state_root(cwd=repo, env=env, home=home)
    b = resolve_state_root(cwd=repo, env=env, home=home)
    assert a.serialize() == b.serialize()
    assert a.project_key == b.project_key
    assert len(a.project_key) == 64
    assert a.project_key.isalnum()
    public = _public_text(a)
    assert SECRET not in public
    assert str(repo.resolve()) not in public
    assert str(home.resolve()) not in public
    assert str(central.resolve()) not in public
    assert "API_TOKEN" not in public
    for part in repo.resolve().parts:
        if len(part) > 3:
            assert part not in a.project_key


def test_diagnostics_omit_private_path_sentinels(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    home = _home(tmp_path)
    res = resolve_state_root(cwd=repo, env={}, home=home)
    dumped = json.dumps(res.to_public_dict())
    assert str(tmp_path) not in dumped
    assert str(repo) not in dumped
    assert str(home) not in dumped
    assert "HOME=" not in dumped
    real_home = str(Path.home())
    if real_home not in str(tmp_path):
        assert real_home not in dumped


def test_state_authority_module_not_imported() -> None:
    import omg_cli.state_root as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert "omg_cli.state" not in source
    assert "set_verified" not in source
    assert "command_registry" not in source
    assert module.resolve_state_root is resolve_state_root


def test_existing_project_root_resolver_unchanged(tmp_path: Path) -> None:
    clear_resolved_project_root()
    repo = tmp_path / "repo"
    _init_git(repo)
    (repo / ".omg").mkdir()
    nested = repo / "nested"
    nested.mkdir()
    legacy = resolve_project_root(cwd=nested, env={})
    assert legacy.root == repo.resolve()
    assert legacy.source == "omg"
    state = resolve_state_root(cwd=nested, env={}, home=_home(tmp_path))
    assert state.project_root == legacy.root


def test_state_root_ignores_unrelated_ancestor_omg(tmp_path: Path) -> None:
    """Leftover parent/.omg must not become state identity for a git child."""
    parent = tmp_path / "tmp"
    parent.mkdir()
    (parent / ".omg").mkdir()
    repo = parent / "omg-live-xxx"
    _init_git(repo)
    res = resolve_state_root(cwd=repo, env={}, home=_home(tmp_path))
    assert res.project_root == repo.resolve()
    assert res.diagnostics["project_root_source"] == "git"


def test_here_uses_cwd_and_does_not_write(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git(repo)
    (repo / ".omg").mkdir()
    deep = repo / "nested"
    deep.mkdir()
    home = _home(tmp_path)
    before = _fingerprint(tmp_path)
    res = resolve_state_root(cwd=deep, here=True, env={}, home=home)
    assert res.project_root == deep.resolve()
    assert res.diagnostics["project_root_source"] == "here"
    assert res.scope == "per_worktree"
    assert _fingerprint(tmp_path) == before


def test_per_worktree_symlink_state_dir_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (repo / ".omg").symlink_to(outside)
    with pytest.raises(StateRootError, match="symlink"):
        resolve_state_root(cwd=repo, env={}, home=_home(tmp_path))


def test_central_filesystem_root_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(StateRootError, match="filesystem root"):
        resolve_state_root(cwd=repo, env={ENV_STATE_DIR: "/"}, home=_home(tmp_path))


def test_disabled_marker_malformed_is_ignored(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    repo = ws / "repo"
    repo.mkdir(parents=True)
    (ws / ".omg-workspace").write_text("NOPE", encoding="utf-8")
    res = resolve_state_root(cwd=repo, env={}, home=_home(tmp_path))
    assert res.scope == "per_worktree"


def test_resolver_does_not_touch_sys_modules_for_state(tmp_path: Path) -> None:
    before = {name for name in sys.modules if name.startswith("omg_cli.state")}
    resolve_state_root(cwd=tmp_path, env={}, home=_home(tmp_path))
    after = {name for name in sys.modules if name.startswith("omg_cli.state")}
    assert after <= before | {"omg_cli.state_root"}
