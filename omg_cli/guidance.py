"""OMG global rules injection reconciler.

Renders templates/omg-rules.md and reconciles it into
$GROK_HOME/rules/omg.md (default ~/.grok/rules/omg.md) idempotently
and non-destructively.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

OMG_START = "<!-- OMG:START -->"
OMG_END = "<!-- OMG:END -->"
USER_POLICY_START = "<!-- USER:OMG:POLICY:START -->"
USER_POLICY_END = "<!-- USER:OMG:POLICY:END -->"

_SOURCE_HASH_LINE_RE = re.compile(r"<!-- OMG:SOURCE-HASH:.*? -->")
_VERSION_LINE_RE = re.compile(r"<!-- OMG:VERSION:(.*?) -->")
_SOURCE_HASH_VALUE_RE = re.compile(r"<!-- OMG:SOURCE-HASH:([0-9a-fA-F]*) -->")


class GuidanceError(Exception):
    """Base error for guidance operations."""


class GuidanceCorruptionError(GuidanceError):
    """Corrupt marker state in a rules file."""


def grok_home() -> Path:
    raw = os.environ.get("GROK_HOME")
    if raw is not None and raw.strip() != "":
        return Path(raw)
    return Path.home() / ".grok"


def rules_file_path(home: Path | None = None) -> Path:
    return (home or grok_home()) / "rules" / "omg.md"


def plugin_root() -> Path:
    return Path(__file__).resolve().parents[1]


def template_path() -> Path:
    return plugin_root() / "templates" / "omg-rules.md"


def plugin_version() -> str:
    try:
        data = json.loads((plugin_root() / "plugin.json").read_text(encoding="utf-8"))
        return str(data["version"])
    except Exception:
        return "0"


def _blank_source_hash_line(text: str) -> str:
    return _SOURCE_HASH_LINE_RE.sub("<!-- OMG:SOURCE-HASH: -->", text)


def _ensure_single_trailing_newline(text: str) -> str:
    return text.rstrip("\n") + "\n"


def render_managed_block(version: str | None = None) -> str:
    version = version if version is not None else plugin_version()
    path = template_path()
    if not path.is_file():
        raise GuidanceError(f"template missing: {path}")
    text = path.read_text(encoding="utf-8")
    text = text.replace("{{VERSION}}", version)
    for_hash = _blank_source_hash_line(text)
    digest = hashlib.sha256(for_hash.encode()).hexdigest()
    text = text.replace("{{SOURCE_HASH}}", digest)
    return _ensure_single_trailing_newline(text)


def _extract_managed_block(text: str) -> tuple[int, int] | None:
    start_count = text.count(OMG_START)
    end_count = text.count(OMG_END)

    if start_count == 0 and end_count == 0:
        return None
    if start_count == 0 and end_count > 0:
        raise GuidanceCorruptionError("OMG_END present without OMG_START")
    if start_count > 1:
        raise GuidanceCorruptionError("OMG_START appears more than once")
    if end_count > 1:
        raise GuidanceCorruptionError("OMG_END appears more than once")
    if end_count == 0:
        raise GuidanceCorruptionError("OMG_START present but no OMG_END after it")

    start_idx = text.index(OMG_START)
    end_idx = text.index(OMG_END)
    if end_idx < start_idx:
        raise GuidanceCorruptionError("OMG_END appears before OMG_START")
    end_exclusive = end_idx + len(OMG_END)
    return (start_idx, end_exclusive)


def _join_regions(*parts: str) -> str:
    """Join non-empty parts with exactly one newline between them."""
    cleaned: list[str] = []
    for p in parts:
        if p is None:
            continue
        s = p.strip("\n")
        if s:
            cleaned.append(s)
    if not cleaned:
        return "\n"
    return "\n".join(cleaned) + "\n"


def reconcile_rules_text(existing: str, new_block: str) -> tuple[str, str]:
    new_block = _ensure_single_trailing_newline(new_block)

    if existing is None or existing.strip() == "":
        return new_block, "created"

    span = _extract_managed_block(existing)
    if span is None:
        # Foreign/user file: append managed block, preserve existing.
        result = _join_regions(existing, new_block)
        if result == existing:
            return existing, "unchanged"
        return result, "updated"

    start, end = span
    before = existing[:start]
    after = existing[end:]
    result = _join_regions(before, new_block, after)
    if result == existing:
        return existing, "unchanged"
    return result, "updated"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def install_global_rules(
    *, version: str | None = None, home: Path | None = None
) -> tuple[Path, str]:
    path = rules_file_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    new_block = render_managed_block(version)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    new_text, action = reconcile_rules_text(existing, new_block)
    if action != "unchanged":
        if path.exists():
            bak = path.with_suffix(".md.bak")
            bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        _atomic_write(path, new_text)
    return (path, action)


def _parse_installed_version(block: str) -> str | None:
    m = _VERSION_LINE_RE.search(block)
    if not m:
        return None
    return m.group(1).strip() or None


def _parse_embedded_source_hash(block: str) -> str | None:
    m = _SOURCE_HASH_VALUE_RE.search(block)
    if not m:
        return None
    value = m.group(1).strip()
    return value or None


def _compute_source_hash(block: str) -> str:
    blanked = _blank_source_hash_line(block)
    return hashlib.sha256(blanked.encode()).hexdigest()


def rules_status(
    *, version: str | None = None, home: Path | None = None
) -> dict:
    expected = version if version is not None else plugin_version()
    path = rules_file_path(home)
    result: dict = {
        "present": False,
        "path": str(path),
        "corrupt": False,
        "installed_version": None,
        "expected_version": expected,
        "version_ok": False,
        "source_hash_ok": False,
        "drift": False,
    }

    if not path.is_file():
        return result

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return result

    try:
        span = _extract_managed_block(text)
    except GuidanceCorruptionError:
        result["corrupt"] = True
        return result

    if span is None:
        return result

    start, end = span
    block = text[start:end]
    # Normalize trailing newline for comparison with rendered block
    block_norm = _ensure_single_trailing_newline(block)

    result["present"] = True
    installed_version = _parse_installed_version(block)
    result["installed_version"] = installed_version
    result["version_ok"] = installed_version == expected

    embedded = _parse_embedded_source_hash(block)
    recomputed = _compute_source_hash(block_norm)
    result["source_hash_ok"] = (
        embedded is not None and embedded.lower() == recomputed.lower()
    )

    # Drift: installed managed block != freshly rendered for installed_version
    if installed_version is not None:
        try:
            fresh = render_managed_block(version=installed_version)
            result["drift"] = block_norm != fresh
        except GuidanceError:
            result["drift"] = True
    else:
        result["drift"] = True

    return result
