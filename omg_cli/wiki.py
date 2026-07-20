"""Local markdown project wiki under ``.omg/wiki`` (Karpathy-style, no vector DB).

CLI is the writer for durable pages; agents propose text via ``omg wiki ingest``.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class WikiError(ValueError):
    pass


def wiki_root(root: Path) -> Path:
    return Path(root) / ".omg" / "wiki"


def ensure_wiki(root: Path) -> Path:
    path = wiki_root(root)
    path.mkdir(parents=True, exist_ok=True)
    index = path / "INDEX.md"
    if not index.is_file():
        index.write_text(
            "# OMG Wiki Index\n\nPages are markdown under `.omg/wiki/`.\n"
            "Use `omg wiki list` / `omg wiki query` / `omg wiki ingest`.\n",
            encoding="utf-8",
        )
    return path


def slugify(title: str) -> str:
    s = (title or "").strip().lower()
    s = _SLUG_RE.sub("-", s).strip("-")
    return s[:80] or "page"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ingest(
    root: Path,
    *,
    title: str,
    body: str,
    tags: list[str] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    if not (title or "").strip():
        raise WikiError("title required")
    if not (body or "").strip():
        raise WikiError("body required")
    root = Path(root)
    wroot = ensure_wiki(root)
    slug = slugify(title)
    path = wroot / f"{slug}.md"
    tags = [t.strip() for t in (tags or []) if t and t.strip()]
    header = [
        f"# {title.strip()}",
        "",
        f"<!-- omg-wiki slug={slug} updated={_utc_now()} -->",
    ]
    if tags:
        header.append(f"<!-- tags: {', '.join(tags)} -->")
    if source:
        header.append(f"<!-- source: {source.strip()[:200]} -->")
    header.append("")
    text = "\n".join(header) + body.strip() + "\n"
    # Append if exists (knowledge accumulates)
    if path.is_file():
        prev = path.read_text(encoding="utf-8")
        text = (
            prev.rstrip()
            + "\n\n---\n\n"
            + f"## Update {_utc_now()}\n\n"
            + body.strip()
            + "\n"
        )
    path.write_text(text, encoding="utf-8")
    _touch_index(wroot, slug, title.strip())
    return {"path": str(path), "slug": slug, "title": title.strip()}


def _touch_index(wroot: Path, slug: str, title: str) -> None:
    index = wroot / "INDEX.md"
    line = f"- [{title}]({slug}.md)\n"
    if index.is_file():
        cur = index.read_text(encoding="utf-8")
        if f"({slug}.md)" in cur:
            return
        index.write_text(cur.rstrip() + "\n" + line, encoding="utf-8")
    else:
        index.write_text("# OMG Wiki Index\n\n" + line, encoding="utf-8")


def list_pages(root: Path) -> list[dict[str, str]]:
    wroot = ensure_wiki(root)
    out: list[dict[str, str]] = []
    for p in sorted(wroot.glob("*.md")):
        if p.name == "INDEX.md":
            continue
        out.append({"slug": p.stem, "path": str(p), "title": p.stem})
    return out


def query(root: Path, needle: str, *, limit: int = 20) -> list[dict[str, Any]]:
    if not (needle or "").strip():
        raise WikiError("query string required")
    wroot = ensure_wiki(root)
    low = needle.lower()
    hits: list[dict[str, Any]] = []
    for p in sorted(wroot.glob("*.md")):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if low not in text.lower():
            continue
        # first matching line snippet
        snip = ""
        for line in text.splitlines():
            if low in line.lower():
                snip = line.strip()[:200]
                break
        hits.append({"slug": p.stem, "path": str(p), "snippet": snip})
        if len(hits) >= max(1, int(limit)):
            break
    return hits


__all__ = [
    "WikiError",
    "ensure_wiki",
    "ingest",
    "list_pages",
    "query",
    "slugify",
    "wiki_root",
]
