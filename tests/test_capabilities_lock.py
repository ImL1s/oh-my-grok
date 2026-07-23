"""Tests for scripts/generate_capabilities_lock.py + doctor soft check."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from omg_cli import doctor

ROOT = Path(__file__).resolve().parents[1]
GEN_SCRIPT = ROOT / "scripts" / "generate_capabilities_lock.py"


def _load_gen_module():
    spec = importlib.util.spec_from_file_location(
        "generate_capabilities_lock", GEN_SCRIPT
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


MCP_OPERATIONS = (
    "run_status.read",
    "trace.timeline",
    "trace.summary",
    "resume_metadata.read",
    "project_memory.search",
    "wiki.read",
    "team_status.read",
    "mailbox.list",
    "proposal.create",
)
CAPABILITY_FILES = {
    "skills/omg-x/SKILL.md",
    "skills/omg-ask/SKILL.md",
    "skills/omg-dual-review/SKILL.md",
    "skills/omg-ralplan/SKILL.md",
    "agents/omg-y.md",
}


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _populate_surface_sources(root: Path) -> None:
    specs = ",\n".join(f'    {{"name": {name!r}}}' for name in MCP_OPERATIONS)
    handlers = ",\n".join(f"    {name!r}: handler" for name in MCP_OPERATIONS)
    _write(
        root / "omg_cli" / "mcp" / "tools.py",
        "EXACT_TOOL_NAMES = "
        + repr(MCP_OPERATIONS)
        + f"\nTOOL_SPECS = [\n{specs}\n]\nTOOL_HANDLERS = {{\n{handlers}\n}}\n",
    )
    _write(
        root / "omg_cli" / "lsp_tools.py",
        'LSP_CONFIG_NAME = ".lsp.json"\n'
        "SEMANTIC_PROXY_OPERATIONS = ()\n\n"
        "def validate_registration():\n    pass\n\n"
        "def load_registration():\n    pass\n\n"
        "def registration_status():\n    return {'ownership': 'host_owned'}\n",
    )
    _write(
        root / ".mcp.json",
        json.dumps(
            {
                "mcpServers": {
                    "omg": {
                        "command": "python3",
                        "args": ["${GROK_PLUGIN_ROOT}/bin/omg", "mcp-server"],
                    }
                }
            }
        ),
    )
    _write(
        root / ".lsp.json",
        json.dumps(
            {
                "pyright": {
                    "command": "pyright-langserver",
                    "args": ["--stdio"],
                    "extensionToLanguage": {".py": "python"},
                }
            }
        ),
    )
    _write(
        root / "omg_cli" / "contracts" / "workflow_contract.py",
        'WORKFLOW_CONTRACT = "repository-workflow/v1"\n',
    )
    _write(
        root / "omg_cli" / "workflows" / "grok_adapter.py",
        "def assess_native_capability():\n"
        "    return {'status': 'optional_unclaimed'}\n\n"
        "def project_to_rhai():\n"
        "    raise RuntimeError('E_WORKFLOW_NATIVE_UNSUPPORTED')\n",
    )
    _write(
        root / "omg_cli" / "ask" / "providers.py",
        "PROVIDERS = frozenset({'codex', 'claude', 'gemini'})\n"
        "STRUCTURED_VERDICT_PROVIDERS = frozenset({'codex', 'claude'})\n"
        "ADVISOR_SKILLS = frozenset({'omg-ask', 'omg-dual-review', 'omg-ralplan'})\n"
        "ALIASES = {'fable': 'claude'}\n\n"
        "SPECS = {'codex': spec, 'claude': spec, 'gemini': spec}\n\n"
        "class AdvisorRoute:\n"
        "    posture: str = 'read-only'\n"
        "    worker_eligible: bool = False\n"
        "    auto_apply: bool = False\n"
        "    authoritative: bool = False\n",
    )
    _write(
        root / "omg_cli" / "team" / "roles.py",
        "_ROLES = {'y': RoleMeta(posture='read-only', role_class='reviewer')}\n",
    )
    for skill_name in ("omg-ask", "omg-dual-review", "omg-ralplan"):
        _write(
            root / "skills" / skill_name / "SKILL.md",
            f"---\nname: {skill_name}\ndescription: test\n---\n# {skill_name}\n",
        )


def _fake_repo(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "plugin.json").write_text(
        json.dumps({"name": "oh-my-grok", "version": "9.9.9"}),
        encoding="utf-8",
    )
    skill = tmp_path / "skills" / "omg-x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# skill x\nbody\n", encoding="utf-8")
    agent = tmp_path / "agents" / "omg-y.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("# agent y\n", encoding="utf-8")
    # Non-matching paths must be ignored
    (tmp_path / "skills" / "other" / "SKILL.md").parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "skills" / "other" / "SKILL.md").write_text("nope\n", encoding="utf-8")
    (tmp_path / "agents" / "readme.md").write_text("ignore\n", encoding="utf-8")
    _populate_surface_sources(tmp_path)
    return tmp_path


def test_compute_lock_files_and_aggregate(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    lock = gen.compute_lock(root)
    assert lock["version"] == "9.9.9"
    assert set(lock["files"]) == CAPABILITY_FILES
    assert isinstance(lock["aggregate"], str) and len(lock["aggregate"]) == 64
    # deterministic
    assert gen.compute_lock(root)["aggregate"] == lock["aggregate"]
    # compute_lock_for is the generalized form; compute_lock delegates to it
    assert gen.compute_lock_for(root) == lock
    assert gen.compute_lock_for(root)["aggregate"] == lock["aggregate"]
    assert lock["session_surface"]["mcp"]["operation_count"] == 9
    assert lock["session_surface"]["lsp"]["semantic_proxy_count"] == 0
    assert lock["session_surface"]["workflow"]["contract"] == "repository-workflow/v1"
    assert len(lock["session_surface_aggregate"]) == 64


def test_compute_lock_for_deterministic_on_tmp_root(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    a = gen.compute_lock_for(root)
    b = gen.compute_lock_for(root)
    assert a == b
    assert a["aggregate"] == b["aggregate"]
    assert set(a["files"]) == CAPABILITY_FILES


def test_missing_surface_sources_are_explicitly_unclaimed(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    for relative in (
        "omg_cli/mcp/tools.py",
        "omg_cli/lsp_tools.py",
        "omg_cli/workflows/grok_adapter.py",
        "omg_cli/ask/providers.py",
        "omg_cli/team/roles.py",
    ):
        (root / relative).unlink()

    surface = gen.discover_session_surface(root)

    assert surface["claim_status"] == {
        "roles": "missing",
        "advisor_routing": "missing",
        "mcp": "missing",
        "lsp": "missing",
        "workflow": "missing",
    }
    assert surface["mcp"]["operations"] == []
    assert surface["lsp"]["owner"] == "unclaimed"
    assert surface["workflow"]["contract"] is None
    assert surface["advisor_routing"]["providers"] == {}
    codes = {issue["code"] for issue in surface["issues"]}
    assert {
        "W_MCP_SOURCE_MISSING",
        "W_LSP_SOURCE_MISSING",
        "W_WORKFLOW_SOURCE_MISSING",
        "W_ADVISOR_SOURCE_MISSING",
        "W_ROLES_SOURCE_MISSING",
    } <= codes


def test_source_byte_mutation_changes_session_surface_aggregate(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    before = gen.compute_lock_for(root)
    source = root / "omg_cli" / "mcp" / "tools.py"
    source.write_text(source.read_text(encoding="utf-8") + "# byte drift\n", encoding="utf-8")
    after = gen.compute_lock_for(root)

    assert before["session_surface"]["mcp"] == after["session_surface"]["mcp"]
    assert before["session_surface_aggregate"] != after["session_surface_aggregate"]
    assert not gen.lock_matches(before, after)


@pytest.mark.parametrize("relative", [".mcp.json", ".lsp.json"])
def test_registration_byte_mutation_changes_session_surface_aggregate(
    tmp_path: Path, relative: str
) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    before = gen.compute_lock_for(root)
    registration = root / relative
    parsed = json.loads(registration.read_text(encoding="utf-8"))
    registration.write_text(json.dumps(parsed, indent=2) + "\n", encoding="utf-8")
    after = gen.compute_lock_for(root)

    surface = "mcp" if relative == ".mcp.json" else "lsp"
    assert before["session_surface"][surface] == after["session_surface"][surface]
    assert before["session_surface_aggregate"] != after["session_surface_aggregate"]
    assert not gen.lock_matches(before, after)


@pytest.mark.parametrize(
    ("relative", "surface", "warning"),
    [
        (".mcp.json", "mcp", "W_MCP_REGISTRATION_MISSING"),
        (".lsp.json", "lsp", "W_LSP_REGISTRATION_MISSING"),
    ],
)
def test_missing_registration_manifest_never_claims(
    tmp_path: Path, relative: str, surface: str, warning: str
) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    (root / relative).unlink()

    discovered = gen.discover_session_surface(root)

    assert discovered["claim_status"][surface] == "missing"
    assert any(issue["code"] == warning for issue in discovered["issues"])


@pytest.mark.parametrize(
    ("relative", "surface", "warning"),
    [
        (".mcp.json", "mcp", "W_MCP_REGISTRATION_MISMATCH"),
        (".lsp.json", "lsp", "W_LSP_REGISTRATION_MISMATCH"),
    ],
)
def test_mismatched_registration_manifest_never_claims(
    tmp_path: Path, relative: str, surface: str, warning: str
) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    (root / relative).write_text('{"unexpected":true}\n', encoding="utf-8")

    discovered = gen.discover_session_surface(root)

    assert discovered["claim_status"][surface] == "mismatch"
    assert any(issue["code"] == warning for issue in discovered["issues"])


@pytest.mark.parametrize(
    ("relative", "surface", "warning"),
    [
        (".mcp.json", "mcp", "W_MCP_REGISTRATION_MALFORMED"),
        (".lsp.json", "lsp", "W_LSP_REGISTRATION_MALFORMED"),
    ],
)
def test_malformed_registration_manifest_never_claims(
    tmp_path: Path, relative: str, surface: str, warning: str
) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    (root / relative).write_text('{"duplicate":1,"duplicate":2}\n', encoding="utf-8")

    discovered = gen.discover_session_surface(root)

    assert discovered["claim_status"][surface] == "malformed"
    assert any(issue["code"] == warning for issue in discovered["issues"])


def test_malformed_and_mismatched_sources_never_claim(tmp_path: Path) -> None:
    gen = _load_gen_module()
    malformed = _fake_repo(tmp_path / "malformed")
    (malformed / "omg_cli" / "ask" / "providers.py").write_text(
        "PROVIDERS = {\n", encoding="utf-8"
    )
    malformed_surface = gen.discover_session_surface(malformed)
    assert malformed_surface["claim_status"]["advisor_routing"] == "malformed"
    assert malformed_surface["advisor_routing"]["providers"] == {}
    assert any(
        issue["code"] == "W_ADVISOR_SOURCE_MALFORMED"
        for issue in malformed_surface["issues"]
    )

    mismatched = _fake_repo(tmp_path / "mismatched")
    tools = mismatched / "omg_cli" / "mcp" / "tools.py"
    tools.write_text(
        tools.read_text(encoding="utf-8").replace(
            "'proposal.create': handler", "'unexpected.write': handler"
        ),
        encoding="utf-8",
    )
    mismatch_surface = gen.discover_session_surface(mismatched)
    assert mismatch_surface["claim_status"]["mcp"] == "mismatch"
    assert mismatch_surface["mcp"]["operations"] == []
    assert any(
        issue["code"] == "W_MCP_SOURCE_MISMATCH"
        for issue in mismatch_surface["issues"]
    )


def test_arbitrary_root_python_is_parsed_but_never_executed(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    source = root / "omg_cli" / "ask" / "providers.py"
    source.write_text(
        "raise RuntimeError('installed source must never execute')\n"
        + source.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    surface = gen.discover_session_surface(root)

    assert surface["claim_status"]["advisor_routing"] == "claimed"
    assert surface["advisor_routing"]["providers"]["claude"]["aliases"] == ["fable"]


def test_editing_file_changes_aggregate(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    before = gen.compute_lock(root)["aggregate"]
    (root / "skills" / "omg-x" / "SKILL.md").write_text("# skill x\nchanged\n", encoding="utf-8")
    after = gen.compute_lock(root)["aggregate"]
    assert after != before


def test_read_lock_round_trip(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    lock = gen.compute_lock(root)
    out = root / "omg_capabilities.lock.json"
    out.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    loaded = gen.read_lock(root)
    assert loaded is not None
    assert loaded["aggregate"] == lock["aggregate"]
    assert loaded["files"] == lock["files"]


def test_generate_writes_valid_json(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    path = gen.write_lock(root)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "version" in data and "files" in data and "aggregate" in data
    assert "session_surface" in data and "session_surface_aggregate" in data


def test_check_exits_0_when_current_1_when_stale(tmp_path: Path) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    gen.write_lock(root)
    rc0 = subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--check", "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc0.returncode == 0, rc0.stdout + rc0.stderr

    (root / "agents" / "omg-y.md").write_text("# agent y\nstale now\n", encoding="utf-8")
    rc1 = subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--check", "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert rc1.returncode == 1
    assert rc1.stdout or rc1.stderr  # prints a diff


@pytest.mark.parametrize("drift", ["version", "session_surface"])
def test_check_rejects_non_file_surface_drift(tmp_path: Path, drift: str) -> None:
    root = _fake_repo(tmp_path)
    subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--root", str(root)],
        check=True,
        capture_output=True,
        text=True,
    )
    lock_path = root / "omg_capabilities.lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    if drift == "version":
        lock["version"] = "0.0.0-stale"
    else:
        lock["session_surface"]["mcp"]["operation_count"] = 999
        # A stale producer can forge the nested value while leaving the old
        # aggregate behind; both fields must be compared independently.
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")

    result = subprocess.run(
        [sys.executable, str(GEN_SCRIPT), "--check", "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert drift in result.stdout


def test_doctor_check_capabilities_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    monkeypatch.setattr(doctor, "plugin_root", lambda: root)

    name, level, detail = doctor.check_capabilities_lock()
    assert name == "capabilities lock (local checkout)"
    assert level == "warn"
    assert "no omg_capabilities.lock.json" in detail

    gen.write_lock(root)
    name, level, detail = doctor.check_capabilities_lock()
    assert level == "ok"
    assert "local checkout: 5 files match lock" in detail

    (root / "skills" / "omg-x" / "SKILL.md").write_text("drift\n", encoding="utf-8")
    name, level, detail = doctor.check_capabilities_lock()
    assert level == "warn"
    assert "regenerate" in detail.lower()
    assert "commit-hygiene" in detail.lower()


def test_doctor_check_capabilities_lock_rejects_surface_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen = _load_gen_module()
    root = _fake_repo(tmp_path)
    monkeypatch.setattr(doctor, "plugin_root", lambda: root)
    lock_path = gen.write_lock(root)
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    lock["session_surface"]["lsp"]["semantic_proxy_count"] = 99
    lock_path.write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")

    name, level, detail = doctor.check_capabilities_lock()
    assert name == "capabilities lock (local checkout)"
    assert level == "warn"
    assert "session surface" in detail


def test_run_soft_checks_includes_capabilities_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        doctor,
        "check_plugin_trust",
        lambda: ("plugin trust/inventory", "ok", "stub"),
    )
    monkeypatch.setattr(
        doctor,
        "check_effective_discovery_foreign",
        lambda: ("foreign plugins in discovery", "ok", "stub"),
    )
    monkeypatch.setattr(
        doctor,
        "check_global_rules",
        lambda: ("global rules", "ok", "stub"),
    )
    monkeypatch.setattr(
        doctor,
        "check_plugin_version_drift",
        lambda: ("plugin version drift", "ok", "stub"),
    )
    monkeypatch.setattr(
        doctor,
        "check_plugin_enabled",
        lambda: ("plugin enabled", "ok", "stub"),
    )
    monkeypatch.setattr(
        doctor,
        "check_capabilities_lock",
        lambda: (
            "capabilities lock (local checkout)",
            "ok",
            "local checkout: n files match lock",
        ),
    )
    monkeypatch.setattr(
        doctor,
        "check_installed_capabilities_lock",
        lambda: (
            "installed capabilities lock",
            "ok",
            "installed skills/agents match committed lock",
        ),
    )
    soft = doctor.run_soft_checks()
    names = [n for n, _, _ in soft]
    assert "capabilities lock (local checkout)" in names
    assert "installed capabilities lock" in names
    # installed check runs after local-checkout check
    assert names.index("installed capabilities lock") > names.index(
        "capabilities lock (local checkout)"
    )


def _populate_installed_like_checkout(installed: Path, *, skill_body: str = "# skill x\nbody\n") -> None:
    """Minimal skills/agents tree matching _fake_repo lock inputs."""
    skill = installed / "skills" / "omg-x" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(skill_body, encoding="utf-8")
    agent = installed / "agents" / "omg-y.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("# agent y\n", encoding="utf-8")
    (installed / "plugin.json").write_text(
        json.dumps({"name": "oh-my-grok", "version": "9.9.9"}),
        encoding="utf-8",
    )
    _populate_surface_sources(installed)


def test_doctor_check_installed_capabilities_lock_ok(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed snapshot content identical to checkout lock inputs → ok."""
    gen = _load_gen_module()
    checkout = _fake_repo(tmp_path / "checkout")
    gen.write_lock(checkout)
    installed = tmp_path / "installed"
    _populate_installed_like_checkout(installed)

    monkeypatch.setattr(doctor, "plugin_root", lambda: checkout)
    monkeypatch.setattr(
        doctor,
        "_run_grok_json",
        lambda *_a, **_k: [
            {
                "name": "oh-my-grok",
                "source": str(checkout),
                "path": str(installed),
            }
        ],
    )
    name, level, detail = doctor.check_installed_capabilities_lock()
    assert name == "installed capabilities lock"
    assert level == "ok"
    assert "match committed lock" in detail


def test_doctor_check_installed_capabilities_lock_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed SKILL.md differs → aggregate mismatch → warn."""
    gen = _load_gen_module()
    checkout = _fake_repo(tmp_path / "checkout")
    gen.write_lock(checkout)
    installed = tmp_path / "installed"
    _populate_installed_like_checkout(
        installed, skill_body="# skill x\nDRIFTED installed body\n"
    )

    monkeypatch.setattr(doctor, "plugin_root", lambda: checkout)
    monkeypatch.setattr(
        doctor,
        "_run_grok_json",
        lambda *_a, **_k: [
            {
                "name": "oh-my-grok",
                "source": str(checkout),
                "path": str(installed),
            }
        ],
    )
    name, level, detail = doctor.check_installed_capabilities_lock()
    assert name == "installed capabilities lock"
    assert level == "warn"
    assert "INSTALLED skills/agents differ" in detail
    assert "install-plugin" in detail or "plugin update" in detail


def test_doctor_check_installed_capabilities_lock_rejects_version_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gen = _load_gen_module()
    checkout = _fake_repo(tmp_path / "checkout")
    gen.write_lock(checkout)
    installed = tmp_path / "installed"
    _populate_installed_like_checkout(installed)
    (installed / "plugin.json").write_text(
        json.dumps({"name": "oh-my-grok", "version": "9.9.8"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(doctor, "plugin_root", lambda: checkout)
    monkeypatch.setattr(
        doctor,
        "_run_grok_json",
        lambda *_a, **_k: [
            {"name": "oh-my-grok", "source": str(checkout), "path": str(installed)}
        ],
    )

    name, level, detail = doctor.check_installed_capabilities_lock()
    assert name == "installed capabilities lock"
    assert level == "warn"
    assert "version/session surface" in detail


def test_doctor_check_installed_capabilities_lock_probe_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe returns None → warn, never crash."""
    monkeypatch.setattr(doctor, "_run_grok_json", lambda *_a, **_k: None)
    name, level, detail = doctor.check_installed_capabilities_lock()
    assert name == "installed capabilities lock"
    assert level == "warn"
    assert "cannot locate installed snapshot" in detail


def test_doctor_check_installed_capabilities_lock_missing_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plugin list points at a non-existent installed dir → warn."""
    checkout = _fake_repo(tmp_path / "checkout")
    missing = tmp_path / "no-such-installed"
    monkeypatch.setattr(doctor, "plugin_root", lambda: checkout)
    monkeypatch.setattr(
        doctor,
        "_run_grok_json",
        lambda *_a, **_k: [
            {
                "name": "oh-my-grok",
                "source": str(checkout),
                "path": str(missing),
            }
        ],
    )
    name, level, detail = doctor.check_installed_capabilities_lock()
    assert name == "installed capabilities lock"
    assert level == "warn"
    assert "cannot locate installed snapshot" in detail
