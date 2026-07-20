---
name: omg-wiki
description: >
  Persistent markdown project wiki under .omg/wiki. Use when user says wiki,
  project memory, capture decision, ingest knowledge, or query past notes.
---

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
