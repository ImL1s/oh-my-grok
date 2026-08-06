# Autopilot 用法（skill + CLI）

English | [简体中文](./autopilot.zh.md) | [繁體中文](./autopilot.zh-TW.md)

**對象：** 使用 Grok Build 的人 + 維護 skill 的人。  
**版本：** 與 [`plugin.json`](../plugin.json) 一致（目前 **0.7.6**）。
**Skill 原文：** [`skills/omg-autopilot/SKILL.md`](../skills/omg-autopilot/SKILL.md)

---

## Autopilot 是什麼

| 元件 | 做什麼 |
|------|--------|
| **Skill `omg-autopilot`** | Session 內 playbook：釐清 → 計畫 → 寫碼 → 審查 → QA → accept |
| **CLI `omg autopilot *`** | 嚴格 phase 狀態機 + 目的地閘門；run 狀態在 `.omg/state/runs/<run_id>/` |
| **Workers** | 只透過 Grok `spawn_subagent`（depth 1）；實作者 `capability_mode=read-write`（無 shell） |

**與 OMC 不同：** Stop pin 在 grok **≥0.2.107** 真實存在，但有 **上限**（每 turn 8 次）、fail-open、可被 Esc/Ctrl+C 跳過。  
**跨 turn 持久化（首選手動外層）：** `omg autopilot run --resume RUN --unattended`（#40）— CLI 在 host turn stall 後自動重啟，直到 `verified` / `blocked` / interview / await。亦可 `/loop`、外層 `omg ralph "…"`。

### OMC 體驗 → OMG 對應

| OMC 期望 | OMG 對應 | 說明 |
|----------|----------|------|
| 不離開 session（Stop block） | **Stop pin（主要）** | grok ≥0.2.107；每 turn 上限 8 |
| 不依賴 Stop 的 turn 內繼續 | **`/goal`（次要）** | 宿主原生；Active 時繞過 Stop 閘門 |
| 跨 turn / 無頭 / 超 cap | **`omg autopilot run --resume … --unattended`（#40）** · `/loop` / `omg ralph` | 新 turn；計數重置；無需人工 `go` |
| 人類暫停（需求不清） | **`ask_user_question` + interview** | 閘門讓步；非 mid-phase 閒聊 |
| 破壞性 / 憑證暫停 | **`omg autopilot await`** | 設定 `autopilot_awaiting` |
| 取消粘性模式 | **`omg cancel`** | 不是「解開 Stop」 |
| Verified 完成 | **`omg accept` / `omg autopilot complete`** | 僅 CLI |

**執行時優先序：** 當 host `/goal` 為 **Active** 時，它主導 continuation，Stop 閘門在 goal 釋放前不會被諮詢；goal 釋放後 Stop pin 再執行剩餘 autopilot 閘門。

### Stop pin 誠實說明

- **有效：** autopilot 未完成 + `reason=end_turn` → hook 可 `decision:block` 並注入階段繼續提示（同一 turn）。
- **上限：** 單 turn **8** 次後繼續，宿主結束 turn。
- **Fail-open：** hook 崩潰/超時 → turn 可能結束。
- **跳過：** Esc、Ctrl+C、拒絕、max-turns — 不諮詢 Stop。
- **不用：** `TurnControl::ForceContinue`（宿主 stub；D17）。
- **超 cap / 無人值守：** `omg autopilot run --resume RUN --unattended`（#40）。可選：`/loop 5m omg autopilot status --run RUN`。

---

## 何時用

**適合：**

- 多階段：需求 → 計畫 → 實作 → 審查 → QA → verified  
- 你說 *autopilot*、*full auto*、*build me*、*handle it all*、*幫我做完*  
- 想用一個 coordinator skill，而不是自己串所有 CLI  

**改用別的：**

| 情境 | 改用 |
|------|------|
| 極小修正 | 直接改，或 `omg-ralph` 單一 story |
| 只要計畫 | `omg-ralplan` |
| 只要平行 | `omg-ultrawork` / `omg ulw` |
| 中止 | `omg-cancel` / `omg cancel` |
| 只是腦力激盪 | 聊天即可，不要開 autopilot run |

---

## 怎麼開始

### A. 在 Grok Build 裡（推薦）

1. 專案已跑過 `omg setup`，`omg doctor` hard 檢查通過。  
2. 呼叫 skill：  
   - 自然語言：`autopilot 完成 …` / `full auto: …`  
   - 或：`/oh-my-grok:omg-autopilot` + 目標  
3. 讓 agent 跑 CLI + workers。若 turn 中斷：  
   - 說 **繼續 / continue**  
   - 或：`omg autopilot status --run <RUN>` 後再呼叫 skill  

### B. 純終端機 CLI

```bash
omg doctor
omg autopilot start "完成功能 X"
# 需求已定：
omg autopilot start "完成功能 X" --skip-interview

RUN=…   # start 回傳的 run_id

omg autopilot transition --run "$RUN" --phase ralplan \
  --evidence-json '{"interview_complete":true}' --reason "interview closed"

omg autopilot transition --run "$RUN" --phase implement \
  --evidence-json '{"consensus":true}' --reason "ralplan APPROVE"

omg autopilot transition --run "$RUN" --phase review --reason "impl ready"
# omg review …
omg autopilot transition --run "$RUN" --phase qa --reason "review clean"
# omg qa freeze / run …
omg autopilot transition --run "$RUN" --phase acceptance --reason "ultraqa clean"
omg autopilot complete --run "$RUN"
omg autopilot status --run "$RUN"
```

非法 transition 會 fail closed。

---

## Phase 狀態機

```text
interview → ralplan → implement → review → (rework) → qa → acceptance → verified
```

另有 `blocked`、`cancelled`。

| 進入 | 需要的證據 / 章 |
|------|-----------------|
| `ralplan`（從 interview） | `interview_complete: true` |
| `implement` | `consensus: true` |
| `qa` | `stages/structured_review.json` clean |
| `acceptance` | `stages/ultraqa.json` status=`clean` |
| `verified` | **只能** `omg autopilot complete`（不可 `transition … verified`） |

**QA clean ≠ verified。** UltraQA 永不設 `verified`。

---

## Skill playbook（agent 應做的）

| 階段 | Skill / tools | CLI |
|------|---------------|-----|
| Bootstrap | — | `omg doctor`、`setup`、`autopilot status` |
| interview | `omg-deep-interview` | `omg interview *` → transition `ralplan` |
| ralplan | `omg-ralplan` + critic/verifier **read-only** | transition `implement` |
| implement | `omg-ultrawork` / `omg-ralph` + executor **read-write** | transition `review` |
| review | `omg-dual-review` 或 `omg review` | clean → `qa`；否則 `rework` |
| qa | `omg-ultraqa` | freeze → run → clean → `acceptance` |
| acceptance | — | `omg autopilot complete`（優先） |
| cancel | `omg-cancel` | `omg cancel` |

### Spawn 硬規則

1. 只經 `spawn_subagent`（depth 1）。  
2. 一律設 `capability_mode`。  
3. 被 deny 缺 mode → **立刻重試** 並補上。  
4. 預設 worker 不用 claude/codex/omc team/agy/cursor-agent。  
5. 不手寫 `verified`。

### UltraQA freeze（v0.3.2+）

```bash
omg qa freeze --run "$RUN" --scenarios-json \
  '[{"id":"unit","command":"python3 -m pytest -q -m '"'"'not live'"'"'"}]'
omg qa run --run "$RUN"
```

Clean 後 **`prd.json` 可省略** — accept/complete 會從 scenarios materialize（不覆蓋既有 operator PRD）。

### Complete / short-circuit（v0.3.2+）

```bash
omg autopilot complete --run "$RUN"
# 若已 omg accept --yes 成功，complete 只同步 phase，不再整輪重跑測試
omg autopilot status --run "$RUN"
# 期望：phase=verified、run_status=verified、autopilot_phase=verified
```

---

## Repository workflow 是另一層

若團隊要保存、review、版本化固定 stage graph，請用
`omg workflow install|list|show|plan|run`。Autopilot 可以依 plan 用 Grok 原生
`spawn_subagent` 執行，但不可改寫 contract 或捏造 receipt。Workflow 的
`ship` 也不能取代 `omg accept` 或 release state machine。詳見
[workflows.zh-TW.md](./workflows.zh-TW.md)。

Grok `/create-workflow` 與 Rhai projection 目前仍是 `optional_unclaimed`；只有
help 文字或本地 `.rhai` 檔不能當成已驗證 native integration。

## 相關 skills

| Skill | 角色 |
|-------|------|
| `omg-using` | 路由 |
| `omg-deep-interview` | 需求 |
| `omg-ralplan` | 計畫共識 |
| `omg-ultrawork` | 平行實作 |
| `omg-ralph` | 單 story 堅持 |
| `omg-dual-review` | 審查 |
| `omg-ultraqa` | QA |
| `omg-ultragoal` | 多 story ledger |
| `omg-cancel` | 中止 |

完整 15 個 skill：[`skills.zh-TW.md`](./skills.zh-TW.md)。

---

## 反模式

- 沒有 CLI 章就說「做完」  
- `transition --phase verified`  
- 用假 evidence 跳過 interview/ralplan  
- 實作完 self-approve  
- 無限 skill 自迴圈（先 status + 讓使用者 continue）  
- 把外部 agent CLI 當 worker  
- 宣稱 Stop pin 無限或適用於 grok <0.2.107  
- freeze 用 `grep` / `python -c` / `omg` 當 argv0  

---

## 狀態目錄

```text
.omg/state/runs/<run_id>/
  status.json
  stages/autopilot.json
  stages/structured_review.json
  stages/ultraqa.json
  prd.json                 # 可選；可從 ultraqa materialize
  acceptance.*
```

---

## 安全

主隔離：`capability_mode` + agent disallowed tools。  
Acceptance / QA：`command_policy`（操作者意圖閘，不是 OS sandbox）。  
詳見：[`security-model.md`](./security-model.md)（英文）。

---

## 快速指令

```bash
omg autopilot start "goal"
omg autopilot start "goal" --skip-interview
omg autopilot transition --run RUN --phase PHASE --evidence-json '{…}' --reason "…"
omg autopilot status --run RUN
omg accept --run RUN --yes
omg autopilot complete --run RUN
omg cancel
```
