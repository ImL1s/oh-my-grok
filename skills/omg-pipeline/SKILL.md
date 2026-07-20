---
name: omg-pipeline
description: End-to-end plan→implement→verify playbook for oh-my-grok. Prefer CLI `omg pipeline`. Grok-native workers only.
---

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

- User says autopilot, full pipeline, plan then implement then verify, e2e mode.

## Do not use when

- Single-story loop already clear → `omg ralph`.
- Plan-only → `omg ralplan` or `omg pipeline --plan-only`.

## Anti-patterns

- Model inventing a parallel autopilot that shells codex/claude.
- Treating dual-review APPROVE as product verified (still need `omg accept`).
