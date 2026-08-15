"""Comment checker, simplifier, and Team/read-only apply gates (#76)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from omg_cli.hash_edit.descriptor import HASH_EDIT_KIND
from omg_cli.main import build_parser, main
from omg_cli.workers import build_ownership_manifest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "comment_checker"


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".omg").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMG_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("OMG_CAPABILITY_MODE", raising=False)
    monkeypatch.delenv("OMG_RUN_ID", raising=False)
    monkeypatch.delenv("OMG_TASK_ID", raising=False)
    return tmp_path


def _out(capsys: pytest.CaptureFixture[str]) -> dict:
    raw = capsys.readouterr().out
    assert raw.strip(), "expected JSON on stdout"
    return json.loads(raw)


def _copy_fixture(project: Path, name: str, dest: str | None = None) -> Path:
    target = project.joinpath(*(dest or name).split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / name, target)
    return target


def _code(payload: dict) -> str:
    return (payload.get("error") or {}).get("code") or payload.get("error_code")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _descriptor(path: str) -> dict:
    old_text = "alpha"
    replacement = "beta"
    before_context = "before\n"
    after_context = "\nafter"
    return {
        "schema_version": 1,
        "kind": HASH_EDIT_KIND,
        "edit_id": "edit-hygiene-1",
        "producer": "omg.hash_edit.test",
        "path": path,
        "base_sha256": "0" * 64,
        "old_text": old_text,
        "replacement": replacement,
        "before_context": before_context,
        "after_context": after_context,
        "old_text_sha256": _sha(old_text),
        "replacement_sha256": _sha(replacement),
        "before_context_sha256": _sha(before_context),
        "after_context_sha256": _sha(after_context),
    }


def test_parser_wires_comments_and_simplify() -> None:
    parser = build_parser()
    ns = parser.parse_args(["edit", "comments", "--paths", "a.py"])
    assert ns.edit_action == "comments"
    ns = parser.parse_args(["edit", "simplify", "--paths", "a.py", "--enable"])
    assert ns.edit_action == "simplify"


def test_python_fixture_flags_slop_and_keeps_legal(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _copy_fixture(project, "python_slop.py")
    rc = main(["--json", "edit", "comments", "--paths", "python_slop.py"])
    assert rc == 0
    payload = _out(capsys)
    assert payload["ok"] is True
    assert payload["command"] == "edit.comments"
    assert payload["mode"] == "report"
    rules = {item["rule_id"] for item in payload["findings"]}
    assert "ai_meta" in rules
    assert "stale_todo" in rules
    assert "unverifiable_claim" in rules
    assert "banner_noise" in rules
    assert "prompt_leak" in rules
    assert "redundant_narration" in rules
    assert "inconsistent_with_code" in rules
    legal_hits = [
        item
        for item in payload["findings"]
        if "SPDX" in item.get("excerpt", "") or "Copyright" in item.get("excerpt", "")
    ]
    assert legal_hits == []
    art = project.joinpath(*payload["artifact"].split("/"))
    assert art.is_file()
    art_body = art.read_text(encoding="utf-8")
    assert "verified" not in art_body
    assert "passes" not in art_body
    assert "AI generated this helper" not in art_body


def test_javascript_and_go_fixtures(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _copy_fixture(project, "javascript_slop.js")
    _copy_fixture(project, "go_slop.go")
    rc = main(["--json", "edit", "comments", "--paths", "javascript_slop.js", "go_slop.go"])
    assert rc == 0
    payload = _out(capsys)
    by_path: dict[str, set[str]] = {}
    for item in payload["findings"]:
        by_path.setdefault(item["path"], set()).add(item["rule_id"])
    assert "ai_meta" in by_path["javascript_slop.js"]
    assert "ai_meta" in by_path["go_slop.go"]
    assert "stale_todo" in by_path["javascript_slop.js"]


def test_legal_fixture_has_no_findings(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _copy_fixture(project, "legal.py")
    rc = main(["--json", "edit", "comments", "--paths", "legal.py"])
    assert rc == 0
    payload = _out(capsys)
    assert payload["findings"] == []
    assert payload["finding_count"] == 0


def test_inline_and_config_suppressions(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _copy_fixture(project, "suppressed.py")
    (project / "vendor_like.py").write_text("# AI generated this\n", encoding="utf-8")
    (project / "ignored_slop.py").write_text("# AI generated this\n", encoding="utf-8")
    (project / "banned.py").write_text("# DO NOT SHIP this comment\n", encoding="utf-8")
    cfg = project / ".omg" / "edit-comments.json"
    shutil.copy(FIXTURES / "edit-comments.json", cfg)
    rc = main(
        [
            "--json",
            "edit",
            "comments",
            "--paths",
            "suppressed.py",
            "vendor_like.py",
            "ignored_slop.py",
            "banned.py",
            "--config",
            str(cfg),
        ]
    )
    assert rc == 0
    payload = _out(capsys)
    rules_by_path: dict[str, set[str]] = {}
    for item in payload["findings"]:
        rules_by_path.setdefault(item["path"], set()).add(item["rule_id"])
    assert "suppressed.py" not in rules_by_path
    assert "vendor_like.py" not in rules_by_path
    assert "ignored_slop.py" in payload["skipped"]
    assert "banned_pattern" in rules_by_path["banned.py"]


def test_fix_is_conservative_and_keeps_legal(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = _copy_fixture(project, "python_slop.py")
    before = target.read_text(encoding="utf-8")
    rc = main(["--json", "edit", "comments", "--paths", "python_slop.py"])
    assert rc == 0
    _out(capsys)
    assert target.read_text(encoding="utf-8") == before
    rc = main(["--json", "edit", "comments", "--paths", "python_slop.py", "--fix"])
    assert rc == 0
    payload = _out(capsys)
    assert payload["mode"] == "fix"
    assert payload["fixed"]
    text = target.read_text(encoding="utf-8")
    assert "SPDX-License-Identifier: MIT" in text
    assert "Copyright 2026 Example Corp" in text
    assert "security: do not log secrets" in text
    assert "AI generated this helper" not in text
    assert "========================================" not in text
    assert "TODO: handle overflow" in text


def test_changed_line_scoping_via_diff_input(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "scoped.py").write_text(
        "# SPDX-License-Identifier: MIT\n"
        "def f():\n"
        "    # AI generated this\n"
        "    return 1\n",
        encoding="utf-8",
    )
    diff = (
        "diff --git a/scoped.py b/scoped.py\n"
        "--- a/scoped.py\n"
        "+++ b/scoped.py\n"
        "@@ -2,0 +3,1 @@\n"
        "+    # AI generated this\n"
    )
    diff_path = project / "scoped.diff"
    diff_path.write_text(diff, encoding="utf-8")
    rc = main(["--json", "edit", "comments", "--input", str(diff_path)])
    assert rc == 0
    payload = _out(capsys)
    rules = {item["rule_id"] for item in payload["findings"]}
    assert rules == {"ai_meta"}
    assert all(item["line"] == 3 for item in payload["findings"])


def test_git_diff_scopes_added_lines(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "omg-test@example.com"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "omg-test"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    src = project / "tracked.py"
    src.write_text("# SPDX-License-Identifier: MIT\ndef f():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=project, check=True, capture_output=True)
    src.write_text(
        "# SPDX-License-Identifier: MIT\ndef f():\n    # AI generated this\n    return 1\n",
        encoding="utf-8",
    )
    rc = main(["--json", "edit", "comments", "--git-diff"])
    assert rc == 0
    payload = _out(capsys)
    assert {item["rule_id"] for item in payload["findings"]} == {"ai_meta"}


def test_simplify_disabled_by_default(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    rc = main(["--json", "edit", "simplify", "--paths", "app.py"])
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_DISABLED"
    assert not (project / ".omg" / "state" / "simplify-guard.json").exists()


def test_simplify_assignment_and_recursion_guard(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    rc = main(["--json", "edit", "simplify", "--paths", "app.py", "--enable"])
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_ASSIGNMENT"
    assert payload["blocked"] is True
    err = payload.get("error") or {}
    assert "omg-code-simplifier" in (err.get("next_action") or "")
    assert "omg-code-reviewer" in (err.get("next_action") or "")
    assignment = payload["assignment"]
    assert assignment["role"] == "omg-code-simplifier"
    assert assignment["capability_mode"] == "read-write"
    assert assignment["reviewer_role"] == "omg-code-reviewer"
    assert assignment["self_approve"] is False
    guard = project / ".omg" / "state" / "simplify-guard.json"
    assert guard.is_file()
    guard_body = json.loads(guard.read_text(encoding="utf-8"))
    assert "verified" not in guard_body
    assert "passes" not in guard_body
    assert guard_body["status"] == "assigned"
    art = project.joinpath(*assignment["artifact"].split("/"))
    assert art.is_file()
    assert "verified" not in art.read_text(encoding="utf-8")

    rc = main(["--json", "edit", "simplify", "--paths", "app.py", "--enable"])
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_RECURSION"


def test_simplify_skips_vendor_and_minified(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    vendor = project / "vendor" / "lib.py"
    vendor.parent.mkdir()
    vendor.write_text("x = 1\n", encoding="utf-8")
    (project / "app.min.js").write_text("x=1;\n", encoding="utf-8")
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "vendor/lib.py",
            "app.min.js",
            "--enable",
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    skipped = {item["path"]: item["reason"] for item in payload["assignment"]["skipped"]}
    assert "vendor/lib.py" in skipped
    assert "app.min.js" in skipped


def test_simplify_config_enable_without_flag(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    (project / ".omg" / "simplify.json").write_text(
        json.dumps({"enabled": True, "max_files": 8, "max_bytes": 65536}),
        encoding="utf-8",
    )
    rc = main(["--json", "edit", "simplify", "--paths", "app.py"])
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_ASSIGNMENT"


def test_simplify_max_files_bound(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "a.py").write_text("a = 1\n", encoding="utf-8")
    (project / "b.py").write_text("b = 1\n", encoding="utf-8")
    (project / ".omg" / "simplify.json").write_text(
        json.dumps({"enabled": True, "max_files": 1, "max_bytes": 65536}),
        encoding="utf-8",
    )
    rc = main(["--json", "edit", "simplify", "--paths", "a.py", "b.py"])
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY_BOUNDS"


def test_apply_refuses_read_only(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMG_CAPABILITY_MODE", "read-only")
    desc = project / "descriptor.json"
    desc.write_text(json.dumps(_descriptor("docs/example.md")), encoding="utf-8")
    rc = main(["--json", "edit", "apply", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_READ_ONLY"


def test_comments_fix_refuses_read_only(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _copy_fixture(project, "python_slop.py")
    monkeypatch.setenv("OMG_CAPABILITY_MODE", "read-only")
    rc = main(["--json", "edit", "comments", "--paths", "python_slop.py", "--fix"])
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_READ_ONLY"
    assert "AI generated this helper" in (project / "python_slop.py").read_text(
        encoding="utf-8"
    )


def test_comments_report_allowed_when_read_only(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _copy_fixture(project, "python_slop.py")
    monkeypatch.setenv("OMG_CAPABILITY_MODE", "read-only")
    rc = main(["--json", "edit", "comments", "--paths", "python_slop.py"])
    assert rc == 0
    payload = _out(capsys)
    assert payload["ok"] is True
    assert payload["finding_count"] > 0


def test_apply_refuses_unowned_path(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    build_ownership_manifest(
        project,
        "run1",
        [{"task_id": "t1", "owned_files": ["owned.py"], "capability_mode": "read-write"}],
    )
    desc = project / "descriptor.json"
    desc.write_text(json.dumps(_descriptor("foreign.py")), encoding="utf-8")
    rc = main(
        [
            "--json",
            "edit",
            "apply",
            "--input",
            str(desc),
            "--run-id",
            "run1",
            "--task-id",
            "t1",
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_OWNERSHIP"


def test_apply_env_ids_refuse_unowned(
    project: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    build_ownership_manifest(
        project,
        "run1",
        [{"task_id": "t1", "owned_files": ["owned.py"], "capability_mode": "read-write"}],
    )
    monkeypatch.setenv("OMG_RUN_ID", "run1")
    monkeypatch.setenv("OMG_TASK_ID", "t1")
    desc = project / "descriptor.json"
    desc.write_text(json.dumps(_descriptor("foreign.py")), encoding="utf-8")
    rc = main(["--json", "edit", "apply", "--input", str(desc)])
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_OWNERSHIP"


def test_apply_without_manifest_is_not_ownership_error(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    desc = project / "descriptor.json"
    desc.write_text(json.dumps(_descriptor("docs/example.md")), encoding="utf-8")
    rc = main(
        ["--json", "edit", "apply", "--input", str(desc), "--run-id", "missingrun"]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) != "E_OWNERSHIP"
    assert _code(payload) != "E_READ_ONLY"


def test_comments_fix_preserves_trailing_code(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = project / "mixed.js"
    src.write_text(
        "/* AI-generated helper */ const important = 42;\n",
        encoding="utf-8",
    )
    rc = main(["--json", "edit", "comments", "--paths", "mixed.js", "--fix"])
    assert rc == 0
    _out(capsys)
    assert "const important = 42;" in src.read_text(encoding="utf-8")


def test_simplify_apply_edits_must_stay_in_paths(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (project / "allowed.py").write_text("alpha", encoding="utf-8")
    (project / "outside.py").write_text("alpha", encoding="utf-8")
    desc = project / "edits.json"
    desc.write_text(json.dumps([_descriptor("outside.py")]), encoding="utf-8")
    rc = main(
        [
            "--json",
            "edit",
            "simplify",
            "--paths",
            "allowed.py",
            "--enable",
            "--apply-edits",
            str(desc),
        ]
    )
    assert rc == 1
    payload = _out(capsys)
    assert _code(payload) == "E_SIMPLIFY"
    assert (project / "outside.py").read_text(encoding="utf-8") == "alpha"
