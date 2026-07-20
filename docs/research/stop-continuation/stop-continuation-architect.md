# Architect: Should oh-my-grok add OMC-like in-session Stop continuation?

**Date:** 2026-07-20  
**Product:** oh-my-grok **0.2.5** (Option B: plugin + `omg` CLI)  
**Question:** Keep CLI-only persistence, or add OMC-style Stop → force-continue inside interactive Grok sessions?  
**Role:** Architect (code-backed; no implementation)  
**Audience:** solo maintainer  

---

## Summary

**Recommend Option A: keep CLI-only persistence (status quo).**  
Do **not** ship OMC-like in-session Stop continuation on current Grok Build.

Host evidence is decisive: Grok file hooks treat **`Stop` as non-blocking** — only `PreToolUse` is a blocking gate. `hooks/bin/stop.py` correctly only spools events and never marks `verified`. Persistence, acceptance, cancel, and trust already live in the outer `omg` CLI (`run_mode` loop + `set_verified` + PID cancel). Adding Stop-continuation without a host force-continue contract would either be a no-op (host ignores stdout) or a second, weaker persistence path that splits cancel/trust and multiplies solo maintenance.

**Revisit B/C only after a live host gate proves** that some plugin-visible mechanism can force another agent turn after Stop (or after end_turn) with injectable text and a cancelable session marker.

---

## Analysis

### 1. What the code does today (oh-my-grok)

| Layer | Behavior | Evidence |
|-------|----------|----------|
| **Stop hook** | Log-only event spool; **never** sets verified / acceptance | `hooks/bin/stop.py:1–21` (`"""… NEVER marks runs verified."""`; fail-open `sys.exit(0)`) |
| **SubagentStop** | Same: append event only | `hooks/bin/subagent_stop.py:9–19` |
| **Hooks manifest** | SessionStart / SubagentStop / Stop / PreToolUse; Stop is command-only | `hooks/hooks.json:14–34` |
| **Ralph skill** | **One iteration** then STOP; outer CLI owns loop | `skills/omg-ralph/SKILL.md:8–12`, `33–43`, `72–76` |
| **CLI ralph loop** | `for i in range(1, max_iter+1)`: launch grok → accept → maybe verify → break | `omg_cli/modes.py:724–781` |
| **Prompt contract** | Explicit: “Implement **ONE** story then **stop**. Outer CLI owns the loop.” | `omg_cli/modes.py:223–228` |
| **verified trust** | Only `omg` CLI / `set_verified` after trusted acceptance token | `docs/security-model.md:20–25`; `omg_cli/state.py:4–6`, `632–650` |
| **Cancel** | Fail-closed PID/starttime kill of leader (+ workers skeleton); mark cancelled | `omg_cli/state.py:552–617`; `skills/omg-cancel/SKILL.md:22–63` |
| **Product architecture** | Documented Option B: plugin playbooks + CLI hard keywords / outer loops | `README.md:5–10`, `301–320`; `plugin.json:4` |

Intentional design (from v0.1 plan): Stop appends events; **never** set verified (`docs/superpowers/plans/2026-07-19-oh-my-grok.md:285`).

### 2. What OMC-style Stop continuation means (contrast)

In Claude Code / OMC-class products, a **blocking Stop hook** can:

1. Observe “model wants to end turn”
2. If a **persistent mode** is active (ralph / ultrawork / autopilot), **deny/block the stop**
3. Inject a “continue / keep going / unfinished work” message
4. Loop until mode ends, cancel, or max iterations

That couples **persistence** to the **same process / same session** the user is typing in.

oh-my-grok deliberately inverted this for Option B:

- **Interactive plugin session:** skills teach one-iteration behavior; hooks are ledger + soft PreToolUse.
- **Supervised durability:** `omg ralph` / `omg pipeline` outer process loop, fresh context pack per iter, CLI-owned acceptance.

### 3. Grok host uncertainty (verified from grok-build source)

This is the load-bearing constraint.

| Host fact | Implication for Stop continuation |
|-----------|-----------------------------------|
| `HookEventName::is_blocking()` is **only** `PreToolUse` | Stop stdout cannot deny/block end-of-turn the PreToolUse way | `crates/codegen/xai-grok-hooks/src/event.rs:138–141` |
| Unit test: `Stop` **must not** be blocking | Product cannot claim “block Stop” on file hooks | same file `event.rs:435–453` |
| Lifecycle events (incl. Stop) fire via `dispatch_non_blocking` | “Never denies — callers log results and continue” | `dispatcher.rs:162–167`; shell uses `dispatch_non_blocking` for Stop in `run_loop.rs` / `turn.rs` |
| Stop payload is `{ reason: "end_turn" \| "cancelled" \| "error" }` | Useful for logging; no decision field consumed for loop control | `turn.rs:990–1005`; `event.rs:194–196` |
| Docs: passive hooks → **stdout ignored** | Even a perfectly shaped Claude-style JSON is ignored for non-blocking events | `xai-grok-pager/docs/custom-hooks.md:111–123` |
| `TurnControl::ForceContinue` exists | Workspace→sampler **turn_hook** IPC, **not** plugin file `Stop` hooks | `xai-tool-protocol/src/turn_hook.rs:173–196`, tests with `"control": "force_continue"` |
| ACP client hooks: non-PreToolUse are fire-and-forget | Same observe-only split | `acp_session/hooks.rs:6–9` |

**Net:** Emitting Claude/OMC-style “continue after Stop” from `hooks/bin/stop.py` is **not supported by the current Grok file-hook contract**. Implementing Option B without a new host capability is either:

- **Theatre** (hook writes continue JSON; host ignores; user still stops), or  
- A **private turn_hook / workspace bridge** (out of Option B scope: no fork of grok-build).

### 4. Trust boundary (verified product contract)

Primary contract (`docs/security-model.md:20–25`):

1. Workers without shell via `capability_mode`
2. Depth = 1
3. **Only `omg` CLI** writes `passes` / `verified`
4. Hooks are defense-in-depth, fail-open

Stop continuation would pressure this boundary:

| Risk | Why |
|------|-----|
| Dual persistence owners | CLI loop **and** in-session loop both “keep going until done” |
| Fake completion pressure | In-session loop tempted to treat “model said done” as verified without `omg accept` |
| Hook mutates run state | Today hooks must **not** write `verified` (`stop.py` critical comment; `state.py` module doc) |
| Fail-open vs force-continue | Product security model is **fail-open** on hook crash for PreToolUse; force-continue needs the opposite reliability profile for **stopping** (must not loop forever if cancel state unreadable) |

**Hard rule if ever implementing B/C:** Stop path may inject “continue” only; it must **never** call `set_verified`, never write `acceptance.result.json`, never clear cancel.

### 5. Cancel semantics

Today cancel is well-defined for CLI runs:

- `omg cancel` → fail-closed kill via `pid.json` starttime match → status `cancelled` → clear `active.json`  
  (`state.py:552–617`, `skills/omg-cancel/SKILL.md`)

In-session Stop continuation needs answers the product does **not** have yet:

| Question | CLI path (A) | In-session path (B/C) |
|----------|--------------|------------------------|
| What process to kill? | Recorded PID / pgid | Interactive TUI session (often no omg PID) |
| What marks “mode off”? | Terminal status + clear active | Mode tag file? skill memory? session env? |
| User says “stop” mid-continue | SIGTERM to launcher ends loop | Must clear mode tag **and** stop force-continue **before next Stop** |
| Cancel vs user Esc in TUI | Outer process dies; no more launches | Host cancel reason `"cancelled"` — Stop still fires non-blocking; mode must not re-arm |

Without a durable **session mode lease** + cancel that clears it atomically, Option B recreates the worst OMC failure mode: “I cancelled but it keeps nudging continue.”

### 6. Interactive skill sessions vs supervised CLI

| Path | Persistence | Context refresh | Acceptance | Solo ops cost |
|------|-------------|-----------------|------------|---------------|
| `omg ralph` | Outer `max_iter` loop | Fresh prompt + ralph context pack each iter (`modes.py:99–196`, `732–737`) | CLI freeze + run + token | One binary path; doctor/canary/live suite already track it |
| Interactive skill `omg-ralph` alone | Model convention only | Same long context | Model may “feel done” | Already documented: outer CLI owns loop |
| Hypothetical Stop continue | Same session, same context window pressure | No free context pack unless injected | Easy to skip CLI accept | New mode state, host gate, cancel, live canaries |

Ralph skill already forbids infinite self-loop inside one session (`skills/omg-ralph/SKILL.md:94–95`).

---

## Steelman A — Keep CLI-only persistence (status quo)

**Thesis:** Persistence is an **operator-owned subprocess supervisor**, not a Stop-hook side effect.

**Strongest arguments:**

1. **Host-aligned.** Stop is observe-only; product does not pretend otherwise.
2. **Single writer.** `verified` / accept / active mutex stay in one process tree (`state.py`, `modes.py`).
3. **Cancel works.** PID + starttime + killpg already battle-tested direction for solo ops.
4. **Context hygiene.** Each ralph iter gets a rebuilt prompt (skill body + HARD RULES + context pack) instead of fighting compaction/doom-loop in one endless TUI turn chain.
5. **Honest UX.** “Use `omg ralph` for durable completion; skills are one pass” is teachable in `omg-using` / README.
6. **Solo maintainer.** No second persistence engine, no dual canary matrix, no mode-tag race bugs.

**Best counter (steelman against A):** Interactive users who only open Grok TUI and type “ralph keep going” will **stop early** unless they learn the CLI. That is real UX friction vs OMC “keyword → mode sticks until done.”

**Mitigations without Stop continuation:**

- Stronger skill/using copy: “durable → run `omg ralph` outside or via shell tool”
- Optional: SessionStart advisory if `.omg/state/active.json` present (still non-blocking)
- Optional later: thin `omg ralph --attach` docs, not force-continue

---

## Steelman B — Add Stop continuation for interactive TUI/skill sessions

**Thesis:** Match OMC. When user invokes ralph/ulw skill, set a mode flag; on Stop, force continue until verified/cancel/max.

**Strongest arguments:**

1. **UX parity.** Users coming from OMC expect “don’t stop” to mean the session keeps going.
2. **Lower ceremony.** No second terminal / no remembering `omg ralph`.
3. **Autopilot narrative.** Open-box “type goal and walk away” feels closer to continuous session.
4. **Skill alone is weak.** Without a hard continue, interactive ralph is convention-only (models ignore “stop after one story”).
5. **If host later supports it**, oh-my-grok should be ready to use the same event it already registers.

**Why B fails on current host (not taste — mechanism):**

1. File Stop is **non-blocking**; stdout ignored → **no force-continue**.
2. Building on `TurnControl::ForceContinue` implies workspace/sampler integration → near-fork / private API, violates Option B “no fork of grok-build.”
3. **Trust & cancel** redesign: session mode lease, max-continue budget, cancel clears lease, never verifies from hook.
4. **Quota burn:** endless continue loops in interactive sessions without CLI `max_iter` default (ralph CLI default **3**) are expensive for solo quota even if “generous.”
5. **Context rot:** multi-hour same-session continue loses the free context pack reset CLI already implements.
6. **Dual product paths** forever: every feature (accept, integrate, dual-review) must answer “CLI or in-session?”

**If host someday supports blocking Stop / force_continue for plugins, minimum B design (not recommended to build now):**

- Mode lease file under `.omg/state/sessions/<session_id>/mode.json` written only by explicit skill step or `omg mode enter` (not by Stop)
- Stop: if lease active and not cancelled and continue_count < max → emit host-supported continue; else allow stop
- Cancel: `omg cancel` + skill must clear lease; Stop with `reason=cancelled` never re-arms
- Never touch `verified`
- Live canary: prove host actually starts another turn

---

## Steelman C — Hybrid: CLI for supervised runs; Stop only for mode tags without `omg` CLI

**Thesis:** Keep `omg ralph|ulw|pipeline` as the durable path; add lightweight in-session mode tags so TUI-only sessions get soft/hard continue when no active CLI run.

**Strongest arguments:**

1. Preserves CLI trust path for production/supervised work.
2. Gives TUI-only users *something* without requiring them to learn CLI first.
3. Avoids double-loop when `active.json` points at a CLI-owned run (Stop stays log-only while CLI supervises).
4. Looks like a pragmatic compromise between A and B.

**Hidden costs (why hybrid is often the worst for solo):**

1. **Still blocked by host non-blocking Stop** for the “hard continue” half — hybrid’s valuable half is the half that doesn’t work yet.
2. **Two mental models:** “Did I start `omg ralph` or just say ralph?” Behavior differs; bugs are mode-confusion.
3. **Mutex complexity:** if CLI active, suppress Stop continue; if CLI dies uncleanly, lease/active skew; if skill sets mode without CLI, who owns acceptance?
4. **Soft hybrid (prompt-only nudge via non-blocking Stop log)** is already possible and nearly useless (stdout ignored; cannot inject).
5. Maintainer pays full B complexity for partial UX.

Hybrid only becomes rational **after** host force-continue works **and** CLI remains sole writer for verified.

---

## Root Cause

The gap users feel (“why doesn’t Stop force continue like OMC?”) is **not** a missing 20-line `stop.py` feature.

It is the intersection of:

1. **Architectural choice (Option B):** durability lives in multi-process CLI loops, not in-session Stop.  
2. **Host capability gap:** Grok plugin Stop hooks are **observe-only**; force-continue exists only on a different turn_hook channel.  
3. **Trust model:** `verified` is intentionally CLI-gated; in-session persistence must not become a second authority.

Therefore “implement OMC Stop continuation” is either **impossible honestly** (current host) or a **product fork** of the Option B contract (if forced via private APIs).

---

## Recommendations (prioritized)

### 1. **Ship / keep Option A** — Low effort — High impact (correctness + solo focus)

- Keep `hooks/bin/stop.py` log-only.
- Keep ralph skill “one story then stop.”
- Keep `omg ralph` / pipeline as the only durable loops.
- Document explicitly in README / `omg-using` / security-model:  
  **“Grok Stop hooks are non-blocking; oh-my-grok does not implement OMC-style Stop continuation. Use `omg ralph`.”**

### 2. **Add a host capability gate (research spike), not product code** — Medium effort — Unblocks future B/C

Live + source checklist before any Stop-continue implementation:

| Gate | Pass criterion |
|------|----------------|
| H1 | Documented or source-proven plugin mechanism forces another model turn after Stop/end_turn |
| H2 | Injected continue text reaches the model (not only log) |
| H3 | Cancel / Esc / `reason=cancelled` does **not** re-enter continue |
| H4 | Max continue budget enforced outside the model |
| H5 | No path sets `verified` from hook |
| H6 | Canary checked into `docs/research/live/` |

Until H1–H3 green: **B and C are blocked**.

### 3. **Optional UX without Stop force** — Low effort — Medium UX

- SessionStart / skill text: if user says ralph/don’t-stop → print exact `omg ralph "…"` command.
- Doctor note: “durable persistence = CLI.”
- Do **not** invent mode tags until H1 passes.

### 4. **Do not implement B or C in 0.3.x autopilot track** — Saves weeks

0.3 priority already correctly targets spawn fail-closed / ULW / pipeline (`.omc/research/autopilot-architect-options.md`). Stop continuation is a different product surface and would steal solo capacity from isolation gates that **do** work on PreToolUse.

---

## Trade-offs

| Option | Pros | Cons |
|--------|------|------|
| **A — CLI-only (recommended)** | Host-honest; single trust writer; cancel works; context pack per iter; minimal code; matches Option B | TUI-only “ralph” stops early; OMC parity gap remains |
| **B — Full in-session Stop continue** | Best OMC-like UX *if host supports it*; one surface for users | **Currently no-op or requires private API**; dual persistence; cancel/mode-lease redesign; quota/context risk; solo maintenance spike |
| **C — Hybrid mode tags** | Supervised path stays pure; TUI gets something later | Pays B+A complexity; mode confusion; still host-blocked for hard continue; worst solo ROI until H1 |

---

## Consensus Addendum (steelman review)

- **Antithesis (steelman against A):** If the maintainer’s primary surface is interactive Grok TUI and they almost never run `omg ralph`, then CLI-only persistence is a **paper feature** — the product “has ralph” only in docs. Force-continue (when host allows) would make the skill path real. A pure-CLI durability story fails users who refuse a second process.

- **Tradeoff tension:** **UX continuity (same session keeps going)** vs **trust & host honesty (CLI single-writer + non-blocking Stop)**. You cannot fully maximize both on Grok 0.2.x file hooks.

- **Synthesis (viable later, not now):**  
  - **Now:** A + clearer “run `omg ralph`” UX.  
  - **If H1–H6 pass:** thin **C**: Stop-continue only when no CLI `active.json`, with lease + max budget, never verify; CLI path remains default for supervised/autopilot.

- **Principle violations if B ships today:**  
  - Claiming OMC parity while Stop is non-blocking → **honesty / verification discipline** violation.  
  - Letting hooks influence completion without CLI accept → **single-writer / security-model** violation.  
  - Expanding scope during isolation/autopilot work → **solo prioritization** violation.

---

## Decision

| Decision | **Option A — Keep CLI-only persistence** |
|----------|------------------------------------------|
| For | Solo maintainer, current Grok host, Option B architecture |
| Not now | B (full Stop continuation), C (hybrid mode tags with hard continue) |
| Reopen when | Host gates H1–H6 pass with live evidence |

---

## References

### oh-my-grok

- `hooks/bin/stop.py:1–21` — Stop is log-only; never verified  
- `hooks/bin/subagent_stop.py:9–19` — SubagentStop log-only  
- `hooks/hooks.json:25–34` — Stop registered as command hook  
- `skills/omg-ralph/SKILL.md:8–12, 33–43, 72–76, 94–95` — one iteration; outer CLI owns loop; anti self-loop  
- `omg_cli/modes.py:223–228, 419–425, 607–817` — ralph contract, never force verified, outer for-loop  
- `omg_cli/state.py:4–6, 552–617, 632–650` — CLI single-writer; cancel; set_verified trust  
- `docs/security-model.md:20–25` — primary product contract  
- `README.md:5–10, 21, 301–320` — Option B architecture diagram  
- `skills/omg-cancel/SKILL.md:22–90` — cancel via PID files, not pkill  
- `docs/superpowers/plans/2026-07-19-oh-my-grok.md:285` — original “Stop never set verified” intent  
- `.omc/research/autopilot-architect-options.md` — 0.3 priorities elsewhere (isolation/ULW/pipeline)

### grok-build (host)

- `crates/codegen/xai-grok-hooks/src/event.rs:138–141, 194–196, 435–453` — only PreToolUse blocking; Stop payload reason; tests  
- `crates/codegen/xai-grok-hooks/src/dispatcher.rs:162–167` — non-blocking never denies  
- `crates/codegen/xai-grok-shell/src/session/acp_session_impl/turn.rs:990–1005` — Stop fired after turn with end_turn/cancelled/error  
- `crates/codegen/xai-grok-pager/docs/custom-hooks.md:111–123` — blocking vs passive stdout  
- `crates/common/xai-tool-protocol/src/turn_hook.rs:173–196, 555–571` — ForceContinue is turn_hook IPC, not file Stop  
- `crates/codegen/xai-grok-shell/src/session/acp_session/hooks.rs:6–9` — client hooks: PreToolUse gate vs fire-and-forget  

---

## DONE criteria for this note

- [x] Steelman A, B, C  
- [x] One recommendation for solo maintainer (**A**)  
- [x] Trust boundary, cancel, Grok host uncertainty with file:line evidence  
- [x] Written to `.omc/research/stop-continuation-architect.md`
