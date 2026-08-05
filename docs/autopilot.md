# Autopilot usage (skill + CLI)

English | [简体中文](./autopilot.zh.md) | [繁體中文](./autopilot.zh-TW.md)

**Audience:** humans driving Grok Build + maintainers writing skills.  
**Plugin version:** matches [`plugin.json`](../plugin.json) (currently **0.7.4**).
**Skill source:** [`skills/omg-autopilot/SKILL.md`](../skills/omg-autopilot/SKILL.md)  
**All skills catalog:** [`skills.md`](./skills.md) · [zh](./skills.zh.md) · [zh-TW](./skills.zh-TW.md) · [docs index](./README.md)

---

## What autopilot is

| Piece | What it does |
|-------|----------------|
| **Skill `omg-autopilot`** | In-session playbook: clarify → plan → code → review → QA → accept |
| **CLI `omg autopilot *`** | Strict phase machine + destination gates; owns run state under `.omg/state/runs/<run_id>/` |
| **Workers** | Only Grok `spawn_subagent` (depth 1); implementers `capability_mode=read-write` (no shell) |

**Not OMC-identical:** Stop pin is real on grok **≥0.2.107** but **capped** (8 continuations/turn), fail-open, and skippable (Esc/Ctrl+C).  
**Persistence beyond one turn (primary hands-off path):**  
`omg autopilot run --resume RUN --unattended` (#40) — CLI outer loop re-launches Grok after host-turn stalls until `verified` / `blocked` / `cancelled` / interview / await.  
Also: `/loop`, outer `omg ralph "…"`.

### OMC feel → OMG equivalent

| OMC expectation | OMG equivalent | Notes |
|-----------------|----------------|-------|
| Stay in session until done (Stop block) | **Stop pin (primary)** | grok ≥0.2.107; cap 8/turn; fail-open |
| In-turn “keep going” without Stop | **`/goal` (secondary)** | Host-native; Active bypasses Stop gate |
| Cross-turn / headless / beyond cap | **`omg autopilot run --resume … --unattended` (tertiary, #40)** · `/loop` / `omg ralph` | Fresh turn; counter resets; no human `go` |
| Human pause (requirements unclear) | **`ask_user_question` + interview** | Gate yields; not mid-phase chat |
| Destructive / credential pause | **`omg autopilot await`** | Sets `autopilot_awaiting`; gate yields |
| Cancel sticky mode | **`omg cancel`** | Not “unblock Stop” |
| Verified done | **`omg accept` / `omg autopilot complete`** | CLI only |

**Runtime precedence:** When a host `/goal` is **Active**, it dominates continuation and the Stop gate is not consulted until the goal releases; the Stop pin then enforces remaining autopilot gates.

### Stop pin honesty

- **Works:** incomplete autopilot + `reason=end_turn` → hook may `decision:block` with a phase continuation prompt (same turn).
- **Cap:** after **8** continuations in one turn, the host ends the turn regardless.
- **Fail-open:** hook crash/timeout → turn may end (do not assume pin survived).
- **Skips:** Esc, Ctrl+C, refusal, max-turns — Stop not consulted.
- **Not used:** `TurnControl::ForceContinue` (stubbed in host; D17).
- **Beyond cap / hands-off:** `omg autopilot run --resume RUN --unattended` (primary, #40). Optional: `/loop 5m omg autopilot status --run RUN`.

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

You can drive phases without the skill (scripted ops / debugging).

**Hands-off outer driver (recommended for multi-turn goals, #40):**

```bash
omg doctor
# Start + drive until verified/blocked/await/interview pause (no human "go"):
omg autopilot run "ship feature X" --skip-interview --unattended
# Resume after a pause or crash:
omg autopilot run --resume "$RUN" --unattended
# Optional stall budget (default 32 re-launches with no phase advance):
omg autopilot run --resume "$RUN" --unattended --max-stall-relaunches 16
```

Stdout is machine JSON (`phase` / `pause` / `resume_command`); human hints go to stderr.

**Manual phase transitions (debugging):**

```bash
omg autopilot start "ship feature X"
# or requirements already closed:
omg autopilot start "ship feature X" --skip-interview

RUN=…   # from start JSON: run_id

# … drive the interview under this same run_id (attach — no separate mode=interview run):
omg interview start --attach-run "$RUN"    # task defaults to the autopilot goal
omg interview answer --run "$RUN" --question-id ... --text ...
omg interview close --run "$RUN"
# close JSON `resume_command` for attach mode is a single idempotent entry:
# `omg autopilot run --resume "$RUN"` (outer driver reads sidecar phase and
# dispatches; re-close migrates stale two-step resume commands on disk).

# … after interview closed (preferred: omg interview * writes CLI envelope):
omg autopilot transition --run "$RUN" --phase ralplan --reason "interview closed"
# break-glass only (audited):
# omg autopilot transition --run "$RUN" --phase ralplan \
#   --evidence-json '{"interview_complete":true,"break_glass":true}' \
#   --reason "interview closed (break-glass)"

# … after plan APPROVE (preferred: omg ralplan * writes accepted ralplan.json stamp):
omg autopilot transition --run "$RUN" --phase implement --reason "ralplan APPROVE"
# break-glass only (audited):
# omg autopilot transition --run "$RUN" --phase implement \
#   --evidence-json '{"consensus":true,"break_glass":true}' \
#   --reason "ralplan APPROVE (break-glass)"

# review requires work evidence (fp drift, CLI receipt, or break_glass no_change):
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
`cancelled` is terminal-only — use `omg cancel`; it is **not** reachable via
`omg autopilot transition --phase cancelled` (`MANUAL_TRANSITIONS` excludes it).

### `legal_next` vs `commit_only_next` / `terminal_action`

`omg autopilot status` exposes two different “what’s next?” contracts:

| Field | Meaning |
|-------|---------|
| **`legal_next`** | Phases `omg autopilot transition --phase …` may take **now** (manual edges only). Never includes `verified`. |
| **`commit_only_next`** | Phases reachable only via a commit-style terminal step (today: `verified` from `acceptance`). |
| **`terminal_action`** | Human hint when `commit_only_next` is non-empty (today: `omg autopilot complete`). |

Do **not** call `transition --phase verified` — it is illegal. At `acceptance`, run
`omg autopilot complete` (or `omg accept --yes` then complete).

### Stamp-first gates and `break_glass` audit

Destination-phase gates prefer **CLI-owned on-disk stamps** over caller-supplied
JSON booleans. Bare `evidence.interview_complete` / `evidence.consensus` /
inline `implementation_receipt` / `no_change_reason` are accepted only with
`evidence.break_glass=true`; each such bypass is recorded on autopilot history
as `gate_audit` (for example `break_glass:consensus`, `break_glass:no_change`).

| Enter phase | Preferred (stamp-first) | Break-glass (audited) |
|-------------|-------------------------|------------------------|
| `ralplan` from `interview` | CLI interview envelope: `interview.json` with `writer=omg-cli`, `status=complete`, and a matching CLI-stamped spec artifact (`spec_path` + content hash) | `interview_complete` + `break_glass` |
| `implement` | CLI `stages/ralplan.json` stamp with `writer=omg-cli`, `run_id` match, and `accepted=true` (not `status.ralplan_consensus` alone) | `consensus` + `break_glass` |
| `review` from `implement` **or** `blocked` | Workspace fingerprint drift (curated product surfaces) since implement entry, **or** on-disk CLI implementation receipt (`stages/implementation.json`, fingerprint-rechecked) | `no_change_reason` + `break_glass`, **or** inline `implementation_receipt` + `break_glass` |
| `qa` | CLI `stages/structured_review.json` clean (see fingerprint recheck below) | — |
| `acceptance` | CLI `stages/ultraqa.json` status `clean` (see fingerprint recheck below) | — |
| `verified` | **Only** `omg autopilot complete` after same-process accept | — |

Break-glass is an operator-intent escape hatch, not a silent trust path. See
[`security-model.md`](./security-model.md#autopilot-break_glass-vs-cli-stamps).

### Blocked recovery re-applies destination gates (Round 2)

`blocked` is a pause, not a bypass. Transitions **from** `blocked` to
`implement`, `ralplan`, `review`, or `qa` run the same destination gates as
the linear path — gates key off **target phase**, not only `src`:

| Edge | Gate (same as linear path) |
|------|------------------------------|
| `blocked → implement` | `_consensus_ready` (CLI `ralplan.json` `accepted=true`) or audited `consensus` + `break_glass` |
| `blocked → ralplan` | `_interview_complete` (CLI interview envelope) when recovering from `interview`, or replan from later phases |
| `blocked → review` | Implement→review work evidence (fingerprint drift, CLI receipt, or audited `no_change`) |
| `blocked → qa` | `stage_review_is_clean` (fresh structured review stamp + workspace binding) |

Example: recovering `blocked → review` without product work still needs audited
break-glass (same as `implement → review`):

```bash
omg autopilot transition --run "$RUN" --phase review \
  --evidence-json '{"no_change_reason":"resume without new diff","break_glass":true}' \
  --reason "blocked recovery"
```

Re-entering `review` from `blocked` (or `rework` / `implement`) invalidates
prior review/QA stamps so a pre-block clean stamp cannot reopen `qa`.

Re-entering `ralplan` when `ralplan_epoch ≥ 1` invalidates any CLI-owned
`ralplan.json` stamp and review/QA stamps — gating is by epoch counter, not
stamp existence. This closes detours such as `review→blocked→interview→ralplan`
and break-glass consensus paths with no stamp yet. Only the first
`interview→ralplan` handoff (epoch 0→1) is a no-op; `--skip-interview` starts
at epoch 1 so the next ralplan entry always invalidates.

**Pre-R7 epoch migration (Round 9):** sidecars that predate the
`ralplan_epoch` field no longer default missing values to `0`. Only a run
still at `phase==interview` with no CLI `ralplan.json` stamp and
`cycles.ralplan==0` migrates to epoch `0`; every other missing-epoch run
migrates to at least `1` so the next ralplan entry is treated as re-entry,
not the harmless first handoff. Present values must be plain `int >= 0`
(bool/float/negative rejected).

A fresh accept write clears
`invalidated` on the stamp; a new strict-v2 consensus attempt also clears
stale invalidation at cycle start. A fresh strict-v2 attempt also resets
`history`, per-session `attempts`, and `round` so prior rounds past the
configured ceiling do not pin every future replan into an immediate block;
each role's `session_id` is re-minted (not reused) on that reset.

`omg ralplan * --run RUN` embedded in an autopilot run is fail-closed:
the run must use strict-v2 schema, the autopilot FSM must be at
`phase==ralplan`, the CLI goal must match the frozen run goal exactly, and
the run must be non-terminal with no pending cancellation request. Strict-v2
embedding re-checks those gates immediately before writing `accepted=true`
so a concurrent `omg cancel` cannot race acceptance.
`_consensus_ready` additionally requires a non-empty stamp `goal` matching
the frozen run goal (missing/null/empty → rejected).

`omg interview start --attach-run RUN` re-verifies mode/phase/non-terminal/
goal match **under the execution lease** after pre-lease attach checks,
closing a TOCTOU race before writing `interview.json`. Attach start and
`close_interview` also refuse sidecar writes when the run is terminal or has
a pending cancellation request (same gate, immediately before each save).

**Terminal/cancel gates on transition/resume (Round 9):** `transition()`
re-checks `status.json` under the execution lease and refuses any sidecar
write when the run is terminal (`cancelled` / `completed` / `failed` /
`verified`) or has a pending cancellation request — the sidecar `phase` alone
must never unlock a transition after cancel. `omg autopilot run --resume`
prefers terminal `status.json` over a stale non-terminal sidecar phase and
refuses to launch Grok on a cancelled run. `omg autopilot status` returns
empty `legal_next` for terminal runs.

Attach-mode `omg interview close` sets `resume_command` to
`omg autopilot run --resume RUN` (idempotent; re-close refreshes stale
two-step commands on disk).

### Implement → review work gate

Leaving `implement` (or recovering `blocked → review`) requires evidence that
work happened (or an explicit audited no-op):

1. **Workspace fingerprint drift** — on entering `implement`, the CLI records
   `implement_workspace_fp` via a **dedicated**
   `autopilot._implement_workspace_fingerprint` helper (curated product
   surfaces: `omg_cli/` in full, `plugin.json`, `hooks/`, `skills/`,
   `agents/`, `templates/`, `scripts/`, `bin/`). A later transition to
   `review` passes if the
   current fingerprint differs. This is a separate helper from
   `qa.product_hash` (which only hashes `omg_cli/**/*.py` and also backs
   UltraQA's acceptance repair-cycle semantics) — implementation work
   confined to non-Python product surfaces would otherwise be invisible to
   this gate without ever changing QA's own hash semantics. Generated
   caches (`__pycache__/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/`,
   `.mypy_cache/`, `*.egg-info/` — mirroring this repo's `.gitignore`) are
   excluded, so merely running tests/importing a module during `implement`
   never counts as product work on its own.
2. **On-disk CLI stamp** — `omg_cli.implementation.stamp_implementation_receipt`
   writes a real, CLI-owned `stages/implementation.json`
   (`writer=omg-cli`, `content_sha256`, `stamped_at`) under
   `.omg/state/runs/<run_id>/stages/`. `read_implementation_receipt` verifies
   `writer`/`run_id` before trusting it, and the gate additionally rechecks
   `content_sha256` against a fresh `_implement_workspace_fingerprint(root)`
   recompute — a stale/tampered receipt does not satisfy the gate. Accepted
   **without** `break_glass` (audited as `cli_receipt:implementation.json`).
   No CLI subcommand calls the stamper yet; it exists for direct/test use
   until a phase writer wires it in. The receipt is bound to its implement
   cycle: `implementation.invalidate_implementation_receipt` marks any
   leftover receipt `invalidated=true` on every (re)entry into `implement`,
   so a receipt from a prior cycle can never satisfy the gate for a later
   cycle even if the fingerprint still happens to match (e.g.
   `review → ralplan → implement` with no new work).
3. **Break-glass no-change** — `evidence.no_change_reason` + `break_glass=true`
   (audited as `break_glass:no_change`).
4. **Break-glass inline receipt** — `evidence.implementation_receipt` with
   `writer=omg-cli` **and** `break_glass=true` (inline JSON is trivially
   forgeable; audited as `break_glass:implementation_receipt`).

The outer `omg autopilot run --unattended` driver records `gate_failure` on
status when auto-advance stalls here instead of silently skipping review.

Example (dry-run / scaffold only):

```bash
omg autopilot transition --run "$RUN" --phase review \
  --evidence-json '{"no_change_reason":"dry-run scaffold only","break_glass":true}' \
  --reason "no product diff"
```

### Review / QA fingerprint recheck (honesty limits)

When advancing to `qa` or `acceptance`, the CLI re-validates stage stamps against
the **current** workspace — a prior `clean=true` alone is not enough:

| Stage file | Recheck | Legacy behavior |
|------------|---------|-----------------|
| `structured_review.json` | Top-level `diff_hash` must match nested `code_reviewer_stamp` / `architect_stamp` lane evaluations (same logic as `omg review`). **`workspace_fp`** (recorded by `omg review` at stamp time via `_implement_workspace_fingerprint`) must match a fresh recompute — catches drift on non-Python product surfaces (`hooks/`, `skills/`, …) that `diff_hash` alone might miss. Missing `workspace_fp` on schema_version≥2 stamps → fail-closed. | Stamps with **no** `diff_hash`, **no** lane stamps, and **no** `workspace_fp`: clean flag only (weaker). |
| `ultraqa.json` | Last clean cycle’s `product_hash` must match a fresh `product_hash(root)` recompute. **`implement_workspace_fp`** on the clean cycle (same broader fingerprint as the implement→review gate) must also match — closes the gap where `product_hash` only covers `omg_cli/**/*.py` but config/plugin surfaces changed after QA went clean. | Cycles without `product_hash` / `implement_workspace_fp`: clean flag only (weaker). |

**Limits (Round 2 honesty):** rechecks bind stamps to workspace fingerprints and
lane/hash consistency, but are still not a full cross-file transaction WAL.
Concurrent partial writes, TOCTOU outside the stamp fields, or adversarial
workspace races may still require operator `omg doctor` / manual recovery. Full
StageEvidenceEnvelope v3 + cross-file WAL is planned, not claimed here.

Re-entering `implement` invalidates prior review/QA stamps (`invalidated=true`) so
a `qa→blocked→implement→…→qa` loop cannot reuse stale quality gates.

**QA clean ≠ verified.** UltraQA never sets `verified`.

---

## Skill playbook (what the agent should do)

Normative copy for agents is the skill file; this is the human-readable map.

| Phase | Skill / tools | CLI |
|-------|---------------|-----|
| Bootstrap | — | `omg doctor`, `omg setup`, `omg autopilot status` |
| interview | `omg-deep-interview` | `omg interview *` → transition `ralplan` |
| ralplan | `omg-ralplan` + critic/verifier **read-only** | `omg ralplan *` → accepted stamp → transition `implement` |
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
| `omg-ultragoal` | Multi-story ledger (`omg goal *`; host `/goal` session pressure) |
| `omg-cancel` | Abort |
| `omg-pipeline` | Alternate scripted FSM (not the same as autopilot v2) |

Agents (plugin): `omg-orchestrator`, `omg-executor`, `omg-critic`, `omg-verifier`, `omg-code-reviewer`, `omg-architect`, `omg-qa-tester`, `omg-analyst`.

---

## Anti-patterns

- Claiming “done” without CLI stamps / `omg autopilot status` showing `verified`
- `transition --phase verified` (illegal)
- Lying in `--evidence-json` to skip interview/ralplan (bare booleans without `break_glass`, or trusting `status.ralplan_consensus` without `ralplan.json`)
- Self-approve after implement (skip dual-review / structured review)
- Infinite skill self-loop without status (prefer status + user “continue”)
- External agent CLIs as workers
- Claiming the Stop pin is infinite or works on grok <0.2.107
- Transitioning `implement → review` without workspace change, CLI receipt, or audited `break_glass` no_change

---

## Resume bundle (`omg resume`)

`omg resume [--run RUN]` includes a partial **`resume_bundle`** object
(`schema_version=1`) for cross-turn continuity:

| Key | Contents |
|-----|----------|
| `run_view` | Existing resume view (mode, status, goal, commands, …) |
| `autopilot_phase` / `legal_next` | Best-effort from `omg autopilot status` when `mode=autopilot` |
| `gate_failure` | Last advance-gate stall payload (from status or autopilot status) |
| `provenance` | `{generated_at, run_id, selector}` |

**Round 1 scope:** schema v1 is a skeleton — it does **not** yet embed wiki,
project memory, compaction checkpoints, or full StageEvidence envelopes. Missing
autopilot sidecars must not break the generic pack (best-effort only). Fuller
ResumeBundle fields are planned for a later hardening round.

---

## State layout

```text
.omg/state/runs/<run_id>/
  status.json              # verified, autopilot_phase, autopilot_gate_failure, …
  stages/autopilot.json    # phase, history, history[].gate_audit, implement_workspace_fp, …
  stages/implementation.json  # optional; CLI-stamped implementation receipt (writer, content_sha256)
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
omg autopilot await --run RUN --set   # pause for destructive/credential confirm
omg accept --run RUN --yes
omg autopilot complete --run RUN
omg resume [--run RUN]
omg cancel
```
