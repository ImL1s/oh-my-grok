"""Pure planner contract (#76 PR1, story s2). No filesystem mutation."""

from __future__ import annotations

import hashlib
import inspect
import unicodedata

import pytest

from omg_cli.hash_edit import (
    HashEditAmbiguousError,
    HashEditBindError,
    HashEditCurrentFact,
    HashEditInputError,
    HashEditPlanV1,
    HashEditPlannerError,
    HashEditStaleError,
    parse_hash_edit_descriptor,
    plan_hash_edit,
)
from omg_cli.hash_edit import planner as planner_mod
from omg_cli.hash_edit.descriptor import HASH_EDIT_KIND
from omg_cli.hash_edit.planner import MAX_PLAN_FILE_BYTES, unified_diff_text


def _digest_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _digest_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _payload(current: str, **overrides: object) -> dict[str, object]:
    old_text = str(overrides.pop("old_text", "alpha"))
    replacement = str(overrides.pop("replacement", "beta"))
    before_context = str(overrides.pop("before_context", "before\n"))
    after_context = str(overrides.pop("after_context", "\nafter"))
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": HASH_EDIT_KIND,
        "edit_id": "edit-plan-1",
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


def _plan(current: str, **overrides: object) -> HashEditPlanV1:
    payload = _payload(current, **overrides)
    fact = HashEditCurrentFact(path=str(payload["path"]), current_bytes=current.encode("utf-8"))
    return plan_hash_edit(payload, fact)


def test_planner_module_has_no_filesystem_or_clock_imports() -> None:
    assert planner_mod.__name__ == "omg_cli.hash_edit.planner"
    forbidden = {"os", "pathlib", "subprocess", "socket", "urllib", "datetime", "time"}
    assert forbidden.isdisjoint(set(planner_mod.__dict__))
    source = inspect.getsource(planner_mod)
    assert "datetime.now" not in source
    assert "time.time" not in source
    assert "os.open" not in source


def test_exact_base_plan_offsets_digest_and_diff() -> None:
    current = "before\nalpha\nafter\n"
    plan = _plan(current)
    assert plan.rebased is False
    assert plan.path == "docs/example.md"
    assert plan.before_sha256 == _digest_text(current)
    expected_after = "before\nbeta\nafter\n"
    assert plan.after_sha256 == _digest_text(expected_after)
    assert current.encode("utf-8")[plan.start_offset : plan.end_offset] == b"alpha"
    assert plan.start_line == 2 and plan.end_line == 2
    assert plan.descriptor_digest == parse_hash_edit_descriptor(
        _payload(current)
    ).digest()
    assert plan.unified_diff_sha256 == _digest_text(plan.unified_diff)
    assert "alpha" in plan.unified_diff and "beta" in plan.unified_diff
    assert plan.unified_diff == unified_diff_text("docs/example.md", current, expected_after)
    again = _plan(current)
    assert again.unified_diff == plan.unified_diff
    assert again.unified_diff_sha256 == plan.unified_diff_sha256


def test_unique_shifted_context_is_rebased() -> None:
    original = "keep\nalpha\nend\n"
    shifted = "header\nkeep\nalpha\nend\n"
    payload = _payload(
        original,
        old_text="alpha",
        before_context="keep\n",
        after_context="\nend\n",
        revalidation="unique_shift",
    )
    fact = HashEditCurrentFact(path="docs/example.md", current_bytes=shifted.encode())
    plan = plan_hash_edit(payload, fact)
    assert plan.rebased is True
    assert plan.before_sha256 == _digest_text(shifted)
    assert shifted.encode()[plan.start_offset : plan.end_offset] == b"alpha"
    assert plan.start_line == 3
    assert plan.after_sha256 == _digest_text("header\nkeep\nbeta\nend\n")


def test_require_base_digest_mismatch_is_stale_even_if_text_matches() -> None:
    original = "before\nalpha\nafter\n"
    shifted = "x\nbefore\nalpha\nafter\n"
    payload = _payload(original, revalidation="require_base")
    fact = HashEditCurrentFact(path="docs/example.md", current_bytes=shifted.encode())
    with pytest.raises(HashEditStaleError, match="require_base"):
        plan_hash_edit(payload, fact)


def test_omitted_revalidation_defaults_to_require_base() -> None:
    original = "before\nalpha\nafter\n"
    shifted = "x\nbefore\nalpha\nafter\n"
    payload = _payload(original)
    assert "revalidation" not in payload
    fact = HashEditCurrentFact(path="docs/example.md", current_bytes=shifted.encode())
    with pytest.raises(HashEditStaleError):
        plan_hash_edit(payload, fact)


def test_stale_zero_match_under_unique_shift() -> None:
    original = "before\nalpha\nafter\n"
    changed = "before\nomega\nafter\n"
    payload = _payload(original, revalidation="unique_shift")
    fact = HashEditCurrentFact(path="docs/example.md", current_bytes=changed.encode())
    with pytest.raises(HashEditStaleError, match="candidates=\\[\\]"):
        plan_hash_edit(payload, fact)


def test_duplicate_match_is_ambiguous() -> None:
    current = "before\nalpha\nafter\nmid\nbefore\nalpha\nafter\n"
    with pytest.raises(HashEditAmbiguousError, match="candidates="):
        _plan(current)


def test_exact_base_missing_needle_is_bind_error() -> None:
    current = "unrelated\nfile\n"
    payload = _payload(current)
    payload["base_sha256"] = _digest_text(current)
    with pytest.raises(HashEditBindError, match="absent"):
        plan_hash_edit(
            payload,
            HashEditCurrentFact(path="docs/example.md", current_bytes=current.encode()),
        )


def test_wrong_hinted_range_on_exact_base_is_bind_error() -> None:
    current = "before\nalpha\nafter\n"
    with pytest.raises(HashEditBindError, match="hinted line range"):
        _plan(current, original_start_line=1, original_end_line=1)


def test_correct_hint_on_exact_base_binds() -> None:
    current = "before\nalpha\nafter\n"
    plan = _plan(current, original_start_line=2, original_end_line=2)
    assert plan.start_line == 2
    assert plan.rebased is False


def test_same_text_wrong_context_does_not_match() -> None:
    original = "before\nalpha\nafter"
    current = "zz\nalpha\nww\n"
    payload = _payload(
        original,
        old_text="alpha",
        before_context="before\n",
        after_context="\nafter",
        revalidation="unique_shift",
    )
    fact = HashEditCurrentFact(path="docs/example.md", current_bytes=current.encode())
    with pytest.raises(HashEditStaleError):
        plan_hash_edit(payload, fact)


def test_hint_is_not_authorization_when_two_matches() -> None:
    current = "before\nalpha\nafter\nmid\nbefore\nalpha\nafter\n"
    with pytest.raises(HashEditAmbiguousError):
        _plan(current, original_start_line=2, original_end_line=2)


def test_invalid_utf8_binary_oversize_and_path_mismatch() -> None:
    current = "before\nalpha\nafter\n"
    payload = _payload(current)
    with pytest.raises(HashEditInputError, match="UTF-8"):
        plan_hash_edit(
            payload,
            HashEditCurrentFact(path="docs/example.md", current_bytes=b"\xff\xfe"),
        )
    with pytest.raises(HashEditInputError, match="NUL"):
        plan_hash_edit(
            payload,
            HashEditCurrentFact(path="docs/example.md", current_bytes=b"before\x00alpha"),
        )
    with pytest.raises(HashEditInputError, match="exceed"):
        plan_hash_edit(
            payload,
            HashEditCurrentFact(
                path="docs/example.md",
                current_bytes=b"a" * (MAX_PLAN_FILE_BYTES + 1),
            ),
        )
    with pytest.raises(HashEditInputError, match="path"):
        plan_hash_edit(
            payload,
            HashEditCurrentFact(path="other/file.md", current_bytes=current.encode()),
        )
    with pytest.raises(HashEditInputError):
        HashEditCurrentFact(path="/abs/file.md", current_bytes=b"x")


def test_crlf_unicode_and_terminal_newline_are_not_normalized() -> None:
    current = "before\r\n漢字\r\n"
    plan = _plan(
        current,
        old_text="漢字",
        replacement="仮名",
        before_context="before\r\n",
        after_context="\r\n",
    )
    after = current.encode()[: plan.start_offset] + "仮名".encode() + current.encode()[plan.end_offset :]
    assert after == "before\r\n仮名\r\n".encode()
    assert b"\r\n" in plan.unified_diff.encode("utf-8")
    assert plan.after_sha256 == _digest_bytes(after)

    no_nl = "before\nalpha"
    plan2 = _plan(
        no_nl,
        old_text="alpha",
        replacement="beta",
        before_context="before\n",
        after_context="",
    )
    rebuilt = no_nl.encode()[: plan2.start_offset] + b"beta" + no_nl.encode()[plan2.end_offset :]
    assert rebuilt == b"before\nbeta"
    assert not rebuilt.endswith(b"\n")
    assert "-alpha+beta" not in plan2.unified_diff
    assert "-alpha\n" in plan2.unified_diff
    assert "+beta\n" in plan2.unified_diff
    assert "\\ No newline at end of file\n" in plan2.unified_diff


def test_empty_needle_rejected_and_insert_with_context_ok() -> None:
    current = "head\ntail\n"
    with pytest.raises(HashEditBindError, match="empty"):
        _plan(current, old_text="", replacement="x", before_context="", after_context="")
    plan = _plan(
        current,
        old_text="",
        replacement="mid\n",
        before_context="head\n",
        after_context="tail\n",
    )
    assert plan.start_offset == plan.end_offset
    rebuilt = current.encode()[: plan.start_offset] + b"mid\n" + current.encode()[plan.end_offset :]
    assert rebuilt == b"head\nmid\ntail\n"


def test_diff_digest_is_stable_and_plan_rejects_forged_digest() -> None:
    current = "before\nalpha\nafter\n"
    plan = _plan(current)
    with pytest.raises(HashEditPlannerError):
        HashEditPlanV1(
            descriptor_digest=plan.descriptor_digest,
            path=plan.path,
            before_sha256=plan.before_sha256,
            after_sha256=plan.after_sha256,
            start_offset=plan.start_offset,
            end_offset=plan.end_offset,
            start_line=plan.start_line,
            end_line=plan.end_line,
            rebased=False,
            unified_diff=plan.unified_diff,
            unified_diff_sha256="00" * 32,
        )


def test_expires_at_is_not_evaluated_without_a_clock() -> None:
    current = "before\nalpha\nafter\n"
    plan = _plan(current, expires_at="2000-01-01T00:00:00Z")
    assert plan.rebased is False
    assert current.encode()[plan.start_offset : plan.end_offset] == b"alpha"


def test_multiline_old_text_byte_and_line_span() -> None:
    current = "head\nfoo\nbar\ntail\n"
    plan = _plan(
        current,
        old_text="foo\nbar",
        replacement="qux",
        before_context="head\n",
        after_context="\ntail\n",
    )
    assert current.encode()[plan.start_offset : plan.end_offset] == b"foo\nbar"
    assert plan.start_line == 2 and plan.end_line == 3


def test_unique_shift_duplicate_match_is_ambiguous_and_hint_cannot_pick() -> None:
    original = "before\nalpha\nafter\n"
    shifted = "before\nalpha\nafter\nmid\nbefore\nalpha\nafter\n"
    payload = _payload(original, revalidation="unique_shift")
    fact = HashEditCurrentFact(path="docs/example.md", current_bytes=shifted.encode())
    with pytest.raises(HashEditAmbiguousError, match="candidates="):
        plan_hash_edit(payload, fact)
    payload_hinted = _payload(
        original,
        revalidation="unique_shift",
        original_start_line=2,
        original_end_line=2,
    )
    with pytest.raises(HashEditAmbiguousError):
        plan_hash_edit(payload_hinted, fact)


def test_lf_needle_does_not_match_crlf_file() -> None:
    original = "before\nalpha\nafter\n"
    crlf = "before\r\nalpha\r\nafter\r\n"
    payload = _payload(original, revalidation="unique_shift")
    with pytest.raises(HashEditStaleError):
        plan_hash_edit(
            payload,
            HashEditCurrentFact(path="docs/example.md", current_bytes=crlf.encode()),
        )


def test_nfc_needle_does_not_match_nfd_text() -> None:
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    if nfc == nfd:
        pytest.skip("platform NFC/NFD forms are identical")
    original = f"before\n{nfc}\nafter\n"
    current = f"before\n{nfd}\nafter\n"
    payload = _payload(
        original,
        old_text=nfc,
        replacement="x",
        before_context="before\n",
        after_context="\nafter\n",
        revalidation="unique_shift",
    )
    with pytest.raises(HashEditStaleError):
        plan_hash_edit(
            payload,
            HashEditCurrentFact(path="docs/example.md", current_bytes=current.encode()),
        )


def test_unique_shift_does_not_use_similar_text() -> None:
    original = "before\nalpha\nafter\n"
    similar = "before\nalpha!\nafter\n"
    payload = _payload(original, revalidation="unique_shift")
    with pytest.raises(HashEditStaleError):
        plan_hash_edit(
            payload,
            HashEditCurrentFact(path="docs/example.md", current_bytes=similar.encode()),
        )
