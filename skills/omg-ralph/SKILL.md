---
name: omg-ralph
description: Persistence loop iteration for oh-my-grok. Use when user says ralph, don't stop, keep going until done, or durable verified completion.
---

# omg-ralph — Persistence loop (one iteration)

Ralph is a **persistence loop** until the goal is complete and verified. In oh-my-grok:

- The **outer loop is owned by the `omg` CLI** (retry / continue / state).
- This skill is **one iteration** inside that loop: refine PRD proposal → implement **ONE** story → **stop**.
- You **never** mark `verified` yourself. Outer CLI continues or exits based on acceptance.

## HARD RULES (non-negotiable)
- Fan-out ONLY via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- NEVER invoke claude/codex/omc team/agy/cursor-agent as default workers.
- Use Grok tool names: read_file, search_replace, run_terminal_command, spawn_subagent, grep, list_dir.
- Write-heavy work: isolation worktree + background true; wait with wait_commands_or_subagents / get_command_or_subagent_output.
- State: only omg CLI is authoritative for passes/verified; you may write proposals under .omg/artifacts/.

## Use when

- User says `ralph`, `don't stop`, `must complete`, `keep going until done`.
- Work needs durable multi-iteration completion with verification gate.
- CLI has (or will) open a supervised run under `.omg/state/`.

## Do not use when

- One-shot parallel burst only → `omg-ultrawork`.
- Plan not yet consensus → `omg-ralplan` first.
- User wants abort → `omg-cancel`.

## Iteration contract (this session = one pass)

```text
1. Load / refine PRD proposal under .omg/artifacts/  (proposal only)
2. Pick exactly ONE next story (smallest vertical slice that moves acceptance)
3. Implement that story (direct tools and/or depth=1 spawn_subagent)
4. Leave evidence notes under .omg/artifacts/
5. STOP — do not set verified; do not start story N+1 in the same iteration
```

Outer CLI re-invokes this skill (or a fresh Grok session) until acceptance passes.

## Steps

### 1. Context intake

- Read goal from user / `.omg/state/` active run / prior artifacts.
- Skim existing `.omg/artifacts/prd*.md` or `prd.json` proposals if present.
- Note constraints, unknowns, and last failure reason if retrying.

### 2. Refine PRD proposal

- Update or create a **proposal** under `.omg/artifacts/` (e.g. `prd-proposal.md`).
- Include: goal, stories backlog, current story, acceptance checks.
- This is **not** authoritative state — CLI may copy/merge later.

### 3. Implement ONE story only

- Scope strictly to the chosen story.
- Prefer isolation worktree for write-heavy work.
- Parallelize **within** the story only if slices are independent (`spawn_subagent` depth=1).
- Allowed agents: Grok-native `general-purpose`, `explore`, `plan`, or `omg-executor` / `omg-orchestrator` if registered.
- Forbidden: claude, codex, omc team, agy, cursor-agent as workers.

### 4. Evidence + stop

- Run story-level checks (tests, commands, manual QA notes).
- Write iteration notes: what changed, how to verify, remaining stories.
- **Stop.** Do not claim whole-goal done. Do not write `verified: true` into run state.

## Convergence (outer loop)

- CLI / acceptance runner decides pass vs continue.
- Architect/verifier may be invoked by CLI or a later iteration via `spawn_subagent` (read-only verifier when available).
- Completion promise is **only** valid when CLI marks verified after acceptance evidence.

## Launch via CLI

```bash
omg ralph "goal text"
```

## Anti-patterns

- Implementing multiple stories in one iteration "to go faster".
- Setting passes/verified in JSON under `.omg/state/` yourself.
- Infinite self-loop inside one session without stopping for CLI.
- Nested spawn or external agent CLIs.
- Using self-matching `pkill -f` — cancel with `omg cancel`.
