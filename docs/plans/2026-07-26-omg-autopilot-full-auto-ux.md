# OMG Autopilot Full-Auto UX (Grok Stop-Pin) Implementation Plan

> **For Codex:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Inside an interactive Grok session, autopilot feels OMC-style hands-off (clear reqs → no ask → verified; unclear → interview only), using **real Stop hard-pin** on grok ≥0.2.107 / installed 0.2.112, plus tertiary CLI/`/loop` beyond the 8-cap.

**Architecture:** **Primary** = plugin Stop `decision:block` while autopilot incomplete. **Secondary** = host `/goal`. **Tertiary** = `omg autopilot run` + `/loop`/bg-wake. Interview = `ask_user_question` + gate yields. Contract phases = OMX spine. Supersedes 2026-07-20 Stop ADR and the earlier same-day CLI-only draft.

**Tech Stack:** Python 3.11+, pytest, Grok Build Stop/SubagentStop gates, `omg_cli/stop_gate.py`, hooks, autopilot FSM.

**Authors:** Qwen 3.8 Max plan + orchestrator synthesis (2026-07-26). Grok mechanisms A1–D18 all dispositioned — see §6 checklist.

**Related:** @docs/research/stop-continuation/ · @skills/omg-autopilot/SKILL.md · @hooks/bin/stop.py · @omg_cli/autopilot.py

---

## Inversion vs earlier draft

Earlier same-day draft made `omg autopilot run` the spine and treated Stop as passive. **Wrong for grok 0.2.112.** This plan: **Stop pin primary**; CLI/`/loop` tertiary beyond 8-cap / headless.

## 0. Host contract is verified (read this first)

Source of truth (read-only references in `~/Documents/mine/grok-build`):

- `crates/codegen/xai-grok-shell/src/session/acp_session_impl/stop_gate.rs`
  - `pub const MAX_STOP_HOOK_CONTINUATIONS_PER_TURN: u32 = 8;`
  - `build_stop_payload` → turn-end Stop carries `reason: "end_turn"`, `stop_hook_active`, `last_assistant_message`, `background_tasks`, `session_crons`.
  - `run_stop_gate`: at the cap the hook is **not consulted** (forced `AllowStop`); `prevent_continuation` (`continue:false`) force-stops; `wants_continuation()` (any block or `additionalContext`) → `KeepWorking` with feedback = the block `reason`(s).
  - `dispatch_session_end_stop` + `demote_ignored_blocks`: session-end Stop (`reason: channel_closed|shutdown`) decision is **parsed then discarded**.
- `crates/codegen/xai-grok-pager/docs/user-guide/10-hooks.md` — "Stop Decision Control": `{"decision":"block","reason"}` reinjects reason as a **user message** and continues the **same turn**; `{"hookSpecificOutput":{"additionalContext"}}` also keeps working; `{"continue":false,"stopReason"}` force-stops; **exit 2 + stderr** blocks (only when stdout has no usable JSON); fail-open on crash/timeout/malformed; **Stop/SubagentStop default timeout 600s**; Esc/Ctrl+C/refusal/max-turns skip Stop; counter is **per turn** (next prompt resets); `/goal` runs **before** the stop gate.
- `crates/codegen/xai-grok-shell/tests/test_stop_hook_e2e.rs` — proves: block re-fires with `stopHookActive:true`, reason fed back to model, exit-2+stderr blocks, `continue:false` overrides, cap ends the turn at exactly 8.

**OMG current state (verified):**

| File | Truth |
|---|---|
| `hooks/bin/stop.py` | spool-only; never blocks; never verifies; fail-open |
| `hooks/bin/subagent_stop.py` | spool-only |
| `hooks/bin/_common.py` | `read_hook_event()` (stdin JSON), `append_hook_observation`, `hook_disabled`, `workspace_root`, `ensure_omg_dirs`; already imports `omg_cli.*` |
| `hooks/hooks.json` | **Stop timeout = 10s** (too low for a gate), SubagentStop 10s, SessionStart 10s, PreToolUse 5s |
| `omg_cli/autopilot.py` | full FSM `LEGAL_TRANSITIONS`; `start/transition/complete_with_acceptance/status`; state at `.omg/state/runs/<id>/stages/autopilot.json`; **no `awaiting_confirmation` flag** |
| `omg_cli/state.py` | `load_active_run(root)` → `active.json`→`status.json`; `status.json` mirrors `mode` + `autopilot_phase`; single-writer (hooks may **read**, never write `verified`/`passes`); `merge_status_fields` writes non-authority keys without a lease |
| `omg_cli/interview.py` | interview state `status ∈ {initializing, waiting_input, ready_to_close, ready_for_pressure_pass, complete}` + `pending_question` |
| `omg_cli/main.py` | `cmd_autopilot` = start/transition/status/complete; **no `run`** subcommand |
| `omg_cli/modes.py` | `ralph_context_pack`, `build_prompt`, `build_grok_argv`, `_launch_grok`, `run_mode` (reuse for context pack + outer loop) |
| `omg_cli/doctor.py` | `HOOK_SCRIPTS`, soft checks, `_run_grok_json`; **no grok CLI version check** |
| `omg_cli/command_policy.py` | `_FLUTTER_ALLOWED={test,analyze}`, `_DART_ALLOWED={test,analyze,format}` (basis for `is_analyze_only`) |
| `omg_cli/acceptance.py` | `collect_commands`, `freeze_and_run`, `load_prd`, `result_path`, `materialize_prd_from_ultraqa` |
| `skills/omg-autopilot/SKILL.md` | says **"No Stop hard-pin"** / "Claiming Stop hooks keep the session alive" is an anti-pattern — **must be superseded** |
| `docs/research/stop-continuation/stop-continuation-decision.md` | **"DO NOT BUILD"** — **STALE; must be superseded** |
| `templates/omg-rules.md` | always-loaded rules template (written by `omg setup` → `~/.grok/rules/omg.md`) |
| `tests/test_lifecycle_hooks.py` | `_run_hook(name, tmp_path, payload)` subprocess helper; asserts stop.py fail-open/non-authoritative |

---

## 1. Host mechanism contract table (A–D, none omitted)

### A) Same-turn keep-working

| # | Mechanism | OMG decision | How |
|---|---|---|---|
| A1 | **Stop hook gate** (`decision:block` / `additionalContext`; cap 8/turn; exit2+stderr; `continue:false` force-stop; fail-open) | **PRIMARY — USE** | `hooks/bin/stop.py` → `omg_cli.stop_gate.decide_stop()`: block while an active autopilot run is in a non-terminal phase and not awaiting a human; reason = phase continuation prompt. Tasks 1–3. |
| A2 | **SubagentStop** (same gate inside a subagent; main Stop only gates parent) | **DECLINE for pinning** | `subagent_stop.py` stays **observe-only** — leaf workers must be allowed to finish and return. Task 5 locks this. |
| A3 | **`/goal` in-turn Continue** (runs before the Stop gate) | **SECONDARY — optional, host-native** | No code. Documented as a complementary in-session option (`/goal <continuation>`); independent of our Stop pin. Task 6 docs. |
| A4 | **Tool-call loop** (same turn while model returns tools) | **Implicit — USE passively** | The block reason instructs the model to keep calling tools toward the gate. No code. |
| A5 | **Mid-turn auto-compact** (rebuilds same turn) | **DECLINE (host-managed)** | No code. Documented: compaction rebuilds the same turn; our pin survives because it is state-driven, not transcript-driven. Task 6 docs. |

### B) New-turn without typed user (cross-turn)

| # | Mechanism | OMG decision | How |
|---|---|---|---|
| B6 | **`/loop` + `scheduler_create` → `SchedulerFired`** | **TERTIARY — optional** | Documented fallback for beyond-8 / unattended: `/loop 5m omg autopilot status --run RUN` style wake. Gate reads `sessionCrons` for awareness. Task 6 docs + Task 9 hint. |
| B7 | **Background bash/monitor/subagent complete → auto-wake** | **USE — cooperatively** | Gate **allows stop when `backgroundTasks` is non-empty** (the completion auto-wakes a new turn). `decide_stop` checks `event["backgroundTasks"]`. Task 1. |
| B8 | **Goal post-turn summary queue** | **DECLINE (host-native)** | No code. |
| B9 | **Queued follow-ups / plan-resume** | **DECLINE (host-native)** | No code. |
| B10 | **Outer CLI relaunch** (`omg ralph` / `omg autopilot run`) | **TERTIARY — USE** | `omg autopilot run` = durable cross-turn/headless/crash-recovery driver. Task 10. |

### C) Human pause / ask surfaces

| # | Mechanism | OMG decision | How |
|---|---|---|---|
| C11 | **`ask_user_question`** (structured MCQ; blocks until answer/timeout) | **INTERVIEW mechanism — USE** | Skill routes unclear requirements to `omg-deep-interview` + Grok-native `ask_user_question`. Gate **suppresses block while awaiting interview input**. Tasks 4, 6. |
| C12 | **Tool permission prompts / plan enter-exit approval** | **DECLINE (host-native) + cooperate** | Destructive/credential pauses set the `autopilot_awaiting` flag so the gate yields. Task 4. |
| C13 | **`--no-ask-user` / `GROK_ASK_USER_QUESTION=0` / deny tool** | **DECLINE (host config)** | Documented: removes structured ask but does **not** ban freeform prose questions — the drift guard (C14) covers freeform. Task 6 docs. |
| C14 | **Freeform "Should I continue?"** | **USE — soft drift guard** | Optional `lastAssistantMessage` regex → **block once** (gated by `stopHookActive`). Task 8. |

### D) Passive / NOT continuation

| # | Mechanism | OMG decision | How |
|---|---|---|---|
| D15 | **SessionStart / UserPromptSubmit / PostToolUse / PreCompact** (stdout ignored for control) | **DECLINE as control plane** | SessionStart may **passively refresh `RESUME.md`** (side-effect only, no stdout control). Task 13. |
| D16 | **PreToolUse** (allow/deny tools only; cannot ForceContinue) | **DECLINE for continuation** | Already used for spawn fail-closed; explicitly **not** a continuation engine. Unchanged. |
| D17 | **`TurnControl::ForceContinue`** (tool-protocol) | **DECLINE explicitly** | Stubbed/unused by the workspace — **do not plan on it**. Noted in ADR + architecture. |
| D18 | **Rules / skills** (context only, not schedulers) | **USE as soft rules** | `templates/omg-rules.md` + SKILL: "no mid-phase questions; keep working toward the gate." Enforcement is the Stop gate, not the prose. Task 6. |

---

## 2. Architecture

```
┌────────────────────────── interactive Grok session ──────────────────────────┐
│                                                                                │
│  model works → returns no tools → host fires Stop gate (reason=end_turn)        │
│        │                                                                       │
│        ▼                                                                       │
│  hooks/bin/stop.py → omg_cli.stop_gate.decide_stop(root, event)                │
│        │                                                                       │
│   block? ──yes──► {"decision":"block","reason":<phase continuation prompt>}    │
│        │              host reinjects reason as user msg → SAME turn continues   │
│        │              (cap 8/turn; stopHookActive escalates message)            │
│        no                                                                      │
│        ▼                                                                       │
│   exit 0, no output → turn ends (verified / cancelled / awaiting human /        │
│                       interview waiting_input / backgroundTasks in flight)      │
│                                                                                │
│  PRIMARY   = Stop pin (A1)          — keeps the turn alive while incomplete     │
│  SECONDARY = /goal (A3, host-native)— optional in-turn continuation             │
│  TERTIARY  = omg autopilot run (B10) + /loop (B6) — beyond-8 / cross-turn       │
│  INTERVIEW = ask_user_question (C11) — gate yields via autopilot_awaiting       │
└────────────────────────────────────────────────────────────────────────────────┘
```

**Decision predicate (`decide_stop`, pure + hermetic):** return a block/force-stop dict, or `None` to allow stop.

1. `event["reason"] != "end_turn"` → `None` (session-end observe fire; never block).
2. `load_active_run(root)` is `None` or `mode != "autopilot"` → `None` (optionally drift-guard C14 if a soft mode is active).
3. `autopilot_phase ∈ {verified, cancelled}` → `None`.
4. `status["autopilot_awaiting"]` truthy → `None` (genuine human pause: interview / destructive-credential).
5. `phase == "interview"` and interview state `status == "waiting_input"` → `None` (let the human answer).
6. `event["backgroundTasks"]` non-empty → `None` (background work will auto-wake a new turn — B7).
7. (Optional, Task 9) continuation counter ≥ `OMG_STOP_GRACEFUL_CAP` (default off) → `{"continue":false,"stopReason":"…omg autopilot run --resume RUN…"}` (graceful force-stop before the host's hard 8 annotation).
8. Otherwise → `{"decision":"block","reason":<continuation prompt>}`. The prompt names the phase, the next CLI gate stamp, and "do **not** ask the user mid-phase". When `stopHookActive` is true, append an escalation line ("you already continued; produce the gate or `omg autopilot transition --phase blocked`").

**State the gate reads (read-only):** `status.json` (`mode`, `autopilot_phase`, `autopilot_awaiting`) via `load_active_run`; interview state file only when `phase==interview`. The gate **never writes** `status`/`passes`/`verified`.

**Authority invariants (unchanged):** only `omg` CLI writes `verified`/`passes`; hooks fail-open; PreToolUse stays fail-open soft-guard; `TurnControl::ForceContinue` not used.

---

## 3. TDD tasks

Conventions (verified): tests use `PYTHONPATH=. .venv/bin/python -m pytest -q`; hook tests use the `_run_hook` subprocess pattern from `tests/test_lifecycle_hooks.py`; commit style `type(scope): subject`. Run after each task: `PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live"`.

---

### Task 1 — Pure `decide_stop` predicate + continuation-prompt builder

**Files:** create `omg_cli/stop_gate.py`; test `tests/test_stop_gate.py` (new).

**Step 1 — failing tests** (`tests/test_stop_gate.py`): build a helper that writes `active.json` + `status.json` (+ optional `stages/autopilot.json`, interview state) under `tmp_path`, then:

```python
from omg_cli.stop_gate import decide_stop, continuation_reason

def _mk(tmp, phase, mode="autopilot", awaiting=False, reason="end_turn",
        stop_hook_active=False, last_msg=None, bg=None):
    # write .omg/state/active.json -> run-1 ; status.json with mode/autopilot_phase/autopilot_awaiting
    ...
    return {"reason": reason, "stopHookActive": stop_hook_active,
            "lastAssistantMessage": last_msg, "backgroundTasks": bg or []}

def test_session_end_fire_never_blocks(tmp_path):
    ev = _mk(tmp_path, "implement", reason="channel_closed")
    assert decide_stop(tmp_path, ev) is None

def test_no_active_run_allows_stop(tmp_path):
    assert decide_stop(tmp_path, {"reason": "end_turn"}) is None

def test_non_autopilot_mode_allows_stop(tmp_path):
    ev = _mk(tmp_path, "implement", mode="ralph")
    assert decide_stop(tmp_path, ev) is None

def test_terminal_phases_allow_stop(tmp_path):
    for ph in ("verified", "cancelled"):
        assert decide_stop(tmp_path, _mk(tmp_path, ph)) is None

def test_awaiting_confirmation_allows_stop(tmp_path):
    ev = _mk(tmp_path, "implement", awaiting=True)
    assert decide_stop(tmp_path, ev) is None

def test_interview_waiting_input_allows_stop(tmp_path):
    # write interview state status=waiting_input for run-1
    ev = _mk(tmp_path, "interview")
    assert decide_stop(tmp_path, ev) is None

def test_background_tasks_in_flight_allow_stop(tmp_path):
    ev = _mk(tmp_path, "implement", bg=[{"id": "t1", "type": "shell", "status": "running"}])
    assert decide_stop(tmp_path, ev) is None

def test_active_incomplete_blocks_with_phase_reason(tmp_path):
    ev = _mk(tmp_path, "review")
    d = decide_stop(tmp_path, ev)
    assert d["decision"] == "block"
    assert "review" in d["reason"] and "do not ask" in d["reason"].lower()

def test_stop_hook_active_escalates_message(tmp_path):
    d = decide_stop(tmp_path, _mk(tmp_path, "implement", stop_hook_active=True))
    assert "already" in d["reason"].lower() or "blocked" in d["reason"].lower()

def test_continuation_reason_names_next_gate():
    assert "structured_review.json" in continuation_reason("review", goal="g", run_id="r1")
    assert "ultraqa.json" in continuation_reason("qa", goal="g", run_id="r1")
```

**Step 2 — run:** `... pytest -q tests/test_stop_gate.py` → FAIL (module missing).

**Step 3 — impl** (`omg_cli/stop_gate.py`): pure functions, defensive reads, **no exceptions escape** (caller wraps). `decide_stop(root, event, *, env=None)` implements predicate 1–8 (skip 7 here; Task 9). `continuation_reason(phase, *, goal, run_id, stop_hook_active=False)` maps phase→next gate stamp (ralplan→consensus, implement→`transition --phase review`, review→`stages/structured_review.json clean`, qa→`stages/ultraqa.json status clean`, acceptance→`omg autopilot complete`). Read `status.json` via `omg_cli.state.load_active_run`; read interview state via a defensive `_read_interview_status(root, run_id)` (returns `None` on any error). Terminal set `{"verified","cancelled"}`.

**Step 4 — run:** PASS.

**Step 5 — commit:** `git add omg_cli/stop_gate.py tests/test_stop_gate.py && git commit -m "feat(stop-gate): pure autopilot-aware Stop decision predicate"`

---

### Task 2 — Wire `hooks/bin/stop.py` to the gate (fail-open)

**Files:** modify `hooks/bin/stop.py`; extend `tests/test_lifecycle_hooks.py`.

**Step 1 — failing tests** (subprocess via `_run_hook`):

```python
def test_stop_blocks_active_incomplete_autopilot(tmp_path):
    _seed_active_autopilot(tmp_path, phase="implement")   # writes active.json+status.json
    proc = _run_hook("stop.py", tmp_path, '{"reason":"end_turn","stopHookActive":false}')
    assert proc.returncode == 0
    assert json.loads(proc.stdout)["decision"] == "block"

def test_stop_allows_session_end_fire(tmp_path):
    _seed_active_autopilot(tmp_path, phase="implement")
    proc = _run_hook("stop.py", tmp_path, '{"reason":"shutdown"}')
    assert proc.returncode == 0 and proc.stdout.strip() == ""

def test_stop_allows_verified(tmp_path):
    _seed_active_autopilot(tmp_path, phase="verified")
    proc = _run_hook("stop.py", tmp_path, '{"reason":"end_turn"}')
    assert proc.stdout.strip() == ""

def test_stop_fail_open_on_malformed_and_crash(tmp_path, monkeypatch):
    # malformed stdin + a seeded state that would raise inside decide_stop
    proc = _run_hook("stop.py", tmp_path, "not-json{")
    assert proc.returncode == 0   # never exit 2, never crash
```

**Step 2 — run:** FAIL (stop.py still spool-only → no block JSON).

**Step 3 — impl** (`hooks/bin/stop.py`): keep `hook_disabled("stop")` early-out and `append_hook_observation`; then:

```python
from omg_cli.stop_gate import decide_stop
ev = read_hook_event()
append_hook_observation(root, "Stop", ev)          # keep diagnostics
decision = decide_stop(root, ev)                    # pure; defensive
if decision is not None:
    sys.stdout.write(json.dumps(decision))          # host reads stdout JSON
sys.exit(0)                                         # ALWAYS 0; fail-open
```
Wrap the whole body in `try/except Exception: sys.exit(0)` (preserve fail-open). **Never** exit 2 here (we use stdout JSON, not stderr).

**Step 4 — run:** PASS. Also re-run existing `test_stop_and_subagent_alias_are_fail_open_bounded_and_non_authoritative` (must still pass — no `verified` ever written).

**Step 5 — commit:** `git commit -m "feat(hooks): Stop hook blocks while autopilot incomplete (fail-open)"`

---

### Task 3 — `hooks.json` Stop timeout bump + doctor adequacy check

**Files:** modify `hooks/hooks.json`; modify `omg_cli/doctor.py`; test `tests/test_doctor.py`.

**Why:** Stop gate default is 600s; our `hooks.json` sets **10s** — a slow disk read could time out → fail-open → turn ends. Our gate is fast (reads 1–2 JSON files), so **120s** is ample and honest.

**Step 1 — failing test:**

```python
def test_stop_hook_timeout_is_gate_adequate():
    data = json.loads((ROOT / "hooks/hooks.json").read_text())
    stop = data["hooks"]["Stop"][0]["hooks"][0]
    assert stop["timeout"] >= 60          # gate must not fail-open on slow IO

def test_doctor_flags_low_stop_timeout(tmp_path, monkeypatch):
    # point doctor at a hooks.json with Stop timeout=10 -> soft WARN/FAIL
    ...
```

**Step 2 — run:** FAIL (timeout is 10).

**Step 3 — impl:** set Stop `timeout: 120` in `hooks/hooks.json` (leave PreToolUse 5, SessionStart 10; SubagentStop stays 10 — observe-only). Add `check_stop_gate_timeout()` soft check to `doctor.py` (WARN if Stop timeout < 60; OK otherwise) and register it in the soft-check list near `check_plugin_version_drift`.

**Step 4 — run:** PASS.

**Step 5 — commit:** `git commit -m "fix(hooks): bump Stop gate timeout to 120s + doctor adequacy check"`

---

### Task 4 — `autopilot_awaiting` state flag + `omg autopilot await`

**Files:** modify `omg_cli/autopilot.py`, `omg_cli/main.py`; test `tests/test_autopilot.py`.

**Step 1 — failing tests:**

```python
def test_set_awaiting_mirrors_flag_into_status(tmp_path):
    st = start_autopilot(tmp_path, "vague", skip_interview=False)
    set_awaiting_confirmation(tmp_path, st["run_id"], True, reason="interview:waiting_input")
    run = load_run(tmp_path, st["run_id"])
    assert run["autopilot_awaiting"] is True
    assert run["autopilot_awaiting_reason"] == "interview:waiting_input"

def test_clear_awaiting(tmp_path):
    ...
def test_set_awaiting_never_touches_verified(tmp_path):
    ...  # verified stays False; status not terminal
def test_cli_autopilot_await_action(tmp_path):  # via cmd_autopilot argparse
    ...
```

**Step 2 — run:** FAIL.

**Step 3 — impl:**
- `autopilot.py`: `set_awaiting_confirmation(root, run_id, value, *, reason=None)` → `merge_status_fields(root, run_id, {"autopilot_awaiting": bool(value), "autopilot_awaiting_reason": reason or ""})` (no lease needed; never touches `verified`). Export it.
- `main.py`: add `ap_sub.add_parser("await", ...)` with `--run`, `--reason`, `--clear`; wire into `cmd_autopilot`.
- The Stop gate already reads `autopilot_awaiting` (Task 1).

**Step 4 — run:** PASS.

**Step 5 — commit:** `git commit -m "feat(autopilot): awaiting_confirmation pause flag + CLI await action"`

---

### Task 5 — Lock SubagentStop policy (leaf workers may stop)

**Files:** modify/extend `tests/test_lifecycle_hooks.py`; (no code change to `subagent_stop.py` — it stays observe-only).

**Step 1 — failing test:**

```python
def test_subagent_stop_never_emits_block_even_when_autopilot_active(tmp_path):
    _seed_active_autopilot(tmp_path, phase="implement")
    proc = _run_hook("subagent_stop.py", tmp_path,
                     '{"reason":"end_turn","phase":"gate","subagentType":"omg-executor"}')
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""        # observe-only: no decision JSON
```

**Step 2 — run:** PASS if already observe-only (documents intent); if anyone later adds a block, this FAILs — that is the lock.

**Step 3 — impl:** none (or a one-line comment in `subagent_stop.py`: "observe-only by policy: leaf workers must return to parent; only the parent Stop gate pins"). 

**Step 4 — run:** PASS.

**Step 5 — commit:** `git commit -m "test(hooks): lock SubagentStop as observe-only (leaf workers may stop)"`

---

### Task 6 — Supersede ADR + rewrite skill/rules/docs (honest Stop pin)

**Files:** modify `docs/research/stop-continuation/stop-continuation-decision.md` (status → **SUPERSEDED**), `skills/omg-autopilot/SKILL.md`, `templates/omg-rules.md`, `docs/autopilot.md` (+ `.zh.md` / `.zh-TW.md`), `CLAUDE.md`; tests `tests/test_docs_cli_drift.py`, `tests/test_skill_inventory.py`.

**Step 1 — failing tests:**

```python
def test_skill_no_longer_claims_stop_nonblocking():
    body = (ROOT/"skills/omg-autopilot/SKILL.md").read_text()
    assert "No Stop hard-pin" not in body
    assert "8" in body and "cap" in body.lower()      # honest cap mention
    assert "ask_user_question" in body

def test_adr_is_superseded():
    adr = (ROOT/"docs/research/stop-continuation/stop-continuation-decision.md").read_text()
    assert "SUPERSEDED" in adr and "0.2.107" in adr

def test_rules_forbid_midphase_questions():
    rules = (ROOT/"templates/omg-rules.md").read_text()
    assert "do not ask" in rules.lower()
```

**Step 2 — run:** FAIL.

**Step 3 — impl (docs/prose only):**
- **ADR:** prepend a `## SUPERSEDED (2026-07-26)` block: grok ≥0.2.107 honors Stop `decision:block` (cite `stop_gate.rs` cap 8, `10-hooks.md`, e2e). Keep the old text below for history. New decision: **BUILD Stop pin (primary)**, honesty bounds (8/turn, fail-open, Esc skips, not infinite). Explicitly note `TurnControl::ForceContinue` is stubbed/unused (D17) and is **not** relied upon.
- **SKILL.md:** replace "No Stop hard-pin" / "Host-forced continuation: Not available" / anti-pattern "Claiming Stop hooks keep the session alive" with: "**Stop gate pins the turn while autopilot is incomplete** (host-honored ≥0.2.107); honest caps: 8 continuations/turn then the turn ends; fail-open on hook crash; Esc/Ctrl+C skip Stop. Pause only for interview (`ask_user_question`) / destructive confirmation via `omg autopilot await`. Beyond the cap: `omg autopilot run --resume` or `/loop`." Add the persistence-honesty table row: "Host-forced continuation on Stop → **Available ≥0.2.107 (cap 8/turn)**".
- **templates/omg-rules.md** `<workflow_routing>`: HARD RULE — "Inside ralplan/implement/review/qa/rework/acceptance: **do not ask the user**. Record uncertainty under `.omg/artifacts/` and keep working, or `omg autopilot transition --phase blocked`. The Stop gate will reinject this rule if you try to stop mid-phase."
- **docs/autopilot.md** (+zh twins): add "OMC feel → OMG equivalent" table (Stop pin primary; `/goal` secondary; `omg autopilot run`/`/loop` tertiary) + honesty section.
- **CLAUDE.md:** fix SessionStart/RESUME.md drift (SessionStart is passive; CLI writes RESUME.md) — pairs with Task 13.
- Regen lock if skill hashes are covered: `python3 scripts/generate_capabilities_lock.py`; note `omg setup` refreshes `~/.grok/rules/omg.md` (docs only, not CI).

**Step 4 — run:** PASS (+ `python3 scripts/check_docs_links.py`).

**Step 5 — commit:** `git commit -m "docs: supersede Stop ADR; skill/rules now describe honest Stop pin"`

---

### Task 7 — `omg doctor` min grok version ≥ 0.2.107

**Files:** modify `omg_cli/doctor.py`; test `tests/test_doctor.py`.

**Step 1 — failing tests:**

```python
def test_parse_grok_version():
    assert parse_grok_version("grok 0.2.112 (abc)") == (0, 2, 112)
    assert parse_grok_version('{"version":"0.2.107"}') == (0, 2, 107)
    assert parse_grok_version("garbage") is None

def test_check_grok_version_fails_below_min(monkeypatch):
    monkeypatch.setattr("omg_cli.doctor._probe_grok_version", lambda: (0, 2, 99))
    name, ok, detail = check_grok_version()
    assert ok is False and "0.2.107" in detail

def test_check_grok_version_ok_at_min(monkeypatch):
    monkeypatch.setattr("omg_cli.doctor._probe_grok_version", lambda: (0, 2, 112))
    assert check_grok_version()[1] is True
```

**Step 2 — run:** FAIL.

**Step 3 — impl:** `parse_grok_version(text)` (regex `\d+\.\d+\.\d+`, also accept JSON `{"version":...}`); `_probe_grok_version()` tries `grok version --json` then `grok --version` via `_run_grok_json`/subprocess, returns tuple or `None`; `check_grok_version()` → FAIL if `< (0,2,107)` ("Stop gate requires grok ≥0.2.107; installed X"), WARN if unparseable, OK otherwise. Register as a **hard** check. *(Implementer: confirm the exact `grok version --json` key empirically; the regex fallback makes this robust either way.)*

**Step 4 — run:** PASS.

**Step 5 — commit:** `git commit -m "feat(doctor): require grok >= 0.2.107 for Stop gate"`

---

### Task 8 — Drift guard: freeform chatty question → block once (optional, env-gated)

**Files:** modify `omg_cli/stop_gate.py`; test `tests/test_stop_gate.py`.

**Why (C14):** the host cannot hard-forbid freeform "Should I continue?" prose. Soft guard: when the final assistant message looks like a chatty yes/no question and we have **not** already nudged this turn (`stopHookActive` false), block **once** with a "don't ask, keep working" reason.

**Step 1 — failing tests:**

```python
def test_drift_guard_blocks_chatty_question_once(tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_STOP_DRIFT_GUARD", "1")
    ev = _mk(tmp_path, "implement", last_msg="Should I continue with the tests?",
             stop_hook_active=False)
    d = decide_stop(tmp_path, ev)
    assert d and d["decision"] == "block" and "do not ask" in d["reason"].lower()

def test_drift_guard_does_not_refire_when_stop_hook_active(tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_STOP_DRIFT_GUARD", "1")
    # active autopilot still blocks via the main path, but the *drift* reason
    # must not loop: with stopHookActive true we use the escalation reason, not a fresh chatty block
    ev = _mk(tmp_path, "implement", last_msg="Shall I proceed?", stop_hook_active=True)
    d = decide_stop(tmp_path, ev)
    assert "already" in d["reason"].lower()

def test_drift_guard_off_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("OMG_STOP_DRIFT_GUARD", raising=False)
    # no active autopilot, chatty message -> allow stop (guard disabled)
    ev = {"reason": "end_turn", "lastAssistantMessage": "Want me to keep going?"}
    assert decide_stop(tmp_path, ev) is None
```

**Step 2 — run:** FAIL.

**Step 3 — impl:** add `_CHATTY_RE` (e.g. `\b(should i|shall i|would you like|do you want me|want me to|may i)\b.*\?` case-insensitive, last ~200 chars). In `decide_stop`, when enabled and `stopHookActive` is false and `_CHATTY_RE.search(lastAssistantMessage)` → return a block with the no-mid-phase-questions reason ("block once" is enforced because the re-fire has `stopHookActive:true`). Default off; opt-in via `OMG_STOP_DRIFT_GUARD=1`.

**Step 4 — run:** PASS.

**Step 5 — commit:** `git commit -m "feat(stop-gate): optional drift guard for freeform chatty questions"`

---

### Task 9 — Graceful force-stop near the cap (optional, diagnostic counter)

**Files:** modify `omg_cli/stop_gate.py`; test `tests/test_stop_gate.py`.

**Why:** the host hard-caps at 8 and annotates "limit reached". Optionally end **gracefully** a bit earlier with a `continue:false` + resume hint, so the operator gets a clean cross-turn hand-off (B6/B10) instead of the host annotation.

**Note on single-writer:** the counter is **diagnostic observability**, not run authority — it lives under `.omg/state/stop_gate/<session>.json` and never touches `status`/`passes`/`verified`. Default **off** (`OMG_STOP_GRACEFUL_CAP` unset).

**Step 1 — failing tests:**

```python
def test_graceful_force_stop_at_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("OMG_STOP_GRACEFUL_CAP", "6")
    _seed_active_autopilot(tmp_path, phase="implement")
    # simulate 6 prior continuations this turn via the diagnostic counter
    _seed_stop_counter(tmp_path, session="session-1", n=6)
    ev = _mk(tmp_path, "implement")
    d = decide_stop(tmp_path, ev)
    assert d["continue"] is False and "autopilot run --resume" in d["stopReason"]

def test_no_counter_write_when_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv("OMG_STOP_GRACEFUL_CAP", raising=False)
    ...  # decide_stop does not create the counter file
```

**Step 2 — run:** FAIL.

**Step 3 — impl:** when `OMG_STOP_GRACEFUL_CAP` is set, read/increment the per-session diagnostic counter (keyed by `GROK_SESSION_ID`, reset when `stopHookActive` is false i.e. a fresh turn) and, at the cap, return `{"continue": False, "stopReason": "Stop-pin reached N continuations this turn; continue cross-turn: omg autopilot run --resume <run_id> (or /loop …)"}`. Keep it fail-open and off by default.

**Step 4 — run:** PASS.

**Step 5 — commit:** `git commit -m "feat(stop-gate): optional graceful force-stop near continuation cap"`

---

### Task 10 — `omg autopilot run` outer driver (tertiary, cross-turn/headless) + context pack

**Files:** modify `omg_cli/autopilot.py` (`autopilot_context_pack`, `build_phase_prompt`, `run_autopilot`), `omg_cli/main.py` (`run` subcommand), `docs/skills.md` (+zh); tests `tests/test_autopilot.py`, `tests/test_docs_cli_drift.py`. Reuse `omg_cli.modes._launch_grok` / `build_grok_argv`.

**Step 1 — failing tests:**

```python
def test_autopilot_context_pack_names_phase_and_gate():
    pack = autopilot_context_pack(run_id="r1", phase="review", goal="g",
                                  next_gate="CLI stages/structured_review.json clean")
    assert "phase=review" in pack and "structured_review.json" in pack

def test_build_phase_prompt_maps_skill_and_forbids_questions(tmp_path):
    text = build_phase_prompt("implement", root=tmp_path, goal="g", run_id="r1")
    assert "ultrawork" in text.lower() or "implement" in text.lower()
    assert "do not ask" in text.lower()

def test_run_autopilot_walks_to_verified_with_mocked_launches(tmp_path, monkeypatch):
    launches = []
    monkeypatch.setattr("omg_cli.modes._launch_grok",
                        lambda **kw: (launches.append(kw) or _stamp_gate_for(tmp_path, kw) or 0))
    rc = run_autopilot(tmp_path, "add pure add(a,b) with test", skip_interview=True)
    assert rc == 0
    assert status_autopilot(tmp_path, _rid(tmp_path))["phase"] == "verified"
    assert launches

def test_run_autopilot_pauses_at_interview(tmp_path, monkeypatch):
    rc = run_autopilot(tmp_path, "vague idea")     # no skip
    assert rc == 0 and status_autopilot(tmp_path, _rid(tmp_path))["phase"] == "interview"

def test_cli_autopilot_run_listed_in_skills_md():
    assert "omg autopilot run" in (ROOT/"docs/skills.md").read_text()
```

**Step 2 — run:** FAIL.

**Step 3 — impl:** `run_autopilot(root, goal, *, skip_interview=False, resume_run_id=None, max_phase_cycles=5, dry_run=False, timeout=None, **launch_kw) -> int`: start/load run; write pid metadata (for `omg cancel`); loop { read phase; `verified`→0; interview-incomplete→write RESUME + print resume cmd + 0; terminal blocked/cancelled→1; build prompt; `_launch_grok`; inspect stamps; `transition`/`complete_with_acceptance`; bump cycles → `blocked` past max; refresh RESUME each phase }. Wire `main.py` `run` parser (`--skip-interview --resume --max-phase-cycles --dry-run --timeout --yolo/--safe`). Update `docs/skills.md` (+zh) so the docs-CLI drift test passes.

**Step 4 — run:** PASS.

**Step 5 — commit:** `git commit -m "feat(autopilot): CLI outer run driver (cross-turn/headless persistence)"`

---

### Task 11 — `--resume` + RESUME `recommend_commands`

**Files:** modify `omg_cli/autopilot.py`, `omg_cli/main.py`, `omg_cli/resume.py`; tests `tests/test_autopilot.py`, `tests/test_resume.py`.

**Step 1 — failing tests:**

```python
def test_run_resume_reenters_current_phase(tmp_path, monkeypatch): ...
def test_recommend_commands_includes_autopilot_run_resume():
    run = {"mode": "autopilot", "run_id": "r1", "status": "running",
           "autopilot_phase": "review"}
    assert any(c.startswith("omg autopilot run --resume") for c in recommend_commands(run))
```

**Step 2–4:** mirror `run_mode(resume_run_id=...)`; extend `resume.recommend_commands` autopilot branch.

**Step 5 — commit:** `git commit -m "feat(autopilot): resume driver + RESUME.md command"`

---

### Task 12 — Goal-bound acceptance (close analyze-only false-green)

**Files:** modify `omg_cli/command_policy.py` (`is_analyze_only`), `omg_cli/autopilot.py`/`omg_cli/acceptance.py` (refuse path); tests `tests/test_command_policy.py`, `tests/test_acceptance.py`, `tests/test_autopilot.py`.

**Step 1 — failing tests:**

```python
def test_is_analyze_only_detects_analyze_plus_unit():
    assert is_analyze_only([["flutter","analyze","lib"],
                            ["flutter","test","test/foo_test.dart"]]) is True
    assert is_analyze_only([["flutter","test","test/foo_test.dart"],
                            ["python3","-m","pytest","-q"]]) is False  # real test run present

def test_autopilot_complete_rejects_analyze_only_acceptance(tmp_path):
    # freeze an analyze-only manifest on an autopilot run -> complete_with_acceptance raises
    ...
```

**Step 2–4:** `is_analyze_only(commands)` builds on `command_basename` + the existing `_FLUTTER_ALLOWED`/`_DART_ALLOWED`/pytest knowledge: true when every command is lint/analyze/format-only with no goal-bound test/build/run. For `mode=="autopilot"` only, refuse `verified` when acceptance is analyze-only unless break-glass `OMG_ALLOW_SOFT_ACCEPT=1` (TTY) or explicit flag; the error tip says how to add a goal-bound command.

**Step 5 — commit:** `git commit -m "fix(autopilot): refuse analyze-only verified for autopilot runs"`

---

### Task 13 — SessionStart passive RESUME refresh + CLAUDE.md drift fix

**Files:** modify `hooks/bin/session_start.py` (call `write_resume_md` fail-open), `CLAUDE.md`; test `tests/test_lifecycle_hooks.py`, `tests/test_resume.py`.

**Step 1 — failing test:**

```python
def test_session_start_refreshes_resume_for_active_autopilot(tmp_path):
    _seed_active_autopilot(tmp_path, phase="review")
    proc = _run_hook("session_start.py", tmp_path, '{"event_id":"s1"}')
    assert proc.returncode == 0
    assert (tmp_path/".omg/state/RESUME.md").is_file()   # passive side-effect
    # stdout still ignored for control; never block; never verify
```

**Step 2–4:** import `omg_cli.resume.write_resume_md`; call it for an active non-terminal autopilot run; wrap in try/except fail-open; **no stdout control**, **never** verify. Update `CLAUDE.md` to match (SessionStart = passive refresh; CLI remains the authoritative RESUME writer).

**Step 5 — commit:** `git commit -m "feat(hooks): SessionStart passively refreshes RESUME.md for active autopilot"`

---

### Task 14 — Interactive live smoke (gated, not default CI)

**Files:** create `scripts/live_autopilot_smoke.sh` (or extend `scripts/live_suite.sh`); marker `@pytest.mark.live`; evidence under `docs/research/live/` (gitignored).

**Steps:**
1. Scratch repo + `omg autopilot run --dry-run "…"` → asserts argv/state only (no launch).
2. **Live interactive:** start a real `grok` session, `omg autopilot start "add a pure function + a failing-then-passing unit test" --skip-interview`; verify the Stop gate **blocks** mid-phase (scrollback shows `↩ Stop blocked by hook …, continuing`), the turn does **not** end politely, and it reaches `verified` with a **non-analyze-only** accept.
3. **Live interview pause:** `omg autopilot start "vague idea"` (no skip) → verify it pauses for `ask_user_question` (gate yields via `autopilot_awaiting`/interview `waiting_input`) and resumes after the answer.
4. **Cap honesty:** force a trivially-unresolvable gate and confirm the turn ends at 8 with the host annotation (or earlier with the Task-9 graceful `continue:false` when enabled) — record that the pin is **not** infinite.
5. Save evidence; **do not** claim ship until green.

**Commit:** `git commit -m "test(autopilot): gated interactive live smoke for Stop pin"`

---

## 4. Non-goals & honesty limits (non-negotiable)

**Non-goals:**
- Claiming the pin is **infinite**, immune to **Esc/Ctrl+C**, immune to **hook crash**, or immune to the **8/turn cap**. It is none of these.
- Letting agents/hooks write `passes`/`verified` (CLI-only after acceptance).
- Weakening PreToolUse fail-open into a fake sandbox.
- Building on `TurnControl::ForceContinue` (D17 — stubbed/unused).
- Blocking a **subagent's** stop (leaf workers must return — A2).
- Blocking the **session-end** Stop fire (decision is discarded by the host anyway).

**Honesty limits (must appear in skill/docs/doctor):**
- **Cap:** 8 continuations per turn; then the host forces `AllowStop` and the hook is not consulted. The counter is **per turn** — the next user prompt (or `/loop`/bg-wake/`omg autopilot run`) starts fresh.
- **Fail-open:** a crashed/timed-out/malformed hook → the turn ends normally. Our gate exits 0 always.
- **Skips:** Esc/Ctrl+C, refusals, and max-turns turns skip Stop entirely; API-error turns fire `StopFailure` (observe-only).
- **Freeform questions** cannot be hard-forbidden by the host — only soft skill rules + the optional drift guard.
- **`--no-ask-user`** removes structured `ask_user_question` but not prose questions.

---

## 5. PR slices

| PR | Tasks | Theme |
|----|-------|-------|
| **PR1 — Stop pin spine** | 1, 2, 3, 4, 5 | Pure gate → wired hook → timeout → awaiting flag → SubagentStop lock. **This alone delivers the OMC feel in-session.** |
| **PR2 — Honesty & guards** | 6, 7, 8, 9 | ADR/skill/rules supersede, doctor version gate, drift guard, graceful cap. |
| **PR3 — Cross-turn & acceptance** | 10, 11, 12, 13 | `omg autopilot run` outer loop, resume, goal-bound acceptance, SessionStart RESUME. |
| **PR4 — Live evidence** | 14 | Interactive smoke; no marketing until green. |

**Per-PR verification:**
```bash
PYTHONPATH=. .venv/bin/python -m pytest -q -m "not live"
python3 scripts/generate_capabilities_lock.py --check   # if skills/agents changed
python3 scripts/check_docs_links.py
grok plugin validate .
# after install-path changes: ./scripts/install-plugin.sh && omg doctor
```

---

## 6. Mechanism coverage checklist (proof nothing in A–D was forgotten)

| Mechanism | Disposition | Task(s) |
|---|---|---|
| **A1** Stop hook gate | **PRIMARY — implemented** | 1, 2, 3 |
| **A2** SubagentStop gate | **Declined for pinning** (observe-only lock) | 5 |
| **A3** `/goal` in-turn Continue | **Secondary, host-native** (documented) | 6 |
| **A4** Tool-call loop | Implicit (reason instructs continued tool use) | 1 |
| **A5** Mid-turn auto-compact | Declined (host-managed; documented) | 6 |
| **B6** `/loop` + `scheduler_create` → SchedulerFired | **Tertiary, optional** (documented + graceful-cap hint) | 6, 9 |
| **B7** Background task complete → auto-wake | **Used** (gate allows stop when `backgroundTasks` non-empty) | 1 |
| **B8** Goal post-turn summary queue | Declined (host-native) | — (noted §1) |
| **B9** Queued follow-ups / plan-resume | Declined (host-native) | — (noted §1) |
| **B10** Outer CLI relaunch | **Tertiary — `omg autopilot run`** | 10, 11 |
| **C11** `ask_user_question` | **Interview mechanism** (gate yields) | 4, 6, 14 |
| **C12** Tool permission / plan approval | Declined (host-native) + `autopilot_awaiting` cooperation | 4 |
| **C13** `--no-ask-user` / env / deny | Declined (host config; documented interaction) | 6 |
| **C14** Freeform "Should I continue?" | **Drift guard — block once** (optional) | 8 |
| **D15** SessionStart/UserPromptSubmit/PostToolUse/PreCompact | Declined as control; SessionStart passive RESUME refresh | 13 |
| **D16** PreToolUse (allow/deny only) | Declined for continuation (unchanged spawn fail-closed) | — (noted §1) |
| **D17** `TurnControl::ForceContinue` | **Declined explicitly** (stubbed/unused; ADR note) | 6 |
| **D18** Rules/skills (context only) | **Used as soft rules** (no-mid-phase-questions) | 6 |

**Every mechanism A1–D18 has an explicit disposition and owner. None omitted.**

---

### Synthesis verdict

| Source | Keep |
|---|---|
| **OMC** | UX target (hands-off except interview) **and now the mechanism** — Stop `decision:block` pin, honestly capped |
| **OMX** | Phase contract + fail-closed destination gates + rework-vs-return-to-ralplan |
| **Grok host (0.2.112)** | Stop **is** a blocking gate (cap 8/turn, fail-open, `stopHookActive`, `backgroundTasks`, `continue:false`); `/goal` before gate; `ask_user_question` for interviews |
| **Qwen / prior draft** | `omg autopilot run` + `/loop` as **tertiary** cross-turn; goal-bound acceptance; non-goals — **but invert the spine**: Stop pin primary, not CLI loop |
| **2026-07-20 ADR** | **Superseded** — its "host cannot honor Stop block" premise is false for ≥0.2.107 |

**Highest leverage: PR1 (Tasks 1–2).** Until `hooks/bin/stop.py` emits a real `decision:block`, interactive autopilot will keep "politely stopping" mid-goal even though the FSM is correct — the exact crisis the user reported.
