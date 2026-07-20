# Discussion delivery STATUS — 2026-07-20

**Question answered:**「剛剛討論的都做完了嗎？Claude Code 和 Codex 的？」

**Short answer:** **No — not everything.** Product P0 + Grok council + Codex free audit + local live verification shipped; **Claude/Fable independent free audit remains BLOCKED**; several Codex roadmap items remain open.

**Repo HEAD at docs write:** see `git log -1` on `main` (docs commit follows verification `9b5c806` era).

---

## 1. External advisors (Claude / Codex)

| Seat | Requested | Delivered? | Artifact | Notes |
|------|-----------|------------|----------|--------|
| **Codex** free explore (OMC parity / don’t-stop / roadmap) | Yes | **YES** | [`08-codex.md`](./08-codex.md) | Full long-form audit; drove P0 “false green” priority |
| **Claude Fable** free explore (same brief) | Yes | **NO** | [`09-fable.md`](./09-fable.md) | **BLOCKED** — CLI hang / argv contract failures; no independent report |
| Dual-review of *post-ship* product diff (Codex + Fable) | Optional later | **NO** | — | Not re-run after P0 commits |
| Fable CLI reliability notes in global skills | Yes (ops) | **YES** | `~/.agents/skills/dual-review/SKILL.md`, `multi-llm-council/SKILL.md` | Outside this repo; argv contract documented |

### Claude / Fable — what failed

Repeated headless launches failed or hung:

1. Prompt before flags → `Input must be provided…`
2. Empty stdin + bad argv order → same
3. Stdin-only prompt → process alive, log stuck on permission noise, no report file

**Skill fix (global, not product code):** all `claude -p` options **before** prompt, or prompt via stdin only; empty MCP; no `--bare`.

**To complete Fable seat later:** re-run free audit with fixed contract → overwrite `09-fable.md` with real report (not BLOCKED stub).

### Codex — what was used

- Free exploration of OMG + OMC 4.15.5 + Grok docs + live evidence.
- Verdict: **ONLY_IF** narrow CLI skeleton; product-level **NO**.
- Critical: dual/ralplan prose APPROVE false green; live suite semantic weakness; foreign host pollution.

---

## 2. Multi-Grok council

| # | Role | File | Status |
|---|------|------|--------|
| 1 | Feature inventory | `01-feature-inventory.md` | Done |
| 2 | Don’t-stop design | `02-dont-stop-design.md` | Done |
| 3 | Critic honesty | `03-critic-gaps.md` | Done |
| 4 | Skill depth | `04-skill-depth.md` | Done |
| 5 | Roadmap 0.3 | `05-roadmap-0.3.md` | Done |
| 6 | Security isolation | `06-security-isolation.md` | Done |
| 7 | Live evidence | `07-live-evidence.md` | Done |
| — | Synthesis | `SYNTHESIS.md` | Done (updated by Codex strictest-wins) |
| — | Spawn-retry review | `code-review-spawn-retry.md` | Done |

---

## 3. Product work shipped after discussion

| Theme | Status | Code / commits (approx) |
|-------|--------|-------------------------|
| Spawn deny **RETRY IMMEDIATELY** UX | **Shipped** | `omg_cli/deny.py`, skills, AGENTS, orchestrator |
| Strict **verdict** (negation, terminal APPROVE, stubs) | **Shipped** | `omg_cli/verdict.py`, dual_review, ralplan |
| Stage **rc≠0** cannot APPROVE | **Shipped** | dual_review + ralplan |
| ULW **auto-integrate** (fail if dirty envelopes) | **Shipped** | `modes._ulw_auto_integrate` |
| Live **L-DUAL-1** semantic gate | **Shipped** | `scripts/live_suite.sh` |
| Doctor **foreign orch** soft check | **Shipped** | `doctor.check_effective_discovery_foreign` |
| `omg state --human` | **Shipped** | `main.py` |
| Canary: parent host + child **capability** isolation pass | **Shipped** | `canary_classify.py` → `DENIED_PARENT_HOST_CHILD_CAPABILITY` |

---

## 4. Verification (ran on author machine 2026-07-20)

Canonical table: [`../live/verification-2026-07-20.md`](../live/verification-2026-07-20.md).

| Gate | Result |
|------|--------|
| `pytest -m 'not live'` | **301 passed** |
| `scripts/smoke.sh` | **OK** + e2e ALL_REAL_E2E_OK |
| `omg doctor` (hard) | **OK** (soft WARN: foreign orch, Claude hooks) |
| `canary_pretool.py --live` | **exit 0** `DENIED_PARENT_HOST_CHILD_CAPABILITY` |
| `live_suite.sh --quick` | **OK** |
| `live_suite.sh --full` | **OK** (incl. L-DUAL-1 semantic) |
| `live_suite.sh --quota-heavy` | **Not run** this round |

---

## 5. Codex roadmap checklist (strictest)

| ID | Item | Status after ship |
|----|------|-------------------|
| P0-1 | Strict verdict schema | **Done** |
| P0-2 | Stage fail-closed on rc / stub | **Done** |
| P0-3 | dual + ralplan + pipeline inheritance | **Done** for dual/ralplan parse; pipeline inherits parse |
| P0-4 | Semantic live suite | **Partial** — L-DUAL-1 only; not ralplan/pipeline/ask L2 |
| P0-5 | Clean-host proof + doctor oracle | **Partial** — soft `grok inspect` foreign WARN; no clean-host re-run |
| P0-6 | Env / run-scope leaks | **Open** |
| P1-1 | Session-aware ralph / native `--resume` | **Open** |
| P1-2 | Multi-worker ULW live closed path | **Open** |
| P1-3 | Full L2 matrix | **Open** |
| NEVER | Stop pin / tmux team / hard sandbox claim | **Still NEVER** |

---

## 6. Marketing / claim freeze

### Allowed

- CLI run state / cancel / accept→verified skeleton exists and has live smoke evidence.
- Persistence = **outer CLI** (`omg ralph` / `omg pipeline`), not Grok Stop pin.
- Primary isolation = **capability_mode**; PreToolUse is soft fail-open.
- Codex free audit completed; Claude free audit **did not**.
- Post-P0: dual/ralplan no longer accept `Do not APPROVE` / free-floating APPROVE / non-zero rc as green.

### Forbidden

- 「OMC 功能基本都有了」
- 「Claude/Fable 也審完了」
- 「dual-review / ralplan 永遠可信」（until more live matrix + clean host）
- 「Workers hard sandbox」
- 「ultrawork multi-worker proven」

---

## 7. Next actions (if “全部” means external seats too)

1. Re-run **Fable free audit** with fixed `claude -p` argv → replace `09-fable.md`.
2. Optional: **dual-review** (Codex + Fable) on current `main` product commits only.
3. Run **quota-heavy** live on demand.
4. Clean-host live (disable foreign OMC plugins) for attributable evidence.
