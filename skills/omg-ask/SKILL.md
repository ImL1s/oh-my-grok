---
name: omg-ask
description: Human-only broker for external advisor CLIs (codex/claude/gemini). Use when user wants a second opinion via omg ask. Never shell external CLIs as workers.
---

# omg-ask — External advisors (user-invoked only)

`omg ask` is a **trusted human broker** for Codex / Claude (fable) / optional Gemini. It is **not** a product executor and **not** a default worker path.

## HARD RULES (non-negotiable)

- Fan-out ONLY via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- **NEVER** invoke `claude` / `codex` / `omc team` / `agy` / `cursor-agent` via `run_terminal_command` as workers.
- For external second opinions, tell the **human** to run:

```bash
omg ask codex "your question"
omg ask claude "your question"   # fable alias
omg ask gemini "your question"   # optional; may be missing
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
