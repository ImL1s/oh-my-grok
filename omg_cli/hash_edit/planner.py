"""Pure hash-edit planner: descriptor + caller-supplied current bytes only.

No filesystem, network, subprocess, or clock mutation. Matching is exact
byte/text + context only — never fuzzy, similar, or hint-authorized.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from omg_cli.contracts.state_schemas import require_sha256

from .descriptor import (
    HashEditDescriptorV1,
    parse_hash_edit_descriptor,
    require_workspace_relpath,
)
from .errors import (
    HashEditAmbiguousError,
    HashEditBindError,
    HashEditDescriptorError,
    HashEditInputError,
    HashEditPlannerError,
    HashEditStaleError,
)

MAX_PLAN_FILE_BYTES: Final[int] = 16 * 1024 * 1024
UNIFIED_DIFF_CONTEXT: Final[int] = 3

_SPLIT_KEEPENDS = re.compile(r"(?<=\n)")


@dataclass(frozen=True, slots=True)
class HashEditCurrentFact:
    """Caller-supplied path + current file bytes. Not a filesystem handle."""

    path: str
    current_bytes: bytes

    def __post_init__(self) -> None:
        try:
            require_workspace_relpath(self.path, label="current path")
        except HashEditDescriptorError as exc:
            raise HashEditInputError(str(exc)) from exc
        if not isinstance(self.current_bytes, (bytes, bytearray)):
            raise HashEditInputError("current bytes must be bytes")
        object.__setattr__(self, "current_bytes", bytes(self.current_bytes))


@dataclass(frozen=True, slots=True)
class HashEditCandidate:
    """Copy-safe location of one exact needle match (old_text span)."""

    start_offset: int
    end_offset: int
    start_line: int
    end_line: int


@dataclass(frozen=True, slots=True)
class HashEditPlanV1:
    """Immutable plan. Does not contain raw replacement or absolute paths."""

    descriptor_digest: str
    path: str
    before_sha256: str
    after_sha256: str
    start_offset: int
    end_offset: int
    start_line: int
    end_line: int
    rebased: bool
    unified_diff: str
    unified_diff_sha256: str

    def __post_init__(self) -> None:
        try:
            require_workspace_relpath(self.path, label="plan path")
        except HashEditDescriptorError as exc:
            raise HashEditPlannerError(str(exc)) from exc
        try:
            require_sha256(self.descriptor_digest, label="descriptor_digest")
            require_sha256(self.before_sha256, label="before_sha256")
            require_sha256(self.after_sha256, label="after_sha256")
            require_sha256(self.unified_diff_sha256, label="unified_diff_sha256")
        except Exception as exc:
            raise HashEditPlannerError(str(exc)) from exc
        if not isinstance(self.rebased, bool):
            raise HashEditPlannerError("rebased must be a bool")
        if not isinstance(self.unified_diff, str):
            raise HashEditPlannerError("unified_diff must be a string")
        if not isinstance(self.start_offset, int) or isinstance(self.start_offset, bool):
            raise HashEditPlannerError("start_offset must be an integer")
        if not isinstance(self.end_offset, int) or isinstance(self.end_offset, bool):
            raise HashEditPlannerError("end_offset must be an integer")
        if not isinstance(self.start_line, int) or isinstance(self.start_line, bool):
            raise HashEditPlannerError("start_line must be an integer")
        if not isinstance(self.end_line, int) or isinstance(self.end_line, bool):
            raise HashEditPlannerError("end_line must be an integer")
        if self.start_offset < 0 or self.end_offset < self.start_offset:
            raise HashEditPlannerError("byte offsets must satisfy 0 <= start <= end")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise HashEditPlannerError("line range must be 1-based and ordered")
        actual = hashlib.sha256(self.unified_diff.encode("utf-8")).hexdigest()
        if actual != self.unified_diff_sha256:
            raise HashEditPlannerError("unified_diff_sha256 does not match unified_diff UTF-8")


def _sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _require_current_bytes(body: object) -> bytes:
    if not isinstance(body, (bytes, bytearray)):
        raise HashEditInputError("current bytes must be bytes")
    data = bytes(body)
    if len(data) > MAX_PLAN_FILE_BYTES:
        raise HashEditInputError(f"current bytes exceed {MAX_PLAN_FILE_BYTES} byte limit")
    if b"\x00" in data:
        raise HashEditInputError("current bytes are binary (contain NUL)")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HashEditInputError("current bytes are not valid UTF-8") from exc
    return data


def _require_descriptor(
    descriptor: HashEditDescriptorV1 | Mapping[str, Any] | bytes | str,
) -> HashEditDescriptorV1:
    if isinstance(descriptor, HashEditDescriptorV1):
        # Re-parse so a caller-built instance cannot skip allowlist checks.
        return parse_hash_edit_descriptor(descriptor.to_canonical_mapping())
    try:
        return parse_hash_edit_descriptor(descriptor)
    except HashEditDescriptorError:
        raise
    except Exception as exc:
        raise HashEditInputError(f"descriptor is not usable: {exc}") from exc


def _char_finds(haystack: str, needle: str) -> list[int]:
    found: list[int] = []
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return found
        found.append(index)
        start = index + 1


def _utf8_offset(text: str, char_index: int) -> int:
    return len(text[:char_index].encode("utf-8"))


def _line_start_offsets(data: bytes) -> list[int]:
    starts = [0]
    for index, byte in enumerate(data):
        if byte == 0x0A:
            starts.append(index + 1)
    if len(starts) > 1 and data.endswith(b"\n"):
        starts.pop()
    return starts


def _line_of(starts: Sequence[int], offset: int) -> int:
    line = 1
    for index, start in enumerate(starts):
        if start <= offset:
            line = index + 1
        else:
            break
    return line


def _candidate_from_match(
    text: str,
    data: bytes,
    starts: Sequence[int],
    needle_char: int,
    before: str,
    old_text: str,
) -> HashEditCandidate:
    old_char = needle_char + len(before)
    start_offset = _utf8_offset(text, old_char)
    end_offset = start_offset + len(old_text.encode("utf-8"))
    start_line = _line_of(starts, start_offset)
    end_line = start_line if end_offset == start_offset else _line_of(starts, end_offset - 1)
    if end_offset > len(data) or start_offset > len(data):
        raise HashEditPlannerError("computed offsets escape current bytes")
    return HashEditCandidate(
        start_offset=start_offset,
        end_offset=end_offset,
        start_line=start_line,
        end_line=end_line,
    )


def _split_keepends(text: str) -> list[str]:
    if not text:
        return []
    parts = _SPLIT_KEEPENDS.split(text)
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def _hunk_span(start: int, length: int) -> str:
    if length == 0:
        return f"{start},0"
    return f"{start + 1},{length}"


def _emit_diff_line(prefix: str, line: str) -> list[str]:
    """One unified-diff record. Preserve CR/LF inside the line; never glue records."""

    if line.endswith("\n"):
        return [prefix + line]
    return [prefix + line + "\n", "\\ No newline at end of file\n"]


def unified_diff_text(path: str, before: str, after: str) -> str:
    """Deterministic unified diff. Preserves original newline bytes; no dates."""

    old_lines = _split_keepends(before)
    new_lines = _split_keepends(after)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    groups = matcher.get_grouped_opcodes(n=UNIFIED_DIFF_CONTEXT)
    if not groups:
        return ""
    chunks: list[str] = [f"--- {path}\n+++ {path}\n"]
    for group in groups:
        old_first = group[0][1]
        old_last = group[-1][2]
        new_first = group[0][3]
        new_last = group[-1][4]
        chunks.append(
            f"@@ -{_hunk_span(old_first, old_last - old_first)} "
            f"+{_hunk_span(new_first, new_last - new_first)} @@\n"
        )
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for line in old_lines[i1:i2]:
                    chunks.extend(_emit_diff_line(" ", line))
            elif tag == "replace":
                for line in old_lines[i1:i2]:
                    chunks.extend(_emit_diff_line("-", line))
                for line in new_lines[j1:j2]:
                    chunks.extend(_emit_diff_line("+", line))
            elif tag == "delete":
                for line in old_lines[i1:i2]:
                    chunks.extend(_emit_diff_line("-", line))
            elif tag == "insert":
                for line in new_lines[j1:j2]:
                    chunks.extend(_emit_diff_line("+", line))
            else:  # pragma: no cover - SequenceMatcher tags are closed
                raise HashEditPlannerError(f"unexpected diff opcode {tag!r}")
    return "".join(chunks)


def _planned_bytes(current: bytes, candidate: HashEditCandidate, replacement: str) -> bytes:
    return current[: candidate.start_offset] + replacement.encode("utf-8") + current[candidate.end_offset :]


def _raise_with_candidates(
    exc_type: type[HashEditPlannerError],
    message: str,
    candidates: Sequence[HashEditCandidate],
) -> None:
    listing = ", ".join(
        f"{item.start_line}-{item.end_line}@{item.start_offset}:{item.end_offset}"
        for item in candidates
    )
    suffix = f" candidates=[{listing}]" if listing else " candidates=[]"
    raise exc_type(message + suffix)


def plan_hash_edit(
    descriptor: HashEditDescriptorV1 | Mapping[str, Any] | bytes | str,
    fact: HashEditCurrentFact,
) -> HashEditPlanV1:
    """Return an immutable plan or raise a typed planner error.

    ``expires_at`` is recorded on the descriptor only. This function never
    reads a clock; expiry is not evaluated here.
    """

    desc = _require_descriptor(descriptor)
    if not isinstance(fact, HashEditCurrentFact):
        raise HashEditInputError("current fact must be a HashEditCurrentFact")
    path = require_workspace_relpath(fact.path, label="current path")
    if path != desc.path:
        raise HashEditInputError("current path does not match descriptor path")
    current = _require_current_bytes(fact.current_bytes)
    text = current.decode("utf-8")
    needle = desc.before_context + desc.old_text + desc.after_context
    if needle == "":
        raise HashEditBindError("empty before+old+after needle cannot bind uniquely")

    before_digest = _sha256_bytes(current)
    base_matches = before_digest == desc.base_sha256
    policy = desc.revalidation if desc.revalidation is not None else "require_base"

    starts = _line_start_offsets(current)
    char_hits = _char_finds(text, needle)
    candidates = [
        _candidate_from_match(text, current, starts, hit, desc.before_context, desc.old_text)
        for hit in char_hits
    ]

    if policy == "require_base" and not base_matches:
        _raise_with_candidates(
            HashEditStaleError,
            "base digest mismatch under require_base",
            candidates,
        )

    if not base_matches and policy == "unique_shift":
        if len(candidates) == 0:
            _raise_with_candidates(HashEditStaleError, "no exact text+context match", candidates)
        if len(candidates) > 1:
            _raise_with_candidates(
                HashEditAmbiguousError,
                "multiple exact text+context matches",
                candidates,
            )
        chosen = candidates[0]
        rebased = True
    else:
        # Exact base path: needle and hint (if present) must bind.
        if len(candidates) == 0:
            _raise_with_candidates(
                HashEditBindError,
                "base digest matches but old/context needle is absent",
                candidates,
            )
        if len(candidates) > 1:
            _raise_with_candidates(
                HashEditAmbiguousError,
                "multiple exact text+context matches",
                candidates,
            )
        chosen = candidates[0]
        if desc.original_start_line is not None and desc.original_end_line is not None:
            if (
                chosen.start_line != desc.original_start_line
                or chosen.end_line != desc.original_end_line
            ):
                raise HashEditBindError(
                    "hinted line range does not bind the unique exact match "
                    f"(have {chosen.start_line}-{chosen.end_line}, "
                    f"hint {desc.original_start_line}-{desc.original_end_line})"
                )
        rebased = False

    planned = _planned_bytes(current, chosen, desc.replacement)
    if len(planned) > MAX_PLAN_FILE_BYTES:
        raise HashEditInputError(
            f"planned bytes exceed {MAX_PLAN_FILE_BYTES} byte limit"
        )
    after_text = planned.decode("utf-8")
    diff = unified_diff_text(desc.path, text, after_text)
    return HashEditPlanV1(
        descriptor_digest=desc.digest(),
        path=desc.path,
        before_sha256=before_digest,
        after_sha256=_sha256_bytes(planned),
        start_offset=chosen.start_offset,
        end_offset=chosen.end_offset,
        start_line=chosen.start_line,
        end_line=chosen.end_line,
        rebased=rebased,
        unified_diff=diff,
        unified_diff_sha256=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
    )
