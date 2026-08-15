"""Confined atomic apply (#76 PR1, story s3)."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
import socket
import stat
import sys
import tempfile
from pathlib import Path

import pytest

from omg_cli.hash_edit import (
    APPLY_RESULT_KIND,
    HashEditAmbiguousError,
    HashEditApplyError,
    HashEditBindError,
    HashEditConcurrencyError,
    HashEditCurrentFact,
    HashEditInputError,
    HashEditPathError,
    HashEditPlanV1,
    HashEditStaleError,
    apply_hash_edit,
    parse_hash_edit_descriptor,
    plan_hash_edit,
    read_confined_regular_file,
)
from omg_cli.hash_edit.descriptor import HASH_EDIT_KIND

pytestmark = [
    pytest.mark.platform,
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="apply_hash_edit requires POSIX O_NOFOLLOW/fcntl",
    ),
]


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _payload(current: str, **overrides: object) -> dict[str, object]:
    old_text = str(overrides.pop("old_text", "alpha"))
    replacement = str(overrides.pop("replacement", "beta"))
    before_context = str(overrides.pop("before_context", "before\n"))
    after_context = str(overrides.pop("after_context", "\nafter"))
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": HASH_EDIT_KIND,
        "edit_id": "edit-apply-1",
        "producer": "omg.hash_edit.test",
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


def _write(path: Path, text: str, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    if mode is not None:
        path.chmod(mode)


def _plan_for(workspace: Path, current: str, **overrides: object) -> tuple[dict[str, object], HashEditPlanV1]:
    payload = _payload(current, **overrides)
    target = workspace / str(payload["path"])
    _write(target, current)
    fact = HashEditCurrentFact(path=str(payload["path"]), current_bytes=current.encode())
    return payload, plan_hash_edit(payload, fact)


def test_exact_apply_writes_bytes_and_copy_safe_result(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\n"
    payload, plan = _plan_for(tmp_path, current)
    result = apply_hash_edit(tmp_path, payload, plan)
    target = tmp_path / "docs" / "example.md"
    assert target.read_text(encoding="utf-8") == "before\nbeta\nafter\n"
    assert result.ok is True
    assert result.kind == APPLY_RESULT_KIND
    assert result.path == "docs/example.md"
    assert result.before_sha256 == _digest_text(current)
    assert result.after_sha256 == _digest_text("before\nbeta\nafter\n")
    assert result.descriptor_digest == parse_hash_edit_descriptor(payload).digest()
    dumped = repr(result)
    assert "before\nalpha" not in dumped
    assert str(tmp_path) not in dumped
    assert not hasattr(result, "unified_diff")
    assert not hasattr(result, "replacement")
    assert not list(tmp_path.rglob("*.hash-edit.lock"))


def test_unique_shifted_apply(tmp_path: Path) -> None:
    original = "keep\nalpha\nend\n"
    shifted = "header\nkeep\nalpha\nend\n"
    payload = _payload(
        original,
        old_text="alpha",
        before_context="keep\n",
        after_context="\nend\n",
        revalidation="unique_shift",
    )
    target = tmp_path / "docs" / "example.md"
    _write(target, shifted)
    plan = plan_hash_edit(
        payload,
        HashEditCurrentFact(path="docs/example.md", current_bytes=shifted.encode()),
    )
    assert plan.rebased is True
    result = apply_hash_edit(tmp_path, payload, plan)
    assert target.read_text(encoding="utf-8") == "header\nkeep\nbeta\nend\n"
    assert result.rebased is True


def test_concurrent_change_between_plan_and_apply_leaves_bytes(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\n"
    payload, plan = _plan_for(tmp_path, current)
    target = tmp_path / "docs" / "example.md"
    target.write_text("before\nalpha\nafter\nCHANGED\n", encoding="utf-8")
    before = target.read_bytes()
    with pytest.raises(HashEditConcurrencyError):
        apply_hash_edit(tmp_path, payload, plan)
    assert target.read_bytes() == before


def test_stale_unique_shift_after_zero_match_does_not_write(tmp_path: Path) -> None:
    original = "before\nalpha\nafter\n"
    payload = _payload(original, revalidation="unique_shift")
    target = tmp_path / "docs" / "example.md"
    _write(target, original)
    plan = plan_hash_edit(
        payload,
        HashEditCurrentFact(path="docs/example.md", current_bytes=original.encode()),
    )
    changed = "before\nomega\nafter\n"
    target.write_text(changed, encoding="utf-8")
    with pytest.raises(HashEditConcurrencyError):
        apply_hash_edit(tmp_path, payload, plan)
    assert target.read_text(encoding="utf-8") == changed


def test_forged_plan_on_ambiguous_file_does_not_write(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\nmid\nbefore\nalpha\nafter\n"
    payload = _payload(current)
    target = tmp_path / "docs" / "example.md"
    _write(target, current)
    desc = parse_hash_edit_descriptor(payload)
    empty_diff = ""
    forged = HashEditPlanV1(
        descriptor_digest=desc.digest(),
        path="docs/example.md",
        before_sha256=_digest_text(current),
        after_sha256="ab" * 32,
        start_offset=0,
        end_offset=5,
        start_line=1,
        end_line=1,
        rebased=False,
        unified_diff=empty_diff,
        unified_diff_sha256=_digest_text(empty_diff),
    )
    with pytest.raises(HashEditAmbiguousError):
        apply_hash_edit(tmp_path, payload, forged)
    assert target.read_text(encoding="utf-8") == current


def test_wrong_hint_and_wrong_context_do_not_write(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\n"
    target = tmp_path / "docs" / "example.md"
    _write(target, current)
    hinted = _payload(current, original_start_line=1, original_end_line=1)
    fact = HashEditCurrentFact(path="docs/example.md", current_bytes=current.encode())
    with pytest.raises(HashEditBindError):
        plan_hash_edit(hinted, fact)
    assert target.read_text(encoding="utf-8") == current

    original = "before\nalpha\nafter"
    wrong = "zz\nalpha\nww\n"
    payload = _payload(
        original,
        old_text="alpha",
        before_context="before\n",
        after_context="\nafter",
        revalidation="unique_shift",
    )
    _write(target, wrong)
    with pytest.raises(HashEditStaleError):
        plan_hash_edit(
            payload,
            HashEditCurrentFact(path="docs/example.md", current_bytes=wrong.encode()),
        )
    assert target.read_text(encoding="utf-8") == wrong


def test_symlink_leaf_and_ancestor_rejected(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\n"
    payload, plan = _plan_for(tmp_path, current)
    target = tmp_path / "docs" / "example.md"
    outside = tmp_path / "outside.md"
    outside.write_text(current, encoding="utf-8")
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(HashEditPathError, match="symlink"):
        apply_hash_edit(tmp_path, payload, plan)
    assert outside.read_text(encoding="utf-8") == current

    target.unlink()
    _write(target, current)
    payload2, plan2 = _plan_for(tmp_path, current)
    docs = tmp_path / "docs"
    real_docs = tmp_path / "real-docs"
    real_docs.mkdir()
    (real_docs / "example.md").write_text(current, encoding="utf-8")
    docs.unlink() if docs.is_symlink() else None
    # replace docs dir with symlink
    for child in docs.iterdir():
        child.unlink()
    docs.rmdir()
    docs.symlink_to(real_docs, target_is_directory=True)
    with pytest.raises(HashEditPathError, match="symlink"):
        apply_hash_edit(tmp_path, payload2, plan2)
    assert (real_docs / "example.md").read_text(encoding="utf-8") == current


def test_path_escape_absolute_backslash_rejected(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\n"
    _write(tmp_path / "docs" / "example.md", current)
    for path in ("/etc/passwd", "..\\secret", "../secret", "docs\\example.md"):
        with pytest.raises(Exception):
            parse_hash_edit_descriptor(_payload(current, path=path))
    root_link = tmp_path / "rootlink"
    root_link.symlink_to(tmp_path, target_is_directory=True)
    payload, plan = _plan_for(tmp_path, current)
    with pytest.raises(HashEditPathError, match="symlink"):
        apply_hash_edit(root_link, payload, plan)


def test_fifo_rejected_and_bytes_unchanged(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\n"
    payload, plan = _plan_for(tmp_path, current)
    target = tmp_path / "docs" / "example.md"
    target.unlink()
    os.mkfifo(target)
    try:
        with pytest.raises(HashEditPathError, match="regular|fifo"):
            apply_hash_edit(tmp_path, payload, plan)
        assert stat.S_ISFIFO(target.stat().st_mode)
    finally:
        target.unlink()


def test_unix_socket_leaf_rejected() -> None:
    current = "before\nalpha\nafter\n"
    # pytest tmp_path is often longer than AF_UNIX sockaddr (~104 bytes on macOS).
    root = Path(tempfile.mkdtemp(prefix="he", dir="/tmp"))
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        payload, plan = _plan_for(root, current, path="s")
        target = root / "s"
        target.unlink()
        server.bind(str(target))
        with pytest.raises(HashEditPathError, match="regular|socket|fifo|device"):
            apply_hash_edit(root, payload, plan)
        assert stat.S_ISSOCK(os.lstat(target).st_mode)
    finally:
        server.close()
        shutil.rmtree(root, ignore_errors=True)


def test_device_leaf_rejected_if_mknod_permitted(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\n"
    payload, plan = _plan_for(tmp_path, current)
    target = tmp_path / "docs" / "example.md"
    target.unlink()
    try:
        os.mknod(target, mode=stat.S_IFCHR | 0o600, device=os.makedev(1, 3))
    except (OSError, AttributeError, PermissionError) as exc:
        pytest.skip(f"chr device mknod not permitted: {exc}")
    try:
        if not stat.S_ISCHR(os.lstat(target).st_mode):
            pytest.skip("mknod did not create a character device")
        with pytest.raises(HashEditPathError, match="regular|device|fifo|socket"):
            apply_hash_edit(tmp_path, payload, plan)
        assert stat.S_ISCHR(os.lstat(target).st_mode)
    finally:
        try:
            target.unlink()
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                raise


def test_multi_link_rejected(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\n"
    payload, plan = _plan_for(tmp_path, current)
    target = tmp_path / "docs" / "example.md"
    other = tmp_path / "docs" / "link2.md"
    other.hardlink_to(target)
    before = target.read_bytes()
    with pytest.raises(HashEditPathError, match="single-link"):
        apply_hash_edit(tmp_path, payload, plan)
    assert target.read_bytes() == before


def test_invalid_utf8_file_rejected(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\n"
    payload, plan = _plan_for(tmp_path, current)
    target = tmp_path / "docs" / "example.md"
    # Keep size so a naive reader might proceed; digest will not match plan.
    target.write_bytes(b"\xff\xfe" + b"x" * (len(current.encode()) - 2))
    before = target.read_bytes()
    with pytest.raises((HashEditConcurrencyError, HashEditStaleError, HashEditBindError)):
        apply_hash_edit(tmp_path, payload, plan)
    assert target.read_bytes() == before


def test_crlf_unicode_and_terminal_newline(tmp_path: Path) -> None:
    current = "before\r\n漢字\r\n"
    payload, plan = _plan_for(
        tmp_path,
        current,
        old_text="漢字",
        replacement="仮名",
        before_context="before\r\n",
        after_context="\r\n",
    )
    apply_hash_edit(tmp_path, payload, plan)
    assert (tmp_path / "docs" / "example.md").read_bytes() == "before\r\n仮名\r\n".encode()

    no_nl = "before\nalpha"
    payload2, plan2 = _plan_for(
        tmp_path,
        no_nl,
        old_text="alpha",
        replacement="beta",
        before_context="before\n",
        after_context="",
    )
    apply_hash_edit(tmp_path, payload2, plan2)
    body = (tmp_path / "docs" / "example.md").read_bytes()
    assert body == b"before\nbeta"
    assert not body.endswith(b"\n")


def test_mode_preservation_and_noop_idempotent(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\n"
    payload, plan = _plan_for(tmp_path, current)
    target = tmp_path / "docs" / "example.md"
    target.chmod(0o755)
    result = apply_hash_edit(tmp_path, payload, plan)
    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert result.preserved_mode == 0o755

    same = _payload(current, old_text="alpha", replacement="alpha")
    _write(target, current, mode=0o644)
    plan_same = plan_hash_edit(
        same,
        HashEditCurrentFact(path="docs/example.md", current_bytes=current.encode()),
    )
    result_same = apply_hash_edit(tmp_path, same, plan_same)
    assert target.read_text(encoding="utf-8") == current
    assert result_same.before_sha256 == result_same.after_sha256
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_single_component_path_applies_under_workspace_root(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\n"
    payload = _payload(current, path="README.md")
    target = tmp_path / "README.md"
    _write(target, current)
    plan = plan_hash_edit(
        payload,
        HashEditCurrentFact(path="README.md", current_bytes=current.encode()),
    )
    result = apply_hash_edit(tmp_path, payload, plan)
    assert target.read_text(encoding="utf-8") == "before\nbeta\nafter\n"
    assert result.path == "README.md"
    assert not list(tmp_path.rglob("*.hash-edit.lock"))


def test_apply_rejects_unbound_plan_and_missing_target(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\n"
    payload, plan = _plan_for(tmp_path, current)
    other = _payload(current, path="docs/other.md")
    with pytest.raises(HashEditApplyError, match="not bound"):
        apply_hash_edit(tmp_path, other, plan)
    (tmp_path / "docs" / "example.md").unlink()
    with pytest.raises(HashEditPathError, match="does not exist"):
        apply_hash_edit(tmp_path, payload, plan)


def test_apply_wrong_hint_forged_plan_leaves_bytes(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\n"
    hinted = _payload(current, original_start_line=1, original_end_line=1)
    target = tmp_path / "docs" / "example.md"
    _write(target, current)
    desc = parse_hash_edit_descriptor(hinted)
    empty = ""
    forged = HashEditPlanV1(
        descriptor_digest=desc.digest(),
        path="docs/example.md",
        before_sha256=_digest_text(current),
        after_sha256="ab" * 32,
        start_offset=0,
        end_offset=5,
        start_line=1,
        end_line=1,
        rebased=False,
        unified_diff=empty,
        unified_diff_sha256=_digest_text(empty),
    )
    with pytest.raises(HashEditBindError):
        apply_hash_edit(tmp_path, hinted, forged)
    assert target.read_text(encoding="utf-8") == current
    assert not list(tmp_path.rglob("*.hash-edit.lock"))


def test_invalid_utf8_matching_digest_is_typed_reject(tmp_path: Path) -> None:
    raw = b"\xff\xfe not utf8"
    payload = _payload("before\nalpha\nafter\n")
    target = tmp_path / "docs" / "example.md"
    _write(target, "before\nalpha\nafter\n")
    desc = parse_hash_edit_descriptor(payload)
    target.write_bytes(raw)
    empty = ""
    forged = HashEditPlanV1(
        descriptor_digest=desc.digest(),
        path="docs/example.md",
        before_sha256=hashlib.sha256(raw).hexdigest(),
        after_sha256="ab" * 32,
        start_offset=0,
        end_offset=1,
        start_line=1,
        end_line=1,
        rebased=False,
        unified_diff=empty,
        unified_diff_sha256=_digest_text(empty),
    )
    with pytest.raises(HashEditInputError, match="UTF-8"):
        apply_hash_edit(tmp_path, payload, forged)
    assert target.read_bytes() == raw


def test_unique_shift_does_not_rebind_after_another_unique_hit(tmp_path: Path) -> None:
    original = "keep\nalpha\nend\n"
    first = "header\nkeep\nalpha\nend\n"
    second = "other\nkeep\nalpha\nend\n"
    payload = _payload(
        original,
        old_text="alpha",
        before_context="keep\n",
        after_context="\nend\n",
        revalidation="unique_shift",
    )
    target = tmp_path / "docs" / "example.md"
    _write(target, first)
    plan = plan_hash_edit(
        payload,
        HashEditCurrentFact(path="docs/example.md", current_bytes=first.encode()),
    )
    target.write_text(second, encoding="utf-8")
    with pytest.raises(HashEditConcurrencyError):
        apply_hash_edit(tmp_path, payload, plan)
    assert target.read_text(encoding="utf-8") == second


def test_atomic_write_failure_leaves_original_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    current = "before\nalpha\nafter\n"
    payload, plan = _plan_for(tmp_path, current)
    target = tmp_path / "docs" / "example.md"

    def boom(*_args, **_kwargs):
        raise OSError("injected atomic failure")

    monkeypatch.setattr("omg_cli.hash_edit.apply.atomic_write_bytes_at", boom)
    with pytest.raises(HashEditApplyError, match="atomic replace failed"):
        apply_hash_edit(tmp_path, payload, plan)
    assert target.read_text(encoding="utf-8") == current


def test_atomic_failure_wrong_kind_leaves_directory(tmp_path: Path) -> None:
    current = "before\nalpha\nafter\n"
    payload, plan = _plan_for(tmp_path, current)
    target = tmp_path / "docs" / "example.md"
    target.unlink()
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("stay", encoding="utf-8")
    with pytest.raises(HashEditPathError):
        apply_hash_edit(tmp_path, payload, plan)
    assert marker.read_text(encoding="utf-8") == "stay"


def test_confined_read_accumulates_short_os_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "docs" / "example.md"
    payload = b"short-read-accumulation-body"
    _write(target, payload.decode("utf-8"))
    real_read = os.read

    def _one_byte(fd: int, n: int) -> bytes:
        return real_read(fd, 1 if n else 0)

    monkeypatch.setattr(os, "read", _one_byte)
    assert read_confined_regular_file(tmp_path, "docs/example.md") == payload
