---
name: omg-autopilot
description: >
  In-session end-to-end coordinator for oh-my-grok. Use when the user says
  autopilot, auto pilot, full auto, autonomous, build me, create me, make me,
  handle it all, or wants idea→working code with interview/plan/implement/review/QA/accept.
  CLI owns phase state and verified; this skill is the session playbook.
---

# omg-autopilot — In-session end-to-end coordinator

You are running **inside a Grok Build session**. Autopilot means **you** drive the
strict CLI phase machine and Grok-native workers until acceptance — not that the
host Stop-hook forces the chat to continue (it cannot).

**Authority split**

| Concern | Owner |
|---------|--------|
| Phase legality, stamps, `verified` | **`omg` CLI only** |
| Spec / plan / code proposals | Session + `spawn_subagent` |
| Outer “don’t stop” across many turns | Re-invoke this skill / user “continue” / optional `omg ralph` outer loop |

## HARD RULES (non-negotiable)

1. Fan-out **only** via Grok `spawn_subagent` (depth = 1; children must **not** spawn).
2. **Always** set `capability_mode` on spawn:
   - implementers (`omg-executor`, write `general-purpose`): **`read-write`** (no Execute)
   - critic / verifier / explore / plan: **`read-only`**
3. If spawn is **DENIED** for capability_mode: **RETRY IMMEDIATELY** same turn with the correct mode. Do **not** abandon multi-agent; do **not** solo-fallback after one deny.
4. **Never** invoke `claude` / `codex` / `omc team` / `agy` / `cursor-agent` as default workers.
5. Grok tool names: `read_file`, `search_replace`, `run_terminal_command`, `spawn_subagent`, `grep`, `list_dir`.
6. **Never** write `passes` / `verified` under `.omg/state/`. Only CLI after acceptance.
7. **No Stop hard-pin:** PreToolUse is fail-open soft-guard. Do not claim OMC-style “chat cannot end until done.”
8. Cancel with `omg cancel` — never self-matching `pkill -f`.

## Use when

- User says: `autopilot`, `auto pilot`, `full auto`, `autonomous`, `build me`, `create me`, `make me`, `handle it all`, `end to end`, `from idea to working code`.
- Multi-phase work: requirements → plan consensus → implement → review → QA → accept.
- User wants hands-off orchestration **inside this session** and will re-prompt “continue” if the turn ends.

## Do not use when

- Single tiny fix → work directly or `omg-ralph` one story.
- Plan-only / critique-only → `omg-ralplan`.
- Parallel burst only → `omg-ultrawork`.
- Abort → `omg-cancel`.
- User wants brainstorm without shipping → answer conversationally; do not start autopilot state.

## Persistence honesty (read this)

| Want | How on Grok / OMG |
|------|-------------------|
| Strict phases + destination gates | This skill + `omg autopilot *` |
| Outer retry until verified | `omg ralph "…"` **or** user re-invokes this skill after each turn |
| Host-forced continuation on Stop | **Not available** — see `docs/research/stop-continuation/` |

If the session ends mid-phase: run `omg autopilot status --run RUN` and resume the playbook from the current phase.

## CLI phase machine (normative)

```text
interview → ralplan → implement → review → (rework) → qa → acceptance → verified
```

Illegal transitions fail closed. Destination gates (CLI-enforced):

| Enter phase | Required evidence / stamp |
|-------------|---------------------------|
| `ralplan` from `interview` | `interview_complete: true` |
| `implement` | `consensus: true` |
| `qa` | CLI `stages/structured_review.json` clean |
| `acceptance` | CLI `stages/ultraqa.json` status clean |
| `verified` | **Only** `omg autopilot complete` after same-process accept — never `transition … verified` |

## Session playbook

### 0. Bootstrap

```bash
omg doctor          # fix FAILs first
omg setup           # if .omg/ missing
omg autopilot status --run RUN   # if resuming
```

If no run yet:

```bash
omg autopilot start "GOAL TEXT"
# skip interview only when requirements already closed:
# omg autopilot start "GOAL" --skip-interview
```

Record `run_id` from output. Prefer `run_terminal_command` for all `omg` invocations.

### 1. Phase `interview` (unless skip)

- Follow **omg-deep-interview** / CLI:
  - `omg interview start "…"` or continue with printed `resume_command`
  - `omg interview answer …` / `pressure-pass` / `close`
- When complete:

```bash
omg autopilot transition --run RUN --phase ralplan \
  --evidence-json '{"interview_complete":true}' \
  --reason "interview closed"
```

### 2. Phase `ralplan`

- Follow **omg-ralplan** playbook **without product code**:
  - draft plan under run `stages/` + `.omg/artifacts/`
  - `spawn_subagent` critic **read-only**
  - revise
  - `spawn_subagent` verifier **read-only** → stage artifact must contain whole-word **APPROVE**
- Prefer CLI when available: `omg ralplan "…"`; then transition with evidence.

```bash
omg autopilot transition --run RUN --phase implement \
  --evidence-json '{"consensus":true}' \
  --reason "ralplan APPROVE"
```

### 3. Phase `implement`

- Decompose into stories / independent slices.
- Prefer **omg-ultrawork** patterns for parallel slices; **omg-ralph** one-story discipline for sequential must-finish.
- Spawn implementers with `capability_mode=read-write`; worktrees for write-heavy work.
- Write notes under `.omg/artifacts/`. Do **not** claim verified.

```bash
omg autopilot transition --run RUN --phase review --reason "implementation ready for review"
```

### 4. Phase `review`

- Prefer **omg-dual-review** / native critic→verifier (read-only).
- Or CLI: `omg review --run RUN --diff-text "…" --code-reviewer-json '…' --architect-json '…'`
- On REQUEST CHANGES:

```bash
omg autopilot transition --run RUN --phase rework --reason "review findings"
# fix, then:
omg autopilot transition --run RUN --phase review --reason "rework done"
```

When CLI stamps review clean:

```bash
omg autopilot transition --run RUN --phase qa --reason "structured review clean"
```

### 5. Phase `qa`

- Follow **omg-ultraqa**:

```bash
omg qa freeze --run RUN --scenarios-json '[{"id":"t1","command":"python3 -m pytest -q -m not live"}]'
omg qa run --run RUN
omg qa status --run RUN
```

- QA clean **≠** verified.

```bash
omg autopilot transition --run RUN --phase acceptance --reason "ultraqa clean"
```

### 6. Phase `acceptance` → `verified`

```bash
omg accept --run RUN --yes
# same process / same shell turn chain when possible:
omg autopilot complete --run RUN
omg autopilot status --run RUN
```

Only then report success with evidence (commands + outputs).

### 7. Blocked / cancel

```bash
omg autopilot transition --run RUN --phase blocked --reason "…"
omg cancel
```

## Capability cheat sheet

| Role | `capability_mode` | Notes |
|------|-------------------|--------|
| Implementer | `read-write` | No shell/Execute |
| Critic / verifier / explore | `read-only` | No product edits |
| Shell / tests for verified | **CLI** `omg accept` / `omg qa` | Never child self-verify run |

## Anti-patterns

- Thin “done” prose without CLI stamps
- `transition --phase verified`
- Skipping interview/ralplan gates by lying in evidence-json
- Self-approve after implement (skip dual-review)
- Infinite self-loop without CLI status (burn tokens; prefer status + continue)
- External agent CLIs as workers
- Claiming Stop hooks keep the session alive

## Optional durable multi-story

When implement/QA spans **more than one story** that must survive process or run
boundaries (depends_on, checkpoints, cross-session resume), load **`omg-ultragoal`**
and drive `omg goal *` ledger in parallel with (or after) autopilot phases.
Grok has **no host `/goal`** — only the repo ledger under `.omg/ultragoal/`.
Still finish run acceptance via `omg accept` / `omg autopilot complete` before
`omg goal link-run` + `omg goal verify`.

## Related skills

- `omg-using` — router
- `omg-deep-interview`, `omg-ralplan`, `omg-ultrawork`, `omg-ralph`
- `omg-ultragoal` — durable multi-story goal ledger (no host `/goal`)
- `omg-dual-review`, `omg-ultraqa`, `omg-cancel`
- Security: `docs/security-model.md`

## CLI quick reference

```bash
omg autopilot start "goal"
omg autopilot start "goal" --skip-interview
omg autopilot transition --run RUN --phase PHASE --evidence-json '{…}' --reason "…"
omg autopilot status --run RUN
omg accept --run RUN --yes
omg autopilot complete --run RUN
omg cancel
```
