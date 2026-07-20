---
name: omg-ultragoal
description: >
  In-session durable multi-story goal ledger for oh-my-grok. Use when user says
  ultragoal, goal ledger, multi-story durable, durable goals, resume goal,
  omg goal, hash-chained stories, or needs multi-story work that survives process
  and run boundaries. CLI owns snapshot/ledger and verified; this skill is the
  session playbook (Grok has no host /goal).
---

# omg-ultragoal — In-session durable multi-story goal ledger

You are running **inside a Grok Build session**. Ultragoal means **you** drive the
`omg goal *` CLI ledger and Grok-native workers across multiple stories until the
goal is CLI-verified — not that the host Stop-hook forces the chat to continue.

## Honesty: no host `/goal` (OMC/OMX vs OMG)

| Platform | Durable goal surface |
|----------|----------------------|
| **OMC / OMX** | Host `/goal` or `get_goal` / `create_goal` + repo ledger under `.omc/ultragoal` |
| **Grok Build / OMG** | **No host `/goal` API** — **repo ledger only** under `.omg/ultragoal/` |

Session continuity:

1. Re-invoke this skill (or user says “continue” / “resume goal”).
2. Run `omg goal status --goal GOAL` and resume the next ready story.
3. Optional outer “don’t stop until verified” on a **linked run**: `omg ralph "…"`.

**R2 three pillars (research):** OMC uses Stop veto + boulder state. OMG cannot
Stop-block the chat. Use (a) **`omg resume`** smart routing, (b) SessionStart
**`.omg/state/RESUME.md`** inject, (c) louder pack in resume/hud output. For
goals specifically also re-invoke this skill + `omg goal status`. Do **not**
invent host `/goal` or claim Stop pins the session.

**Authority split**

| Concern | Owner |
|---------|--------|
| Snapshot, ledger, hash chain, `verified` on goal | **`omg` CLI only** |
| Story implementation / evidence files | Session + `spawn_subagent` |
| Linked run `verified` | CLI accept/complete on that run — required before `omg goal verify` |

## HARD RULES (non-negotiable)

1. Fan-out **only** via Grok `spawn_subagent` (depth = 1; children must **not** spawn).
2. **Always** set `capability_mode` on spawn:
   - implementers (`omg-executor`, write `general-purpose`): **`read-write`**
   - critic / verifier / explore / plan: **`read-only`**
3. If spawn is **DENIED** for capability_mode: **RETRY IMMEDIATELY** same turn with the correct mode. Do **not** abandon multi-agent; do **not** solo-fallback after one deny.
4. **Never** invoke `claude` / `codex` / `omc team` / `agy` / `cursor-agent` as default workers.
5. Grok tool names: `read_file`, `search_replace`, `run_terminal_command`, `spawn_subagent`, `grep`, `list_dir`.
6. **Never write the goal ledger by hand.** Do not edit `.omg/ultragoal/goals/*/snapshot.json` or `ledger.jsonl` from the agent. Only `omg goal *` is authoritative.
7. **Never set `verified`** on goals or runs from agent prose. Goal verify requires a **linked CLI-verified run**.
8. **No Stop hard-pin.** PreToolUse is fail-open soft-guard. Do not claim OMC-style “chat cannot end until done.”
9. Cancel with `omg cancel` — never self-matching `pkill -f`.

## Use when

- User says: `ultragoal`, `goal ledger`, `multi-story`, `durable goals`, `resume goal`, `omg goal`, multi-story work that must survive process/run boundaries.
- Several stories with `depends_on` / readiness, checkpoints with evidence hashes, or forensic repair of a goal ledger.
- After autopilot when **more than one durable story** needs a ledger across sessions.

## Do not use when

- Single tiny fix → work directly or `omg-ralph` one story.
- Full idea→accept phase machine without multi-story ledger → `omg-autopilot`.
- Plan-only → `omg-ralplan`.
- Parallel one-shot without durable ledger → `omg-ultrawork`.
- Abort → `omg-cancel`.
- User wants brainstorm only → answer conversationally; do not init a goal.

## Contract (ledger)

- Goals live under `.omg/ultragoal/goals/<goal_id>/` (`snapshot.json` + `ledger.jsonl`).
- Every ledger event is hash-chained: contiguous `sequence`, `prev_hash`, `event_hash`.
- Story readiness follows `depends_on`. Only **ready** stories may start.
- Checkpoints require a real **evidence file path** + SHA-256 (CLI computes).
- Goal `verified` is allowed only when a linked run is CLI-verified.
- Agent proposals become durable only via CLI events — direct snapshot/ledger edits are rejected.
- Corrupt final tail: `omg goal repair --dry-run` then `--yes` (byte-for-byte hash-named backup first). Mid-chain/hash corruption refuses automatic repair and sets a forensic blocker.

## Session playbook

### 0. Bootstrap

```bash
omg doctor
omg setup    # if .omg/ missing
omg goal status --goal GOAL   # if resuming
```

Prefer `run_terminal_command` for all `omg` invocations. Record `goal_id` (and any `run_id` you will link).

### 1. Init stories

```bash
omg goal init --goal GOAL --stories-json '[
  {"id":"s1","depends_on":[],"acceptance":"…"},
  {"id":"s2","depends_on":["s1"],"acceptance":"…"}
]'
omg goal status --goal GOAL
```

Stories must have stable `id`, `depends_on` (list), and `acceptance` text.

### 2. Per ready story loop

For each story that status shows as ready:

1. **Start**

```bash
omg goal start-story --goal GOAL --story STORY_ID
```

2. **Implement** via `spawn_subagent` (`capability_mode=read-write` for writers; worktree for heavy writes). Depth 1 only.

3. **Evidence file** — write a concrete path under `.omg/artifacts/` or the workspace (tests output, review note, patch summary). Path must exist for checkpoint.

4. **Checkpoint**

```bash
omg goal checkpoint --goal GOAL --story STORY_ID \
  --evidence PATH \
  --message "what landed and how to re-check"
```

5. **Complete** (when acceptance criteria for that story are met with evidence)

```bash
omg goal complete-story --goal GOAL --story STORY_ID
omg goal status --goal GOAL
```

6. **Blocked** (if stuck)

```bash
omg goal block-story --goal GOAL --story STORY_ID \
  --reason "…" --next-action "…"
# later:
omg goal resume-story --goal GOAL --story STORY_ID
```

### 3. Link run + verify goal

Before claiming the multi-story goal done:

```bash
# Run must already be CLI-verified (accept/complete path)
omg goal link-run --goal GOAL --run RUN
omg goal verify --goal GOAL
omg goal status --goal GOAL
```

Only after CLI `verify` succeeds report goal complete with command evidence.

### 4. Repair (forensic)

```bash
omg goal repair --goal GOAL --dry-run
omg goal repair --goal GOAL --yes   # only when dry-run says safe tail truncate
```

Do not hand-edit hash chains. Mid-chain corruption → restore backup / human forensic path.

### 5. Session ends mid-goal

1. User returns → load this skill.
2. `omg goal status --goal GOAL`
3. Continue from next ready story (start → implement → evidence → checkpoint → complete).
4. Do not re-init the same goal id unless status shows it is missing.

## Capability cheat sheet

| Role | `capability_mode` | Notes |
|------|-------------------|--------|
| Implementer | `read-write` | No Execute as worker default |
| Critic / verifier / explore | `read-only` | No product edits |
| Ledger / verify / repair | **CLI** `omg goal *` | Never child-writes ledger |
| Run verified | **CLI** accept/complete | Required before goal verify |

## Anti-patterns

- Hand-editing `snapshot.json` / `ledger.jsonl` “to fix” state
- Fake `verified` in prose or agent-written state files
- Checkpoint without a real evidence file
- Claiming host `/goal` exists on Grok
- Claiming Stop hooks keep the session alive
- Completing all stories then calling goal verified without **link-run** to a CLI-verified run
- Mid-chain truncate instead of `omg goal repair`
- External agent CLIs as workers
- Self-matching `pkill -f` instead of `omg cancel`

## Related skills

- `omg-using` — router (priority includes ultragoal for durable multi-story)
- `omg-autopilot` — phase machine; optional hook to this skill for multi-story ledger
- `omg-ralph` — outer don’t-stop on a run
- `omg-ultrawork` — parallel implement slices inside a story
- `omg-ralplan` / `omg-dual-review` — plan/review before heavy stories
- `omg-cancel` — abort
- Research pointer: `docs/research/omc-omx-mechanism-research-pointer.md`

## CLI quick reference

```bash
omg goal init --goal GOAL --stories-json '[{"id":"s1","depends_on":[],"acceptance":"..."}]'
omg goal status --goal GOAL
omg goal link-run --goal GOAL --run RUN
omg goal start-story --goal GOAL --story s1
omg goal checkpoint --goal GOAL --story s1 --evidence PATH --message "..."
omg goal block-story --goal GOAL --story s1 --reason "..." --next-action "..."
omg goal resume-story --goal GOAL --story s1
omg goal complete-story --goal GOAL --story s1
omg goal verify --goal GOAL
omg goal repair --goal GOAL --dry-run
omg goal repair --goal GOAL --yes
```
