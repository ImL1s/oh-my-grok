"""Strict canonical V1 hash-edit descriptor.

Pure validation: no filesystem, network, subprocess, or clock mutation.
Unknown keys and future schema versions fail closed.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Final

from omg_cli.contracts.state_schemas import (
    require_exact_keys,
    require_integer,
    require_object,
    require_safe_id,
    require_sha256,
)
from omg_cli.contracts.writer_chain import canonical_json_bytes, parse_canonical_json_bytes

from .errors import HashEditDescriptorError

HASH_EDIT_SCHEMA_VERSION: Final[int] = 1
HASH_EDIT_KIND: Final[str] = "omg.hash_edit.v1"

REVALIDATION_POLICIES: Final[frozenset[str]] = frozenset(
    {
        "require_base",
        "unique_shift",
    }
)

REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "schema_version",
        "kind",
        "edit_id",
        "producer",
        "path",
        "base_sha256",
        "old_text",
        "replacement",
        "before_context",
        "after_context",
        "old_text_sha256",
        "replacement_sha256",
        "before_context_sha256",
        "after_context_sha256",
    }
)
OPTIONAL_KEYS: Final[frozenset[str]] = frozenset(
    {
        "run_id",
        "task_id",
        "original_start_line",
        "original_end_line",
        "revalidation",
        "expires_at",
    }
)

MAX_PATH_CHARS: Final[int] = 512
MAX_PRODUCER_CHARS: Final[int] = 256
MAX_TEXT_CHARS: Final[int] = 262_144
MAX_CONTEXT_CHARS: Final[int] = 16_384
MAX_RANGE_SPAN: Final[int] = 100_000
MIN_EXPIRES_YEAR: Final[int] = 2000
MAX_EXPIRES_YEAR: Final[int] = 2100

_EXPIRES_AT_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z$")
_CONTENT_ALLOWED_CONTROLS = frozenset({"\t", "\n", "\r"})


def content_sha256(text: str) -> str:
    """SHA-256 (lowercase hex) of the UTF-8 bytes of *text*."""

    if not isinstance(text, str):
        raise HashEditDescriptorError("content hash input must be a string")
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError as exc:
        raise HashEditDescriptorError("content hash input is not UTF-8 encodable") from exc


def _reject_control_or_surrogate(text: str, *, label: str, allow_text_controls: bool) -> None:
    for char in text:
        codepoint = ord(char)
        if allow_text_controls and char in _CONTENT_ALLOWED_CONTROLS:
            continue
        if 0xD800 <= codepoint <= 0xDFFF:
            raise HashEditDescriptorError(f"{label} contains a control or surrogate")
        # C0, DEL, and C1 (Cc). Matches other OMG control floors.
        if unicodedata.category(char) == "Cc" or 0x7F <= codepoint <= 0x9F:
            raise HashEditDescriptorError(f"{label} contains a control or surrogate")


def require_workspace_relpath(value: Any, *, label: str = "path") -> str:
    """Canonical workspace-relative POSIX file path (no FS resolution)."""

    if not isinstance(value, str):
        raise HashEditDescriptorError(f"{label} must be a string")
    if not value:
        raise HashEditDescriptorError(f"{label} must be a non-empty workspace-relative POSIX path")
    if len(value) > MAX_PATH_CHARS:
        raise HashEditDescriptorError(f"{label} exceeds {MAX_PATH_CHARS} characters")
    if "\\" in value:
        raise HashEditDescriptorError(f"{label} must not contain a backslash")
    _reject_control_or_surrogate(value, label=label, allow_text_controls=False)
    if any(unicodedata.category(char) == "Cf" for char in value):
        raise HashEditDescriptorError(f"{label} contains a Unicode format character")
    if unicodedata.normalize("NFC", value) != value:
        raise HashEditDescriptorError(f"{label} must be Unicode NFC (normalization ambiguity)")
    if value.startswith("/") or value.startswith("~") or value.startswith("//"):
        raise HashEditDescriptorError(f"{label} must not be absolute or home-anchored")
    if len(value) >= 2 and value[1] == ":" and value[0].isalpha():
        raise HashEditDescriptorError(f"{label} must not be a drive-prefixed path")
    pure = PurePosixPath(value)
    if pure.is_absolute() or pure.anchor:
        raise HashEditDescriptorError(f"{label} must be a relative POSIX path")
    parts = pure.parts
    if not parts:
        raise HashEditDescriptorError(f"{label} must not contain empty, dot, or dotdot components")
    for part in parts:
        if part in {".", "..", ""}:
            raise HashEditDescriptorError(
                f"{label} must not contain empty, dot, or dotdot components"
            )
        if part != part.strip() or part.endswith("."):
            raise HashEditDescriptorError(
                f"{label} component is not canonical (space or trailing dot): {part!r}"
            )
    canonical = "/".join(parts)
    if canonical != value:
        raise HashEditDescriptorError(f"{label} is not in canonical POSIX form")
    return value


def _require_producer(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise HashEditDescriptorError("producer must be a non-empty string")
    if len(value) > MAX_PRODUCER_CHARS:
        raise HashEditDescriptorError(f"producer exceeds {MAX_PRODUCER_CHARS} characters")
    _reject_control_or_surrogate(value, label="producer", allow_text_controls=False)
    return value


def _require_content_text(value: Any, *, label: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise HashEditDescriptorError(f"{label} must be a UTF-8 string")
    if len(value) > max_chars:
        raise HashEditDescriptorError(f"{label} exceeds {max_chars} characters")
    _reject_control_or_surrogate(value, label=label, allow_text_controls=True)
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HashEditDescriptorError(f"{label} is not UTF-8 encodable") from exc
    return value


def _require_bound_hash(text: str, digest: Any, *, label: str) -> str:
    expected = require_sha256(digest, label=label)
    actual = content_sha256(text)
    if actual != expected:
        raise HashEditDescriptorError(f"{label} does not match {label[:-7]} UTF-8 bytes")
    return expected


def _require_expires_at(value: Any) -> str:
    if not isinstance(value, str):
        raise HashEditDescriptorError("expires_at must be a string")
    match = _EXPIRES_AT_RE.fullmatch(value)
    if match is None:
        raise HashEditDescriptorError(
            "expires_at must be a bounded UTC timestamp YYYY-MM-DDTHH:MM:SSZ"
        )
    year = int(match.group(1))
    if year < MIN_EXPIRES_YEAR or year > MAX_EXPIRES_YEAR:
        raise HashEditDescriptorError(
            f"expires_at year must be in {MIN_EXPIRES_YEAR}..{MAX_EXPIRES_YEAR}"
        )
    try:
        parsed = datetime(
            year,
            int(match.group(2)),
            int(match.group(3)),
            int(match.group(4)),
            int(match.group(5)),
            int(match.group(6)),
            tzinfo=timezone.utc,
        )
    except ValueError as exc:
        raise HashEditDescriptorError("expires_at is not a real UTC calendar time") from exc
    if parsed.tzinfo is not timezone.utc:
        raise HashEditDescriptorError("expires_at must be UTC")
    return value


def _optional_line_range(payload: Mapping[str, Any]) -> tuple[int | None, int | None]:
    has_start = "original_start_line" in payload
    has_end = "original_end_line" in payload
    if has_start ^ has_end:
        raise HashEditDescriptorError(
            "original_start_line and original_end_line must be supplied together"
        )
    if not has_start:
        return None, None
    try:
        start = require_integer(
            payload["original_start_line"], label="original_start_line", minimum=1
        )
        end = require_integer(payload["original_end_line"], label="original_end_line", minimum=1)
    except Exception as exc:
        raise HashEditDescriptorError(str(exc)) from exc
    if start > end:
        raise HashEditDescriptorError("original_start_line must be <= original_end_line")
    if end - start + 1 > MAX_RANGE_SPAN:
        raise HashEditDescriptorError(f"original line range exceeds {MAX_RANGE_SPAN} lines")
    return start, end


@dataclass(frozen=True, slots=True)
class HashEditDescriptorV1:
    """Immutable validated V1 descriptor plus its canonical digest."""

    edit_id: str
    producer: str
    path: str
    base_sha256: str
    old_text: str
    replacement: str
    before_context: str
    after_context: str
    old_text_sha256: str
    replacement_sha256: str
    before_context_sha256: str
    after_context_sha256: str
    run_id: str | None = None
    task_id: str | None = None
    original_start_line: int | None = None
    original_end_line: int | None = None
    revalidation: str | None = None
    expires_at: str | None = None

    def __post_init__(self) -> None:
        # Direct construction is not an authority bypass: re-run the same
        # allowlist as parse. _validated_fields does not construct another
        # instance, so this cannot recurse.
        checked = _validated_fields(self.to_canonical_mapping())
        for key, value in checked.items():
            if getattr(self, key) != value:
                raise HashEditDescriptorError(
                    f"HashEditDescriptorV1.{key} failed revalidation"
                )

    @property
    def schema_version(self) -> int:
        return HASH_EDIT_SCHEMA_VERSION

    @property
    def kind(self) -> str:
        return HASH_EDIT_KIND

    def to_canonical_mapping(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "after_context": self.after_context,
            "after_context_sha256": self.after_context_sha256,
            "base_sha256": self.base_sha256,
            "before_context": self.before_context,
            "before_context_sha256": self.before_context_sha256,
            "edit_id": self.edit_id,
            "kind": HASH_EDIT_KIND,
            "old_text": self.old_text,
            "old_text_sha256": self.old_text_sha256,
            "path": self.path,
            "producer": self.producer,
            "replacement": self.replacement,
            "replacement_sha256": self.replacement_sha256,
            "schema_version": HASH_EDIT_SCHEMA_VERSION,
        }
        if self.expires_at is not None:
            payload["expires_at"] = self.expires_at
        if self.original_end_line is not None:
            payload["original_end_line"] = self.original_end_line
        if self.original_start_line is not None:
            payload["original_start_line"] = self.original_start_line
        if self.revalidation is not None:
            payload["revalidation"] = self.revalidation
        if self.run_id is not None:
            payload["run_id"] = self.run_id
        if self.task_id is not None:
            payload["task_id"] = self.task_id
        return payload

    def canonical_bytes(self) -> bytes:
        return canonical_json_bytes(self.to_canonical_mapping())

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def _validated_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = require_object(payload, label="hash_edit descriptor")
    try:
        require_exact_keys(
            data,
            required=REQUIRED_KEYS,
            optional=OPTIONAL_KEYS,
            label="hash_edit descriptor",
        )
    except Exception as exc:
        raise HashEditDescriptorError(str(exc)) from exc

    schema_version = data["schema_version"]
    try:
        version = require_integer(schema_version, label="schema_version", minimum=1)
    except Exception as exc:
        raise HashEditDescriptorError(str(exc)) from exc
    if version != HASH_EDIT_SCHEMA_VERSION:
        raise HashEditDescriptorError(
            f"unsupported hash_edit schema_version={version}; expected {HASH_EDIT_SCHEMA_VERSION}"
        )

    kind = data["kind"]
    if not isinstance(kind, str) or kind != HASH_EDIT_KIND:
        raise HashEditDescriptorError(f"kind must be {HASH_EDIT_KIND!r}")

    try:
        edit_id = require_safe_id(data["edit_id"], label="edit_id")
    except Exception as exc:
        raise HashEditDescriptorError(str(exc)) from exc

    producer = _require_producer(data["producer"])
    path = require_workspace_relpath(data["path"])
    try:
        base_sha256 = require_sha256(data["base_sha256"], label="base_sha256")
    except Exception as exc:
        raise HashEditDescriptorError(str(exc)) from exc

    old_text = _require_content_text(data["old_text"], label="old_text", max_chars=MAX_TEXT_CHARS)
    replacement = _require_content_text(
        data["replacement"], label="replacement", max_chars=MAX_TEXT_CHARS
    )
    before_context = _require_content_text(
        data["before_context"], label="before_context", max_chars=MAX_CONTEXT_CHARS
    )
    after_context = _require_content_text(
        data["after_context"], label="after_context", max_chars=MAX_CONTEXT_CHARS
    )
    try:
        old_text_sha256 = _require_bound_hash(
            old_text, data["old_text_sha256"], label="old_text_sha256"
        )
        replacement_sha256 = _require_bound_hash(
            replacement, data["replacement_sha256"], label="replacement_sha256"
        )
        before_context_sha256 = _require_bound_hash(
            before_context, data["before_context_sha256"], label="before_context_sha256"
        )
        after_context_sha256 = _require_bound_hash(
            after_context, data["after_context_sha256"], label="after_context_sha256"
        )
    except HashEditDescriptorError:
        raise
    except Exception as exc:
        raise HashEditDescriptorError(str(exc)) from exc

    run_id: str | None = None
    if "run_id" in data:
        try:
            run_id = require_safe_id(data["run_id"], label="run_id")
        except Exception as exc:
            raise HashEditDescriptorError(str(exc)) from exc
    task_id: str | None = None
    if "task_id" in data:
        try:
            task_id = require_safe_id(data["task_id"], label="task_id")
        except Exception as exc:
            raise HashEditDescriptorError(str(exc)) from exc

    start_line, end_line = _optional_line_range(data)

    revalidation: str | None = None
    if "revalidation" in data:
        policy = data["revalidation"]
        if not isinstance(policy, str) or policy not in REVALIDATION_POLICIES:
            raise HashEditDescriptorError(
                "revalidation must be one of " + ", ".join(sorted(REVALIDATION_POLICIES))
            )
        revalidation = policy

    expires_at: str | None = None
    if "expires_at" in data:
        expires_at = _require_expires_at(data["expires_at"])

    return {
        "edit_id": edit_id,
        "producer": producer,
        "path": path,
        "base_sha256": base_sha256,
        "old_text": old_text,
        "replacement": replacement,
        "before_context": before_context,
        "after_context": after_context,
        "old_text_sha256": old_text_sha256,
        "replacement_sha256": replacement_sha256,
        "before_context_sha256": before_context_sha256,
        "after_context_sha256": after_context_sha256,
        "run_id": run_id,
        "task_id": task_id,
        "original_start_line": start_line,
        "original_end_line": end_line,
        "revalidation": revalidation,
        "expires_at": expires_at,
    }


def _parse_mapping(payload: Mapping[str, Any]) -> HashEditDescriptorV1:
    return HashEditDescriptorV1(**_validated_fields(payload))


def parse_hash_edit_descriptor(value: Mapping[str, Any] | bytes | str) -> HashEditDescriptorV1:
    """Parse and validate a V1 descriptor from a mapping or canonical JSON bytes/text.

    Mapping input is validated then re-encoded; digest is always of canonical JSON.
    Byte/text input must already be canonical JSON v1 (no BOM, no trailing newline).
    """

    if isinstance(value, bytes):
        try:
            parsed = parse_canonical_json_bytes(value)
        except Exception as exc:
            raise HashEditDescriptorError(f"descriptor bytes are not canonical JSON v1: {exc}") from exc
        if not isinstance(parsed, Mapping):
            raise HashEditDescriptorError("canonical descriptor JSON must be an object")
        descriptor = _parse_mapping(parsed)
        if descriptor.canonical_bytes() != value:
            raise HashEditDescriptorError("canonical descriptor bytes do not round-trip")
        return descriptor
    if isinstance(value, str):
        try:
            return parse_hash_edit_descriptor(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise HashEditDescriptorError("descriptor text is not UTF-8 encodable") from exc
    if isinstance(value, Mapping):
        return _parse_mapping(value)
    raise HashEditDescriptorError("descriptor must be an object or canonical JSON bytes")
