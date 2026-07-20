---
name: omg-ralplan
description: Plan consensus FSM for oh-my-grok (plan → critic → revise → verifier). Use when user says ralplan, plan consensus, or steelman the plan before coding.
---

# omg-ralplan — Plan consensus (no implementation)

Finite-state planning loop that produces a **consensus plan** before any code execution mode (ulw/ralph). Implementation is **out of scope** for this skill.

## HARD RULES (non-negotiable)
- Fan-out ONLY via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- Critic/verifier: **MUST** spawn with `capability_mode=read-only`. If spawn DENIED for capability_mode: **RETRY IMMEDIATELY** same turn — do not abandon multi-agent.
- NEVER invoke claude/codex/omc team/agy/cursor-agent as default workers.
- Use Grok tool names: read_file, search_replace, run_terminal_command, spawn_subagent, grep, list_dir.
- Write-heavy work: isolation worktree + background true; wait with wait_commands_or_subagents / get_command_or_subagent_output.
- State: only omg CLI is authoritative for passes/verified; you may write proposals under .omg/artifacts/.

## Use when

- User says `ralplan`, `plan consensus`, `critic the plan`, `steelman plan`.
- Multi-step or high-risk work needs agreement before coding.
- Need read-only critic/verifier passes on a written plan.

## Do not use when

- User already authorized implementation → `omg-ultrawork` or `omg-ralph`.
- User wants cancel → `omg-cancel`.
- Quick one-file change with clear approach — plan inline; skip full FSM.

## FSM

CLI-owned state machine (``omg_cli/ralplan.py``). Artifacts + transitions live
under ``.omg/state/runs/<id>/ralplan.json`` and ``stages/``.

### v1 (default new CLI run without schema_version)

```text
draft → critic → revise → verifier → (accept | revise)* → accepted | failed
max_rounds default 3
```

### strict-v2 (lifecycle kernel; identity-bound proposals)

When the run is schema_version=2 / lifecycle_version=2 (or an existing strict
run is resumed), the CLI uses a **planner → architect → critic** path with
structured stamps (`invocation_id`, `session_id`, `input_sha256`, stage
verdicts). Still never sets product `verified` — only plan consensus.

```text
planner → architect → critic → (revise loop)* → accepted | failed
```

| State | Actor | Writes? | Notes |
|---|---|---|---|
| **draft** | Leader (or `plan` / `omg-orchestrator`) | Yes — plan draft under run `stages/` + `.omg/artifacts/` | Goals, constraints, steps, risks, acceptance |
| **critic** | `spawn_subagent` **read-only** | Proposals/notes only | Attack assumptions, missing tests, scope holes |
| **revise** | Leader | Yes — update plan artifact | Address critic findings; no product code |
| **verifier** | `spawn_subagent` **read-only** | Notes only | Check plan is coherent, testable, scoped; no code |
| **accepted** | **omg CLI only** | `ralplan.json` status | Only if verifier artifact contains whole-word **APPROVE** |
| **failed** | **omg CLI only** | `ralplan.json` status | After max_rounds without APPROVE |

### Capability defaults

| Role | Prefer `capability_mode` | Notes |
|------|--------------------------|--------|
| **draft / revise** (leader) | `read-write` only for plan artifacts under run `stages/` + `.omg/artifacts/` | No product implementation |
| **critic / verifier** | **`read-only`** (or permissionMode `plan`) | Cannot edit the repo |
| **implementer agents** | **Do not spawn** in ralplan | Implementation is out of scope |
| **Shell / acceptance** | N/A in ralplan | Product tests run later via **`omg accept`** / ulw/ralph only |

When spawning critic or verifier, **MUST** set **capability_mode=read-only** (or equivalent) so they cannot edit the repo. They may only:

- `read_file`, `grep`, `list_dir`
- Return structured findings
- Optionally append critique notes under `.omg/artifacts/` if the host allows write to that path; prefer returning findings to the leader who writes

PreToolUse is a soft-gate and may not cover all subagent children — **read-only capability is the primary control** for critic/verifier. See `docs/research/subagent-pretooluse-spike.md`.

### No implementation

- Do **not** apply product code changes in ralplan.
- Do **not** run feature implementation agents as executors for app code.
- Exit ralplan with a consensus plan path; user/CLI then starts `omg ulw` / `omg ralph` for execution.

## Playbook

1. **Draft** — Write stage artifact (CLI path `stages/draft-01.md` or `.omg/artifacts/plan-draft.md`): problem, goals, non-goals, steps, risks, acceptance criteria.
2. **Critic fan-out** — `spawn_subagent` with read-only capability; prompt to find blind spots (security, migration, test theatre, contract mismatch). Depth=1; Grok-native types only (`explore`, `plan`, `omg-critic`, `general-purpose` in read-only).
3. **Revise** — Leader merges valid critique into the plan; restate acceptance checks.
4. **Verifier** — `spawn_subagent` read-only; pass/fail against: clarity, testability, scope, risk coverage. Emit explicit **APPROVE** | **REQUEST CHANGES** | **FAILED** into the verifier stage artifact.
5. **Loop or accept** — CLI reads verifier artifact: whole-word `APPROVE` (or JSON `"verdict":"APPROVE"`) → `accepted`. Else revise again until `max_rounds` → `failed`. Do not start coding here.

## Launch via CLI

```bash
omg ralplan "goal or problem statement"
omg ralplan "goal" --max-iter 3 --dry-run   # max_rounds=3; record FSM only
```

State file: `.omg/state/runs/<id>/ralplan.json`. Stage prompts/artifacts under `stages/`.

## Anti-patterns

- Implementing "just a small fix" during ralplan.
- Critic/verifier with write permissions editing source.
- Nested spawn from children.
- External claude/codex workers for critique (use Grok spawn only). Second
  opinion: human runs **`omg ask`** / skill `omg-ask` separately — never
  auto-shell advisors from ralplan.
- Marking plan "verified" in CLI state yourself — report readiness; CLI owns status.
