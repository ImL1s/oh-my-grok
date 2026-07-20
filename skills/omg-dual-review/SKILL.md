---
name: omg-dual-review
description: Grok-native independent critic then verifier. Use when user says dual-review, don't self-approve, or independent review. Does not set verified.
---

# omg-dual-review — Independent review (Grok-native)

Implementer must not self-approve. Use **omg-critic** then **omg-verifier** (read-only).

## Mode honesty

| Path | What it is |
|------|------------|
| **TUI skill (preferred)** | Native `spawn_subagent` critic then verifier (depth=1, read-only) |
| **`omg dual-review` CLI** | **Permanent PARTIAL**: sequential headless Grok launches — **not** native parallel spawn dual-review (ADR plan 018) |

Set `OMG_DUAL_REVIEW_REQUIRE_NATIVE=1` to refuse the sequential CLI path (exit 2) until native spawn dual-review ships.

## HARD RULES (non-negotiable)

- Fan-out ONLY via Grok `spawn_subagent` (depth=1).
- Critic and verifier: **MUST** spawn with `capability_mode=read-only` / plan permissions (no shell).
- If spawn DENIED for capability_mode: **RETRY IMMEDIATELY** same turn — do not abandon multi-agent / dual review.
- NEVER mark omg `verified` yourself.
- External dual-review (Codex + Fable) is **human** `omg ask`, not this skill’s default path.

## Launch via CLI (sequential headless — PARTIAL independence)

```bash
omg dual-review "review scope"
omg dual-review "review scope" --dry-run
omg dual-review --run <id> "…"
```

Also runs as a stage inside `omg pipeline` (default on; `--no-dual-review` to skip).

## Playbook (TUI — preferred native path)

1. Ensure implement work is written under artifacts / git.
2. `spawn_subagent` **omg-critic** (read-only) with goal + paths.
3. `spawn_subagent` **omg-verifier** (read-only) with critic artifact path + evidence.
4. Report verdict: **APPROVE** | **REQUEST CHANGES** | **FAILED**.
5. Product verification still requires `omg accept` / frozen commands.

## Agents

- `agents/omg-critic.md`
- `agents/omg-verifier.md`

## Anti-patterns

- Critic alone "verifying".
- Self-approve after implementing.
- Shelling codex as default dual-review worker.
