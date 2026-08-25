from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from omg_cli.contracts.path_keys import (
    ContractPathError,
    DATA_FILE_MODE,
    MANAGED_DIR_MODE,
    append_locked_jsonl,
    atomic_write_bytes,
    confined_path,
    ensure_managed_dir,
    exclusive_lock,
    mode_bits,
    read_managed_regular_bytes,
    safe_path_key,
    validate_safe_key,
)
from omg_cli.contracts.writer_chain import canonical_json_bytes
from omg_cli import state as state_mod

pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(
        os.name != "posix",
        reason="POSIX dir_fd/O_NOFOLLOW managed-store",
    ),
]


def test_safe_path_keys_are_namespace_bound_and_reject_hostile_text() -> None:
    key = safe_path_key("opaque/run/id", namespace="runtime")
    assert len(key) == 64
    assert validate_safe_key(key) == key
    assert key != safe_path_key("opaque/run/id", namespace="session")
    assert "opaque" not in key
    for value in ("", "nul\0byte", "line\nbreak", "\ud800"):
        with pytest.raises(ContractPathError):
            safe_path_key(value)


def test_confined_path_rejects_traversal_and_symlink_parent(tmp_path: Path) -> None:
    root = tmp_path / "managed"
    root.mkdir()
    assert confined_path(root, "one", "two") == root.absolute() / "one" / "two"
    for part in ("..", "a/b", "a\\b", "."):
        with pytest.raises(ContractPathError):
            confined_path(root, part)

    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContractPathError, match="symlink"):
        confined_path(root, "link", "secret")


def test_atomic_write_has_exact_modes_and_leaves_no_temp_file(tmp_path: Path) -> None:
    path = tmp_path / "state" / "record.json"
    atomic_write_bytes(path, b"old", replace=False)
    assert path.read_bytes() == b"old"
    assert mode_bits(path.parent) == MANAGED_DIR_MODE
    assert mode_bits(path) == DATA_FILE_MODE
    with pytest.raises(FileExistsError):
        atomic_write_bytes(path, b"forbidden", replace=False)
    atomic_write_bytes(path, b"new")
    assert path.read_bytes() == b"new"
    assert not list(path.parent.glob(".*.tmp"))


def test_atomic_no_clobber_has_one_concurrent_winner_and_never_follows_symlink(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state" / "winner"
    barrier = threading.Barrier(12)
    outcomes: list[bytes] = []

    def publish(body: bytes) -> None:
        barrier.wait()
        try:
            atomic_write_bytes(path, body, replace=False)
            outcomes.append(body)
        except FileExistsError:
            pass

    threads = [
        threading.Thread(target=publish, args=(f"body-{index}".encode(),))
        for index in range(12)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(outcomes) == 1
    assert path.read_bytes() == outcomes[0]

    target = tmp_path / "outside"
    target.write_bytes(b"unchanged")
    link = tmp_path / "state" / "link"
    link.symlink_to(target)
    with pytest.raises((FileExistsError, ContractPathError)):
        atomic_write_bytes(link, b"forbidden", replace=False)
    assert target.read_bytes() == b"unchanged"


def test_managed_read_uses_pinned_parent(tmp_path: Path) -> None:
    path = tmp_path / ".omg" / "receipts" / "generation-1.json"
    body = b'{"generation":1}'
    atomic_write_bytes(path, body, replace=False)

    assert read_managed_regular_bytes(path) == body


def test_managed_read_rejects_leaf_symlink_and_hardlink(tmp_path: Path) -> None:
    receipts = tmp_path / ".omg" / "receipts"
    ensure_managed_dir(receipts)
    outside = tmp_path / "outside-receipt"
    outside.write_bytes(b"outside")

    symlink = receipts / "symlink.json"
    symlink.symlink_to(outside)
    with pytest.raises(ContractPathError, match="symlink"):
        read_managed_regular_bytes(symlink)

    hardlink = receipts / "hardlink.json"
    hardlink.hardlink_to(outside)
    with pytest.raises(ContractPathError, match="single-link regular"):
        read_managed_regular_bytes(hardlink)


def test_managed_read_is_bounded(tmp_path: Path) -> None:
    path = tmp_path / ".omg" / "receipts" / "oversized.json"
    atomic_write_bytes(path, b"12345", replace=False)

    with pytest.raises(ContractPathError, match="exceeds 4 byte read limit"):
        read_managed_regular_bytes(path, max_bytes=4)


def test_managed_read_rejects_same_size_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / ".omg" / "receipts" / "racing.json"
    atomic_write_bytes(path, b"12345", replace=False)
    real_fstat = os.fstat
    regular_calls = 0

    def racing_fstat(descriptor: int):
        nonlocal regular_calls
        current = real_fstat(descriptor)
        if not stat.S_ISREG(current.st_mode):
            return current
        regular_calls += 1
        if regular_calls == 2:
            return SimpleNamespace(
                st_mode=current.st_mode,
                st_nlink=current.st_nlink,
                st_dev=current.st_dev,
                st_ino=current.st_ino,
                st_size=current.st_size,
                st_mtime_ns=current.st_mtime_ns,
                st_ctime_ns=current.st_ctime_ns + 1,
            )
        return current

    monkeypatch.setattr(os, "fstat", racing_fstat)

    with pytest.raises(ContractPathError, match="changed while reading"):
        read_managed_regular_bytes(path)


def test_managed_read_required_mode_uses_same_fd(tmp_path: Path) -> None:
    path = tmp_path / ".omg" / "receipts" / "mode.json"
    atomic_write_bytes(path, b'{"ok":true}', replace=False)
    assert read_managed_regular_bytes(path, required_mode=DATA_FILE_MODE) == b'{"ok":true}'
    os.chmod(path, 0o644)
    with pytest.raises(ContractPathError, match="mode must be 0600"):
        read_managed_regular_bytes(path, required_mode=DATA_FILE_MODE)
    with pytest.raises(ValueError, match="required_mode"):
        read_managed_regular_bytes(path, required_mode=True)  # type: ignore[arg-type]


def _rename_leaf_and_plant(lexical: Path, body: bytes, mode: int) -> Path:
    """Move the opened inode aside and plant a new inode at the lexical path."""
    relocated = lexical.with_name(lexical.name + ".opened-inode")
    lexical.rename(relocated)
    planted = os.open(str(lexical), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(planted, body)
        os.fchmod(planted, mode)
    finally:
        os.close(planted)
    return relocated


def _install_replace_after_leaf_fd_open(
    monkeypatch: pytest.MonkeyPatch,
    *,
    leaf_name: str,
    replace: Callable[[], None],
) -> dict:
    """Replace the lexical leaf immediately after its O_NOFOLLOW fd opens."""
    import omg_cli.contracts.path_keys as pk

    real_open = pk.os.open
    real_fstat = pk.os.fstat
    seen: dict = {
        "opened": False,
        "flags": None,
        "fd": None,
        "before": None,
        "fstats": [],
    }

    def wrapping_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        directory = getattr(os, "O_DIRECTORY", 0)
        if (
            not seen["opened"]
            and kwargs.get("dir_fd") is not None
            and os.path.basename(str(path)) == leaf_name
            and flags & os.O_NOFOLLOW
            and not (flags & directory)
            and not (flags & os.O_CREAT)
        ):
            seen["opened"] = True
            seen["flags"] = flags
            seen["fd"] = fd
            seen["before"] = real_fstat(fd)
            replace()
        return fd

    def wrapping_fstat(fd):
        st = real_fstat(fd)
        if seen["fd"] is not None and fd == seen["fd"]:
            seen["fstats"].append(
                (
                    st.st_dev,
                    st.st_ino,
                    st.st_nlink,
                    st.st_size,
                    st.st_mtime_ns,
                    st.st_ctime_ns,
                    stat.S_IMODE(st.st_mode),
                )
            )
        return st

    monkeypatch.setattr(pk.os, "open", wrapping_open)
    monkeypatch.setattr(pk.os, "fstat", wrapping_fstat)
    return seen


def _assert_same_fd_pin(seen: dict, *, required_mode: int | None = None) -> None:
    assert seen["opened"] is True
    assert seen["flags"] & os.O_NOFOLLOW
    assert not (seen["flags"] & getattr(os, "O_DIRECTORY", 0))
    before = seen["before"]
    assert before is not None
    assert before.st_nlink == 1
    assert stat.S_ISREG(before.st_mode)
    if required_mode is not None:
        assert stat.S_IMODE(before.st_mode) == required_mode
    assert len(seen["fstats"]) >= 2
    assert seen["fstats"][0] == seen["fstats"][-1]
    # Rename-after-open may bump ctime; pin identity is the product pre/post pair
    # plus the same (dev, ino, nlink, size) as the inode that was opened.
    assert seen["fstats"][0][:4] == (
        before.st_dev,
        before.st_ino,
        before.st_nlink,
        before.st_size,
    )


def test_managed_read_same_fd_keeps_opened_inode_bytes_after_leaf_replace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Replacement after leaf open cannot change the bytes or hash of that inode."""
    path = tmp_path / ".omg" / "receipts" / "pin.json"
    original = b'{"ok":true}'
    planted = b'{"no":true}'
    assert len(original) == len(planted)
    atomic_write_bytes(path, original, replace=False)
    original_stat = path.stat()

    def replace() -> None:
        relocated = _rename_leaf_and_plant(path, planted, DATA_FILE_MODE)
        assert relocated.stat().st_ino == original_stat.st_ino

    seen = _install_replace_after_leaf_fd_open(
        monkeypatch, leaf_name=path.name, replace=replace
    )
    body = read_managed_regular_bytes(path, required_mode=DATA_FILE_MODE)
    _assert_same_fd_pin(seen, required_mode=DATA_FILE_MODE)
    assert body == original
    assert hashlib.sha256(body).hexdigest() == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == planted
    assert stat.S_IMODE(path.stat().st_mode) == DATA_FILE_MODE
    assert (path.stat().st_dev, path.stat().st_ino) != (
        original_stat.st_dev,
        original_stat.st_ino,
    )
    assert (seen["before"].st_dev, seen["before"].st_ino) == (
        original_stat.st_dev,
        original_stat.st_ino,
    )


def test_managed_read_same_fd_rejects_opened_0644_after_0600_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mode check is on the opened inode, not a later 0600 plant at the path."""
    path = tmp_path / ".omg" / "receipts" / "mode-pin.json"
    original = b'{"ok":true}'
    planted = b'{"no":true}'
    atomic_write_bytes(path, original, replace=False)
    os.chmod(path, 0o644)
    opened_stat = path.stat()
    assert stat.S_IMODE(opened_stat.st_mode) == 0o644

    def replace() -> None:
        _rename_leaf_and_plant(path, planted, DATA_FILE_MODE)

    seen = _install_replace_after_leaf_fd_open(
        monkeypatch, leaf_name=path.name, replace=replace
    )
    with pytest.raises(ContractPathError, match="mode must be 0600"):
        read_managed_regular_bytes(path, required_mode=DATA_FILE_MODE)
    assert seen["opened"] is True
    assert seen["flags"] & os.O_NOFOLLOW
    assert seen["before"].st_nlink == 1
    assert stat.S_IMODE(seen["before"].st_mode) == 0o644
    assert (seen["before"].st_dev, seen["before"].st_ino) == (
        opened_stat.st_dev,
        opened_stat.st_ino,
    )
    assert path.read_bytes() == planted
    assert stat.S_IMODE(path.stat().st_mode) == DATA_FILE_MODE


def test_locked_jsonl_uses_one_complete_canonical_line_per_record(
    tmp_path: Path,
) -> None:
    journal = tmp_path / "events" / "journal.jsonl"
    expected = [{"index": index, "payload": "x" * 32} for index in range(32)]

    threads = [
        threading.Thread(
            target=append_locked_jsonl, args=(journal, canonical_json_bytes(row))
        )
        for row in expected
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = journal.read_bytes().splitlines()
    assert len(lines) == len(expected)
    assert {json.loads(line)["index"] for line in lines} == set(range(32))
    assert all(canonical_json_bytes(json.loads(line)) == line for line in lines)
    assert mode_bits(journal) == DATA_FILE_MODE
    with pytest.raises(ValueError, match="physical line"):
        append_locked_jsonl(journal, b"{}\n{}")


def _sentinel(outside: Path, body: bytes = b"sentinel-unchanged") -> tuple[bytes, int]:
    outside.write_bytes(body)
    mode = mode_bits(outside)
    return body, mode


def test_ensure_omg_dirs_rejects_dot_omg_symlink(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    body, mode = _sentinel(outside / "marker")
    (project / ".omg").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContractPathError, match="symlink"):
        state_mod.ensure_omg_dirs(project)
    assert (outside / "marker").read_bytes() == body
    assert mode_bits(outside / "marker") == mode
    # No state directory should appear inside the outside tree from a successful walk.
    assert not (outside / "state").exists() or mode_bits(outside / "marker") == mode


def test_nested_state_symlink_rejects_run_write(tmp_path: Path) -> None:
    project = tmp_path / "project"
    omg = project / ".omg"
    omg.mkdir(parents=True)
    outside = tmp_path / "outside-state"
    outside.mkdir()
    body, mode = _sentinel(outside / "payload", b"keep-me")
    (omg / "state").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ContractPathError, match="symlink"):
        ensure_managed_dir(omg / "state" / "runs")
    with pytest.raises(ContractPathError, match="symlink"):
        atomic_write_bytes(omg / "state" / "active.json", b'{"x":1}')
    assert (outside / "payload").read_bytes() == body
    assert mode_bits(outside / "payload") == mode


def test_lock_file_symlink_rejects_journal_append(tmp_path: Path) -> None:
    journal = tmp_path / ".omg" / "events" / "events.jsonl"
    ensure_managed_dir(journal.parent)
    outside = tmp_path / "outside-lock-target"
    body, mode = _sentinel(outside, b"lock-target")
    lock_path = journal.with_name(journal.name + ".lock")
    lock_path.symlink_to(outside)
    with pytest.raises(ContractPathError, match="symlink"):
        append_locked_jsonl(journal, b'{"ok":true}')
    with pytest.raises(ContractPathError, match="symlink"):
        with exclusive_lock(lock_path):
            pass  # pragma: no cover - must not enter
    assert outside.read_bytes() == body
    assert mode_bits(outside) == mode


def test_component_swap_race_cannot_redirect_publication(tmp_path: Path) -> None:
    """If a parent component becomes a symlink mid-flight, publication fails closed.

    The parent descriptor is opened before the swap; rename/link stay inside the
    original directory inode. A post-open path that re-enters via the swapped
    name must not write the outside sentinel.
    """

    store = tmp_path / ".omg" / "state"
    ensure_managed_dir(store)
    destination = store / "record.json"
    atomic_write_bytes(destination, b"original")

    outside = tmp_path / "outside-swap"
    outside.mkdir()
    body, mode = _sentinel(outside / "record.json", b"outside-original")

    # Swap the managed state directory for a symlink after the first publication.
    import shutil

    real_state = store.resolve()
    backup = tmp_path / "state-backup"
    shutil.move(str(real_state), str(backup))
    store.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ContractPathError, match="symlink"):
        atomic_write_bytes(destination, b"attacker")
    assert (outside / "record.json").read_bytes() == body
    assert mode_bits(outside / "record.json") == mode
    # Original bytes remain in the real (moved) directory.
    assert (backup / "record.json").read_bytes() == b"original"


def test_regular_managed_store_modes_still_exact(tmp_path: Path) -> None:
    path = tmp_path / ".omg" / "state" / "record.json"
    atomic_write_bytes(path, b"payload")
    assert path.read_bytes() == b"payload"
    assert mode_bits(path.parent) == MANAGED_DIR_MODE
    assert mode_bits(path) == DATA_FILE_MODE
    ensure_managed_dir(tmp_path / ".omg" / "runs")
    assert mode_bits(tmp_path / ".omg") == MANAGED_DIR_MODE
    assert mode_bits(tmp_path / ".omg" / "runs") == MANAGED_DIR_MODE


def test_lock_open_never_falls_back_to_absolute_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ENOENT on descriptor-relative open must fail closed (Codex P1)."""

    import omg_cli.contracts.path_keys as pk

    store = tmp_path / ".omg" / "state"
    ensure_managed_dir(store)
    lock_path = store / "events.jsonl.lock"
    parent_fd, name = pk._ensure_parent_dir_fd(lock_path)
    try:

        def always_enoent(*_a, **_k):
            raise FileNotFoundError(errno.ENOENT, "forced", name)

        monkeypatch.setattr(pk.os, "open", always_enoent)
        with pytest.raises(ContractPathError, match="unable to open lock"):
            with pk.exclusive_lock_at(parent_fd, name):
                pass  # pragma: no cover
    finally:
        import os as _os

        _os.close(parent_fd)


def test_journal_lock_and_append_share_pinned_parent(tmp_path: Path) -> None:
    """Journal lock must not re-resolve a swapped directory (Codex P1)."""

    import shutil

    journal = tmp_path / ".omg" / "events" / "events.jsonl"
    ensure_managed_dir(journal.parent)
    append_locked_jsonl(journal, b'{"n":1}')

    outside = tmp_path / "outside-events"
    outside.mkdir()
    body, mode = _sentinel(outside / "events.jsonl", b"outside-journal")

    real_events = journal.parent.resolve()
    backup = tmp_path / "events-backup"
    shutil.move(str(real_events), str(backup))
    journal.parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ContractPathError, match="symlink"):
        append_locked_jsonl(journal, b'{"n":2}')
    assert (outside / "events.jsonl").read_bytes() == body
    assert mode_bits(outside / "events.jsonl") == mode
    # Original journal still only has the first record under the real inode.
    lines = (backup / "events.jsonl").read_bytes().splitlines()
    assert lines == [b'{"n":1}']
    # Lock file must not appear under the swapped outside tree.
    assert not (outside / "events.jsonl.lock").exists()
