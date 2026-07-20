# Plan 017: [Direction spike] Productize host-session resume (no Stop pin)

> **Drift check**: `git diff --stat 997bcce..HEAD -- omg_cli/host_session.py omg_cli/resume.py omg_cli/modes.py omg_cli/pipeline.py skills/omg-ralph skills/omg-autopilot docs/research/stop-continuation`

## Status

- **Priority**: P2 (direction)
- **Effort**: M–L
- **Risk**: MED
- **Depends on**: none (resume CLI shipped in 0.3.0 — build on it)
- **Category**: direction
- **Planned at**: commit `997bcce`, 2026-07-21

## Why this matters

Stop-continuation is **DO NOT BUILD** (host cannot hard-pin chat). Durability is CLI outer loop + host session binding. Partial pieces exist (`host_session.py`, `omg resume`, ralph `--resume`); productizing means every long mode reuses session IDs and surfaces one next-command UX.

## Current state

- `omg_cli/resume.py` + `cmd_resume` shipped (v0.3.0).
- `host_session.py` allocates UUID, binds `--session-id` / `--resume`.
- Ralph resume path exists; pipeline/autopilot continuity varies.
- CONSENSUS: no Stop reinject.

## Spike deliverables

1. Matrix of modes × session fields persisted in status.json (`grok_session_id`, state).
2. Gaps: which modes silently start new sessions.
3. Live canary sketch: ralph max_iter≥2 reuses same session id.
4. Skill text: when to run `omg resume` vs `omg ralph --resume`.

## Out of scope

- Stop hook blocking
- Cross-host session migration
- HUD TUI (lightweight `state --human` / hud already partially exist)

## Done criteria

- [ ] Gap matrix file written
- [ ] Prioritized implementation list (P0/P1) for a follow-up build plan
- [ ] No Stop pin proposal

## STOP conditions

- Host drops `--resume` support — redesign around context packs only and document.

## Maintenance notes

- Keep single next-action dialect via `omg resume` / `state --human`.
