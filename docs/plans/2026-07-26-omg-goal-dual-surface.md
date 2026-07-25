# OMG Goal Dual-Surface — host `/goal` + ledger (2026-07-26)

**Context:** Grok ≥0.2.94 has session `/goal` (single-goal harness). OMC/OMX bind host goal + durable ultragoal ledger via CLI handoff. OMG shipped `omg goal *` but honesty docs falsely claimed "no host `/goal`". Qwen source review: `docs/research/goal-dual-surface/2026-07-26-qwen-full-review.md`.

**Architecture:**
- **Ledger** = `omg goal *` / `.omg/ultragoal/` (cross-session, CLI `verified`)
- **Host `/goal`** = session pressure; while Active it **bypasses** Stop gate; after release, OMG Stop pin enforces autopilot gates
- **Handoff only** — hooks cannot mutate host `/goal`; print instructions (OMC pattern)
- Default **aggregate pointer** objective (not per-story enumeration)

## Tasks

### Task G1 — P0 honesty + runtime precedence (docs/skills)

**Files:** `skills/omg-ultragoal/SKILL.md`, `docs/skills.md` (+zh), `docs/autopilot.md` (+zh), `templates/omg-rules.md`, `tests/test_skill_inventory.py` / `tests/test_autopilot_honesty_docs.py` as needed; regen `omg_capabilities.lock.json`.

**Must:**
1. Replace "No host `/goal` API" with: Grok **has** slash `/goal` (session-scoped, single goal, replace-on-set; Active bypasses Stop; restart → paused needing `/goal resume`). What Grok lacks is OMX multi-goal **tool** API (`get_goal`/`create_goal`).
2. Remove anti-pattern "Claiming host `/goal` exists on Grok".
3. `docs/autopilot.md`: one sentence on runtime precedence (Active `/goal` dominates; Stop pin after release).
4. Keep Stop pin honesty (cap 8, fail-open) unchanged.

**Commit:** `docs(goal): correct host /goal honesty; document Stop-bypass precedence`

### Task G2 — P1 handoff: skill + rules + `omg goal set-host`

**Files:** `omg_cli/goals.py`, `omg_cli/main.py`, `skills/omg-ultragoal/SKILL.md`, `templates/omg-rules.md`, `docs/skills.md` (+zh), tests.

**CLI:** `omg goal set-host --goal GOAL` prints model-facing handoff (never mutates host):
- Check `/goal status` first; setting **replaces** any active goal
- After restart use `/goal resume` (Active demoted to paused)
- Suggested aggregate pointer objective embedding snapshot path + acceptance + artifacts
- Never claim CLI set the host goal

**Rules:** ultragoal / `omg goal` routing row in `<workflow_routing>`.

**Tests:** CLI prints handoff with `/goal` and snapshot path; skill no longer claims no host `/goal`; docs drift OK.

**Commit:** `feat(goal): set-host handoff for Grok /goal + rules routing`

### Task G3 (deferred this PR) — claim-guard / `--arm-goal` / live probe

See Qwen addendum P1-5, P2-1, E.1. Not in this PR.

## Out of scope
- Programmatic host `/goal` mutation from hooks
- Per-story Stop pin while ledger pending
- Hand-editing `goal_mode_state` JSON
