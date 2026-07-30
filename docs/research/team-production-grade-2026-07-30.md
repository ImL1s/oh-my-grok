# OMG Team + orchestration: production-grade research (non-experimental)

**Date:** 2026-07-30  
**Goal:** Define what “not experimental” means for `omg team` and related mechanisms, grounded in **OMC / OMX / OmO** references, without fake full-surface clone.  
**Product rule:** Grok-native, self-contained runtime (no OMC/OMX dependency). Sibling ideas only.

---

## 1. Executive answer

| Question | Answer |
|----------|--------|
| Is OMG tmux team “complete” today? | **Production path yes** (default-on, process-ready, P0′ API, doctor, **LIVE_TEAM_SMOKE_OK** 2026-07-30) — **not** full OMX clone |
| Can it become production without cloning OMC/OMX? | **Yes** — adopted *contracts* (lifecycle, CLI-first API, worktree seal, readiness/shutdown), not host quirks |
| What “production” means here | Default-on + kill switch, fixture + live smoke green, worker lifecycle, honest isolation docs, residual ops deferred *explicitly* |
| What we must **not** claim | Full OMX 33-op parity; execution sandbox; OMC Stop infinite stickiness; native Claude implicit-team on Grok |

### Promotion log (shipped)

| Step | Evidence |
|------|----------|
| P0-1 process-ready | PR #62 — `worker-ready` before agent; read-only no longer structural `failed_start` |
| P0′ reliability API + doctor | PR #63 — heartbeat/shutdown/orphan + team plane soft-check |
| Default-on + fixture smoke | PR #64 — `OMG_DISABLE_TMUX_TEAM` kill switch; `FIXTURE_TEAM_SMOKE_OK` |
| Task/events/manifest + live gate | PR #65 — process-ready live path |
| **Grok live smoke** | **2026-07-30 local:** `scripts/live_team_smoke.py --live` → **`LIVE_TEAM_SMOKE_OK`** (startup_status=running, process=2, stop identity verified). JSON under `docs/research/live/` is gitignored — regenerate with the script. |

---

## 2. Three reference stacks (best of each)

### 2.1 OMC — dual plane (critical insight)

OMC does **not** treat “team” as one thing:

| Surface | Workers | Coordination | When |
|---------|---------|--------------|------|
| **`/team`** | Claude Code **implicit agent team** (Agent/Task, in-session) | Native messages + task list + staged pipeline | Default multi-agent in Claude session |
| **`/omc-teams` / `omc team`** | Real **tmux CLI** panes (claude/codex/gemini/agy/grok/cursor) | Files + `omc team api` | Process-based / multi-CLI |

**Best practices to steal for OMG:**

1. **Staged pipeline** `team-plan → team-prd → team-exec → team-verify → team-fix` with handoffs (`.omc/handoffs/`)  
2. **Stage-aware specialists** (planner/executor/verifier/debugger), not N identical shells  
3. **CLI-first mutation** for tmux plane (MCP team tools deprecated)  
4. **Shutdown is blocking** (request → ack → cleanup; orphan scan)  
5. **Role routing snapshot** resolved once at launch, sticky for lifetime  
6. **Pre-assign owners** (no atomic claim race on native task list)  
7. **Handoffs survive cancel** for resume  

**Host-impossible on Grok (do not fake):**

- Implicit Claude agent-teams / TeamCreate semantics  
- OMC Stop hook infinite re-injection (Grok Stop: cap 8/turn, fail-open)  
- Full OMC MCP ~54-tool bridge  

### 2.2 OMX — durable tmux/worktree team (primary CLI reference)

OMX `$team` / `omx team` is the closest model for **OMG’s `omg team`**:

| Mechanism | OMX | OMG today |
|-----------|-----|-----------|
| Gate | Product feature (with app/tmux caveats) | **Default on**; kill `OMG_DISABLE_TMUX_TEAM=1` |
| State root | `.omx/state/team/<name>/` | `.omg/state/runs/<run>/team/…` |
| Identity | `manifest.v2.json`, worker heartbeats | team.json + identity receipt chain (partial) |
| API | Broad `omx team api` surface | **P0′ ~24 handlers** / catalog ~35; residual ops deferred |
| Lifecycle | status / resume / shutdown (force) | start/run/scale/resume/status/collect/stop |
| Worktrees | Optional `--worktree` | Ownership + seal + integrate |
| Preflight | `preflight-context.json` for resume after compaction | Weak / incomplete |
| Doctor | `omx doctor --team` | `omg doctor` soft **team plane** check |
| Ultragoal link | Workers report up; leader owns ledger | Partial (linked flags) |
| Worker skill | Explicit worker preamble + API-only mutations | Exists in seed prompts; uneven |

**Best practices to steal:**

1. **Manifest + heartbeat + mailbox** as three durable surfaces  
2. **No ad-hoc tmux send-keys as primary control plane** — API only  
3. **Completion gate before shutdown** (no in_progress unless abort)  
4. **Force-shutdown with confirm** for stale state  
5. **Preflight context** for compaction-safe resume  
6. **Leader nudge / stall evidence** (OMX notify hook patterns → OMG notify optional)  

### 2.3 OmO / OMA (oh-my-agy) — managed team + claim/revision

OmO exposes a **managed** team FSM: start/status/stop/supervise/reclaim/deliver/tick, mailbox subset, **fork resolution**, worktree + tmux bootstrap.

**Best practices:**

1. Explicit **revision/claim-token** on deliver/reclaim (CAS-style)  
2. **Supervise/tick** loops as first-class CLI (not only skill prose)  
3. Separation: skill = playbook, CLI = exact_env / durable FSM  

---

## 3. OMG current gap matrix (tmux team)

### 3.1 Shipped (experimental)

- Lifecycle commands + shorthand `omg team N[:role] "goal"`  
- `--plan-only` / materialize split (#27)  
- Zero-config grok panes; optional `--routing` multi-CLI  
- P0 API: send-message, mailbox-list/mark-delivered, create/list tasks, claim/transition/release, get-summary, read-config, write-worker-inbox  
- Worktree ownership + seal + integrate  
- Scale / resume / thin `team run` staged driver + optional ralph wrap  
- Live smoke script exists; promotion still **not** claimed  

### 3.2 Missing for “production / non-experimental”

| Gap | Severity | Reference |
|-----|----------|-----------|
| Experimental env gate still required | **Blocker** for “default product” | OMX ships team as feature (with caveats) |
| Live smoke promotion not green / not CI-required | **Blocker** | `LIVE_TEAM_SMOKE_OK` |
| 22/33 API ops unimplemented | High for OMX interop tools | OMX full catalog names already listed |
| No `omg doctor --team` | High | OMX doctor --team |
| Shutdown protocol incomplete vs OMC blocking ack | High | OMC shutdown sequence |
| Heartbeat/events/monitor-snapshot API missing | High | OMX worker status files |
| Preflight-context for resume after compaction | Medium | OMX preflight-context.json |
| Orphan cleanup CLI | Medium | OMC cleanup-orphans / OMX force shutdown |
| Atomic claim hardening evidence | Medium | OMC race notes; OMX claim tokens |
| Provider posture honesty in UX (gemini none) | Medium | security-model already documents |
| In-session Grok “implicit team” analogue | Out of scope / host limit | Use ULW + spawn_subagent |
| Full 33-op clone | Optional | Do **not** block promotion on this |

### 3.3 P0 vs remaining API (fact)

**P0 (11):**  
`send-message`, `mailbox-list`, `mailbox-mark-delivered`, `create-task`, `list-tasks`, `claim-task`, `transition-task-status`, `release-task-claim`, `get-summary`, `read-config`, `write-worker-inbox`

**Not P0 (22):**  
`broadcast`, `mailbox-mark-notified`, `read-task`, `update-task`, `read-manifest`, `read-worker-status`, `read-worker-heartbeat`, `update-worker-heartbeat`, `write-worker-identity`, `append-event`, `read-events`, `await-event`, `read-idle-state`, `read-stall-state`, `cleanup`, `orphan-cleanup`, `write-shutdown-request`, `read-shutdown-ack`, `read-monitor-snapshot`, `write-monitor-snapshot`, `read-task-approval`, `write-task-approval`

**Production minimum API set (recommended P0′):**  
P0 + **heartbeat/status + events append/read + shutdown request/ack + orphan-cleanup + read-manifest + read-task/update-task**.  
Defer: approval dual-write, await-event long poll polish, broadcast (or implement carefully as N DMs).

---

## 4. Definition of Done — “non-experimental team”

Promotion removes `OMG_EXPERIMENTAL_TMUX_TEAM` **or** flips default to enabled with kill-switch `OMG_DISABLE_TMUX_TEAM=1`.

### 4.1 Must (P0 production)

1. **Live gate green**  
   - `OMG_EXPERIMENTAL_TMUX_TEAM=1 python3 scripts/live_team_smoke.py --live` → `LIVE_TEAM_SMOKE_OK`  
   - Evidence archived under `docs/research/live/` (or CI secret path)  
2. **Hermetic suite**  
   - All team unit/integration tests green including claim race, stop kill path (no pkill -f), plan-only purity  
3. **Lifecycle reliability**  
   - ACK ready before success; partial ACK → non-zero + durable diagnostic state  
   - stop kills only recorded session/window/pgid  
   - resume idempotent under scale lock  
4. **Worker contract**  
   - Mutations only via `omg team api`  
   - Identity + owner_token checks fail-closed  
5. **Shutdown**  
   - write-shutdown-request / read-shutdown-ack ops OR equivalent plane method  
   - No state wipe while in_progress unless `--force` + confirm  
6. **Doctor**  
   - `omg doctor` (or `--team`) reports: gate, tmux present, stale sessions, orphan panes, schema version  
7. **Docs honesty**  
   - Still: integration isolation, not execution sandbox; provider posture table  
   - Changelog: “team promoted from experimental” with smoke evidence pointer  

### 4.2 Should (P1)

- Heartbeat + stall detection  
- Events.jsonl append/read  
- Preflight-context resume pack  
- Orphan cleanup command  
- Handoffs for `team run` stages under `.omg/handoffs/team/`  

### 4.3 Could (P2 / never)

- Full 33-op OMX clone  
- OMC-style in-session implicit teams on Grok  
- Uniform sandbox across gemini/agy  
- Infinite Stop stickiness  

---

## 5. Phased plan (implementation order)

### Phase A — Reliability to promote (remove experimental)

| # | Work | Acceptance |
|---|------|------------|
| A1 | Run & fix live_team_smoke --live on grok-only path | `LIVE_TEAM_SMOKE_OK` |
| A2 | Implement shutdown request/ack + force stop confirm | tests + smoke |
| A3 | Heartbeat read/write + stall read | unit + smoke |
| A4 | `omg doctor` team section | doctor --strict soft/hard policy |
| A5 | Docs + CHANGELOG promote; gate default-on or dual-gate | version bump policy |

### Phase B — OMX interop completeness (subset)

| # | Work | Acceptance |
|---|------|------------|
| B1 | P0′ ops: read-task, update-task, read-manifest, worker status/heartbeat, events, orphan-cleanup | golden envelope tests |
| B2 | Preflight-context on launch/resume | resume after delete RESUME.md still works |
| B3 | Broadcast as fan-out DMs (safe) | tests |

### Phase C — OMC pipeline quality on Grok host

| # | Work | Acceptance |
|---|------|------------|
| C1 | `team run` handoffs written per stage | files under `.omg/handoffs/` |
| C2 | Stage agent routing table in skill + CLI | docs match |
| C3 | ULW remains default in-session parallel; team is durable-pane product | skills routing clear |

### Phase D — Other mechanisms (not only team)

| Mechanism | OMC/OMX | OMG now | Production move |
|-----------|---------|---------|-----------------|
| Continuity / don’t-stop | Stop veto, ralph | Stop cap 8 + unattended autopilot | Keep honest; strengthen `omg resume` + autopilot unattended evidence |
| Dual-review / verdict | Strict gates | `verdict.py` hardened | Maintain probes; no false-green |
| Ultragoal / goal | Durable ledger | `omg goal` + host `/goal` pressure | Document dual-surface; no fake host mutation |
| HUD | Live statusline | `omg hud` | Optional richer pack; not authority |
| Notify | Nudges | outbound queue | Never authority for verified |
| Wiki / memory | Project wiki | present | Keep |
| LSP | Host tools | honest E_LSP_* | Never claim proxy |
| Ask advisors | Multi-CLI | `omg ask` | Keep human/broker path |

---

## 6. Architecture recommendation (Grok-honest)

```text
                    ┌─────────────────────────────┐
  User / Grok session│  skills (omg-team playbook) │
                    └─────────────┬───────────────┘
                                  │ CLI-only mutations
                                  v
                    ┌─────────────────────────────┐
                    │  omg team *  (promoted)      │
                    │  plane + api + worktree      │
                    └───────┬─────────────┬───────┘
               grok panes   │             │  optional --routing
            (default prod)  │             │  codex/agy/cursor/gemini
                            v             v
                    integration isolation only
                    (worktree + seal + integrate)
                                  │
                                  v
                         omg accept → verified
```

**Two parallel products remain intentional:**

1. **ULW / spawn_subagent** — default in-session parallel (Grok-native, depth=1, capability_mode)  
2. **tmux team** — durable panes + multi-CLI when operator opts in  

Do not merge them into one fake “team”.

---

## 7. Advisor fan-out

Brief: `.omg/artifacts/team-prod-research-brief.md`  
Advisors (via `omg ask`): Codex / Claude(Fable) / Gemini — artifacts under `.omg/artifacts/ask-team-prod-*.md` when complete.  
Synthesize advisor deltas into this doc’s §8 after collection.

---

## 8. Advisor synthesis (2026-07-30)

| Advisor | Result |
|---------|--------|
| **Claude (Fable path via `omg ask claude`)** | **Primary synthesis** — read `omg_cli/team/*` + live smoke evidence; exit 0 |
| **Codex (`omg ask codex`)** | Timed out at 600s (`exit 4`), truncated; model was gpt-5.6-sol max — no usable structured answer |
| **Gemini (`omg ask gemini`)** | **Auth blocked** (ToS disabled on account) — no answer |

### 8.1 Critical findings (Claude, verified against tree)

1. **ACK ↔ read-only posture structural conflict**  
   - Grok read-only panes use `--permission-mode plan` (no shell).  
   - Seed prompt still requires all roles to shell `omg team api send-message` ACK.  
   - Read-only roles (`verifier`, `critic`, `architect`, …) **cannot** ACK → systematic `failed_start`.  
2. **Readiness is LLM-discretionary**  
   - Success depends on the model voluntarily running a long CLI line within 45s.  
   - Production readiness must be **process-level** (wrapper writes receipt before `exec grok`).  
3. **Live evidence is red**  
   - Only archived live: `docs/research/live/team-smoke-20260725T081001Z.json` → `ok:false`, acks 0/2.  
4. **Cheap wins already coded**  
   - `liveness.py` / `recovery.py` implemented + tests, **not wired** to API/CLI.  
   - claim_token CAS + stop killpg (no pkill -f) already solid.  
5. **doctor has zero team checks** today.

### 8.2 Revised promotion order (Claude overrides earlier Phase A)

**P0-1 readiness redesign first** (process `worker-ready` receipt + optional model ACK enrichment), **then** live smoke matrix (executor + verifier), **then** shutdown ack ops, heartbeat API wiring, doctor --team, gate flip to default-on + `OMG_DISABLE_TMUX_TEAM=1`.

Full 33-op clone is **not** a promotion requirement (~9 ops needed for shutdown/heartbeat/orphan/read-task/manifest).

### 8.3 Forbidden claims (adopt into CI if possible)

- No “OMX/33-op parity” unless all handlers + goldens exist.  
- No “sandbox” for panes — only integration isolation.  
- No OMC infinite Stop stickiness.  
- No verified from team/HUD/notify.  
- No promotion without successful `LIVE_TEAM_SMOKE_OK`.

---

## 9. Status (2026-07-30, post-merge)

| Item | Status |
|------|--------|
| P0-1 process `worker-ready` | **Shipped** (#62) |
| P0′ heartbeat / shutdown / orphan + doctor | **Shipped** (#63) |
| Default-on gate + `OMG_DISABLE_TMUX_TEAM` + fixture smoke | **Shipped** (#64) |
| P0′ `read-task` / `update-task` / `read-manifest` / events | **Shipped** (branch `feat/team-p0-prime-task-events`) |
| Grok-live `LIVE_TEAM_SMOKE_OK` | **Still open** (quota; not CI-required) |
| Full 33-op / broadcast / await-event / preflight pack | Deferred (Phase B residual) |

### Immediate next actions

1. ~~Land research doc~~ done.  
2. ~~Phase A reliability + default-on~~ done on main via #62–#64.  
3. Land P0′ task/events/manifest API PR.  
4. Run `scripts/live_team_smoke.py --live` when quota available → archive `LIVE_TEAM_SMOKE_OK`.  
5. Residual Phase B/C only after live proof (handoffs, preflight-context, broadcast).

---

## 10. Scope honesty checklist (non-negotiable)

- [ ] Never claim execution sandbox for multi-CLI panes  
- [ ] Never claim full OMX 33-op without implementing + tests  
- [ ] Never claim OMC Stop stickiness on Grok  
- [ ] Never set `verified` from team collect/stop  
- [ ] Never use pkill -f for stop  
- [ ] Live promotion evidence required before “production team” marketing  
