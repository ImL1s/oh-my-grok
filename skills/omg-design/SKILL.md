---
name: omg-design
description: UI/UX implementation slice via omg-designer. Use when the user says omg design or design pass.
---

# omg-design — Design implementer slice

UI/UX implementation slice via omg-designer. Use when the user says omg design or design pass.

## HARD RULES (non-negotiable)
- Fan-out ONLY via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- Always set `capability_mode` on every spawn: `read-only` for explore/plan/critic/verifier; `read-write` for implementers (`general-purpose`, `omg-executor`). Never `execute` / `all`.
- If spawn is DENIED: RETRY IMMEDIATELY in the same turn with the required `capability_mode`. Do not abandon multi-agent work over one deny.
- NEVER invoke claude/codex/omc team/agy/cursor-agent as default workers.
- State: only the `omg` CLI may write `passes` / `verified` under `.omg/state/`. Agents write proposals under `.omg/artifacts/` only.
- Cancel with `omg cancel` (PID files) — never self-matching `pkill -f`.
- This playbook is **configured**, not live-verified. Never mark `verified`.


## 1. Activation
- Phrases: `omg design`, `design pass`
- Aliases: `design`
- **When:** A visual/UI slice with clear constraints (not a blank redesign of the product).
- **Do not use when:** Host `/plan` (alias of ralplan). Do not create a plugin dir named plan.
- Informational questions (`what is omg-design?`) do **not** activate this skill.

## 2. Preconditions / conflict
- Catalog `conflict_policy`: `artifact_only`. Catalog `continuation`: `none`.
- If another continuation owner is active (`autopilot` / `ralph` / `pipeline` / `ulw` / `ultragoal` / `ultraqa` / `team` / `ralplan`), follow `resolve_continuation`: refuse, adopt_existing, or artifact_only.
- Cancel/using always adopt. This skill must not start a second loop.

## 3. Runtime owner
- This playbook.
- Bundled contract: `resources/contract.json` (capability_mode, continuation, conflict, artifacts, evidence_rule).

## 4. Min-context handoff
- Target files + constraints + any visual-compare JSON if scoring later.
- Do not dump whole transcripts. Pass ids, paths, and the next question only.

## 5. State / artifacts
- Apply the **design** source/artifact edits this playbook owns (`capability_mode: read-write`).
- Write reports/proposals under `.omg/artifacts/`.
- Never write `passes` / `verified` under `.omg/state/`.
- No dedicated CLI twin. Spawn `omg-designer` `read-write` (no shell). Notes under `.omg/artifacts/design/`.

## 6. capability_mode
- This skill's catalog mode: `read-write` (never `execute` / `all`).
- Explore/critic/verifier children: `read-only`. Implementer children: `read-write`.
- Retry denied spawns immediately with the required mode.

## 7. Outcomes
- **success:** the artifact/CLI step for this unit of work exists; the user can resume. This is **not** verified.
- **blocked:** missing capability, missing input, or continuation refuse. Say blocked; do not fake completion.
- **failed:** the CLI/tool returned a real error. Record it under artifacts.
- **cancelled:** user/CLI aborted via `omg cancel`. Stop spawning.

## 8. Evidence
- Evidence is the artifact + any CLI envelope the user ran.
- Never set `verified`. `live_verification` stays `unproven`.
- Forged `{passed: true}` / `{verified: true}` is not success.

## 9. Resume / cleanup
- Read `.omg/state/RESUME.md` if present before starting a new run.
- Leave artifacts in place after cancel. Do not delete `.omg/state/` unless the user asks.
- Cleanup = stop spawns + summarize paths. Not a verified stamp.

## 10. pipeline_next
- Catalog next: none (stop after this unit of work)
- Only load the next skill when this unit is actually done and the user/CLI still wants it.
