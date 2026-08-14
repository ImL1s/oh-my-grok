---
name: omg-wiki
description: OMG skill projection (omg_native, read-only)
omg_projection: true
omg_classification: omg_native
omg_capability_mode: read-only
omg_source_skill: skills/omg-wiki/SKILL.md
---
# PROJECTION — not an installed Antigravity plugin

This file is a static parity projection of the Grok plugin skill
`skills/omg-wiki/SKILL.md`. It is not an installed Antigravity plugin,
not live AG evidence, and does not mean `agy` skill discovery works.

- Catalog: `skills/catalog.json`
- capability_mode: `read-only` (never `execute`/`all`)
- Playbook without runtime evidence stays `configured`, not `verified`.

# omg-wiki — local knowledge base

Karpathy-style **markdown wiki** (no vector DB). CLI writes pages under
`.omg/wiki/`.

## HARD RULES

- Prefer `omg wiki ingest|query|list` over hand-editing random paths.
- Do not store secrets, tokens, or PII.
- Wiki is **not** verified/run authority — never replace `omg state` / accept.

## Use when

- Capture a durable decision, bug diagnosis, or architecture note across sessions.
- Search prior notes: `omg wiki query "…"`.

## Session playbook

```bash
omg wiki list
omg wiki ingest --title "Topic" --text "facts…" --tags "arch,bug"
omg wiki query "keyword"
```

On SessionEnd / before compact: ingest 1–3 high-value notes (decisions, not transcripts).

## CLI

```bash
omg wiki list
omg wiki ingest --title T --text "..." [--tags a,b] [--source note]
omg wiki query "needle" [--limit 20]
```
