"""Local markdown project wiki under ``.omg/wiki`` (Karpathy-style, no vector DB).

CLI is the writer for durable pages; agents propose text via ``omg wiki ingest``.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from omg_cli.contracts.path_keys import (
    DATA_FILE_MODE,
    atomic_write_bytes,
    ensure_managed_dir,
    exclusive_lock,
)
from omg_cli.contracts.writer_chain import sha256_hex
from omg_cli.redaction import redact_text

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class WikiError(ValueError):
    pass


def wiki_root(root: Path) -> Path:
    return Path(root) / ".omg" / "wiki"


def ensure_wiki(root: Path) -> Path:
    path = wiki_root(root)
    ensure_managed_dir(path)
    index = path / "INDEX.md"
    with exclusive_lock(path / ".wiki-init.lock"):
        if index.is_file():
            try:
                index.read_text(encoding="utf-8")
                os.chmod(index, DATA_FILE_MODE)
            except UnicodeDecodeError:
                raw = index.read_bytes()
                quarantine = path / f"INDEX.corrupt-{sha256_hex(raw)}.md"
                if quarantine.exists() and quarantine.read_bytes() == raw:
                    index.unlink()
                else:
                    os.replace(index, quarantine)
                    os.chmod(quarantine, DATA_FILE_MODE)
        if not index.is_file():
            atomic_write_bytes(
                index,
                (
                    "# OMG Wiki Index\n\nPages are markdown under `.omg/wiki/`.\n"
                    "Use `omg wiki list` / `omg wiki query` / `omg wiki ingest`.\n"
                ).encode("utf-8"),
                mode=DATA_FILE_MODE,
                replace=False,
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
    clean_title = redact_text(title.strip())
    clean_body = redact_text(body.strip())
    slug = slugify(clean_title)
    path = wroot / f"{slug}.md"
    clean_tags = [redact_text(t.strip()) for t in (tags or []) if t and t.strip()]
    with exclusive_lock(wroot / ".wiki.lock"):
        header = [
            f"# {clean_title}",
            "",
            f"<!-- omg-wiki slug={slug} updated={_utc_now()} -->",
        ]
        if clean_tags:
            header.append(f"<!-- tags: {', '.join(clean_tags)} -->")
        if source:
            header.append(f"<!-- source: {redact_text(source.strip())[:200]} -->")
        header.append("")
        text = "\n".join(header) + clean_body + "\n"
        if path.is_file():
            try:
                prev = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                raw = path.read_bytes()
                quarantine = path.with_name(f"{slug}.corrupt-{sha256_hex(raw)}.md")
                os.replace(path, quarantine)
                os.chmod(quarantine, DATA_FILE_MODE)
                prev = ""
            if prev:
                text = (
                    prev.rstrip()
                    + "\n\n---\n\n"
                    + f"## Update {_utc_now()}\n\n"
                    + clean_body
                    + "\n"
                )
        atomic_write_bytes(
            path,
            text.encode("utf-8"),
            mode=DATA_FILE_MODE,
            replace=True,
        )
        _rebuild_index(wroot)
    return {"path": str(path), "slug": slug, "title": clean_title}


def _touch_index(wroot: Path, slug: str, title: str) -> None:
    del slug, title
    _rebuild_index(wroot)


def _rebuild_index(wroot: Path) -> None:
    pages: list[tuple[str, str]] = []
    for page in sorted(wroot.glob("*.md"), key=lambda item: item.name.encode("utf-8")):
        if page.name == "INDEX.md" or ".corrupt-" in page.name:
            continue
        try:
            first = page.read_text(encoding="utf-8").splitlines()[0]
        except (OSError, UnicodeDecodeError, IndexError):
            continue
        title = first[2:].strip() if first.startswith("# ") else page.stem
        pages.append((page.stem, title))
    body = (
        "# OMG Wiki Index\n\nPages are markdown under `.omg/wiki/`.\n"
        "Use `omg wiki list` / `omg wiki query` / `omg wiki ingest`.\n\n"
        + "".join(f"- [{title}]({slug}.md)\n" for slug, title in pages)
    )
    atomic_write_bytes(
        wroot / "INDEX.md",
        body.encode("utf-8"),
        mode=DATA_FILE_MODE,
        replace=True,
    )


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
