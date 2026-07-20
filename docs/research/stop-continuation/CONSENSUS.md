# Consensus — Stop continuation (OMC persistent-mode) for oh-my-grok?

**date_utc:** 2026-07-20  
**Council:** explore (host) · architect · critic · planner (Grok subagents)  
**Verdict:** **DO NOT BUILD** in-session Stop continuation for 0.3.x

## Unanimous finding

| Agent | Verdict |
|-------|---------|
| Explore (host feasibility) | **Not host-feasible** — Grok Stop is non-blocking; only PreToolUse blocks |
| Architect | **Option A (CLI-only)** — keep status quo |
| Critic | **DO_NOT_BUILD** — dead code or product lie |
| Planner | **DO NOT BUILD** decision record |

## Why “not implemented” is correct

1. **Host:** `HookEventName::is_blocking()` is true **only** for `PreToolUse`. Stop is lifecycle/passive; stdout control JSON is ignored for blocking.
2. **Architecture:** Option B already puts durability in **`omg ralph` / `omg pipeline` outer process loop**, not Stop hooks.
3. **Trust:** `verified` stays CLI-only; Stop must never become a second completion story.
4. **ROI:** Porting OMC persistent-mode would either no-op or fight cancel/max-iter; solo capacity better spent on ULW product path / pipeline polish.

## What users should use instead of “OMC autopilot feel”

```bash
omg ralph "goal"              # persist until verified (CLI loop)
omg pipeline "goal"           # plan → implement → dual → accept
omg ulw "goal"                # parallel one-shot (+ optional pipeline)
omg cancel                    # stop supervised run
```

## Revisit only if

Grok adds **blocking Stop** (or documented ForceContinue for plugin file hooks) **and** a live canary proves reinjection works end-to-end.

## Source docs

- `stop-continuation-host-feasibility.md`
- `stop-continuation-architect.md`
- `stop-continuation-critic.md`
- `stop-continuation-decision.md`
