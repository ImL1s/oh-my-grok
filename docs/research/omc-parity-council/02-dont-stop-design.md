# Architect #2 — Grok-native「不要停到做完」設計（host-feasible）

**date_utc:** 2026-07-20  
**role:** ARCHITECT（唯讀分析 + 設計；不實作 product code）  
**product:** oh-my-grok ~0.2.5 Option B  
**must_respect:** `docs/research/stop-continuation/CONSENSUS.md` → **DO NOT BUILD** in-session Stop continuation for 0.3.x  
**question:** 在 Grok host **Stop 非 blocking** 的前提下，如何達到 OMC persistent-mode **同目標**（別半途「禮貌結束」），卻**不造假 Stop block**？

---

## Summary

OMC「不要停到做完」靠 Claude Code **Stop hook `decision: "block"` + reason 重注**；Grok Build 的 Stop 是 **lifecycle / passive**（stdout 被忽略），只有 `PreToolUse` 能 block。oh-my-grok 已用 **CLI 外迴圈**（`omg ralph` / `omg pipeline`）+ 每輪 **context pack** 達成同目標。0.3.x 應強化 **P0 context pack / resume / SessionStart 注入** 與 **P1 pipeline·ralph UX 連續感**；**永不**在 0.3.x 實作假 Stop block。Blocking Stop 僅能當 **P2 host feature request**，且需 live canary 才重開 ADR。

---

## 1. 為何 OMC `decision:block` 在 Claude 有效，在 Grok 無效

### 1.1 OMC / Claude 機制（host-enforced Stop veto）

Claude Code 的 Stop hook 可回傳：

```json
{
  "continue": false,
  "decision": "block",
  "reason": "[RALPH LOOP - ITERATION n/max] Work is NOT done. Continue…"
}
```

證據（本機 marketplace）：

| 來源 | 行為 |
|------|------|
| OMC `templates/hooks/persistent-mode.mjs` ~L976–982 | active ralph → bump iteration → `decision: "block"` + reason 文案 |
| OMC Stop 鏈（`hooks/hooks.json`） | context-guard → workflow-drift → **persistent-mode** → code-simplifier |
| 官方 `ralph-loop` stop-hook | 同契約：`decision: "block"`, `reason`, 可選 `systemMessage` |

Host 會：**阻止 turn 結束**，並把 `reason` 當下一輪 user/system 輸入 → 同 session 內形成 while 迴圈。這是 OMC「keyword → mode 黏住直到 cancel」的物理基礎。

### 1.2 Grok host 契約（決定性）

官方 hooks 文件 `~/.grok/docs/user-guide/10-hooks.md`：

| 事實 | 行 / 要點 |
|------|-----------|
| `Stop`：agent turn 結束時觸發 | L90：`Blocking?` = **No** |
| **只有 `PreToolUse` 可 block**；其餘事件皆 passive | L99 |
| Blocking 決策 JSON **只文件化給 PreToolUse** | L188–193：`allow` / `deny` |
| Passive hooks：**stdout 被忽略**，成功即 exit 0 | L203–205 |
| Hook 失敗 fail-open | L152 |

既有研究交叉驗證：`docs/research/stop-continuation/stop-continuation-host-feasibility.md`、`CONSENSUS.md`、`stop-continuation-architect.md`（grok-build `HookEventName::is_blocking()` **僅** `PreToolUse`；Stop 走 `dispatch_non_blocking`）。

### 1.3 oh-my-grok 今天的 Stop hook（正確的誠實實作）

```1:21:hooks/bin/stop.py
#!/usr/bin/env python3
"""Stop hook: record session stop only. NEVER marks runs verified."""
...
        # CRITICAL: never set verified / acceptance status here — omg CLI is sole writer.
        append_event(
            root,
            {"event": "Stop", "status": "ok", "raw_keys": list(ev.keys())[:20]},
        )
...
        sys.exit(0)
```

掛線：`hooks/hooks.json` L25–34（command hook，timeout 10s）。**無** stdout decision protocol。

| 若在 Grok 印出 OMC JSON… | 結果 |
|--------------------------|------|
| `{"decision":"block","reason":"…"}` | **被忽略**（passive stdout） |
| 宣稱「Stop 會強制繼續」 | **產品謊言**（theatre） |
| 從 hook 寫 `verified` | **違反 single-writer**（`state.py` L1–6；`set_verified` 需 CLI acceptance token） |

### 1.4 根因（一句話）

**不是少寫 20 行 stop.py**，而是 **host 控制平面不同**：Claude Stop = 可 veto 的 control hook；Grok Stop = 可觀察的 lifecycle hook。在 Option B「不 fork grok-build」前提下，**無法**用 plugin file hook 重現 OMC persistent-mode。

**狀態標籤：** Stop pin / force continue = **NEVER**（host impossible on Grok 0.2.x file hooks；0.3.x 不建）

---

## 2. OMG 既有「同目的」機制地圖（HAVE / PARTIAL）

目標分解：**（A）工作沒做完不宣告完成** + **（B）多輪持續推進** + **（C）可取消** + **（D）verified 可信**。

| 目的 | OMC 做法 | OMG 對應 | Status | 證據 |
|------|----------|----------|--------|------|
| 多輪直到完成 | Stop-block 同 session 迴圈 | **`omg ralph` outer `for i in range(1, max_iter+1)`** | **HAVE** | `omg_cli/modes.py` L30–35, L724–781；`DEFAULT_MAX_ITER["ralph"]=3` |
| 每輪有新鮮指令 | hook `reason` 注入 | **`ralph_context_pack` + skill body + HARD_RULES** | **HAVE** | `modes.py` L99–196, L199–262；每 iter 重建 prompt |
| 一輪只做一個 unit | （OMC 常多 turn 黏住） | skill：**ONE story then STOP**；CLI 再 launch | **HAVE** | `skills/omg-ralph/SKILL.md` L8–12, L33–41, L72–76；`modes.py` L223–228 |
| 端到端 autopilot | OMC autopilot skill + persistent modes | **`omg pipeline` FSM** | **HAVE（product path）** | `pipeline.py` STAGE_ORDER L23；`skills/omg-pipeline/SKILL.md` |
| 從中斷接續 | 同 session 不中斷 | **`omg pipeline --resume <run_id>`** | **HAVE** | `pipeline.py` L352, L391–408；`main.py` `--resume` |
| 狀態可查 | mode state files | **`.omg/state/` + `omg state`** | **HAVE** | `state.py` OMG_SUBDIRS；`active.json`；`cmd_state` |
| 取消 | cancel skill + Stop allow | **`omg cancel` PID/starttime killpg** | **HAVE** | `state.py` cancel_run；`skills/omg-cancel` |
| verified 閘門 | 常靠 model / skill 退出 | **僅 CLI `set_verified` + acceptance token** | **HAVE（更嚴）** | `state.py` L1–6；`omg accept`；Stop 永不 verified |
| Session 開場提醒 | OMC 各種 inject | **SessionStart 僅 spool 事件** | **PARTIAL** | `session_start.py` L9–19 — 未注入 active run 摘要 |
| 互動 TUI 不開 CLI 也黏住 | Stop-block | 無 | **MISSING / NEVER** | host 不支援；見 CONSENSUS |
| 通用 resume CLI（ralph 非 pipeline） | n/a | 僅 pipeline resume；ralph 需新 run 或手抄 | **PARTIAL** | 無 `omg resume` 通用命令 |

### 2.1 `.omg/` 狀態平面（控制面真相）

```text
.omg/
  state/
    active.json              # 當前非終態 run 指標（mutex）
    events.jsonl             # hooks 只 append（SessionStart / Stop / SubagentStop）
    runs/<run_id>/
      status.json            # CLI single-writer：status / passes / verified
      prd.json               # ralph scaffold / acceptance 來源
      acceptance.result.json # 僅 omg-cli writer 可信
      pipeline.json          # FSM stage + history（pipeline）
      report.json            # 終態報告
      pid.json / pid         # cancel 用
      ralplan.json …         # 規劃 FSM
  artifacts/                 # agent proposals only（非 authoritative）
  plans/ research/ handoffs/ ultragoal/
```

契約（`state.py` module doc）：**hooks / agents 不可 mutate status·passes·verified**；只可 events + artifacts。

### 2.2 Ralph 外迴圈（persistence spine）

```text
create_run → for iteration 1..max_iter:
  build_prompt(skill + HARD_RULES + context pack) → grok -p
  freeze+run acceptance → maybe set_verified → break
  else next iter
→ completed-without-verified (exit 1 if require_acceptance) | verified | failed
```

Context pack 欄位（`modes.py` L185–196）：`run_id`, `iteration n/max`, `story`, `frozen_commands_summary`, `acceptance.result.json` 路徑，並明確禁止 forge acceptance。

### 2.3 Pipeline FSM（「autopilot feel」的正確落點）

```text
plan → implement (ralph|ulw) → integrate → dual_review → accept → report
```

- Resume：讀 `pipeline.json` stage，不跳過 re-integrate 約束（見 pipeline 註解 AC4）。  
- Dual APPROVE **≠** product verified（仍要 accept stage）。  
- Skill 明示：prefer CLI FSM，不要在 chat 內自造 autopilot。

### 2.4 SessionStart 今日角色

`hooks/bin/session_start.py`：ensure dirs + `append_event({event: SessionStart})`。  
**未**讀 `active.json`、**未**把「未完成 run + 建議 `omg ralph|pipeline --resume`」塞進 model context。

Grok SessionStart 同樣 **non-blocking / stdout ignored**（hooks 文件 L85–90, L203–205）→ 無法像某些 host 用 stdout 改 initial context。若要「注入」，必須走 **host 支援的 context injection 機制**（若有）或 **寫入 workspace 檔案供 skill/AGENTS 讀**（檔案副作用 — 可行）。

**狀態標籤：** Persistence loop = **HAVE**（CLI）；Context pack = **HAVE**（ralph）；SessionStart inject = **PARTIAL**；Stop force = **NEVER**

---

## 3. Grok-native 設計：同目標、無 Stop block

### 3.0 設計原則（硬約束）

1. **不建 fake Stop block**（CONSENSUS + 本文件）。  
2. **Persistence owner = omg CLI process**，不是 chat turn。  
3. **verified 唯一寫入者 = omg CLI**（accept token）。  
4. **Cancel** 仍靠 `omg cancel` + pid metadata，不靠 Stop allow。  
5. **雙迴圈禁止**：未來若 host 加 blocking Stop，CLI 與 in-session enforcer **互斥**（env 預設 CLI 時關 enforcer）。  
6. 對 OMC 移民的文案：**「不要停」= 跑 CLI，不是「在 chat 裡唸 ralph」**。

---

### P0 — 更大聲的 context pack / resume CLI / SessionStart 注入（**2 週內應做**）

目標：讓「中斷後繼續」與「每輪知道自己在第幾圈」變成 **零摩擦、可複製指令**，不需要 Stop。

#### P0-A. Louder context pack（ralph + pipeline implement）

**現況 HAVE：** `ralph_context_pack` 已注入。  
**缺口 PARTIAL：**

| 缺口 | 建議 |
|------|------|
| 失敗原因不明顯 | 若 `acceptance.result.json` 存在且 failed：把 **最後 N 行 stderr / failed command** 摘要進 pack（唯讀） |
| 剩餘 stories 不可見 | 從 prd 列 **remaining story ids**（截斷上限，避免 prompt 爆炸） |
| pipeline stage 不可見 | implement 階段 pack 加 `pipeline_stage` / `implement=ralph|ulw` |
| 終態 guidance | 未 verified 時 pack 尾：`Next operator action: omg accept | omg pipeline --resume <id> | omg cancel` |

**不做：** 把 context pack 寫進 Stop hook 當「continuation reason」並宣稱會重注。

#### P0-B. Resume UX CLI

| 命令 / 行為 | 說明 | Status today |
|-------------|------|--------------|
| `omg pipeline --resume <run_id>` | FSM 從 `pipeline.json` 續跑 | **HAVE** |
| `omg state` / `omg state --run <id>` | JSON dump | **HAVE** |
| **`omg resume`（建議）** | 讀 `active.json` 或最近 non-terminal；依 mode 分流到 pipeline resume 或 ralph re-enter（`existing_run_id` 已在 `run_mode` 支援） | **MISSING → P0** |
| **`omg status` human one-liner** | `run_id · mode · stage/iter · verified? · 下一指令` | **MISSING → P0**（可包在 `state --pretty`） |

`run_mode(..., existing_run_id=)` 已存在（`modes.py` L620, L689–696）— resume ralph **不必**重發明 state，只需 CLI 路由。

#### P0-C. SessionStart / 開場「檔案注入」（非 Stop）

因 SessionStart **stdout 無效**，採 **workspace side-effect**（host-feasible）：

1. SessionStart（或 `omg setup` / `omg doctor` 尾）：若存在非終態 active run，寫／更新：  
   `.omg/state/RESUME.md`（或 `handoffs/active-resume.md`）內容固定模板：

   ```markdown
   # Active OMG run (do not mark verified)
   - run_id: …
   - mode / stage / iteration: …
   - goal: …
   - Operator continue:
     omg resume
     # or: omg pipeline --resume <id>
     # or: omg ralph "…"  (new loop only if intentional)
   - Cancel: omg cancel
   ```

2. `skills/omg-using` + AGENTS fragment：開 session **先讀** `.omg/state/RESUME.md` if present。  
3. **仍 fail-open**；寫檔失敗不得 crash hook。  
4. **永不**從 hook 改 `status.json` / `verified`。

可選強化：`doctor` 若偵測 active 未完成 → 印出同樣 one-liner（人類在 terminal 可見；不依賴 model）。

#### P0 成功標準

- 操作者 **一條 `omg resume` / 印出的指令** 可接續，不必重貼 goal。  
- 新 Grok session 即使 Stop 什麼都不做，模型若遵 skill 會看到 RESUME 檔。  
- Live：ralph 2+ iter 的 prompt 檔含 context pack 欄位（已有 unit tests `test_modes.py`；補 acceptance-fail 摘要測試）。

---

### P1 — pipeline / ralph UX：讓「聊天感」連續（**2 週可排；可與 P0 重疊**）

目標：OMC 用戶體感的「我說了 keep going 它就自己啃」→ 對映到 **一個 terminal 長跑 CLI**，而不是假 sticky chat。

| 項目 | 設計 | 標籤 |
|------|------|------|
| **單一入口文案** | README / omg-using 表格固定：`don't stop` → `omg ralph`；`autopilot` → `omg pipeline` | PARTIAL → 補齊 |
| **Progress 噪音** | CLI 每 iter 印 banner：`[ralph 2/3] story=… accept=fail|pass`；pipeline 每 stage 一行 | PARTIAL → 加強 |
| **max_iter 產品預設** | 保持 ralph 預設 3（可 `--max-iter`）；文件寫「要更長就加 flag」，**不要**學 OMC 靜默 +10 | HAVE（正確） |
| **Chat skill 誠實邊界** | 互動只 load skill：一 unit 後 STOP；回覆末固定附 `To persist: omg ralph "goal"` | HAVE 文案；可再硬 | 
| **pipeline 預設 implement** | 維持 ralph；需要平行再 `--implement ulw` | HAVE |
| **「連續感」錯覺來源** | 外迴圈 N 次 `grok -p` 在同一 terminal scrollback = 連續；不是同一 TUI session | 教育 |

**反模式（P1 禁止）：**

- skill 教 model 在同一 interactive session 無限 self-loop「像 OMC」。  
- Stop 寫 advisory 到 stderr 然後文件寫「enforcer」。  
- dual-review APPROVE 當 verified。

#### P1 成功標準

- 新用戶只記 **三條**：`omg ralph` / `omg pipeline` / `omg cancel`。  
- `omg doctor` 或 using skill 明確一句：**Grok 不支援 OMC Stop continuation**。  
- 手動 interactive ralph 提前停 = **expected**，不是 bug。

---

### P2 — 可選 host feature request（**0.3.x NEVER 實作 enforcer；僅追蹤**）

僅當以下 **全部** 成立才重開 `stop-continuation` ADR：

| Gate | 準則 |
|------|------|
| H1 | Grok 文件或 source 證明 plugin Stop（或等同）可 **force another model turn** |
| H2 | 注入文字 **到達** model（非僅 log） |
| H3 | `reason=cancelled` / Esc / `omg cancel` **不**再 arm |
| H4 | max continue budget 在 hook 外強制 |
| H5 | hook **永不** `set_verified` |
| H6 | live canary 落盤 `docs/research/live/` |

**Feature request 文案（給 Grok Build，非 OMG 實作）：**  
「Claude-compatible blocking Stop：允許 `decision: block` + reason reinject；passive 預設保留。」

**即使 H1–H6 全綠，OMG 仍應：**

- 預設 **CLI loop 優先**；  
- in-session enforcer **僅**在無 `active.json` CLI run 時可選；  
- 與 `omg ralph` **互斥**（防 double-loop / 額度爆炸）。

**狀態：** Blocking Stop product code = **NEVER for 0.3.x**；host RFE = **OUT_OF_SCOPE**（追蹤即可）

---

## 4. 跨產品對照（各 ≤ 1 段）

### 4.1 OMX / Codex Companion — Stop `decision:block`

本機 `~/.claude/plugins/marketplaces/openai-codex/plugins/codex/`：`hooks.json` 註冊 **Stop → `stop-review-gate-hook.mjs`**（timeout 可到 900s）。當 `stopReviewGate` 開啟且 stop-time review 失敗時，hook `emitDecision({ decision: "block", reason })`（`stop-review-gate-hook.mjs` ~L167–171），用意是 **擋 session 結束直到 review 過關 / setup 提示**，並可附「Codex task 仍在跑」筆記。這同樣 **依賴 Claude Code 會尊 Stop block** 的 host 能力；語意偏 **review gate**，不是 OMC ralph 的「沒做完就強制下一 story」，但機制同屬 **Stop veto**。在 Grok 上移植此 hook 會與 OMC persistent-mode **同一類 no-op**。oh-my-grok 的 dual-review + `omg accept` 已用 **CLI 階段** 表達「沒驗過不算完」，無需 Stop gate。

### 4.2 omo / oh-my-opencode — `injectContinuation` / todo-enforcer

參考樹 `antigravity_for_loop/reference_repos/oh-my-opencode/src/hooks/todo-continuation-enforcer.ts`：idle / incomplete todos 時 **countdown toast**，再呼叫 **`ctx.client.session.prompt({ parts: [{ type:"text", text: CONTINUATION_PROMPT }] })`**（`injectContinuation` ~L150–224）— 這是 **OpenCode 客戶端 API 主動灌下一則 user prompt**，不是 Claude Stop JSON，也不是 Grok file hook。Grok plugin 表面 **沒有** 等價的 session.prompt 注入 API；OMG 最接近的同構是 **外層 `omg` 再 spawn 一次 `grok -p`**（已存在）或未來若 host 提供官方 inject API 再評估。**不可**把 omo 的 client inject 誤寫成 Grok Stop hook 行為。

---

## 5. Verdict 表 — 未來 2 週 vs 0.3.x NEVER

| 項目 | 標籤 | 時程 | 理由 |
|------|------|------|------|
| 維持 `stop.py` 僅 spool、永不 verified | **HAVE / keep** | now | CONSENSUS；single-writer |
| **禁止** port OMC persistent-mode / 假 `decision:block` | **NEVER** | 0.3.x | host passive Stop；theatre 風險 |
| **禁止** skill 教 in-session 無限 self-loop | **NEVER** | 0.3.x | 跳過 accept / 雙控制面 |
| Louder ralph/pipeline context pack（fail 摘要、remaining、next cmd） | **PARTIAL → build** | **2 weeks** | 直接提升 CLI 續跑智商；低風險 |
| `omg resume` / human `status` one-liner | **MISSING → build** | **2 weeks** | 已有 `existing_run_id` + pipeline resume 可接 |
| SessionStart → 寫 `RESUME.md` + using/AGENTS 讀取 | **PARTIAL → build** | **2 weeks** | 不需 Stop；fail-open 檔案副作用 |
| README / omg-using 堅持「persistence = CLI」文案 | **PARTIAL → polish** | **2 weeks** | OMC 移民教育 |
| CLI progress banner（iter/stage） | **PARTIAL → polish** | **2 weeks** | 連續感 |
| pipeline polish / ULW product path | **build（既有 0.3 主線）** | 0.3 | ROI > Stop 幻想 |
| capability_mode / spawn fail-closed | **build（既有）** | 0.3 | PreToolUse **能**做事的面 |
| Blocking Stop enforcer product code | **NEVER** | 0.3.x | 等 H1–H6 |
| Host feature request（文件追蹤） | **OUT_OF_SCOPE** | anytime note | 不佔 sprint |
| omo 式 client.session.prompt 注入 | **NEVER**（無 API） | 0.3.x | 宿主無等價；用 CLI relaunch |
| OMX 式 Stop review gate | **NEVER** on Grok | 0.3.x | 同 Stop 不可 block；用 dual+accept 取代 |
| HUD / wiki / notifications（OMC 其他） | **OUT_OF_SCOPE** | — | 非本「don't stop」題 |

---

## 6. 建議實作切片（給 planner；本角色不實作）

**Week slice A（P0，優先序）：**

1. `ralph_context_pack` / pipeline implement pack 增強 + unit tests。  
2. `omg resume`（+ optional `omg state --pretty`）路由至 pipeline resume 或 ralph `existing_run_id`。  
3. SessionStart 寫 `.omg/state/RESUME.md`；using skill / AGENTS.fragment 加「若存在則讀」。  
4. doctor 一行 active-run hint。

**Week slice B（P1）：**

5. CLI stdout banner。  
6. 文案對齊 README / omg-using / security-model 一句 Stop 限制。

**Explicit non-goals：** 任何 `hooks/bin/stop_*.py` 輸出 `decision: block`；任何「soft enforcer」產品命名。

---

## Trade-offs

| 選項 | Pros | Cons |
|------|------|------|
| **A. CLI-only + P0/P1 強化（推薦）** | Host-honest；single-writer；cancel 清楚；每 iter context 重置；符合 CONSENSUS | 純 TUI 不開 CLI 仍會早停；要教育 OMC 用戶 |
| **B. 假 Stop block / theatre** | 看起來像 OMC | **No-op 或謊言**；維護稅；破壞信任 |
| **C. 等 host blocking Stop 再做 in-session** | 未來可能真 sticky chat | 0.3 不該空轉等；且需與 CLI 互斥設計 |
| **D. omo 式 API inject** | 同 session 連續 | Grok **無** session.prompt API；Option B 不 fork host |

**張力（不可抹殺）：**  
**同 session UX 連續感** vs **host 誠實 + verified single-writer**。在 Grok file hooks 上最大化前者而不說謊 = **不可能**；最大化後者並用 CLI 補前者 = **可行且已半完成**。

---

## Consensus Addendum

- **Antithesis（steelman 反對純 CLI）：** 若主力使用面是 Grok 互動 TUI、從不開 terminal，則 `omg ralph` 是紙上功能；Stop-block（當 host 允許）才會讓 skill 路徑「成真」。  
- **Tradeoff tension：** sticky chat vs trust/host-honesty（見上）。  
- **Synthesis：** **現在 A+P0/P1**；**僅 H1–H6 後**考慮極薄 in-session lease，且 CLI 監督時禁用。  
- **Principle violations if B ships today：** 誠實性、verification discipline、single-writer、solo ROI。

---

## References

| Path | 顯示什麼 |
|------|----------|
| `docs/research/stop-continuation/CONSENSUS.md` | DO NOT BUILD Stop continuation |
| `docs/research/stop-continuation/stop-continuation-host-feasibility.md` | Grok Stop passive；OMC block 對照 |
| `docs/research/stop-continuation/stop-continuation-architect.md` | Option A CLI-only 建議與 H1–H6 |
| `docs/research/stop-continuation/stop-continuation-decision.md` | ADR：NOT BUILD + 用戶文案 |
| `~/.grok/docs/user-guide/10-hooks.md` L80–205 | 官方：僅 PreToolUse blocking；passive stdout ignored |
| `hooks/bin/stop.py` | Stop spool only；never verified |
| `hooks/bin/session_start.py` | SessionStart spool only（P0 擴充點） |
| `hooks/hooks.json` | Stop / SessionStart / PreToolUse 掛線 |
| `hooks/bin/_common.py` | events.jsonl append；session_id |
| `omg_cli/modes.py` L99–262, L607–817 | context pack + ralph for-loop |
| `omg_cli/pipeline.py` L22–23, L337–408 | FSM + resume |
| `omg_cli/state.py` L1–34, active/runs | single-writer state plane |
| `omg_cli/main.py` `cmd_state`, pipeline `--resume` | 現有 inspect/resume |
| `skills/omg-ralph/SKILL.md` | one story；outer CLI owns loop |
| `skills/omg-pipeline/SKILL.md` | prefer CLI FSM |
| `skills/omg-using/SKILL.md` L29–41 | 已寫 Stop 不可行 + CLI 對照表 |
| OMC `templates/hooks/persistent-mode.mjs` ~L976–982 | `decision: block` 原型 |
| openai-codex `stop-review-gate-hook.mjs` ~L167–171 | OMX/Codex Stop block review gate |
| oh-my-opencode `todo-continuation-enforcer.ts` ~L150–224 | omo `injectContinuation` via client API |

---

## 一句交付

**同目標、不同宿主：OMC 用 Stop veto；OMG 用 CLI outer loop + context pack。0.3.x 把 resume / SessionStart 檔案注入 / pack 做響，永遠不要在 Grok 上假造 Stop block。**
