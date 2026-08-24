"""#77 leftover: setup import / migrate + manifest-hash uninstall preserve."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from omg_cli.install_manifest import load_manifest, persist_manifest
from omg_cli.install_migrate import (
    InstallMigrateError,
    run_import,
    run_migrate,
)
from omg_cli.main import build_parser, main
from omg_cli.uninstall_cmd import run_uninstall


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _fake_runner():
    calls: list[list[str]] = []

    def runner(argv, *args, **kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _parse_last_json(capsys) -> dict:
    out = capsys.readouterr().out
    start = out.find("{")
    assert start >= 0, out
    return json.loads(out[start:])


def test_parser_wires_setup_import_and_migrate() -> None:
    parser = build_parser()
    imported = parser.parse_args(
        ["setup", "import", "--from", "/tmp/skill.md", "--dry-run"]
    )
    assert imported.func.__name__ == "cmd_setup_import"
    assert imported.from_path == "/tmp/skill.md"
    assert imported.setup_dry_run is True
    migrated = parser.parse_args(
        ["setup", "migrate", "--from", "/tmp/legacy", "--dry-run"]
    )
    assert migrated.func.__name__ == "cmd_setup_migrate"
    ns = parser.parse_args(["setup", "--runtime", "both", "--scope", "user"])
    assert ns.func.__name__ == "cmd_setup"
    assert ns.setup_runtime == "both"


def test_dry_run_import_json_rows_and_target_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    skill = tmp_path / "SKILL.md"
    body = b"# demo skill\nDo not live-verify.\n"
    skill.write_bytes(body)
    rc = main(
        [
            "setup",
            "--here",
            "import",
            "--from",
            str(skill),
            "--dry-run",
            "--json",
        ]
    )
    assert rc == 0
    payload = _parse_last_json(capsys)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["verified"] is False
    assert payload["observed"] is False
    assert payload["healthy"] is False
    rows = payload["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["ownership"] == "imported"
    prov = row["provenance"]
    assert prov["source"].endswith("/SKILL.md")
    assert prov["sha256"] == _sha(body)
    assert prov["byte_size"] == len(body)
    assert "imported_at" in prov
    target = Path(row["target"])
    assert not target.exists()
    assert load_manifest(project_root=tmp_path, scope="project") is None
    assert "sk-" not in json.dumps(payload)
    assert "api_key" not in json.dumps(payload)


def test_real_import_writes_confined_file_and_manifest(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    body = b"# imported skill\nsafe text\n"
    skill.write_bytes(body)
    result = run_import(
        skill,
        project_root=tmp_path,
        scope="project",
        runtime="grok",
        dry_run=False,
    )
    assert result["ok"] is True
    assert result["dry_run"] is False
    assert result["verified"] is False
    assert result["observed"] is False
    row = result["rows"][0]
    assert row["ownership"] == "imported"
    target = Path(row["target"])
    assert target.is_file()
    assert not target.is_symlink()
    assert target.read_bytes() == body
    assert _sha(target.read_bytes()) == row["content_hash"]
    assert ".omg/install/imported/" in target.as_posix()
    doc = load_manifest(project_root=tmp_path, scope="project", strict=True)
    assert doc is not None
    assert doc["verified"] is False
    assert doc["observed"] is False
    assert doc["healthy"] is False
    stored = next(r for r in doc["artifacts"] if r["id"] == row["id"])
    assert stored["ownership"] == "imported"
    assert stored["content_hash"] == _sha(body)
    assert not (tmp_path / ".omg" / "state" / "runs").exists()


def test_credential_file_refused_writes_nothing(tmp_path: Path) -> None:
    secret = tmp_path / "SKILL.md"
    secret.write_text("token api_key=placeholder\n", encoding="utf-8")
    with pytest.raises(InstallMigrateError, match="E_SECRET"):
        run_import(secret, project_root=tmp_path, dry_run=False)
    with pytest.raises(InstallMigrateError, match="E_SECRET"):
        run_import(secret, project_root=tmp_path, dry_run=True)
    assert load_manifest(project_root=tmp_path, scope="project") is None
    imported_root = tmp_path / ".omg" / "install" / "imported"
    assert not imported_root.exists()


def test_credential_cli_json_has_no_secret_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    secret = tmp_path / "SKILL.md"
    secret.write_text("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.aaa\n", encoding="utf-8")
    rc = main(
        ["setup", "--here", "import", "--from", str(secret), "--dry-run", "--json"]
    )
    assert rc != 0
    payload = _parse_last_json(capsys)
    blob = json.dumps(payload)
    assert payload["ok"] is False
    assert payload["dry_run"] is True
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in blob
    assert "Bearer " not in blob


def test_symlink_source_refused(tmp_path: Path) -> None:
    real = tmp_path / "SKILL.md"
    real.write_text("# skill\n", encoding="utf-8")
    link = tmp_path / "link-skill.md"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("symlink creation requires privileges on this host")
    with pytest.raises(InstallMigrateError, match="E_SYMLINK"):
        run_import(link, project_root=tmp_path, dry_run=True)
    assert load_manifest(project_root=tmp_path, scope="project") is None


def test_migrate_classifies_managed_vs_user_owned_without_clobber(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    user_agents = tmp_path / "AGENTS.md"
    user_body = "my personal notes\n"
    user_agents.write_text(user_body, encoding="utf-8")
    managed = tmp_path / "rules" / "omg.md"
    managed.parent.mkdir()
    managed.write_text("<!-- OMG:START -->\nmanaged block\n", encoding="utf-8")
    skill = tmp_path / "skills" / "demo" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# extra skill\n", encoding="utf-8")

    rc = main(
        [
            "setup",
            "--here",
            "migrate",
            "--from",
            str(tmp_path),
            "--dry-run",
            "--json",
        ]
    )
    assert rc == 0
    payload = _parse_last_json(capsys)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["verified"] is False
    by_id = {row["id"]: row for row in payload["rows"]}
    assert by_id["project.agents"]["ownership"] == "user-owned"
    assert by_id["project.agents"]["classification"] == "user_owned"
    assert "classification" in by_id["project.agents"]
    managed_row = by_id["user.grok.rules"]
    assert managed_row["ownership"] == "OMG-managed"
    imported = [row for row in payload["rows"] if row["ownership"] == "imported"]
    assert imported
    assert any("provenance" in row for row in payload["rows"])
    assert user_agents.read_text(encoding="utf-8") == user_body
    assert load_manifest(project_root=tmp_path, scope="project") is None

    applied = run_migrate(
        tmp_path,
        project_root=tmp_path,
        scope="project",
        dry_run=False,
        grok_home=tmp_path,
    )
    assert applied["ok"] is True
    assert applied["dry_run"] is False
    assert user_agents.read_text(encoding="utf-8") == user_body
    doc = load_manifest(project_root=tmp_path, scope="project", strict=True)
    assert doc is not None
    assert doc["verified"] is False
    stored = {row["id"]: row for row in doc["artifacts"]}
    assert stored["project.agents"]["ownership"] == "user-owned"


def test_migrate_apply_refuses_foreign(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "AGENTS.md").write_text("outside\n", encoding="utf-8")
    project = tmp_path / "proj"
    project.mkdir()
    preview = run_migrate(
        legacy,
        project_root=project,
        dry_run=True,
        grok_home=project / "grok",
    )
    assert preview["dry_run"] is True
    assert any(row["classification"] == "foreign" for row in preview["rows"])
    with pytest.raises(InstallMigrateError, match="E_FOREIGN"):
        run_migrate(
            legacy,
            project_root=project,
            dry_run=False,
            grok_home=project / "grok",
        )
    assert load_manifest(project_root=project, scope="project") is None


def test_uninstall_preserves_hash_drifted_and_removes_matching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GROK_HOME", str(tmp_path / "grok-home"))
    matching = tmp_path / ".omg" / "install" / "imported" / "skill" / "owned.md"
    drifted = tmp_path / ".omg" / "install" / "imported" / "skill" / "edited.md"
    state = tmp_path / ".omg" / "state" / "keep-me.txt"
    matching.parent.mkdir(parents=True, exist_ok=True)
    drifted.parent.mkdir(parents=True, exist_ok=True)
    state.parent.mkdir(parents=True, exist_ok=True)
    match_body = b"owned unchanged\n"
    drift_original = b"original managed\n"
    matching.write_bytes(match_body)
    drifted.write_bytes(b"user edited this file\n")
    state.write_text("state stays\n", encoding="utf-8")
    persist_manifest(
        {
            "runtime": "grok",
            "scope": "project",
            "artifacts": [
                {
                    "id": "imported.skill.owned",
                    "type": "skill",
                    "target": str(matching),
                    "ownership": "imported",
                    "content_hash": _sha(match_body),
                    "enabled": True,
                },
                {
                    "id": "imported.skill.edited",
                    "type": "skill",
                    "target": str(drifted),
                    "ownership": "OMG-managed",
                    "content_hash": _sha(drift_original),
                    "enabled": True,
                },
            ],
        },
        project_root=tmp_path,
        scope="project",
    )
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    rc = run_uninstall(
        yes=True,
        runner=_fake_runner(),
        home=grok_home,
        project_root=tmp_path,
        include_user_manifest=False,
    )
    assert rc == 0
    assert not matching.exists()
    assert drifted.is_file()
    assert drifted.read_bytes() == b"user edited this file\n"
    assert state.read_text(encoding="utf-8") == "state stays\n"
    doc = load_manifest(project_root=tmp_path, scope="project")
    assert doc is not None
    assert doc.get("verified") is False


def test_never_writes_passes_or_verified(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("# skill\n", encoding="utf-8")
    result = run_import(skill, project_root=tmp_path, dry_run=False)
    blob = json.dumps(result)
    assert '"verified": false' in blob
    assert '"passes":' not in blob
    dest = Path(result["manifest"])
    stored = dest.read_text(encoding="utf-8")
    assert '"verified": false' in stored
    assert '"observed": false' in stored
    assert '"healthy": false' in stored
    assert '"passes":' not in stored
    assert not (tmp_path / ".omg" / "state").exists()
