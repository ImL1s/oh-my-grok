"""Public hash-edit CLI (#76): omg edit plan|apply."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
from pathlib import Path

import pytest

from omg_cli.cli_envelope import SCHEMA_VERSION
from omg_cli.commands import edit as edit_cmds
from omg_cli.hash_edit.descriptor import HASH_EDIT_KIND
from omg_cli.main import build_parser, cmd_edit, main

_APPLY_SUPPORTED = sys.platform != "win32" and hasattr(os, "O_NOFOLLOW")
_SKIP_APPLY = pytest.mark.skipif(
    not _APPLY_SUPPORTED,
    reason="apply_hash_edit requires POSIX O_NOFOLLOW/fcntl",
)
_REPLACEMENT = "UNIQUE_REPLACEMENT_TOKEN_XYZ"


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
        "edit_id": "edit-cli-1",
        "producer": "omg.hash_edit.cli-test",
        "path": "docs/example.md",
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
    return tmp_path


def _write_target(project: Path, current: str, *, rel: str = "docs/example.md") -> Path:
    target = project.joinpath(*rel.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(current.encode("utf-8"))
    return target


def _write_descriptor(project: Path, payload: dict[str, object]) -> Path:
    path = project / "descriptor.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _out(capsys: pytest.CaptureFixture[str]) -> dict:
    raw = capsys.readouterr().out
    assert raw.strip(), "expected JSON on stdout"
    return json.loads(raw)


def test_edit_cli_module_does_not_claim_state_or_patch() -> None:
    source = inspect.getsource(edit_cmds)
    assert "omg_cli.state" not in source
    assert "subprocess" not in source
    assert "patch(1)" not in source
    assert cmd_edit is edit_cmds.cmd_edit
    assert edit_cmds.cmd_edit.__module__ == "omg_cli.commands.edit"


def test_parser_wires_edit_handlers() -> None:
    parser = build_parser()
    ns = parser.parse_args(["edit", "plan", "--input", "x.json"])
    assert ns.func is edit_cmds.cmd_edit
    assert ns.edit_action == "plan"
    ns = parser.parse_args(["edit", "apply", "--input", "x.json"])
    assert ns.func is edit_cmds.cmd_edit
    assert ns.edit_action == "apply"


def test_cli_plan_does_not_mutate_files(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\n"
    target = _write_target(project, current)
    before_stat = target.stat()
    desc = _write_descriptor(project, _payload(current))
    state_dir = project / ".omg" / "state"
    assert not state_dir.exists()

    rc = main(["--json", "edit", "plan", "--input", str(desc)])
    assert rc == 0
    payload = _out(capsys)
    assert payload["ok"] is True
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "edit.plan"
    plan = payload["plan"]
    assert plan["kind"] == edit_cmds.PLAN_RESULT_KIND
    assert plan["path"] == "docs/example.md"
    assert plan["start_offset"] < plan["end_offset"]
    assert current.encode("utf-8")[plan["start_offset"] : plan["end_offset"]] == b"alpha"
    assert plan["after_sha256"] == _digest_text(f"before\n{_REPLACEMENT}\nafter\n")
    assert _REPLACEMENT in plan["unified_diff"]

    assert target.read_bytes() == current.encode("utf-8")
    after_stat = target.stat()
    assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
    assert after_stat.st_size == before_stat.st_size
    assert not state_dir.exists()
    assert list(project.rglob("*.hash-edit.lock")) == []


def test_cli_plan_stale_base_fails_closed(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = "before\nalpha\nafter\n"
    stale = "before\nomega\nafter\n"
    target = _write_target(project, stale)
    desc = _write_descriptor(project, _payload(original))
    rc = main(["--json", "edit", "plan", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    assert payload["ok"] is False
    assert payload["command"] == "edit.plan"
    err = payload.get("error") or {}
    code = err.get("code") or payload.get("error_code")
    assert code == "E_HASH_EDIT_STALE"
    assert target.read_bytes() == stale.encode("utf-8")


def test_cli_plan_ambiguous_fails_closed(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\nmid\nbefore\nalpha\nafter\n"
    target = _write_target(project, current)
    desc = _write_descriptor(project, _payload(current))
    rc = main(["--json", "edit", "plan", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    code = (payload.get("error") or {}).get("code") or payload.get("error_code")
    assert code == "E_HASH_EDIT_AMBIGUOUS"
    assert target.read_bytes() == current.encode("utf-8")


def test_cli_plan_bad_descriptor_code(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    desc = _write_descriptor(project, {"schema_version": 1, "kind": "nope"})
    rc = main(["--json", "edit", "plan", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    code = (payload.get("error") or {}).get("code") or payload.get("error_code")
    assert code == "E_HASH_EDIT_DESCRIPTOR"


def test_cli_plan_missing_target_is_path_error(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\n"
    desc = _write_descriptor(project, _payload(current))
    rc = main(["--json", "edit", "plan", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    code = (payload.get("error") or {}).get("code") or payload.get("error_code")
    assert code == "E_HASH_EDIT_PATH"


@_SKIP_APPLY
@pytest.mark.platform
def test_cli_apply_splices_at_offsets(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\n"
    expected = f"before\n{_REPLACEMENT}\nafter\n"
    target = _write_target(project, current)
    desc = _write_descriptor(project, _payload(current))
    state_dir = project / ".omg" / "state"

    rc = main(["--json", "edit", "apply", "--input", str(desc)])
    assert rc == 0
    payload = _out(capsys)
    assert payload["ok"] is True
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["command"] == "edit.apply"
    result = payload["result"]
    assert result["kind"] == "omg.hash_edit.apply_result.v1"
    assert result["ok"] is True
    assert result["path"] == "docs/example.md"
    assert result["start_offset"] < result["end_offset"]
    assert current.encode("utf-8")[result["start_offset"] : result["end_offset"]] == (
        b"alpha"
    )
    assert result["after_sha256"] == _digest_text(expected)
    assert set(result) <= edit_cmds.APPLY_RESULT_JSON_KEYS
    dumped = json.dumps(payload)
    assert _REPLACEMENT not in dumped
    assert "unified_diff" not in result
    assert "replacement" not in result
    assert "old_text" not in result
    assert str(project) not in dumped
    assert target.read_bytes() == expected.encode("utf-8")
    assert not state_dir.exists()
    assert list(project.rglob("*.hash-edit.lock")) == []


@_SKIP_APPLY
@pytest.mark.platform
def test_cli_apply_stale_base_fails_and_leaves_bytes(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    original = "before\nalpha\nafter\n"
    stale = "before\nomega\nafter\n"
    target = _write_target(project, stale)
    desc = _write_descriptor(project, _payload(original))
    rc = main(["--json", "edit", "apply", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    code = (payload.get("error") or {}).get("code") or payload.get("error_code")
    assert code == "E_HASH_EDIT_STALE"
    assert target.read_bytes() == stale.encode("utf-8")
    assert not (project / ".omg" / "state").exists()


@_SKIP_APPLY
@pytest.mark.platform
def test_cli_apply_path_error_for_missing_file(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\n"
    desc = _write_descriptor(project, _payload(current))
    rc = main(["--json", "edit", "apply", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    code = (payload.get("error") or {}).get("code") or payload.get("error_code")
    assert code == "E_HASH_EDIT_PATH"


@pytest.mark.skipif(
    _APPLY_SUPPORTED,
    reason="win32-only: apply must fail closed without O_NOFOLLOW/fcntl",
)
def test_cli_apply_fail_closed_without_posix_lock(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\n"
    target = _write_target(project, current)
    desc = _write_descriptor(project, _payload(current))
    rc = main(["--json", "edit", "apply", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    code = (payload.get("error") or {}).get("code") or payload.get("error_code")
    # Library fail-closes before splice: confined_path (PATH) then fcntl (APPLY).
    assert code in {"E_HASH_EDIT_PATH", "E_HASH_EDIT_APPLY"}
    assert target.read_bytes() == current.encode("utf-8")
    dumped = json.dumps(payload)
    assert _REPLACEMENT not in dumped
    assert not (project / ".omg" / "state").exists()


def test_cli_missing_input_emits_json_usage(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = main(["--json", "edit", "plan"])
    assert rc == 2
    payload = _out(capsys)
    assert payload["ok"] is False
    code = (payload.get("error") or {}).get("code") or payload.get("error_code")
    assert code == "E_HASH_EDIT_USAGE"
    rc = main(["--json", "edit", "apply"])
    assert rc == 2
    payload = _out(capsys)
    code = (payload.get("error") or {}).get("code") or payload.get("error_code")
    assert code == "E_HASH_EDIT_USAGE"


def test_cli_unreadable_input_omits_absolute_path(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = project / "no-such-descriptor.json"
    abs_path = str(missing.resolve())
    rc = main(["--json", "edit", "apply", "--input", abs_path])
    assert rc == 2
    payload = _out(capsys)
    code = (payload.get("error") or {}).get("code") or payload.get("error_code")
    assert code == "E_HASH_EDIT_USAGE"
    dumped = json.dumps(payload)
    assert abs_path not in dumped
    assert str(project) not in dumped
    err = payload.get("error") or {}
    assert "cannot read --input" in (err.get("message") or payload.get("message") or "")


def test_cli_plan_oversized_target_is_input_error(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omg_cli.hash_edit.apply.MAX_PLAN_FILE_BYTES", 16)
    monkeypatch.setattr("omg_cli.hash_edit.planner.MAX_PLAN_FILE_BYTES", 16)
    current = "before\nalpha\nafter\n"
    assert len(current.encode("utf-8")) > 16
    _write_target(project, current)
    desc = _write_descriptor(project, _payload(current))
    rc = main(["--json", "edit", "plan", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    code = (payload.get("error") or {}).get("code") or payload.get("error_code")
    assert code == "E_HASH_EDIT_INPUT"


@pytest.mark.skipif(sys.platform == "win32", reason="symlink target confinement")
def test_cli_plan_rejects_symlink_target(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    current = "before\nalpha\nafter\n"
    real = _write_target(project, current, rel="docs/real.md")
    link = project / "docs" / "example.md"
    link.symlink_to(real)
    desc = _write_descriptor(project, _payload(current))
    rc = main(["--json", "edit", "plan", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    code = (payload.get("error") or {}).get("code") or payload.get("error_code")
    assert code == "E_HASH_EDIT_PATH"
    assert real.read_bytes() == current.encode("utf-8")
