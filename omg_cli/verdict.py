"""Shared strict verdict parsing for dual-review and ralplan gates.

Design (Codex P0 / 2026-07-20 council):
- Never treat negated language as acceptance (``Do not APPROVE``).
- ``APPROVE`` is terminal-only (JSON field or dedicated terminal line), not a
  free-floating whole-word anywhere in the body (prompts often mention APPROVE).
- ``REQUEST_CHANGES`` / ``FAILED`` stay fail-closed whole-word (safe to over-match).
- Priority: FAILED > REQUEST_CHANGES > APPROVE > UNKNOWN.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_REQUEST_CHANGES_RE = re.compile(
    r"(?<![A-Za-z0-9_])REQUEST[_\s-]?CHANGES(?![A-Za-z0-9_])",
    re.IGNORECASE,
)
_FAILED_RE = re.compile(
    r"(?<![A-Za-z0-9_])FAILED(?![A-Za-z0-9_])",
)
# Negation that cancels an APPROVE token in nearby context
_NEGATED_APPROVE_RE = re.compile(
    r"(?i)(?:do\s+not|don'?t|does\s+not|never|not)\s+APPROVE"
    r"|APPROVE\s+(?:yet|lightly|blindly|to\s+be\s+helpful)",
)
# Terminal line only — markdown heading optional, bold optional
_TERMINAL_APPROVE_LINE_RE = re.compile(
    r"(?im)^(?:\s*#{1,6}\s*)?(?:\*\*)?(?:verdict\s*[:：]\s*)?APPROVE(?:\*\*)?\s*$"
)
_APPROVE_WORD_RE = re.compile(r"(?<![A-Za-z0-9_])APPROVE(?![A-Za-z0-9_])")

_STUB_MARKERS = (
    "dry_run stub",
    "dry_run: no grok",
    "stub artifact",
    "needs_review",
    "verdict placeholder",
)


def is_stub_artifact_text(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in _STUB_MARKERS)


def _json_verdict(data: dict) -> str | None:
    for key in ("verdict", "decision", "status"):
        val = data.get(key)
        if isinstance(val, str):
            v = val.strip().upper().replace(" ", "_").replace("-", "_")
            if v in ("APPROVE", "REQUEST_CHANGES", "FAILED"):
                return v
            if v == "REQUESTCHANGES":
                return "REQUEST_CHANGES"
    if data.get("approve") is True:
        return "APPROVE"
    nested = data.get("result") or data.get("output")
    if isinstance(nested, dict):
        return _json_verdict(nested)
    return None


def prose_has_terminal_approve(text: str) -> bool:
    """True only if APPROVE appears as a terminal line and is not negated away."""
    if not text or not text.strip():
        return False
    if is_stub_artifact_text(text):
        return False
    # Strip negated APPROVE phrases, then require a terminal-line APPROVE
    cleaned = _NEGATED_APPROVE_RE.sub(" ", text)
    if not _TERMINAL_APPROVE_LINE_RE.search(cleaned):
        return False
    # If any remaining whole-word APPROVE is only inside remaining negation
    # (defensive): require at least one terminal line match on cleaned text
    return True


def parse_verdict(text: str) -> str:
    """Return APPROVE | REQUEST_CHANGES | FAILED | UNKNOWN."""
    if not text or not text.strip():
        return "UNKNOWN"

    stripped = text.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            jv = _json_verdict(data)
            if jv is not None:
                return jv

    has_failed = bool(_FAILED_RE.search(text))
    has_rc = bool(_REQUEST_CHANGES_RE.search(text))
    has_approve = prose_has_terminal_approve(text)

    if has_failed:
        return "FAILED"
    if has_rc:
        return "REQUEST_CHANGES"
    if has_approve:
        return "APPROVE"
    return "UNKNOWN"


def parse_verdict_file(path: Path) -> str:
    if not path.is_file():
        return "UNKNOWN"
    try:
        return parse_verdict(path.read_text(encoding="utf-8"))
    except OSError:
        return "UNKNOWN"


def artifact_contains_approve(path: Path) -> bool:
    """True if path is a text/JSON artifact with terminal APPROVE (strict)."""
    if not path.is_file():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return False
    if not text.strip():
        return False
    return parse_verdict(text) == "APPROVE"


def apply_stage_exit_codes(
    verdict: str,
    *,
    critic_rc: int = 0,
    verifier_rc: int = 0,
) -> str:
    """Fail-closed: non-zero stage exit codes must never yield APPROVE."""
    if int(critic_rc) != 0 or int(verifier_rc) != 0:
        if verdict == "APPROVE":
            return "FAILED"
        if verdict == "UNKNOWN":
            return "FAILED"
    return verdict


__all__ = [
    "apply_stage_exit_codes",
    "artifact_contains_approve",
    "is_stub_artifact_text",
    "parse_verdict",
    "parse_verdict_file",
    "prose_has_terminal_approve",
]
