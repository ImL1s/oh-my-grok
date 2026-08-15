"""Copy-safe edit evidence under ``.omg/artifacts/edit/`` (never verified)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final

from omg_cli.redaction import redact_value

ARTIFACT_KIND: Final[str] = "omg.edit.artifact.v1"
_STRIP_KEYS: Final[frozenset[str]] = frozenset(
    {
        "after_context",
        "before_context",
        "body",
        "comment",
        "contents",
        "excerpt",
        "old_text",
        "replacement",
        "source",
        "text",
        "unified_diff",
    }
)


def _strip_raw(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _strip_raw(item)
            for key, item in value.items()
            if str(key) not in _STRIP_KEYS
        }
    if isinstance(value, list):
        return [_strip_raw(item) for item in value]
    return value


def write_edit_artifact(root: Path, payload: dict[str, Any]) -> str:
    """Write a redacted JSON artifact. Returns a workspace-relative POSIX path.

    Never writes ``passes`` / ``verified``. Raw source fields are stripped
    before redaction. Digest is SHA-256 of the canonical body without the
    digest field.
    """

    body = _strip_raw(dict(payload))
    body.pop("passes", None)
    body.pop("verified", None)
    body["kind"] = body.get("kind") or ARTIFACT_KIND
    body["schema_version"] = int(body.get("schema_version") or 1)
    safe = redact_value(body)
    if not isinstance(safe, dict):
        raise ValueError("edit artifact payload must be an object")
    canonical = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    safe = dict(safe)
    safe["digest"] = digest
    rendered = json.dumps(safe, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    rel = f".omg/artifacts/edit/{digest}.json"
    dest = Path(root) / ".omg" / "artifacts" / "edit" / f"{digest}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(rendered, encoding="utf-8", newline="\n")
    return rel
