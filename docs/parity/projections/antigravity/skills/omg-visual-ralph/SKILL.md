---
name: omg-visual-ralph
description: OMG skill projection (omg_native, read-write)
omg_projection: true
omg_classification: omg_native
omg_capability_mode: read-write
omg_source_skill: skills/omg-visual-ralph/SKILL.md
---
# PROJECTION — not an installed Antigravity plugin

This file is a static parity projection of the Grok plugin skill
`skills/omg-visual-ralph/SKILL.md`. It is not an installed Antigravity plugin,
not live AG evidence, and does not mean `agy` skill discovery works.

- Catalog: `skills/catalog.json`
- capability_mode: `read-write` (never `execute`/`all`)
- Playbook without runtime evidence stays `configured`, not `verified`.

# omg-visual-ralph — Visual-Ralph (compare-only honesty)

Visual persistence is not shipped. Use when the user says visual-ralph. Route to visual compare plus ordinary ralph if they need a loop.

## HARD RULES (non-negotiable)
- Fan-out ONLY via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- Always set `capability_mode` on every spawn: `read-only` for explore/plan/critic/verifier; `read-write` for implementers (`general-purpose`, `omg-executor`). Never `execute` / `all`.
- If spawn is DENIED: RETRY IMMEDIATELY in the same turn with the required `capability_mode`. Do not abandon multi-agent work over one deny.
- NEVER invoke claude/codex/omc team/agy/cursor-agent as default workers.
- State: only the `omg` CLI may write `passes` / `verified` under `.omg/state/`. Agents write proposals under `.omg/artifacts/` only.
- Cancel with `omg cancel` (PID files) — never self-matching `pkill -f`.
- This playbook is **configured**, not live-verified. Never mark `verified`.


## 1. Activation
- Phrases: `visual ralph`, `visual-ralph`
- Aliases: `visual-ralph`
- **When:** User asks for visual-ralph / screenshot persistence loop.
- **Do not use when:** Claiming overlay/diff/reviewer agents or visual-Ralph runtime exist.
- Informational questions (`what is omg-visual-ralph?`) do **not** activate this skill.

## 2. Preconditions / conflict
- Catalog `conflict_policy`: `refuse`. Catalog `continuation`: `none`.
- If another continuation owner is active (`autopilot` / `ralph` / `pipeline` / `ulw` / `ultragoal` / `ultraqa` / `team` / `ralplan`), follow `resolve_continuation`: refuse, adopt_existing, or artifact_only.
- Cancel/using always adopt. This skill must not start a second loop.

## 3. Runtime owner
- omg-cli visual + optional ralph continuation (refuse a second owner).
- Bundled contract: `resources/contract.json` (capability_mode, continuation, conflict, artifacts, evidence_rule).

## 4. Min-context handoff
- If they still want a loop: ralph goal + compare JSON path.
- Do not dump whole transcripts. Pass ids, paths, and the next question only.

## 5. State / artifacts
- Apply the **visual-loop** capture/fix slice this playbook owns (`capability_mode: read-write`).
- Write reports/proposals under `.omg/artifacts/`.
- Never write `passes` / `verified` under `.omg/state/`.
- `omg visual compare` only (same compare-only envelope as visual-verdict). Persistence loop remains `omg ralph` if they need iterations — that loop is not visual-aware.

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
