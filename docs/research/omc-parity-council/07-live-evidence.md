# 07 — Live evidence verifier (core modes)

**Role:** Grok advisor #7 VERIFIER  
**date_utc:** 2026-07-20  
**repo:** `<repo-root>`  
**product:** oh-my-grok ~0.2.5  
**scope:** Evidence-only — do core modes *actually* work under real `grok`?  
**not in scope:** product edits, marketing rewrite, new live runs in this pass  

**Primary sources (fresh on tree, not re-run this pass):**

| Artifact | Path |
|----------|------|
| Suite write-up | `docs/research/live-gates-2026-07-20-suite.md` |
| Earlier live smoke | `docs/research/live-gates-2026-07-19.md` |
| Latest canary snapshot | `docs/research/canary-pretool-latest.json` |
| Suite summaries | `docs/research/live/suite-*-*.summary.json` (3) |
| Canary per-suite | `docs/research/live/canary-20260719T{185729,190043,190456}Z.json` |
| Cap-spawn report | `docs/research/live/cap-spawn-20260719T190456Z.txt` |
| Suite logs | `docs/research/live/suite-*-{quick,full,quota-heavy}.log` |
| ULW run state | `docs/research/live/ulw-runs-*/**/status.json` |
| Live suite script | `scripts/live_suite.sh` |
| Test matrix | `docs/research/test-matrix.md` |
| Pre-suite multi-agent gap list | `docs/research/live-gates-multi-agent-review-2026-07-19.md` |

**Hermetic baseline claimed by suite doc (not re-executed here):** `pytest -q` **274 passed**; `OMG_E2E=1 smoke` OK + `ALL_REAL_E2E_OK`; `omg doctor` hard global PreToolUse soft-gate **OK**.

---

## Verification Report

### Verdict
**Status**: **PASS** (narrow claim) / **FAIL** (broad claim)  
**Confidence**: **high** on narrow; **high** that broad is false  
**Blockers**: **0** for “core CLI loops smoke live”; **several** for “OMC-complete / production isolation / all modes live”

| Claim under test | Verdict |
|------------------|---------|
| **Narrow:** Headless core launchers + accept + soft-gate canary + (heavy) cap-spawn + cancel work with real Grok | **PASS** — dated L2 evidence exists |
| **Broad:** “Core loops work” = full OMC-parity (parallel ULW, pipeline, ask, ralplan, native dual, hard isolation) | **FAIL / overclaim** — live matrix incomplete |

### Evidence (suite runs)

| Check | Result | Command/Source | Output |
|-------|--------|----------------|--------|
| Live suite `--quick` | pass | `scripts/live_suite.sh --quick` · ts `20260719T185729Z` | `suite-…-quick.summary.json` → `"status":"ok"` |
| Live suite `--full` | pass | `… --full` · ts `20260719T190043Z` | `"status":"ok"`; dual-review ran |
| Live suite `--quota-heavy` | pass | `… --quota-heavy` · ts `20260719T190456Z` | `"status":"ok"`; cap-spawn + cancel |
| Canary live ×3 + latest | pass | `canary_pretool.py --live` | all `DENIED_PARENT_AND_CHILD` exit 0; latest `2026-07-19T19:18:08Z` |
| Types / product build | n/a this pass | research only | — |
| Fresh re-run this verifier turn | **not executed** | quota opt-in | Evidence is **dated 2026-07-19 evening**, on-tree, consistent across 3 suite modes |

### Acceptance criteria (honest)

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | Soft-gate canary: parent **and** child deny external `claude` with global hook | **VERIFIED** | 3 suite canaries + `canary-pretool-latest.json` all `DENIED_PARENT_AND_CHILD`; markers absent; host signature true on latest |
| 2 | `omg ulw` real agent writes fixture file | **VERIFIED** (smoke) | Logs + `LIVE-ULW-OK`; status `completed` exit 0; e.g. `ulw-runs-…/status.json` |
| 3 | `omg ralph` real agent writes fixture file | **VERIFIED** (smoke) | `LIVE-RALPH-OK` in quick/full/heavy logs |
| 4 | `omg accept` → `verified=true` only via CLI | **VERIFIED** | L-ACCEPT-1 full+heavy: log shows `verified=true` after hermetic `[["true"]]` prd + `omg accept --yes` |
| 5 | `omg dual-review` live sequential runs; does **not** set verified | **VERIFIED** (launcher + contract) | full+heavy L-DUAL-1: `verdict=REQUEST_CHANGES`; suite asserts no `"verified": true` under dual proj |
| 6 | capability_mode spawn: implementer without shell tool | **VERIFIED** (one heavy run) | `cap-spawn-…txt` `DENIED_OR_RAN=denied`; log: child `omg-executor` / `read-write` reports **no** `run_terminal_command` |
| 7 | `omg cancel` live killpg | **VERIFIED** (one heavy run) | status `cancelled`, `kill_actions: ["leader:killpg:SIGTERM"]` |
| 8 | ULW **parallel multi-worker** fan-out live | **MISSING** | Live ULW goals are single-file smoke; matrix forbids “ultrawork parallel spawn path live verified” |
| 9 | `omg ask` live (allow + deny external advisors) | **MISSING** | Not in `live_suite.sh`; unit only (matrix / multi-agent review) |
| 10 | `omg pipeline` live end-to-end FSM | **MISSING** | Not in live suite; L0/L1 only |
| 11 | `omg ralplan` live multi-round | **MISSING** | Not in live suite |
| 12 | Plugin-only PreToolUse sufficient without global hook | **FAIL (honest residual)** | `live-gates-2026-07-19.md` 3a: `REAL_CLI_RAN_hook_did_not_block` plugin-only |
| 13 | Hard sandbox / fail-closed hooks | **MISSING / NEVER as claimed** | Hooks **fail-open** on timeout/crash; residual explicit in canary + suite docs |

### Gaps

- **No live `omg ask`** — Risk: **high** for any claim about external advisor path / `OMG_ALLOW_EXTERNAL_CLI` child-only — Suggestion: add L-ASK-1 to `--full` (deny by default + allow in ask child).
- **No live `omg pipeline` / `omg ralplan`** — Risk: **high** for “autopilot / consensus plan works live” — Suggestion: one short pipeline fixture + one ralplan round under `--quota-heavy`.
- **ULW live ≠ parallel spawn** — Risk: **high** if marketing says ultrawork — Suggestion: must-spawn multi-worker goal + integrate envelope check live.
- **Dual-review critic quality residual** — Risk: **medium** — full run log shows critic stage as **stub** (`Stub artifact — Grok did not write this file`) while verifier still REQUEST_CHANGES; sequential path “ran” but not always “high quality dual chain”.
- **Cap-spawn / cancel single-shot** — Risk: **medium** — only on `--quota-heavy` once; no CI cadence guarantee.
- **Summary JSON thin** — Risk: **low** — `suite-*.summary.json` only `{status:ok}` without per-gate map; detail lives in logs (ok for humans, weak for automation).
- **This verifier did not re-burn quota** — Risk: **low** if tree unchanged; **medium** if product moved after `20260719T190456Z` without new suite.

### Recommendation
**APPROVE narrow language; REQUEST_CHANGES on broad “core loops all work” marketing.**

Honest one-liner: **Headless `ulw`/`ralph` smoke, accept→verified, global-hook canary parent+child deny, dual-review sequential (does not verify), and one-shot cap-spawn+cancel are live-proven; pipeline/ask/ralplan/parallel ULW are not.**

---

## 1. Evidence table — mode / gate

| Mode / gate | Layer | Last result | Confidence | Notes / source |
|-------------|-------|-------------|------------|----------------|
| **L0 pytest** | unit | 274 passed (suite doc claim) | high if HEAD unchanged | Hermetic baseline in `live-gates-2026-07-20-suite.md` — **not re-run this pass** |
| **L1 smoke + e2e_realpath** | hermetic | OK + `ALL_REAL_E2E_OK` (claimed) | high if HEAD unchanged | No LLM; contracts only |
| **L-CANARY** PreToolUse live | L2 | **`DENIED_PARENT_AND_CHILD`** ×3 suite + latest | **high** | `canary-*.json`, `canary-pretool-latest.json`; requires `~/.grok/hooks/omg-pretool-deny.json` |
| Plugin-only canary (negative) | L2 historical | **`REAL_CLI_RAN_hook_did_not_block`** | **high** | `live-gates-2026-07-19.md` §3a — isolation **not** plugin-alone |
| **L-ULW-1** `omg ulw` | L2 | **OK** (`LIVE-ULW-OK`) quick/full/heavy | **high** (smoke) | Real `grok`; `status=completed`, `verified=false` without accept; **not** multi-worker proof |
| **L-RALPH-1** `omg ralph` | L2 | **OK** (`LIVE-RALPH-OK`) quick/full/heavy | **high** (smoke) | Real agent; single-iter yolo; persistence loop *launched*, multi-iter durability not stressed |
| **L-ACCEPT-1** `omg accept` | L2 | **`verified=true`** full + heavy | **high** | Hermetic prd `[["true"]]` after live ralph; CLI-only verified path |
| **L-DUAL-1** `omg dual-review` | L2 | **REQUEST_CHANGES**; verified **not** set | **medium–high** | full+heavy; expected fail on fixture README `base`; critic **stub** residual on full log |
| **L-CAP-SPAWN** capability | L2 | **`DENIED_OR_RAN=denied`** | **high** (n=1) | heavy only; child no shell tool — primary isolation live oracle |
| **L-CANCEL** | L2 | **`status=cancelled`**, `killpg:SIGTERM` | **high** (n=1) | heavy only; best-effort long ralph then cancel |
| **`omg ask`** | — | **no live gate** | n/a | Unit / env policy only |
| **`omg pipeline`** | — | **no live gate** | n/a | Unit + hermetic FSM fragments |
| **`omg ralplan`** | — | **no live gate** | n/a | Not in suite |
| **`omg doctor` hard soft-gate** | L0+install | claimed OK in suite doc | medium–high | Hard-checks global hook file presence; does not prove deny fires (canary does) |
| **Process fanout** | — | **not live-proven as product path** | n/a | Matrix / security residual: leader shell by design |
| **Stop continuation** | host | **NEVER / DO NOT BUILD** | high | Separate consensus; not a live gap for 0.3.x |

---

## 2. Proven live vs unit-only

### Proven **live** (L2, real `grok -p` / host hooks)

| Capability | What “pass” actually means |
|------------|----------------------------|
| Global PreToolUse soft-gate | Literal external agent CLI (`claude` PATH shim) denied on **leader and spawned child**; marker file never executed |
| ULW / Ralph launchers | CLI can start headless runs with `--prompt-file`, agent produces a **single known file**, exit 0 |
| Accept ownership | After a live run, **only** `omg accept` flips `verified=true` (fixture story `true`) |
| Dual-review sequential interim | Critic→verifier style stages execute against real model; product does **not** treat dual APPROVE as verified |
| capability_mode on spawn | Spawned `omg-executor` with `read-write` **lacks** `run_terminal_command` in toolset (model-reported + report file) |
| Cancel | Live ralph process group gets `leader:killpg:SIGTERM`; state `cancelled` |

### Proven **unit / hermetic only** (green does **not** substitute for L2)

| Capability | Owner |
|------------|--------|
| Command policy floors (`python -c`, shells, agent CLIs, git/make grammar) | L0 `test_command_policy` |
| Accept / integrate / forge / fanout env contracts | L0 + L1 `e2e_realpath` |
| Cancel fail-closed kill logic (library) | L0 |
| Canary **classify** DENIED vs INCONCLUSIVE (dry) | L0 + dry canary |
| Doctor checks / install script writes global hook | L0 + install |
| Ask child-only `OMG_ALLOW_EXTERNAL_CLI` wiring | L0 (claimed in multi-agent review) |
| Dual-review order / RO argv clamps | L0 |
| Pipeline re-integrate / stage machine without Grok | L0 / hermetic fragments |
| Parallel ULW envelope / integrate ancestry edge cases | L0 (not live multi-worker) |

**Rule from test-matrix (binding):** Green L0/L1 **does not** satisfy AC5 (host isolation / dual live / capability spawn). Dated L2 under `docs/research/live/` is required before isolation marketing.

---

## 3. Gaps in live coverage

| Gap | In multi-agent P0 (pre-suite)? | Closed by 2026-07-20 suite? | Residual |
|-----|-------------------------------|-----------------------------|----------|
| dual-review live | P0-1 | **Yes** (`--full` / heavy) | Quality: critic stub once; sequential only, not native dual |
| pipeline live | P0-2 | **No** | Still missing |
| capability_mode live oracle | P0-3 | **Yes** (heavy L-CAP-SPAWN) | n=1; model self-report of toolset (strong but not kernel sandbox) |
| Plugin-only insufficient | P0-4 | **Documented + doctor hard-check** | Soft-gate still depends on **global** hook; fail-open residual |
| CI for live | P0-5 | **No** (opt-in by design) | Expected; not a product bug |
| ULW did not spawn multi-worker | P0-6 | **Partial** — cap-spawn forces **one** child | Not parallel fan-out / integrate of N workers |
| **ask live** | P1 | **No** | Default deny + ask allow path unproven on host |
| ralplan live | P1 | **No** | |
| multi-iter ralph durability | P1 | **No** | max-iter 1 smokes only |
| cancel killpg | P1 | **Yes** (heavy) | best-effort timing |
| accept → verified after live | P1 | **Yes** | |

### Explicit answers to brief questions

| Question | Answer |
|----------|--------|
| Spawn deny canary? | **Yes, live.** Parent+child PreToolUse deny (canary). Separately, **capability spawn deny** (L-CAP-SPAWN) is live on heavy. |
| Dual-review? | **Yes, live launcher** on full/heavy; verdict REQUEST_CHANGES on fixture; **does not** set verified. Not “native dual complete.” |
| Ask? | **No live evidence.** Unit-only. |

---

## 4. Can we claim “core loops work” honestly?

### Allowed claim language (evidence-backed)

> **Core headless CLI loops for ulw/ralph smoke, accept→verified, dual-review sequential (non-verifying), global-hook PreToolUse parent+child deny, capability_mode implementer without shell, and cancel killpg have dated live suite evidence (2026-07-19 / documented 2026-07-20).**

More precisely:

| Phrase | Honest? |
|--------|---------|
| “`omg ulw` / `omg ralph` can drive real Grok to complete a tiny goal file” | **Yes** |
| “Persistence is CLI outer loop; accept is the only verified gate — live-proven for accept” | **Yes** (accept live; multi-iter persistence only weakly exercised) |
| “Soft-gate blocks external agent CLIs when global hook installed (parent+child)” | **Yes** |
| “Primary isolation: spawned implementer without Execute/shell — live oracle once” | **Yes**, with fail-open + leader-still-has-shell caveats |
| “Dual-review runs live and does not mark verified” | **Yes** |
| “Core loops work” **without qualifier** | **No** — ambiguous; fails ask/pipeline/ralplan/parallel ULW |
| “OMC basic functionality complete / production isolation / ultrawork parallel” | **No** |
| “Plugin hooks alone guarantee isolation” | **No** — proven counterexample |
| “Hard sandbox / cannot escape on leader” | **No** — suite claim language forbids this |

### Forbidden overclaims (from suite + matrix — still binding)

- Plugin hooks alone guarantee isolation  
- Ultrawork **parallel spawn** path live verified  
- Native dual-review shipped  
- Soft-gate = hard sandbox  
- Unit green proves live host behavior  

### Final honest verdict for council

| Question | Answer |
|----------|--------|
| Do **basic durable CLI modes** (ulw/ralph launch + accept + cancel + dual sequential + isolation soft/capability canaries) work under real Grok? | **Yes — with dated L2 evidence.** |
| Is that enough to say **“core loops work”** in an OMC-parity sense? | **Only if “core” is defined as those CLI smokes.** Against OMC surface (ask advisors, pipeline autopilot, ralplan, parallel team/ulw, hard isolation), **no.** |
| Confidence | **High** on what was suite-gated; **high** that gaps above are real, not pedantic. |

**Status labels for council matrix feed:**

| Feature | Live status |
|---------|-------------|
| Parallel fan-out (ulw) | **PARTIAL** — launcher live; multi-worker **MISSING** live |
| Persistence loop (ralph) | **PARTIAL** — single-iter live OK; multi-iter **MISSING** live |
| Plan consensus (ralplan) | **MISSING** live (CLI may exist; no L2) |
| Full auto pipeline | **MISSING** live |
| Dual / multi review | **PARTIAL** — sequential live; native dual **MISSING** |
| Ask external advisors | **MISSING** live |
| Cancel | **HAVE** (live heavy) |
| Acceptance / verified | **HAVE** (live full/heavy) |
| Capability isolation | **PARTIAL** — implementer live once; fail-open + leader shell residual |
| PreToolUse canary | **HAVE** (live, global hook) |

---

## Appendix A — Suite mode coverage map

From `scripts/live_suite.sh` headers + body:

| Gate | `--quick` | `--full` | `--quota-heavy` |
|------|-----------|----------|-----------------|
| L-CANARY | ✓ | ✓ | ✓ |
| L-ULW-1 | ✓ | ✓ | ✓ |
| L-RALPH-1 | ✓ | ✓ | ✓ |
| L-ACCEPT-1 | ✓ | ✓ | ✓ |
| L-DUAL-1 | | ✓ | ✓ |
| L-CAP-SPAWN | | | ✓ |
| L-CANCEL | | | ✓ |
| L-ASK / L-PIPELINE / L-RALPLAN | — | — | — |

## Appendix B — Key raw quotes (anchors)

**Canary latest status:** `"status": "DENIED_PARENT_AND_CHILD"`, `"exit_code": 0`, parent+child denied, markers false (`canary-pretool-latest.json`).

**Cap-spawn report:**  
```text
DENIED_OR_RAN=denied
CHILD_ID=019f7bc8-cd5d-75c2-b474-576dff5a1725
```

**Cancel status:** `"status": "cancelled"`, `"kill_actions": ["leader:killpg:SIGTERM"]` (quota-heavy log).

**Dual CLI line:** `omg dual-review: run=… verdict=REQUEST_CHANGES` (full log); suite asserts dual must not set verified.

**ULW status sample:** `"mode": "ulw", "status": "completed", "verified": false, "exit_code": 0`.

---

*Verifier #7 end. No product code touched. Evidence not re-executed; claim language constrained to on-tree artifacts dated 2026-07-19 suite + 2026-07-20 write-up.*
