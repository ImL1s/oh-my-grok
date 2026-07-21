"""Tests for omg_cli.guidance — OMG global rules injection reconciler."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omg_cli.guidance import (
    OMG_END,
    OMG_START,
    USER_POLICY_END,
    USER_POLICY_START,
    GuidanceCorruptionError,
    install_global_rules,
    reconcile_rules_text,
    render_managed_block,
    rules_status,
)


def test_render_managed_block_substitutes_version_and_hash():
    text = render_managed_block(version="1.2.3")
    assert OMG_START in text
    assert OMG_END in text
    assert "{{VERSION}}" not in text
    assert "{{SOURCE_HASH}}" not in text
    assert "<!-- OMG:VERSION:1.2.3 -->" in text
    m = re.search(r"<!-- OMG:SOURCE-HASH:([0-9a-f]{64}) -->", text)
    assert m is not None, "expected non-empty 64-hex SOURCE-HASH"
    assert text.endswith("\n")
    assert not text.endswith("\n\n")


def test_install_create_then_unchanged(tmp_path: Path):
    path, action = install_global_rules(version="1.0.0", home=tmp_path)
    assert action == "created"
    assert path == tmp_path / "rules" / "omg.md"
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert OMG_START in content
    assert OMG_END in content

    path2, action2 = install_global_rules(version="1.0.0", home=tmp_path)
    assert path2 == path
    assert action2 == "unchanged"


def test_idempotency_preserves_user_policy_block(tmp_path: Path):
    install_global_rules(version="1.0.0", home=tmp_path)
    path = tmp_path / "rules" / "omg.md"
    user_block = (
        f"{USER_POLICY_START}\n"
        "my custom rule\n"
        f"{USER_POLICY_END}\n"
    )
    original = path.read_text(encoding="utf-8")
    path.write_text(original.rstrip("\n") + "\n\n" + user_block, encoding="utf-8")

    _, action = install_global_rules(version="1.0.0", home=tmp_path)
    assert action in ("updated", "unchanged")
    final = path.read_text(encoding="utf-8")
    assert "my custom rule" in final
    assert USER_POLICY_START in final
    assert USER_POLICY_END in final
    assert final.count(OMG_START) == 1
    assert final.count(OMG_END) == 1
    # custom rule text survives verbatim
    assert "my custom rule" in final


def test_foreign_file_appends_without_clobber(tmp_path: Path):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir(parents=True)
    path = rules_dir / "omg.md"
    path.write_text("user notes\n", encoding="utf-8")

    _, action = install_global_rules(version="1.0.0", home=tmp_path)
    assert action == "updated"
    content = path.read_text(encoding="utf-8")
    assert content.startswith("user notes")
    assert "user notes" in content
    assert OMG_START in content
    assert OMG_END in content


def test_version_change_updates_managed_preserves_user(tmp_path: Path):
    install_global_rules(version="9.9.9", home=tmp_path)
    path = tmp_path / "rules" / "omg.md"
    user_block = (
        f"{USER_POLICY_START}\n"
        "keep me across version bump\n"
        f"{USER_POLICY_END}\n"
    )
    original = path.read_text(encoding="utf-8")
    path.write_text(original.rstrip("\n") + "\n\n" + user_block, encoding="utf-8")

    _, action = install_global_rules(version="10.0.0", home=tmp_path)
    assert action == "updated"
    final = path.read_text(encoding="utf-8")
    assert "<!-- OMG:VERSION:10.0.0 -->" in final
    assert "<!-- OMG:VERSION:9.9.9 -->" not in final
    assert "keep me across version bump" in final
    assert final.count(OMG_START) == 1


def test_corruption_double_start_raises_and_status_reports():
    corrupt = f"{OMG_START}\nfoo\n{OMG_END}\n{OMG_START}\nbar\n{OMG_END}\n"
    new_block = render_managed_block(version="1.0.0")
    with pytest.raises(GuidanceCorruptionError):
        reconcile_rules_text(corrupt, new_block)


def test_corruption_status_does_not_raise(tmp_path: Path):
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir(parents=True)
    path = rules_dir / "omg.md"
    path.write_text(
        f"{OMG_START}\nfoo\n{OMG_END}\n{OMG_START}\nbar\n{OMG_END}\n",
        encoding="utf-8",
    )
    status = rules_status(version="1.0.0", home=tmp_path)
    assert status["corrupt"] is True
    assert status["path"] == str(path)


def test_rules_status_clean_install(tmp_path: Path):
    install_global_rules(version="2.0.0", home=tmp_path)
    status = rules_status(version="2.0.0", home=tmp_path)
    assert status["present"] is True
    assert status["corrupt"] is False
    assert status["installed_version"] == "2.0.0"
    assert status["expected_version"] == "2.0.0"
    assert status["version_ok"] is True
    assert status["source_hash_ok"] is True
    assert status["drift"] is False


def test_rules_status_detects_hand_edit_drift(tmp_path: Path):
    install_global_rules(version="2.0.0", home=tmp_path)
    path = tmp_path / "rules" / "omg.md"
    text = path.read_text(encoding="utf-8")
    # Hand-edit a line inside the managed markers
    edited = text.replace(
        "always-loaded contract",
        "HAND-EDITED always-loaded contract",
        1,
    )
    assert edited != text
    path.write_text(edited, encoding="utf-8")

    status = rules_status(version="2.0.0", home=tmp_path)
    assert status["present"] is True
    assert status["corrupt"] is False
    # Hand-edit should break source_hash_ok and/or mark drift
    assert status["drift"] is True or status["source_hash_ok"] is False
