# Autopilot usage (skill + CLI)

English | [繁體中文](./autopilot.zh-Hant.md)

**Audience:** humans driving Grok Build + maintainers writing skills.  
**Plugin version:** matches [`plugin.json`](../plugin.json) (currently **0.6.0**).
**Skill source:** [`skills/omg-autopilot/SKILL.md`](../skills/omg-autopilot/SKILL.md)  
**All skills catalog:** [`skills.md`](./skills.md) · [zh-Hant](./skills.zh-Hant.md) · [docs index](./README.md)

---

## What autopilot is

| Piece | What it does |
|-------|----------------|
| **Skill `omg-autopilot`** | In-session playbook: clarify → plan → code → review → QA → accept |
| **CLI `omg autopilot *`** | Strict phase machine + destination gates; owns run state under `.omg/state/runs/<run_id>/` |
| **Workers** | Only Grok `spawn_subagent` (depth 1); implementers `capability_mode=read-write` (no shell) |

**Not available on Grok:** OMC-style Stop `decision:block` (chat cannot be force-pinned open).  
**Persistence:** re-invoke the skill / say “continue”, or outer `omg ralph "…"`.

---

## When to use

**Use autopilot when:**

- Multi-phase: requirements → plan → implement → review → QA → verified
- You say *autopilot*, *full auto*, *build me*, *handle it all*, *end to end*
- You want one coordinator skill instead of wiring every CLI step yourself

**Prefer something else when:**

| Situation | Prefer |
|-----------|--------|
| One tiny fix | Direct edit or `omg-ralph` one story |
| Plan only | `omg-ralplan` / skill `omg-ralplan` |
| Parallel burst only | `omg-ultrawork` / `omg ulw` |
| Abort | `omg-cancel` / `omg cancel` |
| Brainstorm only | Chat; do not start an autopilot run |

---

## How to start (user)

### A. Inside Grok Build (recommended)

1. Open a project where `omg setup` has been run (`omg doctor` hard checks OK).
2. Invoke the skill:
   - Natural language: `autopilot 完成 …` / `full auto: …`
   - Or skill id: `/oh-my-grok:omg-autopilot` + goal text
3. Let the agent run CLI + workers. When the turn ends mid-run:
   - Say **continue** / **繼續**
   - Or: `omg autopilot status --run <RUN>` and re-invoke the skill with that run

### B. Terminal-only CLI

You can drive phases without the skill (scripted ops / debugging):

```bash
omg doctor
omg autopilot start "ship feature X"
# or requirements already closed:
omg autopilot start "ship feature X" --skip-interview

RUN=…   # from start JSON: run_id

# … after interview closed:
omg autopilot transition --run "$RUN" --phase ralplan \
  --evidence-json '{"interview_complete":true}' --reason "interview closed"

# … after plan APPROVE:
omg autopilot transition --run "$RUN" --phase implement \
  --evidence-json '{"consensus":true}' --reason "ralplan APPROVE"

omg autopilot transition --run "$RUN" --phase review --reason "impl ready"
# stamp review via omg review …
omg autopilot transition --run "$RUN" --phase qa --reason "review clean"
# omg qa freeze / run …
omg autopilot transition --run "$RUN" --phase acceptance --reason "ultraqa clean"
omg autopilot complete --run "$RUN"
omg autopilot status --run "$RUN"   # phase=verified, autopilot_phase=verified
```

Illegal transitions fail closed (CLI prints error, phase unchanged).

---

## Phase machine

```text
interview → ralplan → implement → review → (rework) → qa → acceptance → verified
```

Also: `blocked`, `cancelled` (see `omg_cli/autopilot.py` `LEGAL_TRANSITIONS`).

| Enter phase | Required evidence / stamp |
|-------------|---------------------------|
| `ralplan` from `interview` | `interview_complete: true` |
| `implement` | `consensus: true` |
| `qa` | CLI `stages/structured_review.json` clean |
| `acceptance` | CLI `stages/ultraqa.json` status `clean` |
| `verified` | **Only** `omg autopilot complete` after same-process accept — never `transition … verified` |

**QA clean ≠ verified.** UltraQA never sets `verified`.

---

## Skill playbook (what the agent should do)

Normative copy for agents is the skill file; this is the human-readable map.

| Phase | Skill / tools | CLI |
|-------|---------------|-----|
| Bootstrap | — | `omg doctor`, `omg setup`, `omg autopilot status` |
| interview | `omg-deep-interview` | `omg interview *` → transition `ralplan` |
| ralplan | `omg-ralplan` + critic/verifier **read-only** | transition `implement` + consensus evidence |
| implement | `omg-ultrawork` / `omg-ralph` + executor **read-write** | transition `review` |
| review | `omg-dual-review` or `omg review` | clean → transition `qa`; else `rework` |
| qa | `omg-ultraqa` | freeze (allowlisted cmds) → run → clean → transition `acceptance` |
| acceptance | — | `omg autopilot complete` (preferred) or `omg accept` then complete |
| cancel | `omg-cancel` | `omg cancel` |

### Spawn rules (HARD)

1. Fan-out **only** via Grok `spawn_subagent` (depth = 1).
2. Always set `capability_mode`: implementers `read-write`; critic/verifier/explore `read-only`.
3. If spawn denied for missing mode → **retry immediately** with mode set.
4. Never default workers to `claude` / `codex` / `omc team` / `agy` / `cursor-agent`.
5. Never write `passes` / `verified` under `.omg/state/` — CLI only.

### UltraQA freeze examples (v0.3.2+)

```bash
# Quote marker expressions. Freeze rejects grep / test / omg / python -c with tips.
omg qa freeze --run "$RUN" --scenarios-json \
  '[{"id":"unit","command":"python3 -m pytest -q -m '"'"'not live'"'"'"}]'
omg qa run --run "$RUN"
```

After clean UltraQA, **`prd.json` is optional** — accept/complete materialize from scenarios (do not overwrite an existing operator PRD).

### Complete / short-circuit (v0.3.2+)

```bash
# Preferred terminal step (same-process freeze_and_run + set_verified):
omg autopilot complete --run "$RUN"

# If you already ran omg accept --yes successfully, complete only syncs
# autopilot phase (no second full test suite).
omg autopilot status --run "$RUN"
# expect: phase=verified, run_status=verified, autopilot_phase=verified
```

---

## Repository workflows are a separate layer

Use `omg workflow install|list|show|plan|run` when the team wants a reviewed,
versioned stage graph with deterministic task IDs, explicit permissions, and
independent verifier/skeptic receipts. Autopilot may execute such a plan through
Grok-native `spawn_subagent`, but it must not rewrite the workflow contract or
invent receipts. A workflow `ship` result also does not replace `omg accept` or
the release state machine. See [workflows.md](./workflows.md).

Grok `/create-workflow` and Rhai projection remain `optional_unclaimed`; do not
market help text or a local `.rhai` file as a verified native integration.

## Related skills

| Skill | Role |
|-------|------|
| `omg-using` | Router / which mode |
| `omg-deep-interview` | Requirements gate |
| `omg-ralplan` | Plan consensus |
| `omg-ultrawork` | Parallel implement |
| `omg-ralph` | Persist one story |
| `omg-dual-review` | Critic → verifier |
| `omg-ultraqa` | QA loop |
| `omg-ultragoal` | Multi-story ledger (`omg goal *`; no host `/goal`) |
| `omg-cancel` | Abort |
| `omg-pipeline` | Alternate scripted FSM (not the same as autopilot v2) |

Agents (plugin): `omg-orchestrator`, `omg-executor`, `omg-critic`, `omg-verifier`, `omg-code-reviewer`, `omg-architect`, `omg-qa-tester`, `omg-analyst`.

---

## Anti-patterns

- Claiming “done” without CLI stamps / `omg autopilot status` showing `verified`
- `transition --phase verified` (illegal)
- Lying in `--evidence-json` to skip interview/ralplan
- Self-approve after implement (skip dual-review / structured review)
- Infinite skill self-loop without status (prefer status + user “continue”)
- External agent CLIs as workers
- Claiming Stop hooks keep the session alive
- Freezing UltraQA with `grep` / `python -c` / `omg doctor` as argv0 (use project `.py` / pytest)

---

## State layout

```text
.omg/state/runs/<run_id>/
  status.json              # verified, autopilot_phase, …
  stages/autopilot.json    # phase, history, goal
  stages/structured_review.json
  stages/ultraqa.json
  prd.json                 # optional; may be materialized from ultraqa
  acceptance.*             # freeze + result after accept/complete
```

---

## Security

Primary isolation: `capability_mode` + agent disallowed tools.  
Acceptance / QA: `omg_cli.command_policy` (operator intent gate, not an OS sandbox).  
Details: [`security-model.md`](./security-model.md).

---

## Quick reference

```bash
omg autopilot start "goal"
omg autopilot start "goal" --skip-interview
omg autopilot transition --run RUN --phase PHASE --evidence-json '{…}' --reason "…"
omg autopilot status --run RUN
omg accept --run RUN --yes
omg autopilot complete --run RUN
omg cancel
```
