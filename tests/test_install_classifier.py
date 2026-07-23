"""Hermetic unit tests for install same-path classifier (A1)."""
from __future__ import annotations

import json

from scripts.omg_install_classifier import (
    classify_doctor_result,
    classify_oh_my_grok_installs,
    is_same_path_candidate,
    path_field_candidates,
)


def test_doctor_result_classifier_is_exact_and_release_fail_closed():
    assert classify_doctor_result(mode="release", rc=0, valid=True) == "installed"
    assert classify_doctor_result(mode="development", rc=2, valid=True) == "completed_with_warning"
    assert classify_doctor_result(mode="release", rc=2, valid=True) == "hard_failure"
    assert classify_doctor_result(mode="release", rc=1, valid=True) == "hard_failure"
    assert classify_doctor_result(mode="release", rc=None, valid=False) == "hard_failure"
    assert classify_doctor_result(mode="release", rc=True, valid=True) == "hard_failure"


def test_source_absent_path_snapshot_installpath_checkout_is_same_path(tmp_path):
    """A1 false-negative: OR-chain ``source or path or installPath`` picks snapshot.

    When ``source`` is absent and ``path`` is the frozen snapshot, the old
    single-field collapse never reaches ``installPath`` (or a later field) that
    still points at this checkout → force-refresh skipped. Multi-candidate
    classification must see installPath == root → same_path True.
    """
    root = tmp_path / "checkout"
    root.mkdir()
    snapshot = tmp_path / "installed-plugins" / "oh-my-grok-snap"
    snapshot.mkdir(parents=True)
    data = [
        {
            "name": "oh-my-grok",
            "path": str(snapshot),
            "installPath": str(root),
        }
    ]
    result = classify_oh_my_grok_installs(data, str(root))
    assert result["same_path"] is True
    assert result["stale"] == []


def test_source_absent_path_is_checkout_same_path(tmp_path):
    """path dual-meaning: source absent, path holds the checkout → same_path True."""
    root = tmp_path / "checkout"
    root.mkdir()
    data = [{"name": "oh-my-grok", "path": str(root)}]
    result = classify_oh_my_grok_installs(data, str(root))
    assert result["same_path"] is True
    assert result["stale"] == []


def test_source_checkout_and_path_snapshot_is_same_path(tmp_path):
    """Fable: BOTH source (checkout) and path (different snapshot) present →
    same_path True via the source candidate (not the snapshot)."""
    root = tmp_path / "checkout"
    root.mkdir()
    snapshot = tmp_path / "installed-plugins" / "oh-my-grok-abc"
    snapshot.mkdir(parents=True)
    data = [
        {
            "name": "oh-my-grok",
            "source": str(root),
            "path": str(snapshot),
            "installPath": str(snapshot),
        }
    ]
    result = classify_oh_my_grok_installs(data, str(root))
    assert result["same_path"] is True
    assert result["stale"] == []


def test_source_absent_only_snapshot_path_is_not_same_path(tmp_path):
    """Snapshot-only install path must NOT false-positive as this checkout."""
    root = tmp_path / "checkout"
    root.mkdir()
    snapshot = tmp_path / "installed-plugins" / "oh-my-grok-xyz"
    snapshot.mkdir(parents=True)
    data = [{"name": "oh-my-grok", "path": str(snapshot)}]
    result = classify_oh_my_grok_installs(data, str(root))
    assert result["same_path"] is False
    assert len(result["stale"]) >= 1


def test_symlinked_root_matches_realpath(tmp_path):
    """Symlinked root → real checkout: same_path True."""
    real = tmp_path / "real-checkout"
    real.mkdir()
    link = tmp_path / "link-checkout"
    link.symlink_to(real)
    data = [{"name": "oh-my-grok", "source": str(real)}]
    result = classify_oh_my_grok_installs(data, str(link))
    assert result["same_path"] is True


def test_raw_equal_without_needing_realpath_match(tmp_path):
    """Raw string equality remains an extra fallback (never removes a match)."""
    root = tmp_path / "checkout"
    root.mkdir()
    # Use the exact string; realpath also matches — pin path_field_candidates + helper
    assert is_same_path_candidate(str(root), str(root)) is True
    data = [{"name": "oh-my-grok", "source": str(root)}]
    assert classify_oh_my_grok_installs(data, str(root))["same_path"] is True


def test_genuinely_different_path_is_false_mandatory(tmp_path):
    """MANDATORY Fable condition: distinct real path → same_path False.

    A false-POSITIVE here is worse than the false-negative: it would
    uninstall+reinstall someone else's install.
    """
    root = tmp_path / "checkout-a"
    root.mkdir()
    other = tmp_path / "checkout-b"
    other.mkdir()
    data = [
        {
            "name": "oh-my-grok",
            "source": str(other),
            "path": str(other),
            "installPath": str(other),
        }
    ]
    result = classify_oh_my_grok_installs(data, str(root))
    assert result["same_path"] is False
    assert len(result["stale"]) >= 1


def test_path_field_candidates_are_independent():
    item = {
        "source": "/a",
        "path": "/b",
        "installPath": "/c",
        "install_path": "/d",
    }
    cands = path_field_candidates(item)
    assert cands == ["/a", "/b", "/c", "/d"]


def test_list_json_string_and_wrapped_plugins_key(tmp_path):
    root = tmp_path / "co"
    root.mkdir()
    payload = {"plugins": [{"name": "oh-my-grok", "source": str(root)}]}
    assert classify_oh_my_grok_installs(json.dumps(payload), str(root))["same_path"] is True
    assert classify_oh_my_grok_installs(payload, str(root))["same_path"] is True


def test_non_omg_entries_ignored(tmp_path):
    root = tmp_path / "co"
    root.mkdir()
    data = [{"name": "other-plugin", "source": str(root)}]
    result = classify_oh_my_grok_installs(data, str(root))
    assert result["same_path"] is False
    assert result["stale"] == []
