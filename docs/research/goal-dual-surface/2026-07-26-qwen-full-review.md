# Qwen full review (unconstrained) — 2026-07-26

Model: `qwen3.8-max-preview` (read-only). Final stdout is a source-verified **addendum** after Qwen's own deep-dive into grok-build + OMX; it supersedes/corrects Qwen's first pass.

Both deep-dive agents returned source-verified evidence (grok-build `turn.rs`/`goal_tracker.rs` and OMX's 20k-line `codex-native-hook.ts`). They confirm the core thesis, correct **one error of mine**, and sharpen the architecture materially. Here is the addendum — read it on top of the original review.

---

# Addendum — source-verified corrections and deltas

## A. Corrections to the original review

1. **My error — "OMX has no Stop hooks" is wrong.** I globbed `hooks/**` at the OMX root and missed it. OMX's Stop hook is `src/scripts/codex-native-hook.ts` (~20,633 lines), registered via `plugins/oh-my-codex/hooks/hooks.json` → `dist/scripts/codex-native-hook.js`, requiring `[features].plugin_hooks = true` and `[features].goals = true` in `.codex/config.toml`. Codex CLI *does* have native Stop hooks with the same `decision:"block"` + `reason` contract. The correct statement: **OMX has a Stop hook, but it never blocks on pending ledger stories** — see A.3.
2. **Understated — `/goal` doesn't just run "before" the Stop gate; while a goal is Active it completely bypasses it.** Source `turn.rs:838-895`: round completes → if `goal_active`, run `run_goal_round_end()` (or `_legacy()`); on `GoalRoundDecision::Continue(directive)` the harness injects the continuation as a user message and `continue`s the loop — **`run_stop_gate` is never reached**. The Stop gate only runs once the goal loop *releases* (`Complete`/`BudgetLimited`/paused/`Blocked`/evaluator infra-error). Consequence: while `/goal` is Active, OMG's Stop pin is literally unreachable. The two caps are independent: goal loop has its own backoff (`GOAL_CONTINUATION_BACKOFF_THRESHOLD=3` → `BackOffPaused`; classifier cap default 10, `goal_classifier.rs:39`) vs Stop's `MAX_STOP_HOOK_CONTINUATIONS_PER_TURN=8` (`stop_gate.rs:9`, now source-verified, not just doc-cited).
3. **The decisive OMX fact — its Stop hook enforces mode state + false-completion claims, not the ledger.** `buildStopHookOutput` (line 19265) check order: stop-exempt (`cancel|abort|context|compact|limit`) → ralph completion-audit / active modes → `buildModeBasedStopOutput("autopilot"|"ultrawork"|"ultraqa")` (blocks while phase non-terminal: *"OMX ${mode} is still active (phase: ${phase}); continue the task and gather fresh verification evidence before stopping"*) → **`buildGoalWorkflowReconciliationStopOutput` (~3140): the only ultragoal-specific block — conditional, fires only when the last assistant message looks like a goal-completion claim** (`update_goal(`, `omx ultragoal checkpoint|complete`, "the goal is complete") **with an unreconciled goal workflow** → demands `get_goal` snapshot reconciliation → `buildCompletedGoalCleanupStopOutput` (3051) blocks `create_goal` over a completed aggregate without `/goal clear` → ordinary no-progress guard (8 repeats over ≥10 min idle, lines 209-211). Escape hatches: `stop_hook_active` dedup (unchanged signature → allow stop), terminal-phase fall-through, deep-interview wait yield. Agent's blunt summary: *"If oh-my-grok's design assumes 'Stop denied while any goal pending,' that is **stricter than OMX**."*
4. **`/goal` is single-goal only** — `GoalTracker` holds one `Option<GoalOrchestration>` (`goal_tracker.rs:698`); `create_goal` **replaces** any existing goal (rescue-then-remove contract, `goal_tracker.rs:907`; tests `create_goal_replaces_existing`). No priorities, no multiple goals, no declarative completion criteria — completion is judged by an independent evaluator + adversarial skeptic panel (default 3, `GROK_GOAL_VERIFIER_N` 1-5) reading the objective text.
5. **Open question 2 answered:** `/goal` defaults **on** — `GROK_GOAL` env / `[features] goal_enabled`, "Absent ⇒ client default (enabled)" (`config-types/src/lib.rs:606`; pager test `goal_slash_presession_disabled.yaml` confirms `GROK_GOAL=0` hides it). Open question 5 answered: **no goal state in any hook payload** — the separation is total; there is no documented machine-readable goal-status surface at all (`/goal status` prints to UI only).
6. **Goal durability nuance:** goal state persists to the session dir and is restored on resume (`from_snapshot`, `goal_tracker.rs:721`), but on restart an in-flight `Active` goal is **demoted to `UserPaused`** (phases reset to Idle — "subagents don't survive a restart"). So `/goal` survives sessions but **resumes paused** → handoff must mention `/goal resume`, not just re-set.
7. Version intro still unknown: source tree is **0.2.110** (`xai-grok-shell/Cargo.toml`); neither agent could run `git log -S '/goal'` (read-only, no shell). The `≥0.2.94` claim remains unverified — implementer should run `git log -- crates/codegen/xai-grok-shell/src/session/goal_tracker.rs` in grok-build.

## B. Fact-table deltas

**Grok column — replace/augment:**
- Host goal surface: **single** session-scoped goal; `/goal` parsed from the **prompt text itself** (`slash_commands.rs:1113`, intercepted in `handle_prompt` `turn.rs:358` — `GoalSet` creates the goal *and* replaces the prompt with a goal-setup reminder). Statuses: `Active, UserPaused, BackOffPaused, NoProgressPaused, InfraPaused, Blocked, BudgetLimited, Complete`. Completion = evaluator verdict `Continue|CandidateComplete|Blocked` + skeptic panel; plus a **premature-stop detector** (`goal_stop_detector.rs`) that pattern-matches bail phrasing ("Giving up", "Stopping here", "VERDICT: PASS") and tags the continuation.
- Order vs Stop: **bypass, not sequence** (A.2). Stop pin is reachable only after goal release.
- External mutability: **only by sending `/goal …` as a prompt turn** through the normal prompt channel. No CLI flag (headless flag table has no `--goal`), no IPC. Persisted `goal_mode_state` JSON exists (`storage/mod.rs:706`) but is an **undocumented internal** — hand-editing races the actor.
- `/loop`: prompt-only rewrite → model calls `scheduler_create`; fires immediately then repeats; **min 60s, max 50 tasks, 7-day expiry**; each firing = fresh turn = fresh Stop counter; Stop input carries `sessionCrons`. Orthogonal to `/goal`.

**OMX column — replace:**
- Stop hook: **exists** (A.1/A.3); blocks on live mode state + false completion claims; per-mode caps (`max_iterations: 10` autopilot; 3-review-cycle escalation); no-progress guard 8/10min; `stop_hook_active` dedup.
- Autopilot↔ultragoal: **supervised child phase, not a peer** — *"keep `mode:"autopilot"` active and set the supervised phase to `current_phase:"ultragoal"`; do not start a peer Autopilot replacement"*; autopilot never reads `ledger.jsonl`, carries references in `handoff_artifacts.ultragoal`.
- Anti-double-orchestration is **structural, not documented**: one ledger + one aggregate pointer objective + one leader + one parent phase; approved mode overlaps only `team+ralph` / `team+ultrawork`; worker mutation refused (`assertUltragoalMutationAllowedFromCurrentProcess`: *"Ultragoal state is leader-owned; workers must report checkpoint evidence upward"*).
- Zero "Grok" mentions anywhere in OMX — no cross-host parity claims exist to borrow.

## C. Architecture deltas (the important part)

**C.1 — The two mechanisms are complementary by runtime construction, not merely non-colliding.** While `/goal` is Active it owns continuation (toward *goal completion*: evaluator next-step + skeptic gaps injected every round); the moment it releases, OMG's Stop pin owns continuation (toward *autopilot gates*: review/QA/acceptance). Goal-done ≠ autopilot-verified, so the pin still earns its keep exactly when `/goal` stops firing. **Docs consequence (fold into P0-3/P0-6):** `docs/autopilot.md`'s "Stop pin primary / `/goal` secondary" describes OMG *ownership*, not *runtime precedence* — add one sentence: "When a host `/goal` is Active, it dominates continuation and the Stop gate is not consulted until the goal releases; the Stop pin then enforces remaining autopilot gates."

**C.2 — Aggregate mode goes from "recommended" to "the only sane default."** Grok holds exactly one goal and `create_goal` replaces. OMX's pointer-objective rationale now has hard source backing: the `/goal` condition should be a stable pointer ("complete ultragoal G per `.omg/ultragoal/goals/<G>/snapshot.json`, including later added stories; evidence under `.omg/artifacts/`; done when `omg goal status --goal G` shows verified") — never an enumeration of story ids. Per-story mode is mechanically possible in Grok (replace-on-set is cleaner than Codex's create-fails-if-exists; no OMX-style `--status blocked` workaround needed) but should stay opt-in and probe-gated (§E.3).

**C.3 — Encode acceptance into the objective string.** Grok completion criteria are implicit (skeptic panel judges the objective text). The P1-1 handoff's suggested `/goal` condition should embed the current story's acceptance text so the host's own adversarial review pressures the model against OMG's criteria. This is the strongest version of the "evidence alignment" point in original §3.

**C.4 — The outer CLI loop is the legitimate `/goal` re-arm point.** Since `/goal` is set by sending it as a prompt turn, `omg ralph` / the future `omg autopilot run` (which relaunch `grok` with `--rules` + a prompt) can **seed `/goal <aggregate pointer>` as the first prompt of each fresh session** — without ever "mutating host state from the shell mid-session" (it's the documented prompt channel). This upgrades A3 from docs-only to a real, honest wiring for the cross-turn surface — *conditional on* the headless slash-parsing probe (§E.1). Until then: handoff text only.

**C.5 — New cheap mechanism, OMX-faithful: completion-claim reconciliation guard.** OMG's `stop_gate.py` already has the drift-guard shape (regex on `lastAssistantMessage`, block once, fail-open, env-gated). Add an optional guard for active unverified ultragoals: if the last message looks like a completion claim ("done", "verified", "goal complete", `omg goal verify`) but `goal_status` shows unverified stories → block once with "reconcile: `omg goal status --goal G`; checkpoint evidence or `block-story`; never claim verified from prose". This is exactly OMX's `buildGoalWorkflowReconciliationStopOutput` adapted, it directly enforces OMG's existing anti-pattern list ("Fake `verified` in prose"), and it is *not* a per-story Stop pin (no 50-reinforcement loop; yields to all existing predicates; block-once via `stopHookActive`).

## D. Work-item changes

**P0 (unchanged scope, better content):**
- P0-1's 3-row table now has source-verified cells (use A.1-4 / B). Add the runtime-precedence sentence (C.1) to `docs/autopilot.md` (+zh×2) and `templates/omg-rules.md`.
- P0-7 (autopilot.py docstring): phrase the relationship OMX-style — "ultragoal is an optional **supervised ledger** inside implement/qa; autopilot owns the parent phase; no peer coordinator" — instead of "Composes … ultragoal/impl".

**P1-1 (handoff) — content updates from source facts:**
- "Check `/goal status` first; **setting `/goal` replaces any active goal** — `/goal clear` or finish it first" (replace-on-set, `goal_tracker.rs:907`).
- "After a session restart the goal resumes **paused** — run `/goal resume`" (UserPaused demotion).
- Embed the ready story's **acceptance text** in the suggested condition (C.3), plus the pointer to `snapshot.json` + `.omg/artifacts/`.
- If `/goal status` shows a pause status (`BackOffPaused`/`NoProgressPaused`/`Blocked`), tell the model to `omg goal block-story` with the host's gaps as reason (host pause → ledger block bridge).
- Never claim the CLI set the goal; never reference the internal `goal_mode_state` JSON.

**New P1-5 — completion-claim reconciliation guard** (C.5): `omg_cli/stop_gate.py`, env-gated (`OMG_STOP_GOAL_CLAIM_GUARD`, default off like drift guard initially), runs after all yield predicates, reads `goal_status` only when the claim regex matches (keep it fast), block-once, fail-open. Acceptance: hermetic tests (claim + unverified goal → block with `omg goal status` in reason; verified goal → None; `stopHookActive` → no re-fire; disabled by default).

**P2-1 (autopilot run) — add:** optional `--arm-goal` (or auto when a goal is linked) to seed `/goal <pointer>` as the first prompt turn of each relaunch (C.4), gated by the §E.1 probe result.

**New P2-7 — mode-overlap policy doc:** OMX approves only `team+ralph` / `team+ultrawork` and otherwise "preserve state, direct operator to clear". OMG has no documented overlap matrix (autopilot active + ralph active + goal ledger active + `/goal` armed — who wins?). One table in `docs/` + a `omg state` warning on unsupported overlaps.

**Drop/deprioritize:** P2-6 (doctor goal-feature soft check) — `/goal` defaults on; keep only as a "detect explicit `GROK_GOAL=0`" nicety.

## E. Updated open questions (reordered)

1. **#1 priority (agent-flagged): does headless `grok -p "/goal …"` parse the slash and run the goal loop within one invocation?** OMG drives grok via CLI (`ralph`, future `autopilot run`); the headless doc never mentions slash commands. This gates C.4/P2-1. Also test `-r`/`-c` + `/goal resume` continuity.
2. Which version introduced `/goal` — needs `git log -S '/goal' -- crates/codegen/xai-grok-shell/src/session/goal_tracker.rs` in grok-build (static files don't say; source is 0.2.110, installed docs 0.2.112-era).
3. Sequential per-story in one session: `/goal clear` → `/goal <new>` behavior (replace-on-set is source-verified; live UX of clear/re-set and history rendering still worth one probe before enabling per-story mode).
4. Legacy driver (`GROK_WORKFLOWS=0`, model-facing `update_goal`) pressure characteristics — weaker/different than the host-owned evaluator? Matters only if users disable workflows.
5. `--budget` accounting precision under long runs + compaction interaction (best-effort per source comments).
6. Multi-session coordination toward one shared ledger — entirely OMG's problem (each session holds its own single goal); informs the P2-7 overlap policy.

## F. One-line summary of the delta

The original recommendation stands and gets stronger: **`/goal` and the Stop pin are complementary by construction (goal loop bypasses the gate while Active; pin enforces gates after release); Grok's single-goal + replace-on-set + implicit-criteria design makes the aggregate pointer objective the only sane default; the two honest wirings OMG is missing are the ledger→handoff text (P1-1) and an OMX-style completion-claim reconciliation guard (P1-5) — not a per-story Stop pin, which is stricter than both OMC's and OMX's actual behavior.**
