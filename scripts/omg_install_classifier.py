"""Same-path install classifier for ``scripts/install-plugin.sh``.

Importable + hermetically unit-tested. Mirrors doctor.py multi-candidate
path fields (``source``, ``path``, ``installPath``, ``install_path``): any
candidate whose realpath (or raw string) matches the checkout root counts as
same-path. A false-POSITIVE is worse than a false-negative — it would trigger
uninstall+reinstall against a different install.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any


PLUGIN_NAME_SUBSTR = "oh-my-grok"
_PATH_KEYS = ("source", "path", "installPath", "install_path")


def path_field_candidates(item: dict[str, Any]) -> list[str]:
    """Collect path-like fields as independent candidates (no OR-collapse)."""
    out: list[str] = []
    seen: set[str] = set()
    for key in _PATH_KEYS:
        val = item.get(key)
        if val is None:
            continue
        s = str(val).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _realpath_norm(p: str) -> str:
    try:
        return os.path.realpath(p).rstrip("/")
    except OSError:
        return p.rstrip("/")


def is_same_path_candidate(candidate: str, root: str) -> bool:
    """True if candidate matches root via realpath and/or raw equality fallback."""
    if not candidate or not root:
        return False
    cand_raw = candidate.rstrip("/")
    root_raw = root.rstrip("/")
    cand_r = _realpath_norm(candidate)
    root_r = _realpath_norm(root)
    # realpath equality (primary) + raw equality (extra fallback — never removes)
    return (
        cand_r == root_r
        or cand_raw == root_raw
        or cand_r == root_raw
        or cand_raw == root_r
    )


def _plugin_list_entries(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for k in ("plugins", "items", "data", "result"):
            n = data.get(k)
            if isinstance(n, list):
                return [x for x in n if isinstance(x, dict)]
        return [data]
    return []


def classify_oh_my_grok_installs(
    list_data: Any,
    root: str,
) -> dict[str, Any]:
    """Classify inventory for same-path vs different-path oh-my-grok entries.

    Parameters
    ----------
    list_data:
        Parsed JSON (list/dict) or a JSON string from ``grok plugin list --json``.
    root:
        This checkout root (preferably already realpath'd by the shell).

    Returns
    -------
    dict with:
      - same_path: bool — any oh-my-grok entry has a path candidate matching root
      - stale: list[str] — human lines for different-path entries (stderr WARN)
    """
    if isinstance(list_data, (str, bytes, bytearray)):
        try:
            list_data = json.loads(list_data)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {"same_path": False, "stale": []}

    same_path = False
    stale: list[str] = []
    for item in _plugin_list_entries(list_data):
        name = str(item.get("name") or item.get("id") or item.get("plugin") or "")
        if PLUGIN_NAME_SUBSTR not in name:
            continue
        cands = path_field_candidates(item)
        if not cands:
            continue
        # Multi-candidate: ANY field realpath (or raw) match counts as same-path.
        # Do NOT OR-collapse to a single src — path is dual-meaning (checkout vs
        # frozen snapshot) and a truthy snapshot path must not hide source/installPath.
        if any(is_same_path_candidate(c, root) for c in cands):
            same_path = True
        else:
            # Genuinely different: no candidate matches this checkout.
            shown = " | ".join(
                f"{k}={item.get(k)!r}" for k in _PATH_KEYS if item.get(k)
            )
            stale.append(f"  key={name!r} {shown}")
    return {"same_path": same_path, "stale": stale}


def classify_doctor_result(*, mode: str, rc: int | None, valid: bool) -> str:
    """Exact lifecycle classifier shared by source and release installation.

    ``rc=0`` is verified success.  ``rc=2`` is a visible development-only
    warning (never release success).  Non-integer/malformed output and every
    other code are hard failures; callers must roll back and must not print a
    success banner.
    """

    if mode not in {"development", "release"}:
        raise ValueError("mode must be development or release")
    if valid is not True or not isinstance(rc, int) or isinstance(rc, bool):
        return "hard_failure"
    if rc == 0:
        return "installed"
    if rc == 2 and mode == "development":
        return "completed_with_warning"
    return "hard_failure"


def main(argv: list[str] | None = None) -> int:
    """CLI for install-plugin.sh: env OMG_INSTALL_ROOT + OMG_INSTALL_LIST_JSON."""
    del argv  # unused; env-driven
    root = os.environ.get("OMG_INSTALL_ROOT", "")
    raw = os.environ.get("OMG_INSTALL_LIST_JSON", "")
    result = classify_oh_my_grok_installs(raw, root)
    if result["stale"]:
        print(
            "WARN: found oh-my-grok entry(ies) whose source/path differs from this checkout:",
            file=sys.stderr,
        )
        for line in result["stale"]:
            print(line, file=sys.stderr)
        print(f"  this checkout: {root!r}", file=sys.stderr)
        print(
            "  recommend: grok plugin uninstall oh-my-grok  (then re-run this script)",
            file=sys.stderr,
        )
        print(
            "  (installer will NOT auto-uninstall different-path entries — remove those yourself)",
            file=sys.stderr,
        )
    print(f"SAME_PATH_INSTALLED={1 if result['same_path'] else 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
