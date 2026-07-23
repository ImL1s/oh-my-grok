from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from omg_cli.contracts.state_schemas import ContractValidationError
from omg_cli.contracts.writer_chain import (
    collect_dirty_records,
    owner_for_path,
    parse_raw_diff_z,
    verify_dirty_ownership,
    verify_final_candidate,
)


OWNERSHIP = {
    "W0": ("owned/**", "ignored.dat", ".gitignore"),
    "W1": ("other/**", "vendor/**"),
}


def _git(root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def _repo(root: Path, *, agents: bool = True) -> str:
    root.mkdir(parents=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "w0@example.invalid")
    _git(root, "config", "user.name", "W0 Test")
    (root / "owned").mkdir()
    (root / "other").mkdir()
    (root / "owned" / "a.txt").write_text("base\n", encoding="utf-8")
    (root / "other" / "b.txt").write_text("base\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored.dat\n", encoding="utf-8")
    if agents:
        (root / "AGENTS.md").write_text("immutable\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    return _git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("staged", "base_to_index"),
        ("unstaged", "index_to_worktree"),
        ("delete", "base_to_index"),
        ("rename", "base_to_index"),
        ("chmod", "index_to_worktree"),
        ("untracked", "untracked"),
        ("force_ignored", "cached_ignored"),
    ],
)
def test_inclusive_dirty_oracle_observes_every_worktree_surface(
    tmp_path: Path, mutation: str, expected: str
) -> None:
    root = tmp_path / mutation
    base = _repo(root)
    path = root / "owned" / "a.txt"
    if mutation == "staged":
        path.write_text("staged\n", encoding="utf-8")
        _git(root, "add", "owned/a.txt")
    elif mutation == "unstaged":
        path.write_text("unstaged\n", encoding="utf-8")
    elif mutation == "delete":
        path.unlink()
        _git(root, "add", "-u", "owned/a.txt")
    elif mutation == "rename":
        _git(root, "mv", "owned/a.txt", "owned/renamed.txt")
    elif mutation == "chmod":
        os.chmod(path, 0o755)
    elif mutation == "untracked":
        (root / "owned" / "new.txt").write_text("new\n", encoding="utf-8")
    else:
        (root / "ignored.dat").write_text("forced\n", encoding="utf-8")
        _git(root, "add", "-f", "ignored.dat")

    records = verify_dirty_ownership(root, base, OWNERSHIP)
    assert records
    assert {record["owner"] for record in records} == {"W0"}
    assert expected in {record["source"] for record in records}
    if mutation == "rename":
        rename = next(record for record in records if record["old_path"])
        assert (rename["old_path"], rename["path"]) == (
            "owned/a.txt",
            "owned/renamed.txt",
        )


def test_dirty_submodule_is_detected_recursively(tmp_path: Path) -> None:
    child = tmp_path / "child"
    _repo(child, agents=False)
    root = tmp_path / "parent"
    _repo(root)
    _git(root, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(child), "vendor/sub")
    _git(root, "commit", "-q", "-am", "add submodule")
    base = _git(root, "rev-parse", "HEAD")
    (root / "vendor" / "sub" / "owned" / "a.txt").write_text("dirty\n", encoding="utf-8")

    records = collect_dirty_records(root, base)
    submodule = next(record for record in records if record.kind == "submodule")
    assert submodule.path == "vendor/sub"
    assert verify_dirty_ownership(root, base, OWNERSHIP)[0]["owner"] == "W1"


def test_unowned_overlap_escape_and_cross_owner_rename_fail_closed(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base = _repo(root)
    (root / "rogue.txt").write_text("rogue\n", encoding="utf-8")
    with pytest.raises(ContractValidationError, match="exactly one owner"):
        verify_dirty_ownership(root, base, OWNERSHIP)
    with pytest.raises(ContractValidationError, match="exactly one owner"):
        owner_for_path("owned/a.txt", {"A": ("owned/**",), "B": ("owned/a.txt",)})
    with pytest.raises(ContractValidationError, match="unsafe repository path"):
        owner_for_path("../escape", OWNERSHIP)

    root = tmp_path / "rename"
    base = _repo(root)
    _git(root, "mv", "owned/a.txt", "other/a.txt")
    with pytest.raises(ContractValidationError, match="crosses writer ownership"):
        verify_dirty_ownership(root, base, OWNERSHIP)


@pytest.mark.parametrize("mutation", ["add", "content", "delete", "rename", "chmod"])
def test_every_agents_md_mutation_is_rejected(tmp_path: Path, mutation: str) -> None:
    root = tmp_path / mutation
    base = _repo(root, agents=mutation != "add")
    agents = root / "AGENTS.md"
    if mutation == "add":
        agents.write_text("new\n", encoding="utf-8")
    elif mutation == "content":
        agents.write_text("changed\n", encoding="utf-8")
    elif mutation == "delete":
        agents.unlink()
        _git(root, "add", "-u", "AGENTS.md")
    elif mutation == "rename":
        _git(root, "mv", "AGENTS.md", "owned/AGENTS-renamed.md")
    else:
        os.chmod(agents, 0o755)
    with pytest.raises(ContractValidationError, match="AGENTS.md is immutable"):
        verify_dirty_ownership(root, base, {**OWNERSHIP, "W2": ("AGENTS.md",)})


def test_raw_diff_parser_preserves_modes_oids_and_rename_pairs() -> None:
    body = (
        b":100644 100755 " + b"a" * 40 + b" " + b"b" * 40
        + b" R100\0owned/a.txt\0owned/b.txt\0"
    )
    records = parse_raw_diff_z(body, source="fixture")
    assert len(records) == 1
    assert records[0].old_mode == "100644" and records[0].new_mode == "100755"
    assert records[0].old_path == "owned/a.txt" and records[0].path == "owned/b.txt"
    with pytest.raises(ContractValidationError, match="NUL terminated"):
        parse_raw_diff_z(body.rstrip(b"\0"), source="fixture")


def test_final_candidate_requires_clean_single_parent_and_remote_old_oid(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    base = _repo(root)
    bare = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(bare))
    branch = _git(root, "branch", "--show-current")
    _git(root, "remote", "add", "origin", str(bare))
    _git(root, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")

    (root / "owned" / "a.txt").write_text("candidate\n", encoding="utf-8")
    _git(root, "add", "owned/a.txt")
    _git(root, "commit", "-q", "-m", "candidate")
    candidate = _git(root, "rev-parse", "HEAD")
    records = verify_final_candidate(
        root,
        base_commit=base,
        candidate_commit=candidate,
        ownership=OWNERSHIP,
        remote=str(bare),
        approved_branch=branch,
        approved_remote_old_oid=base,
    )
    assert records and records[0]["owner"] == "W0"
    with pytest.raises(ContractValidationError, match="remote old OID drifted"):
        verify_final_candidate(
            root,
            base_commit=base,
            candidate_commit=candidate,
            ownership=OWNERSHIP,
            remote=str(bare),
            approved_branch=branch,
            approved_remote_old_oid="f" * 40,
        )

    # A commit with the wrong parent is never a valid candidate for this frozen base.
    with pytest.raises(ContractValidationError, match="exactly frozen_base_commit"):
        verify_final_candidate(
            root,
            base_commit="e" * 40,
            candidate_commit=candidate,
            ownership=OWNERSHIP,
        )
