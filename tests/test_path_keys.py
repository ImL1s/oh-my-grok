from __future__ import annotations

import json
import threading
from pathlib import Path

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
    safe_path_key,
    validate_safe_key,
)
from omg_cli.contracts.writer_chain import canonical_json_bytes
from omg_cli import state as state_mod


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


def test_locked_jsonl_uses_one_complete_canonical_line_per_record(tmp_path: Path) -> None:
    journal = tmp_path / "events" / "journal.jsonl"
    expected = [{"index": index, "payload": "x" * 32} for index in range(32)]

    threads = [
        threading.Thread(target=append_locked_jsonl, args=(journal, canonical_json_bytes(row)))
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
