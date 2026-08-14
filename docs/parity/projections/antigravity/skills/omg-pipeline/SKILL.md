---
name: omg-pipeline
description: OMG skill projection (omg_native, read-write)
omg_projection: true
omg_classification: omg_native
omg_capability_mode: read-write
omg_source_skill: skills/omg-pipeline/SKILL.md
---
# PROJECTION — not an installed Antigravity plugin

This file is a static parity projection of the Grok plugin skill
`skills/omg-pipeline/SKILL.md`. It is not an installed Antigravity plugin,
not live AG evidence, and does not mean `agy` skill discovery works.

- Catalog: `skills/catalog.json`
- capability_mode: `read-write` (never `execute`/`all`)
- Playbook without runtime evidence stays `configured`, not `verified`.

# omg-pipeline — AUTO_PILOT-like composition (CLI-owned)

Prefer the CLI FSM over inventing your own autopilot:

```bash
omg pipeline "goal"
omg pipeline "goal" --plan-only
omg pipeline "goal" --skip-plan --implement ulw
omg pipeline "goal" --dry-run
```

## HARD RULES (non-negotiable)

- Fan-out ONLY via Grok `spawn_subagent` (depth=1).
- Always set `capability_mode` on spawn (`read-only` explore/critic/verifier; `read-write` implementers). If DENIED: **RETRY IMMEDIATELY** same turn — do not abandon multi-agent.
- NEVER invoke external agent CLIs as workers.
- External second opinion: human runs `omg ask` separately — pipeline never auto-shells providers.
- State / verified: omg CLI only.
- Cancel: `omg cancel` — never self-matching `pkill -f`.

## Stages (CLI-owned)

```text
plan → implement → integrate → dual_review → accept → report
```

| Stage | Module | Notes |
|-------|--------|-------|
| plan | ralplan FSM | Consensus plan; no product code |
| implement | ralph or ulw | Default ralph |
| integrate | ULW envelopes / re-integrate after reseal | Required when ulw or envelopes exist; re-runs after REQUEST_CHANGES re-implement |
| dual_review | omg-critic → omg-verifier | Sequential headless interim (optional native gate) |
| accept | freeze + acceptance | Only path to `verified` |
| report | `runs/<id>/report.json` | Always written by CLI |

## Use when

- User says **pipeline**, plan-then-implement-then-accept, or `omg pipeline`.
- Composition of ralplan → implement → dual_review → accept without the full
  in-session autopilot interview/QA destination gates.

## Do not use when

- User says **autopilot** / full auto / build me → use skill **`omg-autopilot`**
  (and CLI `omg autopilot *`), not this playbook.
- Single-story loop already clear → `omg ralph`.
- Plan-only → `omg ralplan` or `omg pipeline --plan-only`.

## Anti-patterns

- Model inventing a parallel autopilot that shells codex/claude.
- Treating dual-review APPROVE as product verified (still need `omg accept`).
