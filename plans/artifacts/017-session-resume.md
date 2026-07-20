# Spike: Host session resume (plan 017)

**Date:** 2026-07-21 · **Status:** design only · **Never:** Stop pin

## Shipped

- `omg resume`, RESUME.md, `host_session.py`, ralph `--resume`

## Gaps

| Mode | Persist grok_session_id | Reuse on resume |
|------|-------------------------|-----------------|
| ralph | partial | yes via --resume |
| pipeline | check | deepen |
| ulw | check | deepen |
| autopilot | check | deepen |

## Live canary

ralph max_iter≥2 reuses same `grok_session_id` in status.json.

## Next build

Unify next-action dialect: `omg resume` / `state --human` only.
