# OMC parity multi-Grok council — SYNTHESIS

**date_utc:** 2026-07-20  
**roster:** 7 Grok-native subagents + **Codex free audit** + Fable BLOCKED + spawn-retry code review  
**repo:** oh-my-grok 0.2.5+ (post-council P0 shipped on `main`; see [`STATUS.md`](./STATUS.md))  
**detail reports:** `01`–`07`, `08-codex.md`, `09-fable.md` (BLOCKED), `code-review-spawn-retry.md`  
**Index:** [`README.md`](./README.md) · **Done/not-done:** [`STATUS.md`](./STATUS.md) · **Live verify:** [`../live/verification-2026-07-20.md`](../live/verification-2026-07-20.md)

### Post-ship note (same day)

Codex P0 **false-green** items for dual/ralplan + spawn RETRY + ULW auto-integrate + L-DUAL semantic + canary capability path were **implemented and live-verified** after this synthesis was first written.  
**Claude/Fable free audit is still BLOCKED** — do not claim multi-external consensus.

---

## 一句話答案

**不是「OMC 功能基本都有了」。**  
是：**CLI control-plane 骨架有價值**；**完整 OMC 表面遠未到**；**Stop pin NEVER**。  
**Codex 外審升級：0.3 第一優先不是擴功能，是消滅假綠（dual/ralplan verdict）與乾淨 host 證據。**

| 維度 | Grok council | Codex free (strict) |
|------|----------------|---------------------|
| Core orchestration | ~5–6/10 | **4/10**（gate 假綠下調） |
| Full OMC surface | ~2–3/10 | 同方向 NO |
| Trust honesty docs | 7–8/10 | 方向對，但 live 假綠抵銷 |
| Stop pin | NEVER | NEVER |
| Live「core works」 | 窄義 YES | **3/10** — dual 假綠 + 污染 host 證據 |

---

## External advisors

| Seat | File | Status |
|------|------|--------|
| Codex gpt-5.6-sol max free explore | `08-codex.md` | **DONE** — ONLY_IF 窄義 YES；產品級 NO；**P0 dual/ralplan 假綠** |
| Claude Fable free explore | `09-fable.md` | **BLOCKED**（CLI hang / prompt contract；本輪棄權） |
| Code review spawn-retry | `code-review-spawn-retry.md` | **Ready to proceed**（0 Critical） |

### Codex 推翻／硬化 Grok SYNTHESIS 的關鍵點

1. **dual-review 可假綠 APPROVE**（live 已出現 REQUEST CHANGES 文 vs CLI APPROVE；stub/rc=127 可變 APPROVE）→ **P0 blocker**  
2. **ralplan 同類 prose APPROVE 掃描**（`Do not APPROVE` 可通過）→ **P0**  
3. **live suite `status=ok` 語意不足**（不 assert verdict；`|| true`）  
4. **host 非 OMG-only**：`grok inspect` 載入 OMC 4.15.5 等；doctor 說 plugins empty 是 heuristic 盲點  
5. **don’t-stop**：應用 Grok **native sessionId / --resume**，不只 RESUME.md 紙本  
6. Roadmap 重排：**先修 verdict schema + fail-closed stage + semantic live**，再 ULW integrate / session-aware ralph

---

## 七席投票摘要

| # | Role | File | Verdict highlight |
|---|------|------|-------------------|
| 1 | Feature inventory | `01-feature-inventory.md` | Core 70–80%；full 25–35%；Stop NEVER |
| 2 | Architect don’t-stop | `02-dont-stop-design.md` | CLI+P0 resume/pack；永不假 Stop block |
| 3 | Critic | `03-critic-gaps.md` | **REJECT** marketing parity；product-lie table |
| 4 | Skill depth | `04-skill-depth.md` | skill 薄 CLI 厚；deep-interview=0；agents 4 vs 19 |
| 5 | Roadmap planner | `05-roadmap-0.3.md` | P0 spawn/ULW integrate/pipeline resume/live bar |
| 6 | Security | `06-security-isolation.md` | capability primary；PreToolUse soft；compound fail risk |
| 7 | Live verifier | `07-live-evidence.md` | narrow PASS；broad overclaim FAIL |

**Strictest wins:** Critic + Verifier 對「基本都有了 / 所有 core loops 都活了」→ **REJECT**。  
**Architect + prior Stop council** 對 in-session pin → **DO NOT BUILD**。  
**Inventory + Planner** 對「接下來做什麼」→ 強化 CLI outer-loop 產品化，不克隆 skill zoo。

---

## 功能矩陣（council 合併）

| Feature | OMG | Status | 一句 notes |
|---------|-----|--------|------------|
| Parallel fan-out (ulw) | skill + spawn | **PARTIAL** | live 多為 solo smoke；無 auto-integrate 強制 |
| Persistence (ralph) | CLI max_iter + pack | **HAVE** | ≠ chat Stop pin |
| Plan consensus (ralplan) | CLI FSM | **HAVE** / live **MISSING** | 無 L-RALPLAN |
| Full auto (autopilot) | pipeline composition | **PARTIAL** | 無 L-PIPELINE |
| Dual review | sequential interim | **PARTIAL → P0 patched (strict verdict)** | 修後：否定/非 terminal/rc≠0 不可 APPROVE；仍 interim sequential |
| Ask advisors | `omg ask` | **HAVE** | human broker only |
| Team / tmux | — | **OUT_OF_SCOPE** | Option B |
| Stop pin | passive stop.py | **NEVER** | host only PreToolUse blocks |
| Context / resume | ralph pack；pipeline `--resume` | **PARTIAL** | 缺通用 `omg resume` + RESUME.md |
| Doctor / setup | yes | **HAVE** | global PreToolUse hard-check |
| Cancel | killpg + starttime | **HAVE** live | multi-PID thickness **PARTIAL** |
| Accept / verified | CLI-only | **HAVE** live | OMG 強項 |
| Capability isolation | host toolset | **HAVE** | primary isolation |
| PreToolUse canary | parent+child | **HAVE** soft | need global hook |
| Spawn mode gate | Option A | **PARTIAL** soft | unit 強；缺 dedicated live spawn-deny oracle |
| HUD / wiki / notif | — | **MISSING** | 0.3 WONTFIX 多數 |
| Deep-interview | — | **MISSING** | 最大 skill 洞之一 |
| UltraQA / ultragoal engine | empty dir only | **MISSING** | 勿當有功能 |
| Agent catalog | 4 roles | **PARTIAL** | 靠 Grok built-ins |

---

## 「不要停到做完」— 同目的、不同機制

| 產品 | 機制 | Grok 可移植？ |
|------|------|----------------|
| OMC | Stop `decision:block` + reason reinject | **NO**（Stop 非 blocking） |
| OMX | Stop review gate | **NO**（同 host 依賴） |
| omo | client `session.prompt` inject | **NO**（無等價 API） |
| **OMG** | **`omg ralph` / `omg pipeline` 外 process 迴圈 + context pack** | **YES — 這就是正解** |

**0.3 該做（Architect P0/P1）：** louder context pack、`omg resume`/人讀 status、SessionStart→`.omg/state/RESUME.md`、CLI banner + using 文案。  
**0.3 不該做：** 假 Stop block、skill 內無限 self-loop 冒充 OMC。

---

## 分數板（合併）

| 指標 | Inventory | Critic | 採用（strict） |
|------|-----------|--------|----------------|
| Core orchestration | 72–78% | 5/10 | **~5–6/10 可用骨架** |
| Full OMC surface | 25–35% | 2/10 | **2–3/10** |
| Trust honesty | — | 8/10 | **7–8/10**（overclaim 風險仍在） |
| Live proven core | — | — | **窄義通過**（見 07） |

---

## 0.3 下一步（**Codex strictest wins** + Planner）

### P0（先於任何「parity 功能」）

1. **Strict verdict schema** — 禁 prose APPROVE 掃描；negation 不得通過  
2. **Stage fail-closed** — rc≠0 / stub / stale artifact → FAILED  
3. **dual + ralplan + pipeline 繼承修** + 回歸測試  
4. **Semantic live suite** — assert REQUEST_CHANGES 等；summary 列 verdict/hash  
5. **Clean-host proof** — live 前 `grok inspect --json`；doctor 用 discovery graph  
6. **Env / run-scope 洩漏** — parent `OMG_ALLOW_EXTERNAL_CLI` hard fail；ULW run-scoped envelopes  

### 並行已做（本 session）

- Spawn deny **RETRY IMMEDIATELY** UX（code review APPROVE）  
- Fable argv contract 寫入 dual-review / multi-llm-council skills  

### 原 Planner 下 3 PR → 順延到 P0 假綠關閉後

- ULW auto-integrate-or-fail  
- Session-aware ralph / native `--resume`  
- multi-worker live matrix  

---

## 允許 / 禁止說的話

**Allowed**  
「有 CLI run state / cancel / accept-verified 骨架；Stop pin 不做；capability_mode 是主隔離；ulw/ralph 有 smoke。」

**Forbidden**  
「OMC 功能基本都有了。」  
「dual-review / ralplan 是可信 gate。」（**直到 P0 假綠修掉**）  
「live suite green = 產品可信。」  
「Workers hard sandbox。」  
「ultrawork 已證明多 worker 平行。」

---

## Source files

- `BRIEF.md`  
- `01`–`07` Grok council  
- **`08-codex.md`（free external — highest-signal）**  
- `09-fable.md` BLOCKED  
- `code-review-spawn-retry.md`  
- `docs/research/stop-continuation/CONSENSUS.md`  
- Live under `docs/research/live/`
