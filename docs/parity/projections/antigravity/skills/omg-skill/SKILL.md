---
name: omg-skill
description: OMG skill projection (omg_native, read-only)
omg_projection: true
omg_classification: omg_native
omg_capability_mode: read-only
omg_source_skill: skills/omg-skill/SKILL.md
---
# PROJECTION — not an installed Antigravity plugin

This file is a static parity projection of the Grok plugin skill
`skills/omg-skill/SKILL.md`. It is not an installed Antigravity plugin,
not live AG evidence, and does not mean `agy` skill discovery works.

- Catalog: `skills/catalog.json`
- capability_mode: `read-only` (never `execute`/`all`)
- Playbook without runtime evidence stays `configured`, not `verified`.

# omg-skill — Skill catalog inspect

List, show, or resolve oh-my-grok skills from the machine catalog. Use when the user says skill manager, list skills, or skillify (alias).

## HARD RULES (non-negotiable)
- Fan-out ONLY via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- Always set `capability_mode` on every spawn: `read-only` for explore/plan/critic/verifier; `read-write` for implementers (`general-purpose`, `omg-executor`). Never `execute` / `all`.
- If spawn is DENIED: RETRY IMMEDIATELY in the same turn with the required `capability_mode`. Do not abandon multi-agent work over one deny.
- NEVER invoke claude/codex/omc team/agy/cursor-agent as default workers.
- State: only the `omg` CLI may write `passes` / `verified` under `.omg/state/`. Agents write proposals under `.omg/artifacts/` only.
- Cancel with `omg cancel` (PID files) — never self-matching `pkill -f`.
- This playbook is **configured**, not live-verified. Never mark `verified`.


## 1. Activation
- Phrases: `skill manager`, `list skills`
- Aliases: `skill`
- **When:** Need catalog inspect, alias resolve, or bundled resource paths.
- **Do not use when:** Installing host-native names as plugin dirs (plan/goal/loop/...).
- Informational questions (`what is omg-skill?`) do **not** activate this skill.

## 2. Preconditions / conflict
- Catalog `conflict_policy`: `artifact_only`. Catalog `continuation`: `none`.
- If another continuation owner is active (`autopilot` / `ralph` / `pipeline` / `ulw` / `ultragoal` / `ultraqa` / `team` / `ralplan`), follow `resolve_continuation`: refuse, adopt_existing, or artifact_only.
- Cancel/using always adopt. This skill must not start a second loop.

## 3. Runtime owner
- omg-cli skills catalog (read-only).
- Bundled contract: `resources/contract.json` (capability_mode, continuation, conflict, artifacts, evidence_rule).

## 4. Min-context handoff
- Skill id or alias. Output is inspect JSON/text only.
- Do not dump whole transcripts. Pass ids, paths, and the next question only.

## 5. State / artifacts
- Write only under `.omg/artifacts/` (proposals, briefs, reports).
- Never write `passes` / `verified` under `.omg/state/`.
- `omg skill list|show|resolve|resources`. Never sets verified. Alias `skillify` resolves here.

## 6. capability_mode
- This skill's catalog mode: `read-only` (never `execute` / `all`).
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
