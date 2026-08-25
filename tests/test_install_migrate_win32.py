"""#77 leftover: Windows no-follow import/migrate source reads.

Hermetic tests inject FakeWin32API and force the POSIX import/migrate
reader off. Do not set ``os.name='nt'`` on Darwin. Drive the real
``read_regular_nofollow`` / ``run_import`` / ``plan_migrate`` entries.
File copy is not live discovery; never asserts verified/observed/healthy.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omg_cli.install_migrate import (
    InstallMigrateError,
    _require_nofollow,
    plan_migrate,
    read_regular_nofollow,
    run_import,
)
from omg_cli.win32_nofollow import IO_REPARSE_TAG_MOUNT_POINT, windows_nofollow_ready
from tests.support.fake_win32 import FakeWin32API, assert_nofollow_flags as _assert_nofollow_flags

pytestmark = pytest.mark.platform

_SECRET = b"LEAKED-OUTSIDE-BYTES-NOT-FOR-IMPORT"
_SAFE = b"# imported skill\nsafe text\n"


@pytest.fixture
def fake_win32(monkeypatch: pytest.MonkeyPatch) -> FakeWin32API:
    if os.name == "nt":
        pytest.skip(
            "injected FakeWin32API is POSIX-backed; real Windows uses CtypesWin32API"
        )
    api = FakeWin32API()
    monkeypatch.setattr("omg_cli.win32_nofollow._API", api)
    monkeypatch.setattr("omg_cli.install_migrate._posix_nofollow_ready", lambda: False)
    monkeypatch.setattr(
        "omg_cli.contracts.path_keys._posix_confinement_ready", lambda: False
    )
    return api


def test_windows_read_regular_source_without_posix_nofollow(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_bytes(_SAFE)
    assert windows_nofollow_ready() is True
    assert read_regular_nofollow(skill) == _SAFE
    _assert_nofollow_flags(fake_win32)


def test_windows_import_reads_regular_source(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_bytes(_SAFE)
    result = run_import(skill, project_root=tmp_path, dry_run=True)
    assert result["ok"] is True
    assert result["verified"] is False
    assert result["observed"] is False
    assert result["healthy"] is False
    assert "passes" not in result
    assert result["rows"][0]["ownership"] == "imported"
    _assert_nofollow_flags(fake_win32)


def test_windows_import_writes_via_path_keys_atomic(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_bytes(_SAFE)
    result = run_import(skill, project_root=tmp_path, dry_run=False)
    assert result["ok"] is True
    assert result["verified"] is False
    assert result["observed"] is False
    assert result["healthy"] is False
    assert "passes" not in result
    target = Path(result["rows"][0]["target"])
    assert target.is_file()
    assert not target.is_symlink()
    assert target.read_bytes() == _SAFE
    assert ".omg/install/imported/" in target.as_posix()
    _assert_nofollow_flags(fake_win32)


def test_windows_backend_refuses_symlink_source(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    real = tmp_path / "SKILL.md"
    real.write_bytes(_SAFE)
    link = tmp_path / "link-skill.md"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    with pytest.raises(InstallMigrateError, match="E_SYMLINK"):
        read_regular_nofollow(link)
    with pytest.raises(InstallMigrateError, match="E_SYMLINK"):
        run_import(link, project_root=tmp_path, dry_run=True)
    assert real.read_bytes() == _SAFE
    _assert_nofollow_flags(fake_win32)


def test_windows_backend_refuses_reparse_source(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_bytes(_SAFE)
    fake_win32.forced_reparse[str(skill.resolve())] = (
        IO_REPARSE_TAG_MOUNT_POINT,
        False,
    )
    with pytest.raises(InstallMigrateError, match="E_SYMLINK"):
        read_regular_nofollow(skill)
    with pytest.raises(InstallMigrateError, match="E_SYMLINK"):
        run_import(skill, project_root=tmp_path, dry_run=True)
    assert skill.read_bytes() == _SAFE
    _assert_nofollow_flags(fake_win32)


def test_windows_read_refuses_symlink_ancestor(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    """Intermediate junctions must not be skipped by opening a deep parent."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_bytes(_SECRET)
    src = tmp_path / "src"
    src.mkdir()
    nested = src / "link"
    try:
        nested.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    source = nested / "SKILL.md"
    with pytest.raises(InstallMigrateError, match="E_SYMLINK"):
        read_regular_nofollow(source)
    with pytest.raises(InstallMigrateError, match="E_SYMLINK"):
        run_import(source, project_root=tmp_path, dry_run=True)
    assert (outside / "SKILL.md").read_bytes() == _SECRET
    _assert_nofollow_flags(fake_win32)


def test_windows_walk_refuses_symlink_child(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    tree = tmp_path / "legacy"
    tree.mkdir()
    (tree / "SKILL.md").write_bytes(_SAFE)
    outside = tmp_path / "outside.md"
    outside.write_bytes(_SECRET)
    nested = tree / "skills"
    nested.mkdir()
    try:
        (nested / "linked.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    with pytest.raises(InstallMigrateError, match="E_SYMLINK"):
        run_import(tree, project_root=tmp_path, dry_run=True)
    assert outside.read_bytes() == _SECRET


def test_windows_read_does_not_use_path_open(
    tmp_path: Path, fake_win32: FakeWin32API, monkeypatch: pytest.MonkeyPatch
) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_bytes(_SAFE)

    def _banned(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Path.open/read_bytes must not be used for source I/O")

    monkeypatch.setattr(Path, "open", _banned)
    monkeypatch.setattr(Path, "read_bytes", _banned)
    assert read_regular_nofollow(skill) == _SAFE


def test_posix_read_regular_nofollow_refuses_symlink(tmp_path: Path) -> None:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("POSIX O_NOFOLLOW path must stay selected when available")
    real = tmp_path / "SKILL.md"
    real.write_bytes(_SAFE)
    link = tmp_path / "link-skill.md"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    with pytest.raises(InstallMigrateError, match="E_SYMLINK"):
        read_regular_nofollow(link)
    with pytest.raises(InstallMigrateError, match="E_SYMLINK"):
        run_import(link, project_root=tmp_path, dry_run=True)


def test_posix_source_read_unchanged_when_win32_injected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("POSIX O_NOFOLLOW path must stay selected when available")
    api = FakeWin32API()
    monkeypatch.setattr("omg_cli.win32_nofollow._API", api)
    skill = tmp_path / "SKILL.md"
    skill.write_bytes(_SAFE)
    assert read_regular_nofollow(skill) == _SAFE
    assert not api.create_file_calls
    assert not api.nt_create_calls


def test_require_nofollow_accepts_injected_win32(fake_win32: FakeWin32API) -> None:
    assert windows_nofollow_ready() is True
    _require_nofollow()


def test_require_nofollow_without_backend_has_no_windows_leftover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omg_cli.install_migrate._posix_nofollow_ready", lambda: False)
    monkeypatch.setattr("omg_cli.win32_nofollow._API", None)

    def _boom() -> object:
        raise OSError("kernel32 missing")

    monkeypatch.setattr("omg_cli.win32_nofollow.CtypesWin32API", _boom)
    with pytest.raises(InstallMigrateError, match="E_PATH") as caught:
        _require_nofollow()
    assert "Windows leftover" not in str(caught.value)
    assert "Windows leftover" not in caught.value.message


def test_windows_migrate_reads_regular_source(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    agents = tmp_path / "AGENTS.md"
    agents.write_text("my personal notes\n", encoding="utf-8")
    planned = plan_migrate(tmp_path, project_root=tmp_path, grok_home=tmp_path)
    assert planned["ok"] is True
    assert planned["verified"] is False
    assert planned["observed"] is False
    assert any(row["id"] == "project.agents" for row in planned["rows"])
    _assert_nofollow_flags(fake_win32)


@pytest.mark.skipif(os.name != "nt", reason="real Windows CreateFileW")
def test_real_windows_read_regular_and_refuses_symlink(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_bytes(_SAFE)
    assert read_regular_nofollow(skill) == _SAFE
    imported = run_import(skill, project_root=tmp_path, dry_run=True)
    assert imported["ok"] is True
    assert imported["verified"] is False
    outside = tmp_path / "outside.md"
    outside.write_bytes(_SECRET)
    link = tmp_path / "link.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink privilege missing")
    with pytest.raises(InstallMigrateError, match="E_SYMLINK"):
        read_regular_nofollow(link)
    assert outside.read_bytes() == _SECRET
