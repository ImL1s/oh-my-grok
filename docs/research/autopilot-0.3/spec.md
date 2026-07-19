# Autopilot Spec — oh-my-grok 0.3.0 functional product gaps

**date_utc:** 2026-07-20  
**Input:** User functional-gap discussion + `/autopilot` explore (Grok agents)  
**Research:**
- `.omc/research/autopilot-explore-isolation.md`
- `.omc/research/autopilot-explore-ulw-parallel.md`
- `.omc/research/autopilot-explore-pipeline.md`
- `.omc/research/autopilot-architect-options.md`

## Problem

0.2.5 delivered CLI contracts + soft-gate evidence. Product isolation and parallel ULW remain **convention** (model follows prompt), not **fail-closed host policy**. Pipeline is a real FSM but not open-box autopilot.

## Goals (0.3.0 slice)

### P0 — Option A: Spawn fail-closed (this autopilot execution)

1. PreToolUse matcher includes `spawn_subagent` (and Task alias if used).
2. `decide_pre_tool_use` denies spawn when:
   - `capability_mode` missing, OR
   - mode incompatible with `subagent_type` role table
3. Role table (initial):
   - **read-write required:** `omg-executor`, and `general-purpose` when used as implementer (default assume implementer → require `read-write`)
   - **read-only required:** `explore`, `plan`, `omg-critic`, `omg-verifier`, and names matching `*critic*`, `*verifier*`, `*explore*`
4. Unit tests cover allow/deny matrices; doctor notes spawn gate exists.
5. Docs: security-model + HARD RULES honesty — still fail-open on hook crash; this is **defense-in-depth soft-gate** that is **honest fail-closed on decision** when hook runs.

### P1 — Option B (next, not this slice unless time)

- ULW post-run auto-integrate or warn
- Skill: leader owns prepare/seal for multi-task
- Live multi-worker matrix

### P2 — Option C deferred

- Open-box pipeline UX, native dual, process fanout promotion

## Non-goals

- Native dual-review product (user waived as process gate)
- Removing leader shell by default
- Claiming hard sandbox

## Success criteria (P0)

- Unit: missing capability_mode → deny
- Unit: general-purpose without mode → deny
- Unit: omg-executor + read-write → allow
- Unit: explore + read-only → allow; explore + read-write → deny
- hooks.json matcher updated
- pytest green
