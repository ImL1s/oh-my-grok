# Open items after 2026-07-20 council + P0 ship

**Status companion:** [`STATUS.md`](./STATUS.md)  
**Verification done:** [`../live/verification-2026-07-20.md`](../live/verification-2026-07-20.md)  
**Advisor ops:** [`../external-advisors.md`](../external-advisors.md)

This file expands **what is still open** (not “docs missing” — **work not done**).

---

## A. External seats

| Item | Owner | How to close |
|------|-------|----------------|
| **Claude Fable free audit report** | Human / orchestrator | Re-run with argv contract in `../external-advisors.md` §2; replace `09-fable.md` BLOCKED stub |
| **Post-P0 dual-review (Codex + Fable) on product commits** | Optional | Brief = `git log 60d0882..HEAD` + paths `omg_cli/verdict.py`, deny, modes, dual_review, ralplan, live_suite; write to `docs/research/omc-parity-council/dual-review-post-p0-{codex,fable}.md` |

---

## B. Codex P0 leftovers

| ID | Item | Notes |
|----|------|--------|
| P0-4b | Live **ralplan / pipeline / ask** L2 gates | Not in `live_suite --full` default |
| P0-5b | **Clean-host** live (no OMC/OMX plugins) | doctor soft WARN only today; re-run suite after isolating discovery |
| P0-6 | Env / run-scope leaks | Parent `OMG_ALLOW_EXTERNAL_CLI`, run-scoped ULW envelopes enforcement gaps |

## C. Codex P1+

| ID | Item |
|----|------|
| P1-1 | Session-aware ralph / native `grok --resume` continuity |
| P1-2 | Multi-worker ULW live closed path (prepare/seal/integrate count) |
| P1-3 | Full L2 matrix + host fingerprint in every summary |
| P2 | deep-interview playbook, UltraQA-like loop, durable goal ledger |

## D. Live not re-run

| Item | Last known |
|------|------------|
| `live_suite --quota-heavy` | 2026-07-19 evening evidence only (cap-spawn, cancel) |
| Live spawn **missing capability_mode** host canary | Unit + deny reason only |

## E. Host / marketing residuals

| Residual | Policy |
|----------|--------|
| PreToolUse fail-open | Documented; never claim hard sandbox |
| Stop pin | **NEVER** until host blocking Stop + live canary |
| tmux team | **OUT_OF_SCOPE** Option B |
| doctor --strict on this machine | Expected FAIL (Claude hooks + foreign orch) |

---

## Suggested next PR order (if continuing)

1. Fable free audit complete → update `09-fable.md` + STATUS  
2. Optional dual-review post-P0  
3. Live suite gates for ralplan/pipeline (or quota-heavy refresh)  
4. Clean-host evidence pack  
5. Session resume design (host-native first)  
