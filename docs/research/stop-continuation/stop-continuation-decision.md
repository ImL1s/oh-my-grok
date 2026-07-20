# Decision Record — In-session Stop continuation like OMC?

**Date:** 2026-07-20  
**Status:** **DECIDED — DO NOT BUILD** (0.3.x; revisit only if Grok host adds blocking Stop)  
**Product baseline:** oh-my-grok **Option B** (plugin + `omg` CLI; Grok-native `spawn_subagent`; outer loops CLI-owned)  
**Audience:** solo maintainer · private repo · generous Grok quota  
**Related:**  
- `docs/research/autopilot-0.3/autopilot-architect-options.md` (Option B recommendation)  
- `hooks/bin/stop.py` (record-only Stop today)  
- grok-build `xai-grok-hooks` `HookEventName::is_blocking` (host truth)

---

## 1. Context

### What OMC does (Claude Code)

OMC (oh-my-claudecode) delivers “don’t stop until done” **inside the same session** via **Stop hooks** that return Claude’s blocking payload:

```json
{ "decision": "block", "reason": "<continuation prompt / checklist>" }
```

Key behaviors (from OMC `persistent-mode.mjs`, ralph-loop Stop hook, context-guard):

| Behavior | Purpose |
|----------|---------|
| Block stop while mode state active (`ralph`, `ultrawork`, `autopilot`, …) | Model cannot “politely finish” mid-goal |
| Inject reason text as next-turn guidance | Same session keeps iterating without operator re-prompt |
| Max-iteration / safety timeout / `stop_hook_active` re-entrancy guards | Avoid infinite block loops and host override |
| Never block context-limit / user abort / auth failures | Avoid deadlock with compaction & cancel |

That is the **OMC autopilot feel**: persistence is a **host-honored Stop veto**, not an outer process loop.

### What oh-my-grok does today (Option B)

| Layer | Behavior |
|-------|----------|
| **Stop hook** (`hooks/bin/stop.py`) | Spool `event: Stop` only. **Never** marks `verified`. **Never** blocks. Fail-open on error. |
| **Ralph** | Skill = **one story then STOP**. Outer loop = `omg_cli.modes.run_mode` `for i in range(1, max_iter+1)` launching fresh `grok` turns. |
| **Pipeline** | CLI FSM: `plan → implement → integrate → dual_review → accept → report` (`omg_cli/pipeline.py`). |
| **Verified** | **Only** `omg` CLI after acceptance policy — agents must not self-stamp. |

Product intent is explicit in skills (`omg-ralph`, `omg-pipeline`): *prefer CLI FSM over inventing parallel autopilot*; *infinite self-loop inside one session* is an anti-pattern.

### Host constraint (decisive)

Grok Build hooks (`crates/codegen/xai-grok-hooks/src/event.rs`):

```text
HookEventName::is_blocking() → true ONLY for PreToolUse
Stop is lifecycle / non-blocking (hub maps Stop with SessionStart/PostToolUse/…)
```

Implication:

- Emitting Claude-style `{ "decision": "block", "reason": "…" }` from a Grok **Stop** hook **does not** re-open the turn or force continuation.
- Stop on Grok is **observe / side-effect**, same family as SessionStart spool — not a continuation engine.
- Building OMC-parity Stop continuation on Grok **today** is either **no-op** (honest fail) or **false product claim** (dishonest).

Even with generous quota, solo maintainer capacity is better spent on Option B P0/P1 (spawn fail-closed + ULW product path) than on a host-impossible feature.

---

## 2. Options

### Option 1 — BUILD: OMC-style in-session Stop continuation

Implement a “persistent mode” Stop hook that:

- Reads `.omg/state` active run / mode flags  
- If incomplete and under max iterations → emit block + continuation reason  
- Respect cancel / verified / max-iter exits  

| Pros | Cons |
|------|------|
| Matches OMC UX language users already know | **Host cannot honor block on Stop** → dead code or lying docs |
| Single long TUI session feels “sticky” | Fights Option B invariant: outer loop + verified owned by CLI |
| | Re-entrancy / context-limit / abort edge cases OMC spent years hardening |
| | Blurs “one ralph iteration = one process turn” skill contract |
| | High maintainer tax for zero or fake effect |

### Option 2 — NOT BUILD: keep CLI-owned outer loops (status quo + docs)

Keep Stop as spool-only. Persistence via:

- `omg ralph` / `omg pipeline` / `omg ulw` process-level loops  
- Skills that **stop after one unit of work** so CLI can accept / reseal / re-launch  

| Pros | Cons |
|------|------|
| Aligns with **actual Grok host semantics** | Users who expect OMC “never leaves the chat” need a clear mental model shift |
| Preserves single-writer `verified` / acceptance | Multiple short `grok -p` launches (quota cost acceptable given generous quota) |
| Matches Option B architecture already shipping | |
| Zero new failure modes (deadlock block, fake continue) | |
| Leaves room to revisit **if** Grok adds blocking Stop later | |

### Option 3 — Hybrid / half-measure (REJECTED)

e.g. Stop hook prints advisory to stderr / event log “please continue” without host block; or skill-only “don’t stop” without CLI loop.

| Why rejected |
|--------------|
| Looks like OMC, behaves like a comment — worse than honest NOT BUILD |
| Encourages models to ignore “stop after one story” and forge multi-story sessions without acceptance gates |

---

## 3. Decision

### **DO NOT BUILD in-session Stop continuation like OMC for oh-my-grok 0.3.x.**

**Pick:** **Option 2 (NOT BUILD).**

**Why (in order):**

1. **Host impossibility** — Grok Stop is non-blocking; OMC’s mechanism does not transfer. Product must not ship features the host cannot execute.  
2. **Architecture fit** — Option B already chose **CLI outer loops** as the persistence spine; Stop block would dual-own control flow and risk `verified` forgery / skipped accept.  
3. **Solo ROI** — Effort belongs to Option B P0 spawn fail-closed + P1 ULW product path, not a second persistence stack.  
4. **Quota is not a reason to copy OMC’s mechanism** — spending tokens on multi-launch CLI loops is fine; spending engineering on non-functional hooks is not.

**Revisit trigger (only):**

- Grok documents / implements **blocking Stop** (or equivalent “inject user message + continue turn”) **and**  
- Live canary proves host honors deny/block with a continuation prompt **and**  
- Product still wants single-session stickiness after CLI loops feel insufficient.

Until then, treat “Stop continuation” as **out of scope**, not deferred half-work.

---

## 4. Consequences

### Positive

- **Honest marketing:** no claim of OMC-identical autopilot sticky sessions on Grok.  
- **Clear ownership:** process exit = one unit of work; `omg` decides retry vs accept vs fail.  
- **Stop hook stays simple:** event spool; never sets `verified` (current contract preserved).  
- **Security model stays legible:** hooks remain fail-open defense-in-depth; hard paths stay capability_mode + CLI policy.

### Negative / residual

- Operators coming from OMC will feel “it stopped early” if they only invoke the **skill in an interactive Grok chat** without `omg ralph` / `omg pipeline`.  
- Long multi-story durability is **N process launches**, not one infinite session (acceptable under generous quota; slightly worse for pure TUI chat UX).  
- If Grok later adds blocking Stop, this ADR must be reopened deliberately — not sneak-implemented.

### Explicit non-actions

- Do **not** expand `hooks/bin/stop.py` to emit `decision: block`.  
- Do **not** port OMC `persistent-mode.mjs` logic.  
- Do **not** teach skills to self-loop forever “like OMC” inside one chat without CLI.

---

## 5. If BUILD — smallest vertical slice (N/A)

**Not applicable.** Decision is NOT BUILD.

*(If revisit trigger fires later, max-5 slice would be: (1) host canary for blocking Stop, (2) mode-state reader, (3) block only when active run incomplete + under max_iter, (4) never block cancel/verified/context-limit, (5) doctor + live gate. Do not start these now.)*

---

## 6. If NOT BUILD — what to document for users who expect OMC autopilot feel

Ship / keep this **user-facing mental model** (README, `omg-using` skill, doctor footer, autopilot docs):

### OMC feel → oh-my-grok equivalent

| OMC expectation | oh-my-grok reality | Operator command |
|-----------------|--------------------|------------------|
| “Stay in session until done” (Stop block) | **Outer CLI loop** re-launches Grok until accept or max_iter | `omg ralph "goal"` |
| Autopilot plan→code→verify | **Pipeline FSM** (CLI stages, not Stop) | `omg pipeline "goal"` |
| Parallel burst | **ULW** skill fanout + integrate (Option B product path) | `omg ulw "goal"` or `omg pipeline "goal" --implement ulw` |
| Don’t stop mid-story | Skill **must stop after one unit**; CLI continues | (automatic under `omg ralph`) |
| Cancel sticky mode | Cancel run / kill via CLI, not hope Stop unblocks | `omg cancel` |
| “Verified done” | **Only** after `omg accept` (not dual APPROVE, not model prose) | `omg accept` / pipeline accept stage |
| Interactive chat skill only | **Not** the persistence product path | Prefer CLI; chat skill is one-shot playbook |

### Messaging rules (copy-paste ready)

1. **Grok does not support OMC-style Stop continuation.** oh-my-grok will not fake it.  
2. **Persistence is a CLI feature**, not a chat vibe. If it stopped, either the unit of work finished or you were not under `omg ralph` / `omg pipeline`.  
3. **To get “keep going until verified”:**  
   ```bash
   omg ralph "your goal"          # multi-iter, one story per launch
   omg pipeline "your goal"       # plan → implement → integrate → dual → accept
   omg pipeline "goal" --implement ulw   # parallel implement path
   ```  
4. **Interactive Grok + skill only** = best-effort single pass; model may stop; that is expected.  
5. **Never** write `verified: true` yourself; never treat dual-review APPROVE as product verified.

### Doc touchpoints (when editing docs — not this ADR’s implementation)

| Where | What to say |
|-------|-------------|
| `README.md` “What it is” | One sentence: persistence = CLI outer loop, not Stop-hook block (host limitation). |
| `skills/omg-using/SKILL.md` | Table above; anti-pattern “wait for Stop to force continue”. |
| `skills/omg-ralph/SKILL.md` | Already correct; keep “outer loop owned by CLI”. |
| `docs/security-model.md` | Optional note: Stop is non-blocking lifecycle; not a control plane. |
| `docs/research/autopilot-0.3/*` | Autopilot ≠ OMC sticky session; autopilot = pipeline/ulw productization. |

### What success looks like without Stop continuation

- Operator runs **one** `omg pipeline` / `omg ralph` and gets multi-iter progress + accept attempt without manually re-pasting prompts.  
- Incomplete work leaves **run state** under `.omg/state/runs/<id>/` with resume/report — not a blocked chat.  
- Cancel is reliable via `omg cancel` + pid/starttime, not “unblock the Stop hook.”

---

## ADR summary (one screen)

| Field | Value |
|-------|--------|
| **Decision** | **Do not implement** OMC-like in-session Stop continuation |
| **Drivers** | (1) Grok Stop non-blocking, (2) Option B CLI outer loops, (3) solo ROI / no dual control plane |
| **Alternatives** | BUILD host-impossible hook · Hybrid advisory Stop · NOT BUILD |
| **Why chosen** | Only option that matches host + architecture + honesty |
| **Consequences** | Document CLI as autopilot spine; educate OMC migrants; no stop.py control logic |
| **Follow-ups** | Option B P0/P1 execution; doc messaging above; reopen only if Grok adds blocking Stop + live canary |

---

**DONE.** Decision recorded. No implementation tasks authorized by this ADR.
