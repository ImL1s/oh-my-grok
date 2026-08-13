"""Strict V1 hash-edit descriptor contract (#76 PR1, story s1)."""

from __future__ import annotations

import hashlib
import json
import unicodedata

import pytest

from omg_cli.contracts.writer_chain import canonical_json_bytes
from omg_cli.hash_edit import (
    HASH_EDIT_KIND,
    HASH_EDIT_SCHEMA_VERSION,
    REVALIDATION_POLICIES,
    HashEditDescriptorError,
    HashEditDescriptorV1,
    content_sha256,
    parse_hash_edit_descriptor,
)
from omg_cli.hash_edit.descriptor import (
    MAX_CONTEXT_CHARS,
    MAX_PATH_CHARS,
    MAX_PRODUCER_CHARS,
    MAX_TEXT_CHARS,
    require_workspace_relpath,
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _valid_payload(**overrides: object) -> dict[str, object]:
    old_text = str(overrides.pop("old_text", "alpha"))
    replacement = str(overrides.pop("replacement", "beta"))
    before_context = str(overrides.pop("before_context", "before\n"))
    after_context = str(overrides.pop("after_context", "\nafter"))
    payload: dict[str, object] = {
        "schema_version": 1,
        "kind": HASH_EDIT_KIND,
        "edit_id": "edit-001",
        "producer": "omg.hash_edit.test",
        "path": "docs/example.md",
        "base_sha256": "ab" * 32,
        "old_text": old_text,
        "replacement": replacement,
        "before_context": before_context,
        "after_context": after_context,
        "old_text_sha256": _digest(old_text),
        "replacement_sha256": _digest(replacement),
        "before_context_sha256": _digest(before_context),
        "after_context_sha256": _digest(after_context),
    }
    payload.update(overrides)
    return payload


def test_parse_minimal_descriptor_and_canonical_digest() -> None:
    payload = _valid_payload()
    descriptor = parse_hash_edit_descriptor(payload)
    assert descriptor.schema_version == HASH_EDIT_SCHEMA_VERSION
    assert descriptor.kind == HASH_EDIT_KIND
    assert descriptor.edit_id == "edit-001"
    assert descriptor.path == "docs/example.md"
    assert descriptor.digest() == hashlib.sha256(descriptor.canonical_bytes()).hexdigest()
    assert len(descriptor.digest()) == 64
    assert descriptor.digest() == parse_hash_edit_descriptor(payload).digest()
    # Input key order must not change the digest.
    scrambled = dict(reversed(list(payload.items())))
    assert parse_hash_edit_descriptor(scrambled).digest() == descriptor.digest()
    body = descriptor.canonical_bytes()
    assert not body.endswith(b"\n")
    assert parse_hash_edit_descriptor(body).digest() == descriptor.digest()


def test_canonical_bytes_round_trip_and_reject_noncanonical() -> None:
    descriptor = parse_hash_edit_descriptor(_valid_payload())
    pretty = json.dumps(descriptor.to_canonical_mapping(), indent=2).encode()
    with pytest.raises(HashEditDescriptorError, match="canonical JSON"):
        parse_hash_edit_descriptor(pretty)
    with pytest.raises(HashEditDescriptorError, match="canonical JSON"):
        parse_hash_edit_descriptor(descriptor.canonical_bytes() + b"\n")
    with pytest.raises(HashEditDescriptorError, match="object"):
        parse_hash_edit_descriptor(b"[]")


def test_schema_version_must_be_exact_non_bool_integer_one() -> None:
    for version in (True, False, "1", 1.0, 0, 2, None):
        with pytest.raises(HashEditDescriptorError):
            parse_hash_edit_descriptor(_valid_payload(schema_version=version))


def test_kind_must_be_exact() -> None:
    with pytest.raises(HashEditDescriptorError, match="kind"):
        parse_hash_edit_descriptor(_valid_payload(kind="omg.hash_edit.v2"))
    with pytest.raises(HashEditDescriptorError, match="kind"):
        parse_hash_edit_descriptor(_valid_payload(kind="OMG.HASH_EDIT.V1"))


def test_unknown_and_future_fields_fail_closed() -> None:
    with pytest.raises(HashEditDescriptorError, match="extra"):
        parse_hash_edit_descriptor(_valid_payload(extra_field="nope"))
    with pytest.raises(HashEditDescriptorError, match="extra"):
        parse_hash_edit_descriptor(_valid_payload(comment_hygiene=True))
    missing = _valid_payload()
    del missing["producer"]
    with pytest.raises(HashEditDescriptorError, match="missing"):
        parse_hash_edit_descriptor(missing)


def test_edit_id_and_optional_safe_ids() -> None:
    ok = parse_hash_edit_descriptor(
        _valid_payload(run_id="run-1", task_id="task.2")
    )
    assert ok.run_id == "run-1" and ok.task_id == "task.2"
    for bad in ("", "has space", "slash/id", "nul\0id", "../x"):
        with pytest.raises(HashEditDescriptorError):
            parse_hash_edit_descriptor(_valid_payload(edit_id=bad))
        with pytest.raises(HashEditDescriptorError):
            parse_hash_edit_descriptor(_valid_payload(run_id=bad))
        with pytest.raises(HashEditDescriptorError):
            parse_hash_edit_descriptor(_valid_payload(task_id=bad))
    with pytest.raises(HashEditDescriptorError):
        parse_hash_edit_descriptor(_valid_payload(run_id=None))
    with pytest.raises(HashEditDescriptorError):
        parse_hash_edit_descriptor(_valid_payload(task_id=None))


def test_producer_is_required_bounded_and_control_free() -> None:
    assert parse_hash_edit_descriptor(_valid_payload(producer="hash-edit test")).producer
    with pytest.raises(HashEditDescriptorError, match="producer"):
        parse_hash_edit_descriptor(_valid_payload(producer=""))
    with pytest.raises(HashEditDescriptorError, match="producer"):
        parse_hash_edit_descriptor(_valid_payload(producer="bad\nline"))
    with pytest.raises(HashEditDescriptorError, match="producer"):
        parse_hash_edit_descriptor(_valid_payload(producer="bad\x85nel"))
    with pytest.raises(HashEditDescriptorError, match="producer"):
        parse_hash_edit_descriptor(_valid_payload(producer="x" * (MAX_PRODUCER_CHARS + 1)))


def test_path_rejects_absolute_dot_dotdot_backslash_and_aliases() -> None:
    for path in (
        "",
        ".",
        "..",
        "./docs/a.md",
        "docs/./a.md",
        "docs/../a.md",
        "/abs/a.md",
        "~/a.md",
        "docs//a.md",
        "docs/a.md/",
        "docs\\a.md",
        "C:docs/a.md",
        "C:/Windows",
        "//unc/share/a.md",
        "docs/\0a.md",
        "docs/a.md\n",
        "docs/a.md ",
        "docs/a.md.",
        "docs/\x85a.md",
        "docs/\u202ea.md",
        "\ufeffdocs/a.md",
        "docs/a\u200b.md",
    ):
        with pytest.raises(HashEditDescriptorError):
            parse_hash_edit_descriptor(_valid_payload(path=path))
        with pytest.raises(HashEditDescriptorError):
            require_workspace_relpath(path)
    nfd = unicodedata.normalize("NFD", "docs/café.md")
    nfc = unicodedata.normalize("NFC", "docs/café.md")
    if nfd != nfc:
        with pytest.raises(HashEditDescriptorError, match="NFC"):
            parse_hash_edit_descriptor(_valid_payload(path=nfd))
        assert parse_hash_edit_descriptor(_valid_payload(path=nfc)).path == nfc
    with pytest.raises(HashEditDescriptorError):
        parse_hash_edit_descriptor(_valid_payload(path="p" * (MAX_PATH_CHARS + 1)))


def test_base_digest_and_content_hashes_must_bind() -> None:
    with pytest.raises(HashEditDescriptorError, match="base_sha256"):
        parse_hash_edit_descriptor(_valid_payload(base_sha256="AB" * 32))
    with pytest.raises(HashEditDescriptorError, match="base_sha256"):
        parse_hash_edit_descriptor(_valid_payload(base_sha256="deadbeef"))
    for field in (
        "old_text_sha256",
        "replacement_sha256",
        "before_context_sha256",
        "after_context_sha256",
    ):
        wrong = _valid_payload()
        wrong[field] = "00" * 32
        with pytest.raises(HashEditDescriptorError, match=field):
            parse_hash_edit_descriptor(wrong)
    with pytest.raises(HashEditDescriptorError, match="uppercase|lowercase|base_sha256"):
        parse_hash_edit_descriptor(_valid_payload(**{"old_text_sha256": _digest("alpha").upper()}))


def test_empty_texts_and_crlf_unicode_are_hashed_without_normalization() -> None:
    payload = _valid_payload(
        old_text="",
        replacement="line1\r\nline2\n漢字\t🎉",
        before_context="",
        after_context="\r\n",
    )
    descriptor = parse_hash_edit_descriptor(payload)
    assert descriptor.old_text == ""
    assert descriptor.replacement == "line1\r\nline2\n漢字\t🎉"
    assert descriptor.replacement_sha256 == content_sha256(descriptor.replacement)
    # No silent newline rewrite.
    assert "\r\n" in descriptor.replacement
    with pytest.raises(HashEditDescriptorError):
        parse_hash_edit_descriptor(_valid_payload(old_text="nul\0byte"))
    with pytest.raises(HashEditDescriptorError):
        parse_hash_edit_descriptor(_valid_payload(before_context="x" * (MAX_CONTEXT_CHARS + 1)))
    with pytest.raises(HashEditDescriptorError):
        parse_hash_edit_descriptor(_valid_payload(old_text="x" * (MAX_TEXT_CHARS + 1)))


def test_line_range_is_optional_1_based_and_not_required_for_parse() -> None:
    bare = parse_hash_edit_descriptor(_valid_payload())
    assert bare.original_start_line is None and bare.original_end_line is None
    ranged = parse_hash_edit_descriptor(
        _valid_payload(original_start_line=3, original_end_line=5)
    )
    assert ranged.original_start_line == 3 and ranged.original_end_line == 5
    with pytest.raises(HashEditDescriptorError, match="together"):
        parse_hash_edit_descriptor(_valid_payload(original_start_line=1))
    with pytest.raises(HashEditDescriptorError):
        parse_hash_edit_descriptor(_valid_payload(original_start_line=0, original_end_line=1))
    with pytest.raises(HashEditDescriptorError):
        parse_hash_edit_descriptor(_valid_payload(original_start_line=True, original_end_line=2))
    with pytest.raises(HashEditDescriptorError):
        parse_hash_edit_descriptor(_valid_payload(original_start_line=5, original_end_line=4))
    # Hint-only: a range that cannot describe old_text still parses.
    hinted = parse_hash_edit_descriptor(
        _valid_payload(old_text="a", original_start_line=99, original_end_line=99)
    )
    assert hinted.original_start_line == 99
    assert hinted.old_text == "a"


def test_revalidation_enum_and_bounded_utc_expiry() -> None:
    assert REVALIDATION_POLICIES == frozenset({"require_base", "unique_shift"})
    ok = parse_hash_edit_descriptor(
        _valid_payload(revalidation="unique_shift", expires_at="2026-08-13T12:00:00Z")
    )
    assert ok.revalidation == "unique_shift"
    assert ok.expires_at == "2026-08-13T12:00:00Z"
    assert parse_hash_edit_descriptor(_valid_payload(revalidation="require_base")).revalidation == (
        "require_base"
    )
    with pytest.raises(HashEditDescriptorError, match="revalidation"):
        parse_hash_edit_descriptor(_valid_payload(revalidation="fuzzy"))
    with pytest.raises(HashEditDescriptorError, match="revalidation"):
        parse_hash_edit_descriptor(_valid_payload(revalidation="require_base "))
    with pytest.raises(HashEditDescriptorError):
        parse_hash_edit_descriptor(_valid_payload(revalidation=None))
    with pytest.raises(HashEditDescriptorError):
        parse_hash_edit_descriptor(_valid_payload(expires_at=None))
    for stamp in (
        "2026-08-13T12:00:00+00:00",
        "2026-08-13T12:00:00",
        "2026-08-13 12:00:00Z",
        "1999-01-01T00:00:00Z",
        "2101-01-01T00:00:00Z",
        "2026-02-30T00:00:00Z",
        True,
    ):
        with pytest.raises(HashEditDescriptorError):
            parse_hash_edit_descriptor(_valid_payload(expires_at=stamp))


def test_digest_changes_when_prose_or_optional_fields_change() -> None:
    base = parse_hash_edit_descriptor(_valid_payload())
    other = parse_hash_edit_descriptor(_valid_payload(old_text="ALPHA"))
    assert base.digest() != other.digest()
    with_range = parse_hash_edit_descriptor(
        _valid_payload(original_start_line=1, original_end_line=1)
    )
    assert with_range.digest() != base.digest()
    # Canonical encoder used by the rest of OMG contracts.
    assert base.canonical_bytes() == canonical_json_bytes(base.to_canonical_mapping())


def test_reject_non_object_and_non_mapping_inputs() -> None:
    with pytest.raises(HashEditDescriptorError):
        parse_hash_edit_descriptor(123)  # type: ignore[arg-type]
    with pytest.raises(HashEditDescriptorError):
        parse_hash_edit_descriptor([_valid_payload()])  # type: ignore[arg-type]


def test_direct_construction_is_not_an_authority_bypass() -> None:
    with pytest.raises(HashEditDescriptorError):
        HashEditDescriptorV1(
            edit_id="edit-001",
            producer="test",
            path="/etc/passwd",
            base_sha256="nope",
            old_text="a",
            replacement="b",
            before_context="",
            after_context="",
            old_text_sha256="00" * 32,
            replacement_sha256="00" * 32,
            before_context_sha256="00" * 32,
            after_context_sha256="00" * 32,
        )


def test_lone_surrogate_string_is_typed_error() -> None:
    with pytest.raises(HashEditDescriptorError):
        parse_hash_edit_descriptor("{\ud800}")
