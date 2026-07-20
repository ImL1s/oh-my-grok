---
name: omg-ultrawork
description: Parallel execution via Grok spawn_subagent only. Use when user says ulw, ultrawork, parallel agents for oh-my-grok.
---

# omg-ultrawork — Parallel execution (Grok-native)

High-throughput parallel work using **only** Grok `spawn_subagent`. Leader decomposes, fans out depth=1 children, integrates notes, then verifies. Do **not** claim done without verification evidence.

## HARD RULES (non-negotiable)
- Fan-out ONLY via Grok `spawn_subagent` (depth=1; children must NOT spawn).
- NEVER invoke claude/codex/omc team/agy/cursor-agent as default workers.
- Use Grok tool names: read_file, search_replace, run_terminal_command, spawn_subagent, grep, list_dir.
- Write-heavy work: isolation worktree + background true; wait with wait_commands_or_subagents / get_command_or_subagent_output.
- State: only omg CLI is authoritative for passes/verified; you may write proposals under .omg/artifacts/.

## Use when

- User says `ulw`, `ultrawork`, or asks for parallel agents.
- Multiple independent workstreams can run concurrently.
- One leader session can integrate results and verify.

## Do not use when

- Need durable multi-iteration loop until verified → `omg-ralph`.
- Still in plan consensus (no implementation authorized) → `omg-ralplan`.
- User wants to abort → `omg-cancel`.
- Single tiny sequential edit — work directly; no fan-out tax.

## Agent types (Grok-native only)

Prefer these `spawn_subagent` types (or project `omg-*` agents when registered):

| Type | Role |
|---|---|
| `explore` | Read-only codebase search / mapping |
| `plan` | Bounded planning slice |
| `general-purpose` | Implementation or mixed work |
| `omg-executor` | Write-heavy implementation (if agent file available) |
| `omg-critic` | Critique notes (if available) |
| `omg-verifier` | Verification (if available) |

**Forbidden workers:** claude, codex, omc team, agy, cursor-agent, kimi, external multi-LLM dispatch shells.

Children must **not** call `spawn_subagent` again (depth=1 hard cap).

## Playbook

### 1. Decompose

- State goal in one sentence.
- Split into **independent** slices (file ownership / no shared-write conflicts).
- Define pass/fail acceptance criteria **before** spawning (command, artifact, or check).
- Shared-file or prerequisite-heavy slices stay on the leader or run staged.

### 2. Parallel spawn

- Emit **multiple** `spawn_subagent` calls in one turn for independent slices.
- Write-heavy slices: isolation worktree + `background: true`.
- **Capability defaults (HARD REQUIRE when host supports `capability_mode`):**
  - **Implementers / write workers** (`general-purpose`, `omg-executor`): **MUST** spawn with `capability_mode: read-write` — file edit tools OK; **no Execute/shell**. `omg-executor` also lists `run_terminal_command` / `spawn_subagent` in `disallowedTools`. Do not rely on PreToolUse alone (see `docs/research/subagent-pretooluse-spike.md` and `docs/security-model.md`).
  - **Explore / plan / critic / verifier**: **MUST** spawn with `capability_mode: read-only` (or permissionMode `plan`).
  - **If spawn DENIED** for capability_mode: **RETRY IMMEDIATELY** same turn with the required mode.
    Do **not** abandon multi-agent; do **not** solo-fallback after one deny.
  - **Shell / tests / acceptance**: execute via **`omg accept`** / frozen acceptance only (CLI stamps results + semantic command policy). Leader may run checks; children must not self-verify the run.
- Prompt each child with: goal slice, allowed paths, acceptance criteria, "do NOT spawn children", tool name list, **explicit capability_mode**.

### 3. Wait + collect

- Wait with `wait_commands_or_subagents` / `get_command_or_subagent_output`.
- Read summaries / notes under `.omg/artifacts/` if children wrote them.
- Do not dump entire raw child logs into leader context — integrate deltas.

### 3b. Ownership manifest (CLI)

Before sealing, record file ownership so join can fail closed:

```bash
omg worker own --run RUN --tasks-json '[{"task_id":"t1","owned_files":["a.py"]},{"task_id":"t2","owned_files":["b.py"]}]'
omg worker prepare-owned --run RUN
# workers edit worktrees → leader seals
omg worker seal --run RUN --task t1
omg worker seal --run RUN --task t2
omg worker join --run RUN   # blocks if missing/failed/untrusted writer
```

Two envelope files alone are **not** proof of two native workers; join requires
CLI ownership + CLI seals. Live spawn fingerprints are host-session evidence.

### 4. Integrate (result envelopes + CLI)

Write-heavy children must leave a **result envelope** before exit:

```text
.omg/artifacts/ulw-results/<run_id>/<task_id>.json
```

```json
{
  "task_id": "t1",
  "base_sha": "<leader HEAD at spawn>",
  "head_sha": "<worker commit to apply>",
  "worktree_path": "<absolute isolation worktree>",
  "changed_files": ["path/a.py"],
  "status": "ok",
  "evidence": "pytest -q path/tests …"
}
```

- `status` is `ok` or `failed`. Leader base is recorded by `omg ulw` as `base_sha` on the run.
- Prefer clean leader tree (no auto-stash). Apply with:

```bash
omg integrate              # active run
omg integrate --run <id>
omg integrate --dry-run    # validate envelopes only
```

CLI sorts by `task_id`, rejects `base_sha` mismatch, cherry-picks each `head_sha`, stops on conflict, writes `integrate.result.json`. Do **not** claim merge success from agent notes alone.

- Resolve residual conflicts on leader only; re-run greps/tests on integrated tree.
- Write human notes under `.omg/artifacts/` (proposal only).

### 5. Leader verification (required)

- Run the acceptance checks defined in step 1 (or `omg accept` when a PRD exists).
- No green evidence → **not done** (fix, re-spawn failed slice, or escalate).
- Convergence rule: **never claim complete without verification**.
- Do **not** set `passes` / `verified` yourself — report evidence; `omg` CLI owns authoritative state when a run is supervised.

## Tooling cheat sheet

```text
read_file / grep / list_dir          — context
spawn_subagent                       — fan-out (depth=1)
run_terminal_command (background)    — long builds/tests
wait_commands_or_subagents           — join
get_command_or_subagent_output       — poll/read results
search_replace                       — leader integration edits
```

## Launch via CLI (when available)

```bash
omg ulw "goal text"
```

CLI may inject this skill and track run state under `.omg/state/`. Inside the session, still follow HARD RULES.

## Anti-patterns

- Serializing independent work "to be safe".
- Nested spawn from children.
- Shelling out to claude/codex as workers.
- Declaring done because children "said" success without leader-run checks.
- Self-matching `pkill -f` — use `omg cancel` if aborting.
