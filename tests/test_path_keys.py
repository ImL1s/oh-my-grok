from __future__ import annotations

import errno
import json
import os
import stat
import threading
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

pytestmark = pytest.mark.platform


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
