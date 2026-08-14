---
name: omg-ask
description: OMG skill projection (omg_native, read-only)
omg_projection: true
omg_classification: omg_native
omg_capability_mode: read-only
omg_source_skill: skills/omg-ask/SKILL.md
---
# PROJECTION — not an installed Antigravity plugin

This file is a static parity projection of the Grok plugin skill
`skills/omg-ask/SKILL.md`. It is not an installed Antigravity plugin,
not live AG evidence, and does not mean `agy` skill discovery works.

- Catalog: `skills/catalog.json`
- capability_mode: `read-only` (never `execute`/`all`)
- Playbook without runtime evidence stays `configured`, not `verified`.

# omg-ask — External advisors (user-invoked only)

`omg ask` is a **trusted human broker** for Codex / Claude (fable) / optional Gemini / Antigravity (`agy`). It is **not** a product executor and **not** a default worker path.

## Offline catalog (unproven)

`omg ask list-advisors` and `omg ask explain <id>` print **offline registry** facts only. Every harness is `unproven`. Binaries are `not_probed`. This is not qualification and does not execute a provider.

```bash
omg ask list-advisors
omg ask explain fable    # resolves to claude-cli
omg ask explain agy      # resolves to antigravity-cli
```

## HARD RULES (non-negotiable)

- Fan-out ONLY via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- **NEVER** invoke `claude` / `codex` / `omc team` / `agy` / `cursor-agent` via `run_terminal_command` as workers.
- For external second opinions, tell the **human** to run:

```bash
omg ask codex "your question"
omg ask claude "your question"   # fable alias
omg ask gemini "your question"   # optional; may be missing
omg ask agy "your question"      # Antigravity via ProviderAdapter (#67-C)
```

- Output is **advisory** under `.omg/artifacts/ask-*.md`.
- Do **not** mark `verified` / `passes`. Do **not** apply advisor patches automatically.
- Product changes require `omg ulw` / `omg ralph` / `omg pipeline` implement stages.

## Use when

- User asks for Codex review, Fable/Claude second opinion, multi-vendor dual-review.
- High-risk change needs an external advisor **in addition to** Grok-native critic/verifier.

## Do not use when

- Default implement / plan / verify — use Grok-native modes.
- You are an agent tempted to shell `claude -p` — **stop**; use Grok tools only.

## Playbook

1. Draft the question (scope, files, risks).
2. Ask the human to run `omg ask <provider> "…"`.
3. Read the artifact path they paste; treat as advisory.
4. Continue implementation with Grok-native tools only.
