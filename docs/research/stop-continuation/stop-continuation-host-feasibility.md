# Stop continuation host feasibility (Grok Build vs OMC)

**Date:** 2026-07-20  
**Question:** Can oh-my-grok implement OMC-style Stop continuation (ralph/autopilot enforcer) on Grok Build?  
**Verdict:** **Not feasible as host-enforced Stop continuation** under current Grok hook semantics. Persistence must remain **CLI outer-loop owned** (already the design). A Stop hook can only observe/log; it cannot block turn end or reinject prompts.

---

## 1. What can a Grok Stop hook return? Prevent end? Inject follow-up?

### Documented host contract (`~/.grok/docs/user-guide/10-hooks.md`)

| Fact | Evidence |
|------|----------|
| `Stop` fires when “An agent turn ends (completed, cancelled, or error).” | L90 |
| **`Stop` is non-blocking** (`Blocking?` = **No**) | L90 table |
| **Only `PreToolUse` can block**; “every other event is passive.” | L99 |
| Blocking decision JSON is documented **only for PreToolUse** | L188–193: `{"decision":"allow"}` / `{"decision":"deny","reason":"..."}` |
| Exit code `2` = explicit deny **(blocking hooks only)** | L199–201 |
| **Passive hooks:** “stdout is ignored. Just exit 0 on success.” | L203–205 (examples: SessionStart / PostToolUse; same class as Stop) |
| Hook failures are fail-open; only explicit PreToolUse `deny` blocks tools | L152, L369 |

### oh-my-grok current Stop hook

```1:25:<repo-root>/hooks/bin/stop.py
#!/usr/bin/env python3
"""Stop hook: record session stop only. NEVER marks runs verified."""
...
        # CRITICAL: never set verified / acceptance status here — omg CLI is sole writer.
        append_event(
            root,
            {"event": "Stop", "status": "ok", "raw_keys": list(ev.keys())[:20]},
        )
...
        # Fail-open: never crash Stop on I/O or unexpected errors
        sys.exit(0)
```

Wired as passive command in `hooks/hooks.json` L25–34 (timeout 10s). No stdout decision protocol.

### Answers (Q1)

| Capability | On Grok Build (documented) | On Claude Code (OMC / ralph-loop) |
|------------|----------------------------|-----------------------------------|
| Return structured decision from Stop | **No** (stdout ignored for passive events) | Yes: `decision: "block"`, `reason`, optional `systemMessage` / `continue` |
| Prevent session/turn end | **No** | Yes (`decision: "block"` / `continue: false`) |
| Inject follow-up user/system prompt | **No** via hook | Yes (`reason` becomes next-turn prompt) |
| Side effects (append events, write files) | **Yes** (current stop.py) | Yes (state bump + block) |

**Conclusion:** Emitting OMC-shaped JSON from Grok `Stop` would be **silently ignored** per host docs. There is no documented path to “block stop + reinject prompt” on Grok.

---

## 2. Differences vs Claude Code OMC Stop hooks

### OMC registration

`~/.claude/plugins/marketplaces/omc/hooks/hooks.json` registers Stop chain:

- `context-guard-stop.mjs`
- `workflow-drift-guard.mjs`
- **`persistent-mode.mjs`** (continuation enforcer)
- `code-simplifier.mjs`

### OMC `persistent-mode.mjs` contract (host-dependent)

When a mode is active and incomplete, it **blocks stop** and feeds work back:

```976:982:~/.claude/plugins/marketplaces/omc/templates/hooks/persistent-mode.mjs
          console.log(
            JSON.stringify({
              continue: false,
              decision: "block",
              reason,
            }),
          );
```

Ralph path (excerpt):

- Active `ralph-state.json` + session match → bump `iteration` →  
  `reason = [RALPH LOOP - ITERATION n/max] Work is NOT done. Continue…`  
  + `decision: "block"`.
- Respects: context-limit stop (never block — deadlock risk #213), user abort, auth errors, cancel-in-progress, session isolation, stale state (2h), project match.

Allow-stop shape:

```js
{ continue: true, suppressOutput: true }
```

(Also `SAFE_CONTINUE` at top of file.)

### Soft twin: `stop-continuation.mjs`

OMC also has a **non-enforcing** variant:

```1:17:~/.claude/plugins/marketplaces/omc/templates/hooks/stop-continuation.mjs
// Always allows stop - soft enforcement via message injection only.
...
  console.log(JSON.stringify({ continue: true, suppressOutput: true }));
```

### Official Claude `ralph-loop` (same host API)

`ralph-loop/hooks/stop-hook.sh` L179–188:

```json
{
  "decision": "block",
  "reason": "<same prompt text>",
  "systemMessage": "🔄 Ralph iteration N | …"
}
```

Session isolation via `session_id` in state vs hook stdin (L27–35).

### Side-by-side

| Dimension | Claude + OMC | Grok + oh-my-grok |
|-----------|--------------|-------------------|
| Stop can block turn end | **Yes** (host feature) | **No** (host: passive) |
| Continuation owner | In-session Stop enforcer **or** soft messaging | **Outer `omg` CLI loop** (`modes.run_mode` for-loop) |
| Prompt reinjection | Hook `reason` / systemMessage | New `grok -p` / `--prompt-file` each iter |
| Verified ownership | OMC cancel + verification skills (separate) | **Strict CLI-only** `set_verified` + acceptance token |
| Cancel | `/oh-my-claudecode:cancel` clears mode state; hook allows stop | `omg cancel` → killpg + status cancelled |
| Fail-open | Safety timeout → allow stop | stop.py always exit 0; never touch verified |

**Key architectural point:** OMC’s “don’t stop until done” is a **host Stop-block**. oh-my-grok’s ralph is already a **process outer loop** that does not need Stop-block to iterate.

---

## 3. Risks if we tried OMC-style Stop continuation anyway

### 3.1 Double-loop with `omg ralph` CLI

Evidence: `omg_cli/modes.py` owns the loop.

- Docstring L1–4: “(for ralph) loops max_iter times.”
- `DEFAULT_MAX_ITER["ralph"] = 3` (L30–35).
- Skill contract (`skills/omg-ralph/SKILL.md` L9–12, L33–43):  
  **outer loop owned by CLI**; skill is **one iteration**; agent **stops** after one story.
- `run_mode` L724–781: `for i in range(1, max_iter + 1):` → launch grok → acceptance → break if verified; non-ralph modes break after one launch.

If Stop-block reinjected mid-session **and** CLI relaunched after process exit:

| Scenario | Effect |
|----------|--------|
| Interactive session with “ralph” skill only + Stop enforcer | Matches OMC model (single process, many turns) — **but Grok can’t enforce** |
| `omg ralph` headless + Stop enforcer (if host later supports) | **Double loop**: in-session N turns × CLI max_iter launches → cost explosion, confused iteration counters |
| Headless `grok -p` one-shot | Stop may fire once at end; block is meaningless after process exits |

**Risk rating:** High if both layers active; **mitigation already present** by design (CLI owns loop; agent told to stop after one story).

### 3.2 Verified ownership

| Layer | Rule |
|-------|------|
| `omg_cli/state.py` L1–6 | “Only the omg CLI … may mutate status / passes / verified” |
| `set_verified` L632–651 | Requires trusted `acceptance.result.json` (`writer=omg-cli`, passed, manifest sha) |
| `write_status` L387–411 | Never sets `verified=true` via extra |
| `hooks/bin/stop.py` L1, L14–15 | Explicitly **never** marks verified |
| Architecture diagram `README.md` L310–316 | Hooks = event spool; CLI = verified/passes only |

Any Stop-continuation design that set `verified` from a hook would **break the single-writer security model**. Completion promise text in agent output (Claude ralph-loop) is weaker than CLI acceptance — OMG correctly rejects that pattern.

### 3.3 Cancel races

- `cancel_run` (`state.py` L552+): marks cancelled, killpg on pid.json starttime-matched group, clears active.
- `omg-cancel` skill: stop spawning; cancelled ≠ verified.
- OMC pattern: cancel-in-progress → allow stop (`persistent-mode.mjs` L939–942).

If Grok ever gains Stop-block:

1. Hook must treat `status ∈ {cancelled}` / missing active run as **allow stop**.
2. Must not relaunch after cancel.
3. Session isolation: Stop fires per turn; state is project-scoped — same footgun as ralph-loop’s `session_id` (other sessions must not block).

### 3.4 Other risks

| Risk | Notes |
|------|--------|
| Context-limit deadlock | OMC learned this the hard way (#213): never block context-limit stops. Grok has `PreCompact`/`PostCompact` but Stop still passive — moot for now. |
| Fail-open vs infinite block | Grok fail-open is good for safety; OMC uses safety timeout (`DEFAULT_SAFETY_TIMEOUT_MS = 8500`) to force allow-stop. |
| False confidence | Shipping “persistent-mode.py that prints decision:block” without host support is **theatre** — same class of bug as soft PreToolUse claims without canary. |

---

## 4. Minimal viable design (if / when feasible)

### 4.1 Today (host reality): **do not implement OMC Stop-block**

**MVP = status quo + honesty:**

1. Keep `stop.py` as **append-only event spool** (no verified, no loop control).
2. Keep **`omg ralph` CLI for-loop** as the only persistence enforcer for supervised runs.
3. Skill text remains: one story → stop → CLI accept/verify → next iter or exit.
4. Document explicitly: “Grok Stop is passive; no in-session continuation enforcer.”

This already maps to OMC’s **outer orchestration** half, not the Stop half.

### 4.2 Soft-only enhancement (optional, low value)

If product wants “reminder when user stops early in interactive TUI”:

- On Stop: if active non-terminal run exists under `.omg/state/`, append event  
  `{event, active_run_id, status, iteration}` and optionally write a **proposal** under `.omg/artifacts/` (never status.json).
- **Cannot** force the model to continue; user must type continue or re-run `omg ralph`.
- Do **not** claim “enforcer” in docs.

Analog: OMC’s `stop-continuation.mjs` (always `continue: true`) — messaging-only.

### 4.3 If Grok later adds Claude-compatible Stop decisions

**Gate on live canary**, not docs rumor. Required host behavior:

```json
// hypothetical — NOT documented for Grok today
{"decision": "block", "reason": "<next user prompt>", "systemMessage": "optional"}
// or
{"continue": false, "decision": "block", "reason": "..."}
```

Then **minimal** `hooks/bin/stop_enforcer.py` (name TBD):

1. Fail-open default (exit 0, empty / allow-stop).
2. Load active run via CLI-readable paths only (read-only); **no** `set_verified`.
3. Allow stop if: no active run, `status` terminal (`cancelled|completed|failed|verified`), cancel marker, session_id mismatch, stale > N hours, user-abort if payload carries it.
4. **Mutex with CLI loop:**  
   - **Either** interactive mode only (`OMG_STOP_ENFORCE=1` + no outer CLI loop),  
   - **Or** headless `omg ralph` only with enforcer **disabled** (env default off for CLI launches).  
   Never both.
5. Cap iterations in state; never auto-extend max_iter silently without product decision (OMC extends by +10 — controversial for OMG).
6. Live gate: scripted turn-end with active run → prove host re-enters model with `reason` text; prove cancel allows exit; prove dual-session isolation.

### 4.4 Recommended product stance

| Path | Recommendation |
|------|----------------|
| Port OMC `persistent-mode.mjs` 1:1 to Grok | **Reject** — host cannot honor `decision: block` |
| Rely on `omg ralph` outer loop | **Keep** — correct for headless + verified ownership |
| Soft Stop logging | Optional, non-blocking |
| Feature request to Grok Build | “Claude-compatible Stop hook decisions” if interactive autopilot is a goal |

---

## 5. Evidence index (paths)

| Source | Role |
|--------|------|
| `~/.grok/docs/user-guide/10-hooks.md` | Host: Stop non-blocking; only PreToolUse blocks; passive stdout ignored |
| `<repo-root>/hooks/bin/stop.py` | OMG Stop: event only, never verified |
| `<repo-root>/hooks/hooks.json` | Stop / SubagentStop / SessionStart / PreToolUse wiring |
| `<repo-root>/omg_cli/modes.py` | CLI max_iter loop, ralph context pack, one-story stop contract |
| `<repo-root>/skills/omg-ralph/SKILL.md` | Outer CLI owns loop |
| `<repo-root>/omg_cli/state.py` | Single-writer verified / cancel_run |
| `~/.claude/plugins/marketplaces/omc/templates/hooks/persistent-mode.mjs` | OMC Stop enforcer (`decision: block`) |
| `~/.claude/plugins/marketplaces/omc/templates/hooks/stop-continuation.mjs` | Soft allow-stop twin |
| `~/.claude/plugins/marketplaces/omc/hooks/hooks.json` | Stop hook chain registration |
| `~/.claude/plugins/marketplaces/claude-plugins-official/plugins/ralph-loop/hooks/stop-hook.sh` | Upstream Claude ralph Stop-block pattern |

---

## 6. One-line summary

**Grok Build Stop hooks are passive (stdout ignored, non-blocking); OMC-style Stop continuation requires Claude’s `decision: "block"` + reason reinjection, which Grok does not document or support — oh-my-grok should keep CLI-owned ralph loops and never claim host Stop enforcement until a live canary proves otherwise.**
