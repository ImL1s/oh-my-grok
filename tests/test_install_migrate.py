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
    apply_owned_uninstall,
    plan_owned_uninstall,
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


def test_uninstall_rejects_dotdot_escape_of_install_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tampered ``..`` targets must not be unlinked even when hashes match."""
    project = tmp_path / "proj"
    project.mkdir()
    victim_body = b"outside victim matching hash\n"
    victim = tmp_path / "victim.txt"
    victim.write_bytes(victim_body)
    state_body = b"state must survive uninstall\n"
    state = project / ".omg" / "state" / "keep-me.txt"
    state.parent.mkdir(parents=True)
    state.write_bytes(state_body)
    match_body = b"owned unchanged\n"
    matching = project / ".omg" / "install" / "imported" / "skill" / "owned.md"
    matching.parent.mkdir(parents=True)
    matching.write_bytes(match_body)
    drift_original = b"original managed\n"
    drifted = project / ".omg" / "install" / "imported" / "skill" / "edited.md"
    drifted.write_bytes(b"user edited this file\n")
    install = project / ".omg" / "install"
    escape_victim = install / ".." / ".." / ".." / "victim.txt"
    escape_state = install / ".." / "state" / "keep-me.txt"
    assert ".." in escape_victim.parts
    assert ".." in escape_state.parts
    assert escape_victim.resolve() == victim.resolve()
    assert escape_state.resolve() == state.resolve()
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
                {
                    "id": "imported.skill.escape-victim",
                    "type": "skill",
                    "target": str(escape_victim),
                    "ownership": "imported",
                    "content_hash": _sha(victim_body),
                    "enabled": True,
                },
                {
                    "id": "imported.skill.escape-state",
                    "type": "skill",
                    "target": str(escape_state),
                    "ownership": "OMG-managed",
                    "content_hash": _sha(state_body),
                    "enabled": True,
                },
            ],
        },
        project_root=project,
        scope="project",
    )
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    plan = plan_owned_uninstall(project_root=project, grok_home=grok_home)
    assert plan["verified"] is False
    remove_paths = [row["path"] for row in plan["remove"]]
    preserve_by_id = {row["id"]: row for row in plan["preserve"]}
    assert str(escape_victim) not in remove_paths
    assert str(escape_state) not in remove_paths
    assert str(victim) not in remove_paths
    assert str(state) not in remove_paths
    assert preserve_by_id["imported.skill.escape-victim"]["reason"] == "escape"
    assert preserve_by_id["imported.skill.escape-state"]["reason"] == "state"
    assert preserve_by_id["imported.skill.edited"]["reason"] == "hash-drift"
    assert str(matching) in remove_paths

    applied = apply_owned_uninstall(plan)
    assert applied["verified"] is False
    assert victim.read_bytes() == victim_body
    assert state.read_bytes() == state_body
    assert drifted.read_bytes() == b"user edited this file\n"
    assert not matching.exists()
    assert str(escape_victim) not in applied["removed"]
    assert str(escape_state) not in applied["removed"]

    matching.parent.mkdir(parents=True, exist_ok=True)
    matching.write_bytes(match_body)
    monkeypatch.setenv("GROK_HOME", str(grok_home))
    rc = run_uninstall(
        yes=True,
        runner=_fake_runner(),
        home=grok_home,
        project_root=project,
        include_user_manifest=False,
    )
    assert rc == 0
    assert not matching.exists()
    assert victim.read_bytes() == victim_body
    assert state.read_bytes() == state_body
    assert drifted.is_file()


def test_apply_owned_uninstall_refuses_dotdot_remove_row(tmp_path: Path) -> None:
    victim_body = b"crafted plan must not unlink\n"
    victim = tmp_path / "victim.txt"
    victim.write_bytes(victim_body)
    state_body = b"state via crafted plan\n"
    state = tmp_path / "proj" / ".omg" / "state" / "keep-me.txt"
    state.parent.mkdir(parents=True)
    state.write_bytes(state_body)
    install = tmp_path / "proj" / ".omg" / "install"
    install.mkdir(parents=True)
    escape_victim = install / ".." / ".." / ".." / "victim.txt"
    escape_state = install / ".." / "state" / "keep-me.txt"
    applied = apply_owned_uninstall(
        {
            "remove": [
                {
                    "id": "imported.skill.escape-victim",
                    "path": str(escape_victim),
                    "content_hash": _sha(victim_body),
                },
                {
                    "id": "imported.skill.escape-state",
                    "path": str(escape_state),
                    "content_hash": _sha(state_body),
                },
            ],
            "preserve": [],
        }
    )
    assert applied["removed"] == []
    assert victim.read_bytes() == victim_body
    assert state.read_bytes() == state_body
    assert str(escape_victim) in applied["preserved"]
    assert str(escape_state) in applied["preserved"]
    assert applied["verified"] is False
    assert "passes" not in applied


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


def test_uninstall_plan_does_not_admit_arbitrary_grok_home_files(
    tmp_path: Path,
) -> None:
    """Project manifests cannot authorize deleting unrelated GROK_HOME files."""
    project = tmp_path / "proj"
    grok = tmp_path / "grok-home"
    project.mkdir()
    grok.mkdir()
    foreign = grok / "settings.json"
    foreign_body = b'{"user":"unrelated"}\n'
    foreign.write_bytes(foreign_body)
    persist_manifest(
        {
            "runtime": "grok",
            "scope": "project",
            "artifacts": [
                {
                    "id": "imported.skill.foreign-home",
                    "type": "skill",
                    "target": str(foreign),
                    "ownership": "imported",
                    "content_hash": _sha(foreign_body),
                    "enabled": True,
                }
            ],
        },
        project_root=project,
        scope="project",
    )
    plan = plan_owned_uninstall(
        project_root=project,
        grok_home=grok,
        include_user_manifest=False,
    )
    remove_paths = {row.get("path") for row in plan.get("remove") or []}
    assert str(foreign) not in remove_paths
    assert any(
        row.get("path") == str(foreign) and row.get("reason") == "out-of-scope"
        for row in plan.get("preserve") or []
        if isinstance(row, dict)
    )
    applied = apply_owned_uninstall(plan)
    assert foreign.read_bytes() == foreign_body
    assert str(foreign) not in (applied.get("removed") or [])
