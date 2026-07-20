# Critic / adversarial honesty — OMC parity for oh-my-grok

**date_utc:** 2026-07-20  
**role:** Grok advisor #3 — CRITIC (reject false completeness)  
**repo:** `<repo-root>`  
**version under audit:** plugin **0.2.5** (`plugin.json`) + Option A spawn fail-closed (deny policy + matcher)  
**inputs:** `BRIEF.md`, `README.md`, `docs/security-model.md`, `skills/omg-using/SKILL.md`, `docs/research/stop-continuation/CONSENSUS.md`, live-gates docs under `docs/research/live-gates*.md` + `docs/research/live/`, autopilot-0.3 research, skills/agents/CLI inventory  

**Mode:** ADVERSARIAL (CRITICAL marketing claim at stake + multi-MAJOR product-lie surface).

---

## VERDICT: REJECT

**「OMC 功能基本都有了嗎？」→ NO — severity: CRITICAL for any external/marketing claim; MAJOR if only internal engineering shorthand.**

| Question | Answer | Severity if claimed YES |
|----------|--------|-------------------------|
| Core orchestration **subset** roughly present (ulw/ralph/ralplan/accept/cancel/doctor skeleton)? | **PARTIAL** — CLI + skills exist; several paths still convention-not-enforced or live-thin | MAJOR if sold as “parity” |
| Full OMC **surface** / skill zoo parity? | **NO** | CRITICAL |
| In-session “don’t stop until done” like OMC persistent-mode / Stop pin? | **NO — host NEVER for 0.3.x** | CRITICAL (product lie) |
| Isolation “workers can’t shell / hard sandbox”? | **NO** (honest docs say soft-gate + capability primary) | CRITICAL if oversold |

**One line for leadership:**  
0.2.5 is a **real Option B orchestration kit** with honest security prose and strong **CLI library** tests. It is **not** OMC-complete, not autopilot-complete, and not “ultrawork proven parallel.” Claiming “功能基本都有了” is a **product lie** unless you define “basic” as a tiny closed set and print the absences next to it.

---

## Pre-commitment predictions vs findings

| Predicted failure mode | Found? |
|------------------------|--------|
| Name-collision skills thinner than OMC | **YES** — 8 `omg-*` skills vs OMC 4.15.5 large skill surface; pipeline = composition not OMC autopilot |
| Stop continuation sold as missing “soon” vs host-impossible | **YES** — council correctly **DO NOT BUILD**; risk is marketing still implying OMC “keep going” feel without CLI |
| ULW live gates prove launch not parallel | **YES** — L-ULW-1 = single-leader OK file; explore doc: CLI never checks spawn; no auto-integrate on default ulw |
| Dual-review interim sold as dual-review complete | **YES** — CLI sequential headless; residual parser noise; native spawn preferred but not CLI path |
| Trust story better than feature story | **MOSTLY** — `security-model.md` is unusually honest; residual overclaim risk is still “Option A shipped = isolation done” without live spawn-deny canary |

---

## 1. Fairness check: “OMC 功能基本都有了嗎?”

### NO

**Why this is not a nitpick**

1. **Surface area:** OMG ships **8 skills** (`skills/omg-{using,ultrawork,ralph,ralplan,pipeline,dual-review,cancel,ask}/`) and **4 agents**. OMC 4.15.5 reference list in `BRIEF.md` includes autopilot, ultragoal, ultraqa, team/omc-teams, deep-interview, deep-dive, hud, wiki, verify, visual-verdict, notifications, remember, skillify, sciomc, self-improve, project-session-manager, merge-readiness, autoresearch, ccg, … — vast majority **MISSING** or empty directory placeholder (`ultragoal/` dir only).
2. **Persistence semantics differ by host law:** OMC-style Stop `{decision:block}` reinject is **NEVER** on Grok (`docs/research/stop-continuation/CONSENSUS.md`). OMG substitute is **CLI outer loop** (`omg ralph` / `omg pipeline`). Saying “basically have ralph/autopilot” without that distinction **lies about UX**.
3. **“ultrawork” name without product enforcement:** default `omg ulw` is **one leader `grok` process** with a skill that *prefers* spawn; CLI does **not** auto-integrate, does **not** require multi-spawn, live L-ULW-1 is solo-file path (`docs/research/autopilot-0.3/autopilot-explore-ulw-parallel.md`).
4. **pipeline ≠ OMC autopilot:** skill explicitly says “AUTO_PILOT-like composition (CLI-owned)” (`skills/omg-pipeline/SKILL.md`); stages exist in `omg_cli/pipeline.py`, but **no `L-PIPELINE` in `scripts/live_suite.sh`**, and multi-agent live review still listed pipeline live as gap (2026-07-19; full suite later still did not add pipeline gate).
5. **Version marketing lag:** `plugin.json` still **0.2.5** while research talks 0.3.0 Option A — fine for honesty if claim language is 0.2.5 kit; **not** fine if someone says “0.3 parity.”

**Allowed internal sentence (if forced):**  
「**核心 CLI 編排骨架**（ulw/ralph/ralplan/accept/integrate/cancel/doctor）已存在且有 pytest + 部分 live launcher 證據；**不是** OMC 功能面 parity，也不是 host-level persistent session。」

**Forbidden sentence:**  
「OMC 功能基本都有了。」

---

## 2. PRODUCT LIES risks (skills / names that sound like OMC but thinner)

| OMG surface | Sounds like | Reality (evidence) | Lie class |
|-------------|-------------|--------------------|-----------|
| `omg-ultrawork` / `omg ulw` | OMC ultrawork parallel factory | Skill playbook + soft “prefer spawn”; **max_iter default 1**; **no post-run auto-integrate**; live OK can be **leader-solo**; multi-worker is **convention** | **HIGH** — name oversells parallel |
| `omg-ralph` / `omg ralph` | OMC ralph / don’t-stop-in-chat | Skill = **one story then STOP**; durability = **CLI max_iter loop + context pack** (`omg_cli/modes.py`); **not** Stop pin | **HIGH** if user expects TUI infinite continue |
| `omg-pipeline` / “autopilot” trigger | OMC autopilot | Composition FSM `plan→implement→integrate→dual_review→accept→report`; dual is **interim sequential**; **no live pipeline gate** | **HIGH** |
| `omg-dual-review` CLI | Independent dual review complete | Documented **sequential headless interim** (`omg_cli/dual_review.py`, skill honesty table); native spawn is TUI preference; **does not set verified** (good) but live residual: summary vs stage mismatch noted in `live-gates-2026-07-20-suite.md` | **MEDIUM–HIGH** |
| `omg-ralplan` | OMC ralplan full deliberation stack | Real CLI FSM + stages (`omg_cli/ralplan.py`); thinner agent zoo (no dedicated planner/architect/deep-interview stack); **no live ralplan gate** called out historically | **MEDIUM** |
| `omg-ask` | OMC multi-LLM / CCG auto council | **Human-invoked only** broker; never auto-ingest pipeline (`skills/omg-ask/SKILL.md`, security-model layer 7) — **honest if named broker**, lie if “ask = multi-LLM orchestration” | **MEDIUM** if mislabeled |
| PreToolUse / “fail-closed spawn” | Hard isolation | **Soft fail-open** on hook crash; primary = `capability_mode` (`docs/security-model.md`); Option A is defense-in-depth **when hook+matcher run**; live **shell** canary exists; **live spawn-deny canary not proven as host oracle** (unit only for `decide_spawn_subagent`) | **CRITICAL** if “workers cannot escape” |
| `ultragoal/` directory | OMC ultragoal durable goals | Dir created by setup/state (`omg_cli/state.py` dirs list) — **no skill, no FSM, no goal engine** | **HIGH** empty shell |
| Process fanout `omg ulw --fanout process` | Team-like multi-process | **Experimental env gate** `OMG_EXPERIMENTAL_PROCESS_FANOUT=1`; workers still full shell risk; **not** default isolation story | **HIGH** if demoted to “also team” |
| Stop / SubagentStop hooks | OMC persistent-mode | **Passive** observe/spool only (`CONSENSUS.md`) | **CRITICAL** if sold as continue |

### Naming discipline (must enforce in marketing)

- Say **「CLI outer-loop ralph」**, not 「和 OMC 一樣的 don’t stop」。
- Say **「pipeline composition」**, not 「OMC autopilot」。
- Say **「ulw skill + optional spawn」**, not 「proven parallel ultrawork」 until multi-spawn + envelope + integrate live matrix exists.
- Say **「dual-review interim sequential」** for CLI; never 「native dual shipped」.

---

## 3. REAL core-complete areas (users can rely on **today** — with evidence)

Label: **HAVE** = ship-grade for stated contract; **PARTIAL** = usable with known holes.

| Area | Status | Evidence users can trust |
|------|--------|---------------------------|
| **Option B architecture** (plugin + `omg` CLI, no Rust fork, no tmux v1) | **HAVE** | `README.md`, `plugin.json`, design docs |
| **State single-writer / verified ownership** | **HAVE** | `omg accept` + forge reject unit/e2e story; README C4; live suite: accept → `verified=true` on full/heavy |
| **Acceptance semantic policy floors** | **HAVE** | `omg_cli/command_policy.py`, `docs/security-model.md` families; pytest |
| **Doctor / setup scaffold** | **HAVE (soft doctor)** | `omg setup`, `omg doctor`; global PreToolUse hard check documented; `doctor --strict` still fails on host `~/.claude` compat WARNs (expected) |
| **Cancel with fail-closed kill** | **HAVE (live-lite)** | `omg cancel` + `pid.json` starttime; L-CANCEL: `status=cancelled`, `kill_actions: ["leader:killpg:SIGTERM"]` (`live-gates-2026-07-20-suite.md`) |
| **PreToolUse soft-gate for external agent CLIs (parent+child)** | **HAVE when global hook installed** | Live canary `DENIED_PARENT_AND_CHILD`; plugin-only path historically **failed** (`live-gates-2026-07-19.md`) — **rely only after `install-plugin.sh` + doctor** |
| **capability_mode implementer without shell (live sample)** | **PARTIAL→HAVE for one oracle** | L-CAP-SPAWN: `DENIED_OR_RAN=denied`, child toolset no `run_terminal_command` (`docs/research/live/cap-spawn-20260719T190456Z.txt`) — **one child type sample**, not adversarial omit-mode suite |
| **Integrate CLI (ancestry / merge reject / changed_files / require-squash)** | **HAVE (unit/e2e, not multi-worker live)** | `omg_cli/integrate.py`, remaining-blockers closed I11; e2e seal→integrate path |
| **Worker prepare/seal no-shell bridge** | **HAVE (CLI contract)** | `omg worker prepare|seal`, workers module; unit tests |
| **Ralph CLI loop + context pack** | **HAVE for launcher** | `modes.py` max_iter + context pack; L-RALPH-1 live OK (one-iter style gate) |
| **Ralplan CLI FSM (draft→critic→revise→verifier APPROVE)** | **HAVE as CLI machine** | `omg_cli/ralplan.py`, skill; live depth not proven like dual/ulw |
| **Dual-review sequential headless + verified still CLI-owned** | **HAVE as interim** | Live L-DUAL-1 REQUEST_CHANGES; suite claim table forbids “native dual shipped” |
| **Ask broker env hygiene** | **HAVE as human broker** | stdin default, child-only allow env (unit + design) |
| **Security honesty documentation** | **HAVE** | `docs/security-model.md` “Do not claim” section — **best asset against product lies** |
| **Hermetic pytest / smoke** | **HAVE** | 274 passed + `OMG_E2E=1 smoke` cited in `live-gates-2026-07-20-suite.md` |

### Explicitly **not** “rely today as OMC-equivalent”

- Multi-worker ULW happy path end-to-end under default CLI  
- Pipeline live e2e  
- In-session Stop continuation  
- Team / tmux / multi-process default  
- HUD / wiki / ultraqa / ultragoal engine / deep-interview / skillify / notifications / remember  
- Hard sandbox / PreToolUse-as-sandbox  

---

## 4. MUST-SHIP before any **「0.3 parity」** marketing

Parity here means **honest “core orchestration parity with OMC’s useful subset on Grok host”**, not full surface clone. Until these ship **and** claim language is rewritten, **ban** the words: parity / 基本都有 / OMC-complete / production isolation complete.

### P0 — block marketing (CRITICAL)

1. **Claim matrix published in README** with HAVE | PARTIAL | MISSING | NEVER rows (use section 7 below). No marketing without link.
2. **ULW product path that cannot greenwash solo:**  
   - either CLI **auto-integrate** or hard fail when envelopes expected;  
   - live gate **multi-spawn + ≥1 envelope + integrate** (not L-ULW-1 file-touch);  
   - skill/CLI agree on when leader-solo is allowed.
3. **Pipeline live gate** (`omg pipeline` dry-or-quota path writing `report.json` with stage history) — currently **absent** from `scripts/live_suite.sh`.
4. **Spawn fail-closed host canary** (missing `capability_mode` → exact deny reason), not only unit tests on `decide_spawn_subagent` — mirror shell canary host-signature discipline (`canary_pretool.py` pattern).
5. **Doctor proves effective soft-gate deploy:** plugin **and** global `~/.grok/hooks/omg-pretool-deny.json` matchers include `spawn_subagent|Task` (code exists in `omg_cli/doctor.py` — re-verify on fresh install; reinstall after upgrades required).
6. **Dual-review claim freeze:** CLI remains labeled **interim sequential**; ban “dual-review complete”; fix or document residual APPROVE/REQUEST_CHANGES summary mismatch (`live-gates-2026-07-20-suite.md`).
7. **Persistence messaging freeze:** every ralph/pipeline skill + README front: **Stop cannot force continue**; use CLI loops only (`CONSENSUS.md`).

### P1 — before calling it “0.3 product complete” (not full OMC)

8. Multi-iter ralph live (max_iter>1 + accept path).  
9. Ralplan live gate (APPROVE / max_rounds fail).  
10. Multi-envelope integrate live.  
11. Process fanout remains experimental **or** removed from default docs surface.  
12. Empty `ultragoal/` either implemented minimally or **removed from “feature list”** language (dir-only is a lie attractor).

### Explicitly **not** required for 0.3 “core” (but required for full surface)

- HUD, wiki, ultraqa, deep-interview, skillify, notifications, remember, CCG auto fan-out, team/tmux, OMC autopilot open-box UX.

---

## 5. NEVER-SHIP / host-impossible (0.3.x and until host changes)

| Item | Label | Why |
|------|-------|-----|
| **In-session Stop continuation / ForceContinue pin** | **NEVER (0.3.x)** | Only `PreToolUse` blocking; Stop passive — `docs/research/stop-continuation/CONSENSUS.md` unanimous DO NOT BUILD |
| **Hard guarantee via PreToolUse alone** | **NEVER market** | Fail-open on timeout/crash/missing hook (`security-model.md`) |
| **Default workers = claude/codex/omc team/agy** | **NEVER (product rule)** | HARD RULES + deny list; advisors only via human `omg ask` |
| **tmux / OMC team multi-process control plane as v1 default** | **OUT_OF_SCOPE / NEVER for stated Option B v1** | README: no tmux v1; experimental process fanout is not team |
| **Rust fork of grok-build** | **OUT_OF_SCOPE** | Architecture choice |
| **Acceptance allowlist as OS sandbox** | **NEVER claim** | Approved runners execute repo code |
| **Models set `verified`** | **NEVER** | CLI-only stamp |
| **OMC full skill zoo parity as 0.3 goal** | **OUT_OF_SCOPE** unless re-scoped multi-quarter | Clone-by-name without host features = product lie factory |

**Revisit Stop continuation only if:** Grok adds blocking Stop **and** live canary proves reinject e2e (`CONSENSUS.md`).

---

## 6. Scores (0–10)

| Axis | Score | Rationale |
|------|------:|-----------|
| **Core orchestration parity** | **5 / 10** | Real skeleton: ulw/ralph/ralplan/pipeline/accept/integrate/cancel/doctor/ask. Parallel ULW and autopilot-depth are thin; live proves launchers more than multi-agent product path. Halfway to “Grok-native useful subset,” not OMC core-complete. |
| **Full OMC surface** | **2 / 10** | ~8 skills + 4 agents vs OMC’s large skill/agent/hud/wiki/team/qa/goal surface. Empty `ultragoal` dir. No HUD/wiki/ultraqa/deep-interview/skillify/notifications/remember/team. |
| **Trust / security honesty** | **8 / 10** | Outstanding for a 0.2.x project: layer table, fail-open honesty, verified CLI-only, canary host-signature discipline, live claim language tables. Deduct for residual risks: Option A overclaim without spawn live canary; dual summary residual; any “parity” narrative collapsing honesty. |

**Composite “can we say OMC parity?”:** **2–3 / 10**. Do not average into a marketing number.

---

## 7. Shared feature matrix (critic-filled)

| Feature | OMC | OMG | Status | Notes |
|---------|-----|-----|--------|-------|
| Parallel fan-out (ulw) | Native multi-agent ultrawork | Skill + spawn convention; CLI one leader; process fanout experimental | **PARTIAL** | Live L-ULW-1 ≠ multi-worker; no default auto-integrate |
| Persistence loop (ralph) | Session + Stop-style continue culture | CLI max_iter + one-story skill stop | **PARTIAL** | Correct host design; **not** OMC chat persistence |
| Plan consensus (ralplan) | Deep ralplan / critic stack | CLI FSM draft→critic→revise→verifier | **PARTIAL→HAVE CLI** | Live depth weak; agent zoo thinner |
| Full auto pipeline (autopilot) | OMC autopilot skill stack | `omg pipeline` composition | **PARTIAL** | No live pipeline gate; dual interim |
| Dual / multi review | Multi-model / dual-review culture | Grok critic→verifier; CLI sequential interim | **PARTIAL** | Live dual ran; not native CLI dual; no verified stamp (good) |
| Ask external advisors | Integrated multi-LLM patterns | `omg ask` human broker | **PARTIAL** | Correct isolation; not auto council |
| Team / tmux multi-process | team / omc-teams | None default; experimental process fanout | **MISSING / OUT_OF_SCOPE v1** | |
| Stop pin / force continue | Persistent-mode / block Stop | Passive Stop hooks only | **NEVER (host)** | CONSENSUS DO NOT BUILD |
| Context pack / resume | Session/project managers | Ralph context pack; pipeline `--resume` | **PARTIAL** | Resume pipeline yes; not OMC project-session-manager |
| Doctor / setup | omc setup ecosystem | `omg setup` / `omg doctor` | **HAVE** | Strict compat host noise remains |
| Cancel | cancel skill | `omg cancel` + skills | **HAVE** | Live cancel evidence |
| Acceptance / verified gate | verify / acceptance culture | `omg accept` CLI stamp only | **HAVE** | Strongest trust feature |
| HUD | hud skill | — | **MISSING** | |
| Wiki | wiki skill | — | **MISSING** | |
| Notifications | configure-notifications | — | **MISSING** | |
| Deep interview | deep-interview | — | **MISSING** | |
| UltraQA | ultraqa | — | **MISSING** | |
| Ultragoal durable goals | ultragoal | empty `.omg/ultragoal/` dir | **MISSING** (dir only) | |
| Skill management | skillify / self-improve | — | **MISSING** | |
| Capability isolation | host/plugin dependent | capability_mode primary + soft PreToolUse + spawn role table | **PARTIAL** | Live cap-spawn sample; soft-gate residual |
| PreToolUse canary | product-dependent | `scripts/canary_pretool.py` dry/live | **HAVE** (shell deny) | Spawn-deny canary still gap for Option A marketing |

---

## 8. Multi-perspective notes

### Executor

- Can run `omg doctor/setup/ulw/ralph/ralplan/accept/cancel` from README.  
- Will get stuck if they believe skill-only “ralph until done” in one TUI turn — **skill intentionally stops**.  
- ULW: if they don’t manually prepare/seal/integrate, “parallel work” evaporates into leader mono-session.  
- Must install **global** PreToolUse hook or soft-gate is theater (`live-gates-2026-07-19.md`).

### Stakeholder

- Problem “Grok multi-agent orchestration kit” is **partially** solved.  
- Problem “OMC parity product” is **not** solved.  
- Success metrics that matter: multi-worker ULW live, pipeline live, honest claim matrix — **not** skill count or version bumps.

### Skeptic

- Strongest failure argument: **pytest + live launcher green** creates **false confidence** that ultrawork/autopilot/isolation are product-complete (already called out in `live-gates-multi-agent-review-2026-07-19.md`).  
- Counter-argument “docs are honest so we’re fine” fails if humans still say 「基本都有了」 in speech — honesty in markdown does not cancel verbal overclaim.

### Security

- Trust model is **correctly layered** when operators follow it.  
- Residual R1 leader shell remains by design.  
- Spawn gate without host canary = policy code, not proven production control plane.

---

## 9. What’s missing (gap dump — scored)

### CRITICAL (blocks “parity” / isolation marketing)

- Full OMC surface (HUD/wiki/ultraqa/ultragoal engine/deep-interview/team/…).  
- Host Stop continuation (impossible → must not be “missing feature, coming soon”).  
- True multi-worker ULW as default proven path.  
- Live spawn-deny canary for Option A.  
- Pipeline live evidence.

### MAJOR (significant rework / product risk)

- Dual-review interim vs native narrative.  
- ULW auto-integrate / envelope requirement productization.  
- Empty ultragoal attractor.  
- Live multi-iter ralph / ralplan depth.  
- Any residual dual verdict parser noise.

### MINOR

- Version string 0.2.5 vs research 0.3.0 labeling hygiene.  
- security-model layer table cross-links already improved for Option A — keep residual text tight.

---

## 10. Self-audit & realist check

| Finding | Confidence | Author could refute? | Realist worst case |
|---------|------------|----------------------|--------------------|
| 「基本都有了」is false for full OMC | HIGH | No — skill inventory arithmetic | Stakeholder over-commit roadmap |
| ULW live ≠ parallel ultrawork | HIGH | No — explore + live review docs agree | Users run solo and think fan-out worked |
| Stop NEVER 0.3.x | HIGH | Only if host changes | Building dead Stop pin = product lie |
| Trust docs are good (score 8) | HIGH | No | Softens nothing if marketing ignores them |
| Install matcher drift (old critic) | — | **Likely fixed** — `install-plugin.sh` now includes `spawn_subagent\|Task` (verified 2026-07-20 read). **Do not re-open as CRITICAL without re-break.** | |

**Realist recalibration:** Core CLI contracts are **not** vaporware (274 tests + live suite). Severity of REJECT is aimed at **parity marketing**, not at “delete the project.”

**Escalation:** ADVERSARIAL mode on for remainder due to CRITICAL false-completeness risk.

---

## 11. Verdict justification

**REJECT** any statement that OMG already has basic OMC functionality **as OMC users understand it**.

Upgrade path to **ACCEPT-WITH-RESERVATIONS** for **narrow** claim only if README ships:

> “oh-my-grok 0.2.5/0.3 delivers a **Grok-native orchestration subset**: CLI-owned ralph/pipeline loops, skill-guided ulw spawn, ralplan FSM, accept-gated verified, soft PreToolUse + capability isolation. **Not** OMC surface parity; **not** Stop continuation; **not** team/tmux; ULW multi-worker and pipeline live matrices required before ‘core complete’.”

Upgrade to **ACCEPT** for “core orchestration parity” only after **§4 P0** items are closed with dated live evidence.

Hand-off:

- **planner** — 0.3.x roadmap must prioritize ULW multi-worker + pipeline live + claim matrix over skill cloning.  
- **architect** — keep Option B; do not resurrect Stop pin.  
- **executor** — productize ULW integrate/spawn proof; spawn canary.  
- **security-reviewer** — when claiming Option A “active in production,” demand host spawn canary.

---

## 12. Open questions (unscored)

- Has L-CAP-SPAWN been re-run after Option A matcher deploy on the same host as canary global hook? (suite dated 20260719; Option A landed around same window — re-run recommended.)  
- Will product deliberately keep dual-review CLI sequential forever (waived native) or is native still a 0.3 goal?  
- Is “0.3 parity” even a desired phrase, or should marketing use “Grok orchestration kit 0.3” only?

---

## Ralplan summary row (this critic artifact)

| Gate | Result |
|------|--------|
| Principle/Option Consistency | **Pass** — Option B CLI durability + soft-gate honesty consistent with CONSENSUS / security-model |
| Alternatives Depth | **Pass** (for stop-continuation) — DO NOT BUILD vs CLI-only already decided |
| Risk/Verification Rigor | **Fail for parity claim** — live matrix incomplete for ULW parallel + pipeline; spawn canary gap for Option A marketing |
| Deliberate Additions | N/A (critic report, not ralplan plan) |

---

## Bottom line (harsh)

**NO — OMC 功能並沒有「基本都有了」。**  
You have a **credibly engineered 0.2.5 orchestration core** with **above-average security honesty**. That is success for Option B foundation work.  
It is **failure** as an OMC parity story: wrong surface coverage, host-impossible Stop semantics, and **name-level product lies** (ulw / ralph / autopilot / dual-review / ultragoal) waiting to fire if anyone shortens the pitch.

**Do not market 0.3 parity. Market measured contracts. Ship P0 live gates. Keep saying no to Stop pins.**
