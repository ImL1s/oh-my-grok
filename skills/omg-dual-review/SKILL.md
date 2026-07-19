---
name: omg-dual-review
description: Grok-native independent critic then verifier. Use when user says dual-review, don't self-approve, or independent review. Does not set verified.
---

# omg-dual-review — Independent review (Grok-native)

Implementer must not self-approve. Use **omg-critic** then **omg-verifier** (read-only).

## HARD RULES (non-negotiable)

- Fan-out ONLY via Grok `spawn_subagent` (depth=1).
- Critic and verifier: **capability_mode read-only** / plan permissions.
- NEVER mark omg `verified` yourself.
- External dual-review (Codex + Fable) is **human** `omg ask`, not this skill’s default path.

## Launch via CLI

```bash
omg dual-review "review scope"
omg dual-review "review scope" --dry-run
omg dual-review --run <id> "…"
```

Also runs as a stage inside `omg pipeline` (default on; `--no-dual-review` to skip).

## Playbook (TUI without CLI FSM)

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
