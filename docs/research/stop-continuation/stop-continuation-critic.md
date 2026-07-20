# Critic Verdict — Port OMC persistent-mode to oh-my-grok Stop hook

**date_utc:** 2026-07-20  
**Reviewer:** Critic (adversarial)  
**Scope:** Attack the idea of porting OMC `persistent-mode` Stop continuation into oh-my-grok's `Stop` hook  
**Mode:** ADVERSARIAL (host non-blocking Stop + verified-only-via-CLI + cancel/max-iter conflict = systemic)

**Sources verified (read, not trusted from memory):**
- OMC: `~/.claude/plugins/marketplaces/omc/scripts/persistent-mode.mjs`
- OMC: `~/.claude/plugins/marketplaces/omc/src/hooks/persistent-mode/index.ts` (+ max-iter / cancel-race tests)
- OMC: `hooks/hooks.json` Stop chain (context-guard → workflow-drift → **persistent-mode** → code-simplifier)
- Grok host: `grok-build/crates/codegen/xai-grok-hooks/src/event.rs` (`is_blocking`)
- Grok host: `xai-grok-shell/.../hook_dispatch.rs` (`dispatch_hook` → `dispatch_non_blocking`)
- Grok host: `xai-grok-hooks/examples/README.md` (blocking = PreToolUse only)
- oh-my-grok: `hooks/bin/stop.py`, `hooks/hooks.json`, `omg_cli/modes.py`, `omg_cli/state.py`, `docs/security-model.md`
- oh-my-grok skills: `omg-cancel`, `omg-ralph`, `omg-pipeline`

---

## VERDICT: **DO_NOT_BUILD**

**Not BUILD_MINIMAL for continuation.**  
**Not BUILD_FULL under any conditions known on current Grok host.**

Optional non-continuation work is **out of scope of this port** and is listed under "If you insist on touching Stop" only so it is not mistaken for a green light.

---

## Overall Assessment

OMC persistent-mode is a **Claude Code Stop-block re-injection engine**: it emits `{"decision":"block","reason":"..."}` so the host feeds the reason back as a synthetic user turn and the agent keeps chatting. oh-my-grok already owns persistence differently: **outer `omg` CLI loops** (`ralph` `max_iter`, `ralplan` `max_rounds`, `pipeline` FSM) + **`verified` only after trusted `omg accept`**. Grok's Stop hook is **lifecycle / observe-only** (`is_blocking() == false` for Stop). Porting OMC's mechanism onto that hook is either a no-op (dead code that looks like a feature) or a product lie (operators believe Stop "keeps going"). Both outcomes are worse than not building it.

---

## Pre-commitment Predictions vs Actual

| # | Prediction (before deep read) | Actual |
|---|-------------------------------|--------|
| 1 | Grok Stop may not honor `decision:block` like Claude Code | **CONFIRMED CRITICAL** — only `PreToolUse` is blocking; Stop is explicitly non-blocking in host tests |
| 2 | Stop continuation will fight `omg cancel` / max-iter | **CONFIRMED** — OMC extends `max_iterations` by +10 at soft cap; cancel is racey (TTL signal + tombstones); omg cancel is PID/status CLI path with no Stop handshake |
| 3 | Infinite loop / token burn via reinforcement | **CONFIRMED in OMC design** — ralph default soft max 100 with auto-extend; autopilot reinforcement ≤20; ultragoal ≤50; still unbounded without hardMax |
| 4 | Fail-open Stop will make "continuation gate" fake | **CONFIRMED** — omg Stop already fail-opens; even a deny JSON would not block on Grok |
| 5 | False "autopilot/ralph done" when only hook keeps chatting | **CONFIRMED pattern in OMC** — phase strings / incomplete todos drive block; exit often requires **human `/cancel`**, not product verified; omg contract forbids hook from setting `verified` |

---

## Attack Surface 1 — Breaks verified-only-via-CLI

### Contract today (oh-my-grok)

```1:2:hooks/bin/stop.py
#!/usr/bin/env python3
"""Stop hook: record session stop only. NEVER marks runs verified."""
```

```14:21:hooks/bin/stop.py
        # CRITICAL: never set verified / acceptance status here — omg CLI is sole writer.
        append_event(
            root,
            {"event": "Stop", "status": "ok", "raw_keys": list(ev.keys())[:20]},
        )
    except Exception:
        # Fail-open: never crash Stop on I/O or unexpected errors
        sys.exit(0)
```

```632:649:omg_cli/state.py
def set_verified(root: Path, run_id: str, *, force: bool = False) -> dict[str, Any]:
    """Mark verified only when trusted CLI acceptance exists (unless force=True).
    ...
    """
    ...
    if not force and not _has_acceptance_artifact(root, run_id):
        raise PermissionError(
            "refusing to set verified=true without trusted CLI acceptance "
```

Acceptance requires `writer: omg-cli` + in-process `run_acceptance` token. Disk forgeries rejected.

### How an OMC-style Stop port attacks this

| Failure | Mechanism | Severity |
|---------|-----------|----------|
| **Semantic verified laundering** | Hook reason says "work complete / continue until done" while `status.json` stays `running`/`completed` with `verified:false`. Operators and models treat chat as done. | MAJOR (product trust) |
| **Accidental write path** | Implementer "helps" by writing `verified:true` or forging `acceptance.result.json` from a hook-driven re-entry turn. CLI path still refuses without token — but **chat will claim verified**. | MAJOR |
| **Dual sources of truth** | OMC mode state (`ralph-state.json`, `autopilot-state.json`, reinforcement counts) vs omg `runs/<id>/status.json`. Port that keeps OMC-shaped files creates a second completion story outside CLI. | CRITICAL if done |
| **Stop mutates run state** | Any "smart" Stop that bumps `passes`, clears active, or marks complete on model stop bypasses `omg accept`. Directly violates AGENTS/HARD RULES. | CRITICAL if done |

**Hard rule for any future work:** Stop may append `events.jsonl` only. No `write_status`, no `set_verified`, no PRD/acceptance mutation.

OMC itself often exits modes via **`/oh-my-claudecode:cancel` after architect verification**, not via a CLI acceptance stamp. That is the opposite of omg's product contract:

> product verified ⇔ CLI-stamped acceptance, not "model stopped after enough reinjections".

---

## Attack Surface 2 — Fights `omg cancel` and max-iter

### omg cancel (authoritative)

```552:617:omg_cli/state.py
def cancel_run(...):
    """Mark run cancelled and clear active if it matches. ...
    Kill path is **fail-closed**: only signals when pid.json starttime matches ps.
    ...
    current["status"] = "cancelled"
    current["verified"] = False
    ...
    clear_active(root, run_id)
```

Cancel is **CLI + PID group**, not a Stop-hook cooperative signal. There is no `cancel-signal-state.json` TTL in oh-my-grok today (OMC invented that precisely because Stop re-blocks after cancel).

### OMC max-iter behavior (what you would import)

From `persistent-mode.mjs` ralph path (verified):

1. If `iteration < maxIter` → **block** with "Work is NOT done", increment iteration.
2. If soft max hit and no hard max → **`max_iterations = maxIter + 10`** and **still block**.
3. Soft default max is often **100** (tests / state fixtures); hard max only if `OMC_SECURITY=strict` or config.
4. Cancel race needed its **own test suite** (`cancel-race.test.ts` issue #921): cancel prompt or 30s TTL signal must suppress re-enforcement.

### Collision matrix (omg × OMC Stop)

| omg mechanism | OMC Stop behavior | Collision |
|---------------|-------------------|-----------|
| `ralph` outer CLI loop `for i in 1..max_iter` then exit | In-session Stop re-injects forever (until cancel / hard max) | **Double loop**: CLI launches N grok processes; inside each process Stop multiplies turns. Token = N × reinjections |
| `DEFAULT_MAX_ITER["ralph"] = 3` | Soft max 100 + auto-extend +10 | Port redefines max-iter away from CLI contract |
| `omg cancel` SIGTERM + status cancelled | Model may still be mid-reinjection; stale state can re-block next session (OMC stale 2h threshold is a bandage) | Cancel appears to work then "zombie continue" if state not cleared |
| Pipeline stage budget (`max_iter`, dual rounds) | Autopilot reinforcement independent of pipeline.json | Stage machine and Stop disagree on "done" |
| `require_acceptance` non-zero exit | Stop can keep chatting after CLI already exited 1 | User sees more agent output after process "failed" only if Stop is interactive TUI — still confuses headless vs TUI |

**Finding (CRITICAL if ported):** Importing max-iter extension (+10) **nullifies** CLI `max_iter` as a cost control. The OMC test literally expects: at 10/10 → block as 11/20.

---

## Attack Surface 3 — Infinite loop / token burn

### Host path that makes "block" either free or expensive

**Path A — honest host (current Grok):**  
Stop stdout is ignored for control flow. `decision:block` is **dead**. Cost: wasted hook CPU only. Product cost: **false feature**.

**Path B — someone "makes Stop blocking like Claude" (host patch or misread docs):**  
Then OMC-class loops apply:

| Mode (OMC) | Cap | Behavior at cap | Burn shape |
|------------|-----|-----------------|------------|
| ralph | soft max, extend +10 | keeps blocking | unbounded until hardMax or cancel |
| autopilot | reinforcement ≤ 20 | then allow stop | up to 20 full turns of "not complete" |
| ultragoal | ≤ 50 | then allow | long goal thrash |
| ultrawork | incomplete todos/tasks | block | thrash on stale todos |
| tool error guidance | retries in reason text | model re-runs failing tools | error × reinjection |

Plus OMC special cases that still burn when wrong:

- `stop_hook_active` re-entry must not block again (Claude safety) — Grok has **no** documented equivalent.
- Context-limit stop must **not** block (OMC issue #213 deadlock with compact). Grok has Stop + PreCompact separately; a naive port that blocks all stops risks compact/stop deadlocks **if** blocking were ever enabled.
- Rate-limit stop must not re-enter (OMC #777).

### Double accounting with oh-my-grok headless

`modes._launch_grok` already waits up to `DEFAULT_TIMEOUT` (3600s) per launch. Stop reinjection inside that process multiplies model turns until timeout 124 — **max token burn under a single CLI iteration**.

**Finding (MAJOR/CRITICAL under Path B):** Token burn is not theoretical; it is the product of Stop-block × soft-max-extend × outer CLI max_iter × per-launch timeout.

---

## Attack Surface 4 — Fail-open vs fail-closed Stop

### Grok host truth

```138:141:crates/codegen/xai-grok-hooks/src/event.rs
    pub fn is_blocking(&self) -> bool {
        matches!(self, Self::PreToolUse)
    }
```

Unit test asserts **Stop is not blocking**.

```211:232:xai-grok-shell/.../hook_dispatch.rs
    /// Dispatch a non-blocking hook event: ...
    pub(super) async fn dispatch_hook(...) {
        ...
        let results =
            xai_grok_hooks::dispatcher::dispatch_non_blocking(&registry, event, &envelope, &ctx)
                .await;
```

Grok hooks examples README:

- Blocking response JSON only documented for **PreToolUse**
- Passive hooks: "stdout is informational only. Exit 0 for success."
- Fail-open: non-0/non-2 / crash does not deny tools (PreToolUse); Stop has nothing to deny

### omg Stop truth

Always fail-open (catch-all `sys.exit(0)`). Correct for lifecycle logging.

### Trap of "fail-closed Stop"

If product docs claim "Stop fail-closed means work continues until verified":

| Interpretation | Reality on Grok |
|----------------|-----------------|
| Fail-closed = stop process refuses to end until verified | **Host cannot do this today** |
| Fail-closed = hook errors abort the session | Hostile UX; fights cancel and context limits |
| Fail-open = log and exit | Current, correct |

**Finding (CRITICAL for any marketing):** Shipping OMC-shaped Stop JSON without host support is a **fail-open feature that looks closed**. Same class of bug as Option A spawn gate with stale global matcher (false confidence).

---

## Attack Surface 5 — False "autopilot done" when only the hook keeps chatting

### OMC autopilot stop logic (simplified)

- If `autopilot.state.active` and phase ≠ `"complete"` and reinforcement ≤ 20 → **block** with  
  `[AUTOPILOT - Phase: {phase}] Autopilot not complete. Continue working.`
- Exit path after "complete": often **run cancel**, not a verified stamp.
- Phase string is **mode state**, not `omg accept`.

### oh-my-grok product definition of done

| Signal | Means done? |
|--------|-------------|
| Model says "done" | No |
| Dual-review APPROVE | No (still need accept) |
| `status=completed` without acceptance | Explicitly **not verified** (`modes.py` note) |
| `omg accept` + trusted token → `set_verified` | **Only** product verified |
| Pipeline report.json | Evidence package; verified flag only if accept passed |

### False-done scenarios if Stop "continuation" exists

1. **Hook keeps chatting** after CLI already marked `completed without acceptance` → UI looks "still working"; no verified.
2. **Hook allows stop** when phase string set to complete by model self-write → **false complete**, no tests run.
3. **Interactive session** with stale `active.json` → Stop reinjects unrelated work into a new user query session (OMC uses 2h stale threshold + session_id match — still leaked bugs historically).
4. **User believes cancel is the completion ceremony** (OMC prompt text) → omg users learn the wrong completion model (`cancel` clears run; verified is separate).

**Finding (MAJOR):** Stop-driven "done" is a **chat liveness signal**, not a **product verification signal**. Porting it teaches the wrong success metric.

---

## Host impossibility summary (the steelman killer)

| OMC Stop primitive | Claude Code | Grok (verified) |
|--------------------|-------------|-----------------|
| `decision: "block"` + reason reinjected as turn | Yes (Stop-block) | **No** — Stop non-blocking |
| `continue: true/false` contract | Yes | Not control plane for Stop |
| `stop_hook_active` re-entry guard | Yes | Not present in omg envelope usage |
| Fail-open on hook crash for Stop | Yes (safe exit) | Yes (and no block possible) |
| Continuation without host block | Prompt-only | Prompt-only = **CLI outer loop already does this** |

**Steelman alternative already shipped:** `omg_cli.modes.run_mode` / `run_ralplan` / `run_pipeline` re-launch `grok -p` with fresh prompts, freeze+accept between iters, exit codes bound cost. That is the correct port of "persistent work" — **process outer loop**, not Stop inner loop.

---

## Multi-Perspective Notes

### Executor

- "Port persistent-mode.mjs to Python Stop hook" is implementable as JSON printf — and **will do nothing useful** on current Grok.
- Where you will get stuck: inventing a host feature that does not exist; wiring cancel signals that omg cancel never writes; dual state under `.omg` vs OMC files.
- Correct executor work if asked for "persistence": harden CLI loop, not Stop.

### Stakeholder

- Problem statement ("keep going until done") is real.
- Success metric must remain **`verified` via accept**, not "agent still typing".
- Shipping Stop continuation that cannot force continue is a **roadmap lie**.

### Skeptic / Security

- Strongest argument against the port: **it cannot work on host contract**.
- Second: if made to work via host change, it recreates OMC's hardest bugs (cancel races, context-limit deadlock, max-iter extend, token burn) on a product that deliberately centralized control in CLI.
- Third: any Stop that mutates verified/state is a privilege escalation around acceptance allowlist.

---

## What's Missing (from the idea itself — gaps)

- Host capability analysis before design (Stop non-blocking) — **must be the first gate**
- Mapping of OMC completion → omg `verified` (missing; they are incompatible)
- Interaction table with `omg cancel`, `max_iter`, pipeline stages, headless timeout
- Token budget model (CLI iters × Stop reinjections × timeout)
- Definition of done that is not cancel-shaped
- Live oracle: "Stop block causes second model turn" — **would fail today**
- Who owns iteration counter: CLI status.json vs hook-local state
- Behavior under `permission-mode plan`, rate limits, compaction

---

## Ambiguity Risks (if someone still drafts a plan)

| Phrase | Interp A | Interp B | Wrong choice cost |
|--------|----------|----------|-------------------|
| "Port OMC persistent-mode" | Full Stop-block reinjection | "Keep working until verified" via CLI only | Building dead Stop code |
| "Fail-closed Stop" | Session cannot end until verified | Hook errors abort session | Uncancelable sessions / false security |
| "Respect max-iter" | CLI max_iter only | Hook soft max + extend | Infinite cost |
| "Done" | Model stopped cleanly | `verified:true` | False product success |
| "Cancel" | `omg cancel` | Model `/cancel` skill text | Zombie runs |

---

## Self-Audit

| Finding class | Confidence | Author refutable? | Flaw vs preference |
|---------------|------------|-------------------|--------------------|
| Stop non-blocking on Grok | HIGH (host unit test + dispatch path) | No without host change | FLAW |
| verified-only CLI + Stop never verified | HIGH (stop.py + set_verified) | No | FLAW |
| OMC max-iter auto-extend | HIGH (script + tests) | No | FLAW (if ported) |
| Double-loop token burn | HIGH under interactive reinjection; N/A if dead | Dead path mitigates burn but not false confidence | FLAW |
| Prefer CLI loop over Stop | HIGH product fit | Preference only if host later adds Stop-block | Design |

Realist check:
- **Dead-code port** worst case: wasted engineering + false docs. Severity stays CRITICAL for **product claim**, not for runtime data loss.
- **Working Stop-block port** (requires host change): token burn + cancel races + verified semantic erosion. Severity CRITICAL for cost and trust.
- Detection of dead feature: slow (users complain "ralph doesn't keep going in TUI") unless canary asserts reinjection.

No downgrade: false confidence on orchestration is the same class as soft-gate matcher drift.

---

## Concrete Fixes (what to build instead)

1. **DO NOT** emit `decision:block` from `hooks/bin/stop.py`. Keep passive event log.
2. **DO NOT** invent OMC-shaped `ralph-state.json` / reinforcement counters for Stop.
3. **Keep** persistence in CLI:
   - `omg ralph --max-iter N` outer loop
   - acceptance between iters
   - exit non-zero if `require_acceptance` and not verified
4. **If interactive "nudge"** is desired later: productize as **user-visible CLI resume** (`omg resume` / pipeline `--resume`), not Stop magic.
5. **If host someday adds Stop-block:** redesign from omg contracts first (cancel handshake, hard max shared with CLI, verified gate, no auto-extend); do not copy OMC script.

### If you insist on touching Stop (NOT a green light for continuation)

**BUILD_MINIMAL-adjacent only (telemetry):**

- Log `active_run_id`, `status`, `verified` snapshot on Stop into `events.jsonl`
- Never block, never write status, never extend max_iter
- Doctor may surface "Stop observed with non-terminal active run" as advisory

This is **not** porting persistent-mode. Call it telemetry or do not build it in the same PR as "continuation".

---

## Verdict Justification

**DO_NOT_BUILD** because:

1. **Host cannot enforce Stop continuation** (non-blocking Stop).
2. **Product already has the right persistence layer** (CLI outer loop + accept).
3. **OMC design actively fights** cancel / max-iter / verified-only semantics omg depends on.
4. Shipping the port creates **false confidence** (worse than absence).
5. Enabling real reinjection without a ground-up redesign recreates known OMC failure modes (extend max, cancel races, context-limit deadlock risk, token burn).

**Upgrade path (only if world changes):**

- Grok documents and ships **Stop-block with reinjected reason** + `stop_hook_active` equivalent  
- **and** design is CLI-native (shared max_iter, cancel signal written by `omg cancel`, never auto-extend, never set verified, hard budget)  
- **and** live canary proves reinjection + cancel within 1 turn + no block on context-limit  

Until then: **reject the idea**. Hand off: **planner must not schedule this**; if stakeholders want "don't stop," invest in **pipeline/ralph CLI reliability**, not Stop.

---

## Open Questions (unscored)

- Will Grok ever make Stop blocking? No evidence in current hooks crate.
- Does any TUI path interpret Stop stdout outside `xai-grok-hooks`? Not found in dispatch path reviewed; would need a separate host audit if claimed.
- Is `docs/research/autopilot-stop-continuation/` intended as a future plan folder (currently empty)? Treat emptiness as non-approval.

---

## Ralplan summary row

- Principle/Option Consistency: **Fail** — OMC Stop-block contradicts omg verified-only-CLI + host non-blocking Stop  
- Alternatives Depth: **Pass (critic-supplied)** — CLI outer loop is the superior existing alternative  
- Risk/Verification Rigor: **Fail for the proposed idea** — no live oracle possible for reinjection on current host  
- Deliberate Additions: N/A (idea rejection, not deliberate ralplan package)

---

**Hand-off:** planner / product — **do not schedule OMC persistent-mode Stop port**. Executor — **leave `hooks/bin/stop.py` as passive logger**. Architect — only revisit if Grok ships Stop-block semantics.

**DONE criteria for this review:** this file written at  
`<repo-root>/.omc/research/stop-continuation-critic.md`
