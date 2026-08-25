"""Windows high-level managed-store confinement (#77 leftover).

Hermetic tests inject FakeWin32API and force the POSIX store off. Real
Windows CreateFileW proof is ``test_real_windows_managed_store_*`` plus
the ``windows-nofollow`` CI job. Do not set ``os.name='nt'`` on Darwin.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omg_cli.contracts.path_keys import (
    ContractPathError,
    append_locked_jsonl,
    atomic_write_bytes,
    atomic_write_bytes_at,
    confined_path,
    ensure_managed_dir,
    exclusive_lock,
    exclusive_lock_at,
    open_managed_dir_fd,
    read_managed_regular_bytes,
)
from omg_cli.win32_nofollow import (
    FILE_CREATE,
    FILE_DIRECTORY_FILE,
    FILE_OPEN_REPARSE_POINT,
    IO_REPARSE_TAG_MOUNT_POINT,
)

from tests.support.fake_win32 import FakeWin32API, assert_nofollow_flags as _assert_nofollow_flags

pytestmark = pytest.mark.platform

_SECRET = b"LEAKED-OUTSIDE-BYTES-NOT-FOR-MANAGED-STORE"


@pytest.fixture
def fake_win32(monkeypatch: pytest.MonkeyPatch) -> FakeWin32API:
    if os.name == "nt":
        pytest.skip(
            "injected FakeWin32API is POSIX-backed; real Windows uses CtypesWin32API"
        )
    api = FakeWin32API()
    monkeypatch.setattr("omg_cli.win32_nofollow._API", api)
    monkeypatch.setattr(
        "omg_cli.contracts.path_keys._posix_confinement_ready", lambda: False
    )
    return api


def test_posix_managed_store_unchanged_when_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "posix":
        pytest.skip("POSIX dir_fd path must stay selected when available")
    api = FakeWin32API()
    monkeypatch.setattr("omg_cli.win32_nofollow._API", api)
    path = tmp_path / ".omg" / "state" / "record.json"
    atomic_write_bytes(path, b"posix", replace=False)
    assert path.read_bytes() == b"posix"
    assert read_managed_regular_bytes(path) == b"posix"
    assert not api.create_file_calls
    assert not api.nt_create_calls


def test_windows_ensure_managed_dir_mkdir_flags(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    store = tmp_path / ".omg" / "state"
    ensure_managed_dir(store)
    assert store.is_dir()
    dir_creates = [
        call
        for call in fake_win32.nt_create_calls
        if call[4] == FILE_CREATE and call[2] & FILE_DIRECTORY_FILE
    ]
    names = {call[1] for call in dir_creates}
    assert ".omg" in names
    assert "state" in names
    for call in dir_creates:
        assert call[2] & FILE_OPEN_REPARSE_POINT
    _assert_nofollow_flags(fake_win32)


def test_windows_ensure_managed_dir_rejects_symlink_component(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker").write_bytes(_SECRET)
    (project / ".omg").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContractPathError, match="symlink"):
        ensure_managed_dir(project / ".omg" / "state")
    assert (outside / "marker").read_bytes() == _SECRET
    assert not (outside / "state").exists()


def test_windows_atomic_write_replace_and_noclobber(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    path = tmp_path / ".omg" / "state" / "record.json"
    atomic_write_bytes(path, b"old", replace=False)
    assert path.read_bytes() == b"old"
    with pytest.raises(FileExistsError):
        atomic_write_bytes(path, b"forbidden", replace=False)
    assert path.read_bytes() == b"old"
    atomic_write_bytes(path, b"new", replace=True)
    assert path.read_bytes() == b"new"
    assert read_managed_regular_bytes(path) == b"new"
    _assert_nofollow_flags(fake_win32)


def test_windows_atomic_write_rejects_reparse_dest(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    store = tmp_path / ".omg" / "state"
    ensure_managed_dir(store)
    outside = tmp_path / "outside"
    outside.write_bytes(_SECRET)
    dest = store / "link.json"
    dest.symlink_to(outside)
    with pytest.raises(ContractPathError, match="symlink"):
        atomic_write_bytes(dest, b"forbidden", replace=False)
    with pytest.raises(ContractPathError, match="symlink"):
        atomic_write_bytes(dest, b"forbidden", replace=True)
    assert outside.read_bytes() == _SECRET


def test_windows_read_rejects_symlink_and_hardlink(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    receipts = tmp_path / ".omg" / "receipts"
    ensure_managed_dir(receipts)
    outside = tmp_path / "outside-receipt"
    outside.write_bytes(_SECRET)
    symlink = receipts / "symlink.json"
    symlink.symlink_to(outside)
    with pytest.raises(ContractPathError, match="symlink"):
        read_managed_regular_bytes(symlink)
    assert outside.read_bytes() == _SECRET
    regular = receipts / "regular.json"
    atomic_write_bytes(regular, b'{"ok":true}', replace=False)
    hardlink = receipts / "hardlink.json"
    hardlink.hardlink_to(regular)
    with pytest.raises(ContractPathError, match="single-link regular"):
        read_managed_regular_bytes(hardlink)


def test_windows_confined_path_rejects_reparse_parent(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    assert confined_path(root, "one", "two") == root.absolute() / "one" / "two"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContractPathError, match="symlink"):
        confined_path(root, "link", "secret")
    fake_win32.forced_reparse[str((root / "one").resolve())] = (
        IO_REPARSE_TAG_MOUNT_POINT,
        True,
    )
    (root / "one").mkdir()
    with pytest.raises(ContractPathError, match="symlink"):
        confined_path(root, "one", "secret")


def test_windows_exclusive_lock_and_append_jsonl(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    journal = tmp_path / ".omg" / "events" / "events.jsonl"
    append_locked_jsonl(journal, b'{"n":1}')
    append_locked_jsonl(journal, b'{"n":2}')
    lines = journal.read_bytes().splitlines()
    assert lines == [b'{"n":1}', b'{"n":2}']
    lock_path = journal.with_name(journal.name + ".lock")
    with exclusive_lock(lock_path):
        pass
    _assert_nofollow_flags(fake_win32)


def test_windows_lock_and_journal_reject_symlink(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    journal = tmp_path / ".omg" / "events" / "events.jsonl"
    ensure_managed_dir(journal.parent)
    outside = tmp_path / "outside-lock-target"
    outside.write_bytes(_SECRET)
    lock_path = journal.with_name(journal.name + ".lock")
    lock_path.symlink_to(outside)
    with pytest.raises(ContractPathError, match="symlink"):
        append_locked_jsonl(journal, b'{"ok":true}')
    with pytest.raises(ContractPathError, match="symlink"):
        with exclusive_lock(lock_path):
            pass  # pragma: no cover - must not enter
    assert outside.read_bytes() == _SECRET
    outside_journal = tmp_path / "outside-journal"
    outside_journal.write_bytes(_SECRET)
    hijack = journal.parent / "hijack.jsonl"
    hijack.symlink_to(outside_journal)
    with pytest.raises(ContractPathError, match="symlink"):
        append_locked_jsonl(hijack, b'{"ok":true}')
    assert outside_journal.read_bytes() == _SECRET


def test_windows_managed_store_does_not_use_path_open(
    tmp_path: Path, fake_win32: FakeWin32API, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _banned(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Path.open/read_bytes must not be used for confined I/O")

    monkeypatch.setattr(Path, "open", _banned)
    monkeypatch.setattr(Path, "read_bytes", _banned)
    monkeypatch.setattr(Path, "write_bytes", _banned)
    path = tmp_path / ".omg" / "state" / "record.json"
    atomic_write_bytes(path, b"body", replace=False)
    assert read_managed_regular_bytes(path) == b"body"


def test_windows_ensure_managed_dir_rejects_mount_point(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    omg = tmp_path / ".omg"
    omg.mkdir()
    fake_win32.forced_reparse[str(omg.resolve())] = (IO_REPARSE_TAG_MOUNT_POINT, True)
    with pytest.raises(ContractPathError, match="symlink"):
        ensure_managed_dir(omg / "state")


def test_windows_fd_level_apis_stay_posix_only(
    tmp_path: Path, fake_win32: FakeWin32API
) -> None:
    with pytest.raises(ContractPathError, match="POSIX dir_fd"):
        open_managed_dir_fd(tmp_path / ".omg" / "state")
    with pytest.raises(ContractPathError, match="POSIX dir_fd"):
        atomic_write_bytes_at(0, "record.json", b"x")
    with pytest.raises(ContractPathError, match="POSIX dir_fd"):
        with exclusive_lock_at(0, "record.json.lock"):
            pass  # pragma: no cover - must not enter
    assert fake_win32.create_file_calls == []


def test_windows_fail_closed_without_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omg_cli.contracts.path_keys._posix_confinement_ready", lambda: False
    )
    monkeypatch.setattr("omg_cli.win32_nofollow._API", None)

    def _boom() -> object:
        raise OSError("kernel32 missing")

    monkeypatch.setattr("omg_cli.win32_nofollow.CtypesWin32API", _boom)
    with pytest.raises(ContractPathError, match="Windows no-follow"):
        ensure_managed_dir(tmp_path / ".omg" / "state")


@pytest.mark.skipif(os.name != "nt", reason="real Windows CreateFileW")
def test_real_windows_managed_store_write_read_lock(tmp_path: Path) -> None:
    store = tmp_path / ".omg" / "state"
    ensure_managed_dir(store)
    path = store / "record.json"
    atomic_write_bytes(path, b"hello", replace=False)
    assert read_managed_regular_bytes(path) == b"hello"
    with pytest.raises(FileExistsError):
        atomic_write_bytes(path, b"nope", replace=False)
    atomic_write_bytes(path, b"world", replace=True)
    assert read_managed_regular_bytes(path) == b"world"
    journal = store / "events.jsonl"
    append_locked_jsonl(journal, b'{"n":1}')
    assert journal.read_bytes().splitlines() == [b'{"n":1}']
    assert confined_path(store, "record.json") == path.resolve()
    with exclusive_lock(path.with_suffix(".lock")):
        pass


@pytest.mark.skipif(os.name != "nt", reason="real Windows CreateFileW")
def test_real_windows_managed_store_rejects_symlink(tmp_path: Path) -> None:
    store = tmp_path / ".omg" / "state"
    ensure_managed_dir(store)
    outside = tmp_path / "outside.json"
    outside.write_bytes(_SECRET)
    dest = store / "link.json"
    try:
        dest.symlink_to(outside)
    except OSError:
        pytest.skip("symlink privilege missing")
    with pytest.raises(ContractPathError, match="symlink|reparse"):
        read_managed_regular_bytes(dest)
    with pytest.raises(ContractPathError, match="symlink|reparse"):
        atomic_write_bytes(dest, b"hijack", replace=True)
    assert outside.read_bytes() == _SECRET


@pytest.mark.skipif(os.name != "nt", reason="real Windows CreateFileW")
def test_real_windows_managed_store_rejects_junction(tmp_path: Path) -> None:
    import subprocess

    real = tmp_path / "real-state"
    real.mkdir()
    (real / "record.json").write_bytes(_SECRET)
    dest = tmp_path / ".omg"
    dest.mkdir()
    state = dest / "state"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(state), str(real)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not state.exists():
        pytest.skip(completed.stderr.strip() or "cannot create junction")
    with pytest.raises(ContractPathError, match="symlink|reparse"):
        read_managed_regular_bytes(state / "record.json")
    assert (real / "record.json").read_bytes() == _SECRET


@pytest.mark.skipif(os.name != "nt", reason="real Windows CreateFileW")
def test_real_windows_managed_store_fd_level_posix_only(tmp_path: Path) -> None:
    with pytest.raises(ContractPathError, match="POSIX dir_fd"):
        open_managed_dir_fd(tmp_path / ".omg" / "state")
