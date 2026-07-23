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
    mode_bits,
    safe_path_key,
    validate_safe_key,
)
from omg_cli.contracts.writer_chain import canonical_json_bytes


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
