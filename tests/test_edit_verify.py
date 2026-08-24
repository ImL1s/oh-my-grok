"""Public hash-edit verify CLI and simplify apply rollback (#76 leftover)."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from omg_cli.cli_envelope import SCHEMA_VERSION
from omg_cli.commands import edit as edit_cmds
from omg_cli.hash_edit import (
    HashEditApplyError,
    HashEditConcurrencyError,
    apply_hash_edit,
    write_confined_regular_file,
)
from omg_cli.hash_edit.descriptor import HASH_EDIT_KIND
from omg_cli.main import build_parser, main

_APPLY_SUPPORTED = sys.platform != "win32" and hasattr(os, "O_NOFOLLOW")
_SKIP_POSIX = pytest.mark.skipif(
    not _APPLY_SUPPORTED,
    reason="hash-edit confined read/write requires POSIX O_NOFOLLOW/fcntl",
)
_REPLACEMENT = "UNIQUE_VERIFY_REPLACEMENT_XYZ"


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _payload(current: str, **overrides: object) -> dict[str, object]:
    old_text = str(overrides.pop("old_text", "alpha"))
    replacement = str(overrides.pop("replacement", _REPLACEMENT))
    before_context = str(overrides.pop("before_context", "before\n"))
    after_context = str(overrides.pop("after_context", "\nafter"))
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": HASH_EDIT_KIND,
        "edit_id": str(overrides.pop("edit_id", "edit-verify-1")),
        "producer": "omg.hash_edit.verify-test",
        "path": str(overrides.pop("path", "docs/example.md")),
        "base_sha256": _digest_text(current),
        "old_text": old_text,
        "replacement": replacement,
        "before_context": before_context,
        "after_context": after_context,
        "old_text_sha256": _digest_text(old_text),
        "replacement_sha256": _digest_text(replacement),
        "before_context_sha256": _digest_text(before_context),
        "after_context_sha256": _digest_text(after_context),
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".omg").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("OMG_CAPABILITY_MODE", raising=False)
    monkeypatch.delenv("OMG_RUN_ID", raising=False)
    monkeypatch.delenv("OMG_TASK_ID", raising=False)
    return tmp_path


def _write_target(project: Path, current: str, *, rel: str = "docs/example.md") -> Path:
    target = project.joinpath(*rel.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(current.encode("utf-8"))
    return target


def _write_descriptor(project: Path, payload: dict[str, object], name: str = "descriptor.json") -> Path:
    path = project / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _out(capsys: pytest.CaptureFixture[str]) -> dict:
    raw = capsys.readouterr().out
    assert raw.strip(), "expected JSON on stdout"
    return json.loads(raw)


def _code(payload: dict) -> str:
    return (payload.get("error") or {}).get("code") or payload.get("error_code") or ""


def _assert_no_verified(payload: dict) -> None:
    dumped = json.dumps(payload)
    assert '"verified"' not in dumped
    assert payload.get("verified") is not True


def test_parser_wires_verify() -> None:
    parser = build_parser()
    ns = parser.parse_args(["edit", "verify", "--input", "x.json"])
    assert ns.func is edit_cmds.cmd_edit
    assert ns.edit_action == "verify"
    ns = parser.parse_args(["edit", "verify", "edit-verify-1", "--input", "x.json"])
    assert ns.edit_id == "edit-verify-1"
    assert ns.input_path == "x.json"


@_SKIP_POSIX
def test_verify_matching_descriptor_ok_and_file_unchanged(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\n"
    target = _write_target(project, current)
    before_stat = target.stat()
    desc = _write_descriptor(project, _payload(current))
    state_dir = project / ".omg" / "state"

    rc = main(["--json", "edit", "verify", "--input", str(desc)])
    assert rc == 0
    payload = _out(capsys)
    assert payload["ok"] is True
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "edit.verify"
    assert payload["status"] == "ok"
    result = payload["result"]
    assert result["kind"] == "omg.hash_edit.verify.v1"
    assert result["status"] == "ok"
    assert result["path"] == "docs/example.md"
    assert result["edit_id"] == "edit-verify-1"
    assert result["start_offset"] < result["end_offset"]
    assert current.encode("utf-8")[result["start_offset"] : result["end_offset"]] == b"alpha"
    assert result["after_sha256"] == _digest_text(f"before\n{_REPLACEMENT}\nafter\n")
    assert set(result) <= edit_cmds.VERIFY_RESULT_JSON_KEYS
    dumped = json.dumps(payload)
    assert _REPLACEMENT not in dumped
    assert "unified_diff" not in result
    assert "replacement" not in result
    assert "old_text" not in result
    _assert_no_verified(payload)

    assert target.read_bytes() == current.encode("utf-8")
    after_stat = target.stat()
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_size == before_stat.st_size
    assert not state_dir.exists()
    assert list(project.rglob("*.hash-edit.lock")) == []


@_SKIP_POSIX
def test_verify_positional_descriptor_path(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\n"
    target = _write_target(project, current)
    desc = _write_descriptor(project, _payload(current))
    rc = main(["--json", "edit", "verify", str(desc)])
    assert rc == 0
    payload = _out(capsys)
    assert payload["status"] == "ok"
    _assert_no_verified(payload)
    assert target.read_bytes() == current.encode("utf-8")


@_SKIP_POSIX
def test_verify_edit_id_must_match_descriptor(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\n"
    target = _write_target(project, current)
    desc = _write_descriptor(project, _payload(current))
    rc = main(["--json", "edit", "verify", "other-id", "--input", str(desc)])
    assert rc == 2
    payload = _out(capsys)
    assert _code(payload) == "E_HASH_EDIT_USAGE"
    assert target.read_bytes() == current.encode("utf-8")
    _assert_no_verified(payload)

    rc = main(["--json", "edit", "verify", "edit-verify-1", "--input", str(desc)])
    assert rc == 0
    payload = _out(capsys)
    assert payload["status"] == "ok"
    assert payload["result"]["edit_id"] == "edit-verify-1"
    assert target.read_bytes() == current.encode("utf-8")
    _assert_no_verified(payload)


@_SKIP_POSIX
def test_verify_after_external_edit_is_stale_and_file_unchanged(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = "before\nalpha\nafter\n"
    stale = "before\nomega\nafter\n"
    target = _write_target(project, original)
    desc = _write_descriptor(project, _payload(original))
    target.write_bytes(stale.encode("utf-8"))
    before = target.read_bytes()

    rc = main(["--json", "edit", "verify", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    assert payload["ok"] is False
    assert payload["command"] == "edit.verify"
    assert payload.get("status") == "stale"
    assert _code(payload) == "E_HASH_EDIT_STALE"
    assert payload.get("path") == "docs/example.md"
    assert target.read_bytes() == before
    _assert_no_verified(payload)


@_SKIP_POSIX
def test_verify_ambiguous_is_conflict_json(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\nmid\nbefore\nalpha\nafter\n"
    target = _write_target(project, current)
    desc = _write_descriptor(project, _payload(current))
    rc = main(["--json", "edit", "verify", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    assert payload.get("status") == "conflict"
    assert _code(payload) == "E_HASH_EDIT_AMBIGUOUS"
    assert target.read_bytes() == current.encode("utf-8")
    _assert_no_verified(payload)


@pytest.mark.skipif(sys.platform == "win32", reason="symlink target confinement")
def test_verify_rejects_symlink_target(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\n"
    real = _write_target(project, current, rel="docs/real.md")
    link = project / "docs" / "example.md"
    link.symlink_to(real)
    desc = _write_descriptor(project, _payload(current))
    rc = main(["--json", "edit", "verify", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_HASH_EDIT_PATH"
    assert real.read_bytes() == current.encode("utf-8")
    _assert_no_verified(payload)


def test_verify_missing_target_is_path_error(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\n"
    desc = _write_descriptor(project, _payload(current))
    rc = main(["--json", "edit", "verify", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_HASH_EDIT_PATH"
    _assert_no_verified(payload)


def test_verify_rejects_path_escape_in_descriptor(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\n"
    _write_target(project, current)
    desc = _write_descriptor(project, _payload(current, path="../secret"))
    rc = main(["--json", "edit", "verify", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_HASH_EDIT_DESCRIPTOR"
    _assert_no_verified(payload)


@_SKIP_POSIX
def test_verify_allowed_when_read_only(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMG_CAPABILITY_MODE", "read-only")
    current = "before\nalpha\nafter\n"
    target = _write_target(project, current)
    desc = _write_descriptor(project, _payload(current))
    rc = main(["--json", "edit", "verify", "--input", str(desc)])
    assert rc == 0
    payload = _out(capsys)
    assert payload["status"] == "ok"
    assert target.read_bytes() == current.encode("utf-8")
    _assert_no_verified(payload)


@pytest.mark.skipif(
    _APPLY_SUPPORTED,
    reason="win32-only: verify must fail closed without O_NOFOLLOW/dir_fd",
)
def test_verify_fail_closed_without_posix_nofollow(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\n"
    target = _write_target(project, current)
    desc = _write_descriptor(project, _payload(current))
    rc = main(["--json", "edit", "verify", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_HASH_EDIT_PATH"
    assert target.read_bytes() == current.encode("utf-8")
    _assert_no_verified(payload)


def _assign_simplify(project: Path, capsys: pytest.CaptureFixture[str], *paths: str) -> None:
    rc = main(["--json", "edit", "simplify", "--paths", *paths, "--enable"])
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_ASSIGNMENT"


@_SKIP_POSIX
@pytest.mark.platform
def test_simplify_apply_edits_success_two_files(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current_a = "before\nalpha\nafter\n"
    current_b = "begin\nalpha\nend\n"
    a = _write_target(project, current_a, rel="a.py")
    b = _write_target(project, current_b, rel="b.py")
    _assign_simplify(project, capsys, "a.py", "b.py")
    edits = [
        _payload(current_a, path="a.py", edit_id="edit-a", replacement="beta"),
        _payload(
            current_b,
            path="b.py",
            edit_id="edit-b",
            replacement="gamma",
            before_context="begin\n",
            after_context="\nend\n",
        ),
    ]
    desc = _write_descriptor(project, edits, name="edits.json")
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "a.py",
            "b.py",
            "--enable",
            "--apply-edits",
            str(desc),
        ]
    )
    assert rc == 0
    payload = _out(capsys)
    assert payload["ok"] is True
    assert payload["kind"] == "omg.edit.simplify.result.v1"
    assert a.read_bytes() == b"before\nbeta\nafter\n"
    assert b.read_bytes() == b"begin\ngamma\nend\n"
    _assert_no_verified(payload)
    guard = json.loads((project / ".omg" / "state" / "simplify-guard.json").read_text(encoding="utf-8"))
    assert guard["status"] == "applied"
    assert "verified" not in guard
    assert "passes" not in guard


@_SKIP_POSIX
@pytest.mark.platform
def test_simplify_apply_edits_second_fails_rolls_back_first(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\n"
    target = _write_target(project, current, rel="a.py")
    _assign_simplify(project, capsys, "a.py")
    edits = [
        _payload(current, path="a.py", edit_id="edit-first", replacement="beta"),
        _payload(current, path="a.py", edit_id="edit-second", replacement="gamma"),
    ]
    desc = _write_descriptor(project, edits, name="edits.json")
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "a.py",
            "--enable",
            "--apply-edits",
            str(desc),
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_HASH_EDIT_CONCURRENCY"
    assert target.read_bytes() == current.encode("utf-8")
    _assert_no_verified(payload)
    guard = json.loads((project / ".omg" / "state" / "simplify-guard.json").read_text(encoding="utf-8"))
    assert guard["status"] == "assigned"
    assert "verified" not in guard


@_SKIP_POSIX
@pytest.mark.platform
def test_simplify_apply_edits_two_files_second_injected_fail_rolls_back(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    current_a = "before\nalpha\nafter\n"
    current_b = "begin\nalpha\nend\n"
    a = _write_target(project, current_a, rel="a.py")
    b = _write_target(project, current_b, rel="b.py")
    _assign_simplify(project, capsys, "a.py", "b.py")
    calls = {"n": 0}

    def wrapped(*args: object, **kwargs: object):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise HashEditApplyError("injected later-descriptor failure")
        return apply_hash_edit(*args, **kwargs)

    monkeypatch.setattr("omg_cli.edit_hygiene.simplify.apply_hash_edit", wrapped)
    edits = [
        _payload(current_a, path="a.py", edit_id="edit-a", replacement="beta"),
        _payload(
            current_b,
            path="b.py",
            edit_id="edit-b",
            replacement="gamma",
            before_context="begin\n",
            after_context="\nend\n",
        ),
    ]
    desc = _write_descriptor(project, edits, name="edits.json")
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "a.py",
            "b.py",
            "--enable",
            "--apply-edits",
            str(desc),
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_HASH_EDIT_APPLY"
    assert a.read_bytes() == current_a.encode("utf-8")
    assert b.read_bytes() == current_b.encode("utf-8")
    _assert_no_verified(payload)
    guard = json.loads((project / ".omg" / "state" / "simplify-guard.json").read_text(encoding="utf-8"))
    assert guard["status"] == "assigned"
    assert "verified" not in guard


@_SKIP_POSIX
@pytest.mark.platform
def test_restore_write_expected_mismatch_does_not_clobber(project: Path) -> None:
    """Locked current != expected: do not replace with original."""

    rel = "a.py"
    original = b"original-bytes\n"
    after = b"after-bytes\n"
    other = b"concurrent-other\n"
    target = project / rel
    target.write_bytes(other)
    with pytest.raises(HashEditConcurrencyError):
        write_confined_regular_file(project, rel, original, expected=after)
    assert target.read_bytes() == other


@_SKIP_POSIX
@pytest.mark.platform
def test_restore_write_expected_match_restores_original(project: Path) -> None:
    rel = "a.py"
    original = b"original-bytes\n"
    after = b"after-bytes\n"
    target = project / rel
    target.write_bytes(after)
    write_confined_regular_file(project, rel, original, expected=after)
    assert target.read_bytes() == original
