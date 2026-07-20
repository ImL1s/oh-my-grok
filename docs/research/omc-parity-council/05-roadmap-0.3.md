# 0.3.x OMC-parity Roadmap — Planner（Advisor #5）

**date_utc:** 2026-07-20  
**role:** PLANNER  
**baseline:** oh-my-grok **0.2.5** Option B（plugin + `omg` CLI；無 Rust fork；無 tmux v1）  
**HEAD note:** ~`60d0882` + Option A spawn fail-closed `8f3bef4`  
**inputs:**
- `docs/research/omc-parity-council/BRIEF.md`
- `README.md`
- `docs/research/stop-continuation/CONSENSUS.md`
- `docs/research/remaining-blockers-0.2.4.md`
- `docs/superpowers/plans/2026-07-20-live-gates-completeness.md`
- `docs/research/autopilot-0.3/spec.md`
- `omg_cli/pipeline.py` / `omg_cli/modes.py`
- live suite evidence under `docs/research/live/`

**性質:** 可實作路線圖（implementable），非 dream list。不寫 product code。

---

## 1. North star（0.3）

0.3 的北極星不是「把 OMC 4.15.5 技能表搬到 Grok 上換皮」，而是：**在 Grok Build 原生能力邊界內，交付一條可信的 outer-loop 產品路徑**——`spawn_subagent` 平行、`capability_mode` 隔離、`omg` CLI 獨占 `passes`/`verified`、`ralph`/`pipeline` 外環負責「做到 verified 為止」。使用者得到的是 **Grok-native orchestration**（CLI 監督 + plugin playbook + host tool policy），不是 chat Stop pin、不是 tmux multi-process team、也不是第二套 completion 故事。0.3 成功的判準是：**一條真實 repo 工作流可被非作者用 `omg setup → doctor → ulw|ralph|pipeline → accept` 跑到 verified，且 isolation / acceptance 聲稱不超前於 unit + live 證據。**

---

## 2. 現況基線（一句話 + 狀態標籤）

| 面 | 狀態 | 證據 |
|----|------|------|
| 核心 CLI surface | **HAVE** | `setup doctor state cancel accept integrate worker ulw ralph ralplan ask pipeline dual-review` |
| 技能 / agents | **HAVE** | `skills/omg-*`、`agents/omg-{orchestrator,executor,critic,verifier}` |
| 平行 fan-out | **PARTIAL** | skill + `spawn_subagent` depth=1；auto-integrate / multi-worker seal 仍偏 convention |
| 持久迴圈 | **HAVE** | `omg ralph` outer CLI loop + `ralph_context_pack`（`modes.py`） |
| Plan consensus | **HAVE** | `omg ralplan` FSM；pipeline plan stage |
| Full auto pipeline | **PARTIAL** | `pipeline.py` FSM `plan→implement→integrate→dual_review→accept→report` + `--resume`；UX / open-box 仍薄 |
| Dual review | **PARTIAL** | sequential headless interim；`OMG_DUAL_REVIEW_REQUIRE_NATIVE` gate |
| Ask advisors | **HAVE** | `omg ask`（opt-in，非 default worker） |
| Team / tmux | **NEVER / OUT_OF_SCOPE** | 明確 v1 不做 |
| Stop pin | **NEVER**（host） | `CONSENSUS.md`：Stop 非 blocking；**DO NOT BUILD** 0.3.x |
| Acceptance / verified | **HAVE** | `omg accept` + `command_policy`；僅 CLI 可 stamp |
| Capability isolation | **PARTIAL→hardening** | primary = `capability_mode`；Option A spawn PreToolUse gate 已落地 |
| Live gates | **PARTIAL** | `scripts/live_suite.sh` + `docs/research/live/*` dated evidence |

---

## 3. Priority buckets

### P0 — 必須在 0.3.0 關閉才稱「core usable」

目標：**產品路徑誠實、可重現、隔離決策 fail-closed（hook 有跑時）**。

| ID | Deliverable | 模組 / 檔案級 | 驗收 |
|----|-------------|---------------|------|
| **P0-1** | Spawn fail-closed 產品化收斂（Option A 已有 → 文件 + doctor + canary 對齊） | `omg_cli/deny.py`、`hooks/hooks.json`、`hooks/bin/pre_tool_use_deny.py`、`omg_cli/doctor.py`、`tests/test_deny.py`、`docs/security-model.md` | unit matrix 全綠；doctor 檢查 matcher 含 `spawn_subagent`；README/security 誠實寫「hook crash 仍 fail-open」 |
| **P0-2** | ULW 一鍵可整合：run 後 auto-integrate **或** 明確 WARN + non-zero when envelopes dirty | `omg_cli/modes.py`（ulw exit path）、`omg_cli/integrate.py`、`skills/omg-ultrawork/SKILL.md`、tests | `omg ulw` 結束時：有 envelope 則 integrate 或 exit≠0 並印下一步；空 `changed_files` 繼續 reject |
| **P0-3** | Leader 擁有 prepare/seal 契約（多 task 不靠 worker shell commit） | `omg_cli/workers.py`、`skills/omg-ultrawork/SKILL.md`、`agents/omg-orchestrator.md`、tests | skill 強制 leader 呼叫 `omg worker prepare|seal`；unit 覆蓋 dirty seal fail-closed |
| **P0-4** | Pipeline resume 可操作 + stage 失敗可讀 | `omg_cli/pipeline.py`、`omg_cli/main.py`、`skills/omg-pipeline/SKILL.md` | `omg pipeline --resume <run>` 從不重複 skip 需 re-integrate 的 stage（現有 `_integrate_stale` 契約有 test）；失敗時 `report.json` + stderr 含 stage |
| **P0-5** | Live-gates 回歸護欄不退化 | `scripts/live_suite.sh`、`scripts/canary_pretool.py`、`docs/research/live/` | `--quick` 在 CI 可選；本地 release 前 `--full` 有 dated summary；claim 不超前 evidence |
| **P0-6** | 聲稱語言凍結（anti-marketing debt） | `README.md`、`docs/security-model.md` | 無「hard sandbox / Stop continue / native dual done」誤導句；dual = sequential interim 明示 |

**P0 不做：** native dual spawn、process fanout 升 default、Stop continuation、wiki/HUD 大功能。

---

### P1 — 0.3.1–0.3.2 產品厚度（core 之後）

| ID | Deliverable | 模組 / 檔案級 | 為何 |
|----|-------------|---------------|------|
| **P1-1** | ULW multi-worker live matrix + evidence | `scripts/fixtures/live/`、`docs/research/live/`、skill 契約 | 證明平行不是單 worker 假象 |
| **P1-2** | Dual-review 產品決策：維持 sequential **或** 單一 leader + 兩 spawn 包裝（仍非 OMC team） | `omg_cli/dual_review.py`、`skills/omg-dual-review/SKILL.md` | 關閉「interim」永久負債；若仍 sequential，README 永久標 **PARTIAL** 並設 flag 防誤用 |
| **P1-3** | Context pack 擴到 pipeline implement / 通用 resume pack | `omg_cli/modes.py`（`ralph_context_pack` 泛化）、`pipeline.py` | 中斷後重進不靠模型記憶 |
| **P1-4** | PRD / stories UX 最小可用（template + doctor 檢查缺 commands） | `templates/`、`omg_cli/acceptance.py`、`omg_cli/doctor.py` | 降低「ralph 空轉無 acceptance commands」 |
| **P1-5** | Cancel 多 PID / worker 表完整性 | `omg_cli/state.py`、`omg_cli/fanout.py`（若仍 experimental）、tests | cancel 必須殺到所有已記錄 worker；fail-closed 不變 |
| **P1-6** | `omg state` / 人讀 run summary（輕量 HUD 替代） | `omg_cli/main.py` state 子命令、可選 `skills/omg-using` | 同目的於 OMC HUD，但不做 TUI |

---

### P2 — 有價值但可延後（0.3.x 晚期或 0.4）

| ID | Deliverable | 備註 |
|----|-------------|------|
| **P2-1** | Open-box pipeline UX（`--plan-only` 產物預覽、stage dry 說明、artifact index） | 對齊 OMC autopilot 可觀察性，非功能複製 |
| **P2-2** | Process fanout 是否 promote：僅在 live isolation 證據齊全後 | 預設仍 **spawn_subagent**；`OMG_EXPERIMENTAL_PROCESS_FANOUT` 維持 |
| **P2-3** | Deep-interview 風格 intake skill（純 playbook，無新 host API） | 對齊 OMC deep-interview **purpose**；CLI 可選 preflight |
| **P2-4** | Ultragoal 目錄慣例 + 單檔 durable goal（`.omg/artifacts/ultragoal/` 已在 scaffold） | **PARTIAL** 慣例 → 單一 skill + state pointer，非完整 OMC ultragoal 系統 |
| **P2-5** | UltraQA / visual-verdict 類驗收擴充 | 僅在 accept policy 可表達時做；不預先建框架 |
| **P2-6** | Notifications / remember / skillify / self-improve | 低 ROI；有需求再單開 |

---

### WONTFIX / NEVER / OUT_OF_SCOPE（0.3.x 明確不做）

| Item | 標籤 | 理由 |
|------|------|------|
| In-session **Stop continuation** / ForceContinue pin | **NEVER**（host） | `CONSENSUS.md`：僅 PreToolUse blocking；Stop 被動。重訪條件：host 加 blocking Stop **且** live canary |
| **tmux / omc-teams** multi-process control plane | **OUT_OF_SCOPE** | Option B 明確；平行 = `spawn_subagent` |
| Fork **grok-build** / Rust agent runtime | **OUT_OF_SCOPE** | README 架構鎖死 |
| Default workers = claude/codex/omc/agy | **WONTFIX** | HARD RULES；advisor 僅 `omg ask` opt-in |
| PreToolUse 宣稱 **hard sandbox** | **WONTFIX** | fail-open 誠實；primary = capability_mode |
| Dual-review 寫入 `verified` | **WONTFIX** | `verified` 僅 `omg accept` |
| 完整 **wiki** 系統 | **OUT_OF_SCOPE** 0.3 | 無產品痛點證據；用 `docs/` + artifacts |
| OMC skill 表全量 clone（hud TUI、sciomc、ccg 內建…） | **OUT_OF_SCOPE** | 只對 **purpose** 做 Grok-native 對等 |
| Chat 內「做到停」靠 hook 再注入 | **NEVER** | 改用 `omg ralph` / `omg pipeline` |

---

## 4. Same purpose as OMC X — without Stop

「功能對等」= **使用者目的對等**，不是 API / hook 同構。

| 使用者目的 | OMC 手段（參考） | OMG 0.3 手段（應建 / 已有） | 狀態 |
|------------|------------------|------------------------------|------|
| 平行拆活、多 worker | ultrawork + team/spawn | `omg ulw` + `spawn_subagent` depth=1 + `omg integrate` + worker prepare/seal | **PARTIAL→P0** |
| 不做到完不罷休 | ralph + **Stop pin** | **`omg ralph` outer loop** + context pack；**無** Stop reinject | **HAVE**（路徑不同） |
| 全自動 plan→code→verify | OMC autopilot skill | **`omg pipeline`** FSM + report；CLI 監督 | **PARTIAL→P0/P1** |
| 計畫共識再動手 | ralplan / plan consensus | `omg ralplan` + pipeline plan stage | **HAVE** |
| 雙重 review | dual-review / ccg | `omg dual-review`（sequential interim）；P1 決策 native-or-honest | **PARTIAL** |
| 問外部顧問 | ask / multi-LLM | `omg ask <provider>`（stdin；不進 worker） | **HAVE** |
| 中止 run | cancel | `omg cancel` + pid.json starttime fail-closed | **HAVE** |
| 驗收閘門 | verify / acceptance | `omg accept` + semantic `command_policy`；CLI stamp only | **HAVE** |
| 環境健康 | doctor / setup | `omg setup` / `omg doctor` / `--strict` | **HAVE** |
| 持久目標 | ultragoal | `.omg/artifacts/ultragoal/` 慣例 + P2 skill | **PARTIAL** |
| Session 繼續感 | Stop continuation / HUD | **CLI resume**（`pipeline --resume`）+ `omg state`（P1 輕量） | **PARTIAL**；Stop = **NEVER** |
| 深度需求訪談 | deep-interview | P2 playbook skill（可選） | **MISSING→P2** |
| Team 多進程 | team / tmux | **不做**；文件導向 spawn | **OUT_OF_SCOPE** |
| Wiki / 知識庫 | wiki | **不做** 0.3 | **OUT_OF_SCOPE** |
| 通知 | notifications | **不做** 0.3 | **OUT_OF_SCOPE** |

**關鍵敘事（給 README / 使用者）：**

```text
OMC「做到停」≈  chat Stop block + reinject
OMG「做到停」≈  omg ralph | omg pipeline   # 外環 process loop
```

禁止在 skill 裡暗示「Stop hook 會強制繼續」。

---

## 5. Acceptance criteria — 「core OMC-class usable」

**宣告 0.3.0 = core usable** 當且僅當下列 **全部** 成立（strictest-wins；文件不算關閉）：

### 5.1 功能路徑（happy path）

1. 新鮮專案：`omg setup` → `omg doctor`（含 global PreToolUse + spawn matcher 檢查）無 hard FAIL。  
2. `omg ralph "…" --max-iter N` 能多輪迭代，每輪注入 context pack；**僅** `omg accept` 後 `verified=true`。  
3. `omg ulw "…"` 能 spawn ≥1 implementer（`capability_mode=read-write`），產出 envelope；**integrate 有結果或明確失敗**（不可 silent orphan worktree）。  
4. `omg pipeline "…"` 走完整 stage order；成功寫 `report.json`（`writer: omg-cli`）；失敗可 `--resume`。  
5. `omg cancel` 能終止 active run（starttime 不符則 **不** kill）。  
6. `omg ralplan` 可在無 implementation 下產出共識計畫產物。

### 5.2 安全 / 誠實

7. Unit：spawn 缺 `capability_mode` → deny；role/mode 不符 → deny（`tests/test_deny.py`）。  
8. Unit：acceptance 拒絕 `python -c` / agent CLI / 破壞性 git；允許 project pytest 等 grammar v2。  
9. Integrate：空 `changed_files` 不可 forge merge。  
10. 文件明確：PreToolUse **fail-open on crash**；primary isolation = **capability_mode**；無 Stop pin。

### 5.3 證據

11. `PYTHONPATH=. python3 -m pytest` 全綠（unit/integration markers）。  
12. `scripts/smoke.sh` dry 全綠。  
13. 至少一份 dated `docs/research/live/suite-*-full.summary.json` status ok（或同等 full suite），且 README claim 不超過該證據。  
14. Canary 不宣稱「DENIED」若無 hook oracle（`DENIED_CLAIMED_NO_HOOK_ORACLE` 誠實分類）。

### 5.4 非條件（明確不要求）

- 不要求 native dual-review spawn。  
- 不要求 tmux team。  
- 不要求 Stop continuation。  
- 不要求 wiki / HUD TUI / notifications。  
- 不要求 process fanout 為 default。

---

## 6. Anti-goals（執行時禁止漂移）

1. **禁止** 為「OMC 同款感覺」實作 Stop reinject / 假 block JSON。  
2. **禁止** 引入 tmux / 多 CLI team 當 default isolation 故事。  
3. **禁止** 讓 agents 寫 `verified` / `passes`。  
4. **禁止** 把 PreToolUse soft-gate market 成 sandbox。  
5. **禁止** 預設呼叫 claude/codex/omc 當 worker（advisor 僅 `omg ask`）。  
6. **禁止** 為對齊 OMC 技能目錄而開一堆空 skill（skillify、sciomc…）。  
7. **禁止** 在 dual-review 未 native 時刪掉 interim 標記或移除 `OMG_DUAL_REVIEW_REQUIRE_NATIVE` 保護。  
8. **禁止** 擴大 process fanout 為預設路徑，除非獨立 ADR + live 證據。  
9. **禁止** fork grok-build 或依賴未文件化 host 私有 API。  
10. **禁止** 版本號超前於 §5 驗收（strictest-wins：版本不是證據）。

---

## 7. Suggested next 3 PRs only

只排 **接下來 3 個 PR**（再往後等這三個合入與證據再切）。每個 PR 可獨立 merge、有明確 test。

### PR-1 — Spawn gate productization + claim freeze

**標題建議:** `fix(0.3): spawn fail-closed productize + honest security claims`

**範圍:**
- 凍結 / 補齊 `deny.py` role table 與 `hooks.json` matcher 文件一致性  
- `doctor` 對 spawn matcher / global hook 的 hard check 保持綠路徑可測  
- `README.md` + `docs/security-model.md`：Stop **NEVER**、pipeline/ralph = persistence、dual interim  
- 測試：`tests/test_deny.py`、`tests/test_doctor.py` 回歸  

**不做:** ULW 行為變更、pipeline 新 stage  

**驗收:** pytest 相關子集綠；文件無 hard-sandbox / Stop-continue 誤導  

---

### PR-2 — ULW post-run integrate + leader prepare/seal contract

**標題建議:** `feat(0.3): ulw auto-integrate-or-fail + leader prepare/seal contract`

**範圍:**
- `omg ulw` 結束路徑：偵測 `.omg/artifacts/ulw-results/*.json` → 呼叫 integrate 或 WARN+exit≠0  
- skill / orchestrator：多 task 時 leader 必須 `omg worker prepare|seal`（無 shell worker commit 幻想）  
- unit/integration：envelope missing、empty changed_files、seal dirty fail-closed  
- 可選：ulw dry-run 印出 integrate 意圖  

**不做:** process fanout、native dual  

**驗收:** 無 envelope 的 ULW（ralph-like 單線）不誤殺；有 envelope 則不可 silent skip integrate  

---

### PR-3 — Pipeline resume/report polish + live_suite regression hook

**標題建議:** `feat(0.3): pipeline resume clarity + live suite regression bar`

**範圍:**
- 強化 `pipeline --resume` 使用者輸出（current stage、next stage、integrate stale 原因）  
- 確保 re-implement 後 integrate 不跳過（鎖 `_integrate_stale` / AC4 測試）  
- `report.json` 失敗路徑欄位穩定  
- `scripts/live_suite.sh`：`--quick` 文件化為 release 最小 live bar；必要時修 flaky fixture  
- 更新 `docs/research/test-matrix.md` 一行：0.3 core path coverage  

**不做:** open-box UX 大改、deep-interview、HUD  

**驗收:** pipeline unit 全綠；`--quick` 可在乾淨環境重跑；§5.1 第 4 條可 demo  

---

**PR 之後（不在「next 3」內，僅路標）:** P1 dual 決策 → multi-worker live matrix → context pack 泛化 → 輕量 `omg state` summary。

---

## 8. 版本切片建議

| 版本 | 內容 | 出貨條件 |
|------|------|----------|
| **0.3.0** | P0 全關 + next 3 PRs | §5 core usable checklist |
| **0.3.1** | P1-1 multi-worker live + P1-2 dual 決策落地 | dated multi-worker evidence |
| **0.3.2** | P1-3..P1-6 厚度 | resume pack + state UX |
| **0.3.x later / 0.4** | P2 可選 | 各項獨立 ADR，不綁 core |

---

## 9. Open questions（執行前需用戶或 host 證據，不阻塞 PR-1）

寫入規劃追蹤；**不**阻擋 spawn/claims PR。

1. Dual-review：永久 sequential honest，還是 0.3.1 做「single leader + 2× read-only spawn」包裝？  
2. ULW auto-integrate：預設 on，還是 `--integrate` / `--no-integrate` flag（建議：**預設 on，可 `--no-integrate`**）？  
3. Process fanout：0.3 是否維持 experimental-only（建議：**是**）？  
4. Host 若未來加 blocking Stop：是否重開 continuation ADR（建議：僅 canary 通過後）？

（若 repo 使用 `.omc/plans/open-questions.md` 慣例，council 合成器可匯入；本檔自洽完整。）

---

## 10. Planner checklist

- [x] North star：Grok-native，非 OMC clone  
- [x] P0/P1/P2/WONTFIX 具體到模組  
- [x] OMC purpose mapping without Stop  
- [x] Core usable acceptance criteria  
- [x] Anti-goals  
- [x] 僅 3 個 next PRs  
- [x] 尊重 Stop **DO NOT BUILD** 共識  
- [x] 無 product code 修改  

---

## 11. 一句話給合成器

**0.3 = 把「CLI outer-loop + spawn isolation + ULW integrate 閉環 + 誠實文件」做成可宣告的 core product；Stop/tmux/wiki 明確永不在本代；下三個 PR 只做 spawn claims、ULW integrate/seal、pipeline resume + live bar。**
