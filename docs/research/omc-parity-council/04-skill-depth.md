# 04 — OMC skill-body vs OMG skill-body 深度對照

**date_utc:** 2026-07-20  
**advisor:** Grok #4 (skill-depth)  
**OMC ref:** oh-my-claudecode **4.15.5**  
  - shims: `~/.claude/plugins/cache/omc/oh-my-claudecode/4.15.5/skills/`  
  - full bodies: `…/skill-bodies/<name>/SKILL.md`  
**OMG ref:** `<repo-root>`  
  - skills: `skills/omg-*/SKILL.md`  
  - agents: `agents/omg-*.md`  
  - CLI FSM: `omg_cli/{modes,ralplan,pipeline,fanout,dual_review,acceptance,ask}.py`

## 評分尺規（1–5）

| 分 | 意義 |
|----|------|
| **1** | 口號 / 別名 / 極薄 playbook（無 FSM、幾乎無 cancel/gate） |
| **2** | 有明確步驟 + hard rules，但狀態機與驗證路徑簡化 |
| **3** | 有可執行協議、狀態檔/CLI、基本 gate 與 cancel |
| **4** | 完整協議頁面：多階段 FSM、gate、cancel 級聯、artifact 契約 |
| **5** | 產品級協議 + 大量 edge case / 多 provider / resume / 級聯 cancel / 配置矩陣 |

**深度維度：** 協議頁面量、gates、state machines、cancel paths、runtime 是否由 CLI/hook 強制。

**gap 類型標籤：**
- **skill-doc-only** — OMG 缺的是文件厚度/agent playbook，核心 CLI 已能跑近似路徑  
- **CLI missing** — 需要新的 runtime / FSM / host 能力，單補 SKILL.md 不夠  
- **host NEVER** — Grok host 無法複製（例：Stop `decision:block`）  
- **OUT_OF_SCOPE** — 刻意不移植（tmux multi-CLI team 等）

---

## 總覽矩陣

| Pair | OMC depth | OMG depth | Gap 類型（主因） | Status |
|------|-----------|-----------|------------------|--------|
| ultrawork ↔ omg-ultrawork | **4** | **4** | skill-doc-only（tier 路由、task graph 文檔） | **HAVE / PARTIAL** |
| ralph ↔ omg-ralph | **5** | **3** | skill-doc + CLI 語意差（agent 內循環 vs CLI 外循環） | **PARTIAL** |
| ralplan ↔ omg-ralplan | **5** | **4** | skill-doc-only（DR/deliberate/interactive） | **PARTIAL** |
| ask ↔ omg-ask | **3** | **3** | skill-doc-only（provider 矩陣較窄） | **HAVE** |
| cancel ↔ omg-cancel | **5** | **3** | CLI missing（級聯 mode / multi-PID / team） | **PARTIAL** |
| plan + deep-interview vs ralplan+pipeline | **5** | **3** | CLI missing（Socratic interview 整條） | **MISSING** interview |
| team / omc-teams vs process fanout | **5** | **2** | OUT_OF_SCOPE / experimental | **PARTIAL / NEVER full clone** |
| OMC autopilot vs pipeline+ralph | **5** | **3** | skill-doc + 階段缺口（QA/多評審） | **PARTIAL** |
| dual-review patterns | **4**（散落 ralph/team/ccg） | **3** | skill-doc + interim CLI | **PARTIAL** |

---

## 1. ultrawork ↔ omg-ultrawork

### OMC（depth **4**）

- **Body 規模：** `skill-bodies/ultrawork/SKILL.md` ≈ **149 行**（結構化 XML sections）。
- **協議：** 意圖 grounding → 平行 context → **dependency-aware task graph / waves** → **model tier 路由**（haiku/sonnet/opus）→ 平行 fire → lightweight verify。
- **Gates：** 輕量 build/test/manual QA；**不做** 持久化與完整 reviewer 迴圈（明確推 ralph）。
- **State：** 無持久 state（「stateless component」）；session / multi-repo caveats 有文。
- **Cancel：** 透過全域 cancel skill + `ultrawork-state.json`（runtime hook 側，不在本 body 詳寫）。
- **Runtime 厚度：** Task 多 agent + background；與 ralph/autopilot 的 **composition 樹**寫死。

### OMG（depth **4**）

- **Body 規模：** `skills/omg-ultrawork/SKILL.md` ≈ **139 行**。
- **協議：** Decompose → 平行 `spawn_subagent` depth=1 → wait/collect → **result envelope** → **`omg integrate`** → leader/`omg accept` 驗證。
- **Gates：**
  - 硬 cap depth=1（子不可再 spawn）
  - **capability_mode** 契約（implementer read-write 無 shell；explore/critic/verifier read-only）
  - integrate 拒絕 `base_sha` mismatch；CLI 擁有 `passes/verified`
- **State：** 監督 run 在 `.omg/state/runs/<id>/`；envelope 在 `.omg/artifacts/ulw-results/`。
- **Cancel：** 指向 `omg cancel`；禁止 `pkill -f` self-match。
- **CLI：** `omg ulw`（`omg_cli/modes.py`）；可選 experimental `omg ulw --fanout process`（`fanout.py`，需 `OMG_EXPERIMENTAL_PROCESS_FANOUT=1`）。

### Gap 摘要

| 項目 | 說明 | 類型 |
|------|------|------|
| Model tier 路由文件 | OMC 強制讀 agent-tiers；OMG 未分 haiku/sonnet/opus 層（Grok 宿主模型不同） | skill-doc-only / host 差異 |
| Task graph / waves 範本 | OMC 步驟更細；OMG 靠 leader 即興 decompose | skill-doc-only |
| Envelope + integrate | **OMG 更深**（cherry-pick / dry-run / conflict stop） | OMG 優勢 |
| capability_mode 隔離 | **OMG 更深**（寫進 skill + security-model） | OMG 優勢 |

**結論：** 功能 **HAVE** 且深度近似（兩邊 4）。缺口主要是 **skill-doc**（OMC 的 tier/graph 儀式），不是缺 CLI。

---

## 2. ralph ↔ omg-ralph

### OMC（depth **5**）

- **Body 規模：** `skill-bodies/ralph/SKILL.md` ≈ **263 行**（含 continuation 注入文案）。
- **協議（單 session 內可多 story）：**  
  PRD setup/refine → pick story → implement（可含 ulw 平行）→ **per-story verify 並寫 `passes: true`** → 全 story 完成 → **reviewer 分級**（architect/critic/`omc ask codex`）→ **ai-slop-cleaner** → regression re-verify → **`/cancel` 清理**。
- **Gates：**  
  - 啟動必須 prd.json；禁止 generic acceptance  
  - reviewer 最低 STANDARD tier；可選 codex optimality review  
  - deslop 後必須回歸綠  
  - **Stop hook /「boulder never stops」** 強制持續（host 級）
- **State：** session-scoped `prd.json`、`progress.txt`、`ralph-state.json`、stop-breaker；可連 ultrawork。
- **Cancel：** 專用 cancel 級聯 linked ultrawork；成功完成路徑也走 cancel 清 state。

### OMG（depth **3**）

- **Body 規模：** `skills/omg-ralph/SKILL.md` ≈ **97 行**。
- **協議（刻意單次 iteration）：**  
  讀 context → refine **proposal** PRD → **只做 ONE story** → evidence 筆記 → **STOP**。  
  **禁止** agent 自己標 `verified` / 在同一 turn 開 story N+1。
- **Gates：**  
  - 外層 **`omg ralph` max_iter 迴圈** + **`omg accept`**（frozen commands + semantic policy + writer token）才可 `verified`  
  - capability_mode 與深度=1 同 ulw  
  - **無** deslop skill、無 session 內 multi-story、無 Stop pin
- **State：** CLI `prd.json` / acceptance artifacts；`ralph_context_pack` 每次迭代注入（`modes.py`）。
- **Cancel：** `omg cancel` 單 PID（v0.1 layout）。

### Gap 摘要

| 項目 | 說明 | 類型 |
|------|------|------|
| Persistence 宿主 | OMC = chat Stop 強制；OMG = **CLI outer loop only**（已決策，見 stop-continuation CONSENSUS） | **host NEVER**（Stop block） |
| Agent 內 PRD 多 story | OMC 允許一 session 連做多 story；OMG **硬切一 story/iter** | 設計差（有意） |
| Reviewer + deslop + regression | OMC 7→7.5→7.6 鏈；OMG 靠 dual-review stage / accept | **CLI/skill missing** 若要同厚度 |
| Critic provider 旗標 | OMC `--critic=architect\|critic\|codex` | skill-doc / CLI missing |
| progress.txt 學習累積 | OMC 有；OMG 靠 artifacts 自由格式 | skill-doc-only |

**結論：** **PARTIAL**。核心「不要停到驗證完」**有**（CLI），但 **skill 深度 5→3**：OMC 是 **agent-owned persistence protocol**；OMG 是 **thin iteration contract + thick CLI accept**。補文檔可縮小儀式差；**不能**用 skill 補 Stop continuation。

---

## 3. ralplan ↔ omg-ralplan

### OMC（depth **5**）

- **ralplan body：** 薄 alias（≈140 行）→ 轉 **`plan --consensus`**。  
- **plan body：** ≈ **290+ 行**；consensus = Planner → Architect → Critic，max **5** 輪。  
- **協議厚度：** RALPLAN-DR（Principles / Drivers / Options）、**short vs deliberate**（pre-mortem + 擴充 test plan）、`--interactive` AskUserQuestion、`--architect/codex` / `--critic/codex`、**planning/execution boundary**、state lifecycle（`state_write` vs `state_clear` 30s cancel 陷阱）、ADR 輸出、handoff 到 team/ralph。  
- **Pre-execution gate：** 對 vague ralph/autopilot/team 的攔截規則表（file path / issue / symbol / force:）。  
- **Cancel：** `ralplan-state` + plan-consensus 獨立 mode。

### OMG（depth **4**）

- **Body：** `skills/omg-ralplan/SKILL.md` ≈ **95 行**。  
- **CLI FSM：** `omg_cli/ralplan.py` — **真正的狀態機**  
  `draft → critic → revise → verifier → accepted|failed`，`max_rounds` default **3**，`ralplan.json` + `stages/*`。  
- **Gates：**  
  - 禁止 product code  
  - critic/verifier **read-only**  
  - **accepted 僅 CLI**：verifier artifact 必須 whole-word **APPROVE**  
  - 永不 set `verified`（那是 accept 的事）  
- **缺：** deliberate/DR 結構、interactive UI、codex architect override、pre-execution gate 關鍵字攔截表、max 5 輪 Architect 鋼人辯論儀式。

### Gap 摘要

| 項目 | 說明 | 類型 |
|------|------|------|
| CLI-owned FSM + APPROVE gate | **OMG 更硬**（程式強制） | OMG 優勢 |
| RALPLAN-DR / deliberate | OMC 文檔極深；OMG 無 | skill-doc-only（可加 stage prompt 模板） |
| Interactive 批准 UI | OMC AskUserQuestion；Grok 可用 ask_user_question 但 skill 未寫 | skill-doc-only |
| Pre-exec vague gate | OMC 有完整表格；OMG 無 runtime keyword gate | **CLI missing**（若要 auto-redirect） |
| Architect 鋼人步驟 | OMC Architect 與 Critic **必須串行**；OMG 是 critic 再 verifier | skill-doc-only |

**結論：** **PARTIAL → 接近 HAVE**。OMG **runtime 深度不差**（4）；OMC 勝在 **規劃方法論與門禁 UX**。優先 skill/prompt 模板即可，不必重造 tmux。

---

## 4. ask ↔ omg-ask

### OMC（depth **3**）

- **Body：** ≈ **65 行**。  
- **協議：** 一律 `omc ask <provider>`；禁止手組 provider argv。  
- **Providers：** claude / codex / gemini / antigravity / grok / cursor。  
- **Artifacts：** `.omc/artifacts/ask/<provider>-…md`。  
- **Gates：** 本地 CLI 已安裝；Windows antigravity 限制註記。

### OMG（depth **3**）

- **Body：** ≈ **42 行**（故意 **human-only broker** 語氣更強）。  
- **CLI：** `omg_cli/ask/broker.py` — child env **僅子進程** 設 `OMG_ALLOW_EXTERNAL_CLI=1`；固定 argv、`shell=False`；artifact + meta；**不 apply patch、不 verified**。  
- **Providers：** codex / claude(fable) / gemini（可缺）。  
- **Gates：** S3/S5/S6 安全不變量；agent **禁止** 用 `run_terminal_command` 當 worker 跑 claude/codex。

### Gap 摘要

| 項目 | 說明 | 類型 |
|------|------|------|
| Provider 覆蓋 | OMC 更廣（agy/cursor/grok） | skill-doc + optional CLI provider |
| 安全敘事 | **OMG 更深**（env 隔離明確） | OMG 優勢 |
| Agent 是否可代跑 | OMC 技能假設 `omc ask` 可被 skill 觸發；OMG **強制 human** | 設計差（有意更嚴） |

**結論：** **HAVE**。深度同級；OMG 更安全保守。

---

## 5. cancel ↔ omg-cancel

### OMC（depth **5**）

- **Body：** ≈ **383 行**。  
- **協議：** 自動偵測 active mode；**依賴順序** cancel（autopilot → ralph → ulw → ultraqa → swarm → ultrapilot → pipeline → team → omc-teams → plan consensus → self-improve）。  
- **Gates / paths：**  
  - MCP `state_clear` / deferred ToolSearch  
  - bash fallback（禁止用於 autopilot resume / omc-teams）  
  - Team **兩階段 graceful shutdown** + orphan scanner  
  - `--force` / `--all` 清 legacy 檔清單  
  - MCP worker heartbeats / tmux `omc-team-*`  
  - skill-active 最後必清（stop hook 防抖）  
- **Preserve：** autopilot resume 狀態保留。

### OMG（depth **3**）

- **Body：** ≈ **98 行**。  
- **CLI：** `omg cancel` → `cancel_run`：讀 active / `--run`、SIGTERM `pid`、標 `cancelled`、清 `active.json`。  
- **Layout：** 單 run 單 PID（v0.1 明示 multi-worker 未完整追蹤）。  
- **Manual fallback：** 只允許 PID 檔；**禁止** self-matching `pkill -f`。  
- **Process fanout：** `fanout.py` 寫 `workers/wNN.pid.json` — cancel 對多 PID 的完整級聯仍偏薄。

### Gap 摘要

| 項目 | 說明 | 類型 |
|------|------|------|
| Mode 級聯圖 | OMC 11+ modes；OMG 單一 run model | CLI missing（若加更多 modes） |
| Multi-PID / process group | OMG 部分寫入 worker pid，cancel 路徑需對齊 | **CLI missing**（中優先） |
| Team graceful | OMC 有；OMG 無 team | OUT_OF_SCOPE until team |
| Autopilot resume-preserving cancel | OMC 有；OMG pipeline 用 report.json，無同等文檔 | skill-doc-only |

**結論：** **PARTIAL**。安全 cancel **有**；**產品級 cancel 協議**仍是 OMC 碾壓。優先把 **process-fanout multi-PID cancel** 補到 CLI，比寫更長 skill 重要。

---

## 6. plan + deep-interview（OMC） vs ralplan + pipeline（OMG）

### OMC 規劃棧（深度 **5** 合體）

| Skill | 約略行數 | 角色 |
|-------|----------|------|
| **deep-interview** | **~800 行** | Socratic + **數學 ambiguity gate** + topology Round0 + ontology 收斂 + challenge agents + pending-approval bridge |
| **plan** | **~290 行** | interview / direct / consensus / review；quality 80%/90% 門檻 |
| **ralplan** | alias | pre-exec gate + consensus entry |

**3-stage pipeline（文件 canon）：**  
`deep-interview (clarity) → plan --consensus (feasibility) → explicit approval → team|ralph|autopilot`

### OMG 規劃/執行棧（深度 **3** 合體）

| 元件 | 角色 |
|------|------|
| **omg-ralplan** + `ralplan.py` | 共識計劃 FSM（無 Socratic） |
| **omg-pipeline** + `pipeline.py` | `plan → implement → integrate → dual_review → accept → report` |
| **無** deep-interview skill | 無 ambiguity 分數、無 topology gate、無 challenge modes |

### Gap 摘要

| 項目 | Status | 類型 |
|------|--------|------|
| Consensus plan | **HAVE** | — |
| E2E plan→implement→accept | **HAVE**（pipeline） | — |
| Socratic + ambiguity math | **MISSING** | **CLI/skill missing**（大工程） |
| Vague → auto interview redirect | **MISSING** | CLI missing |
| Planning quality 量化門檻 | **MISSING** | skill-doc-only 可先做 |

**結論：** OMG 用 **ralplan+pipeline** 覆蓋「有計劃再做」的 **執行面**；**完全沒有** OMC deep-interview 的 **需求澄清深度（5 vs 0）**。這是規劃棧最大缺口。若 0.3 要「parity 感」，最小可行是 **薄版 interview skill**（一問一答 + 手動「夠了」），不必先複製 800 行數學閘。

---

## 7. team / omc-teams（OMC） vs process fanout（OMG）

### OMC team（depth **5**）

- **Body：** `skill-bodies/team/SKILL.md` **~1045 行**。  
- **協議：** staged `team-plan → prd → exec → verify → fix`；handoffs；watchdog；Team+Ralph 連動；worktree merge API；roleRouting 多 provider；Runtime V2 events。  
- **omc-teams：** CLI/tmux 外掛 worker（claude/codex/gemini/agy/grok/cursor），N≤10。  
- **Cancel：** graceful + orphan cleanup。

### OMG process fanout（depth **2**）

- **Module：** `omg_cli/fanout.py`（experimental）。  
- **協議：** N× 獨立 `grok -p`、無 tmux、`workers/wNN.pid.json`、禁止 nested fanout。  
- **Gate：** 需 `OMG_EXPERIMENTAL_PROCESS_FANOUT=1`；**預設隔離故事仍是 spawn_subagent**。  
- **無：** staged pipeline、mailbox、role routing、shared task list、CLI multi-vendor workers。

### Gap 摘要

| 項目 | Status | 類型 |
|------|--------|------|
| 平行多 worker OS 進程 | **PARTIAL**（experimental） | CLI present, thin |
| In-session spawn fanout | **HAVE**（ulw skill） | — |
| Full team orchestration | **MISSING / NEVER 全抄** | **OUT_OF_SCOPE**（tmux + multi-CLI 產品） |
| 多 vendor CLI workers as default | **NEVER**（OMG 硬規則禁 external default workers） | 設計 |

**結論：** 不要用 process fanout 對標 team skill 深度。OMG 正確對標是 **ulw spawn**，不是 omc-teams。

---

## 8. OMC autopilot vs omg-pipeline + omg-ralph

### OMC autopilot（depth **5**）

- **Body：** ≈ **225 行** + 相依 ralph/ulw/ultraqa/reviewers。  
- **Phases：** 0 Expansion → 1 Planning → 2 Ralph+Ulw → 3 UltraQA（≤5 cycles）→ 4 多評審平行（architect + security + code-reviewer）→ 5 cleanup cancel。  
- **Gates：** ralplan/deep-interview 捷徑跳過 0/1；QA 同錯 3 次停；validation 全數 APPROVE。  
- **Config：** maxIterations、pauseAfter*、execution solo|team。  
- **Resume：** cancel 保 phase。

### OMG pipeline + ralph（depth **3**）

- **Skill：** `omg-pipeline` ≈ **53 行**（薄，**CLI-owned** 為主）。  
- **FSM：** `pipeline.py` — plan(ralplan) → implement(ralph|ulw) → integrate → dual_review → accept → **report.json**。  
- **Gates：** plan APPROVE；dual_review 可選；**only accept → verified**；永不設 `OMG_ALLOW_EXTERNAL_CLI`。  
- **缺：** Phase0 擴寫、UltraQA 迴圈、security-reviewer 平行、deslop、deep-interview 導流、pauseAfterPlanning。

### Gap 摘要

| 項目 | Status | 類型 |
|------|--------|------|
| 一鍵 plan→code→verify | **HAVE** | — |
| CLI FSM + report | **HAVE**（厚 runtime） | OMG 優勢在「狀態誠實」 |
| Expansion / UltraQA / multi-reviewer 陣 | **PARTIAL / MISSING** | skill-doc + agent catalog |
| Stop 持續到做完 | **NEVER**（CLI loop 替代） | host NEVER |

**結論：** **PARTIAL**。OMG **pipeline 是誠實的 AUTO_PILOT 子集**（尤其 verified 門禁比「聊天宣稱完成」更硬）。要拉到 4–5：加 **accept 失敗→re-implement 迴圈文檔化**、可選 **ultraqa-like test fix stage**、擴 agent 目錄做 Phase4 多視角（不必等 tmux）。

---

## 9. dual-review patterns

### OMC（depth **4**，分散）

- Ralph 完成：architect / critic / **codex critic** + deslop。  
- Autopilot Phase4：architect + security-reviewer + code-reviewer **平行**。  
- Team：team-verify 階段多 reviewer 路由。  
- CCG / ask：跨 vendor。  
- **規則文化：** 不 self-approve；獨立 verifier。

### OMG（depth **3**）

- **Skill：** `omg-dual-review` ≈ **54 行**。  
- **TUI 優選：** spawn `omg-critic` → `omg-verifier`（read-only）。  
- **CLI interim：** `omg dual-review` **順序 headless** 兩次 grok（**不是** native spawn）；`OMG_DUAL_REVIEW_REQUIRE_NATIVE=1` 可拒絕 interim。  
- **Agents：** `omg-critic.md` / `omg-verifier.md` 有 verdict 契約。  
- **硬規則：** dual-review **APPROVE ≠ verified**（仍要 `omg accept`）。  
- **外部雙審：** human `omg ask`，非 default worker。

### Gap 摘要

| 項目 | 說明 | 類型 |
|------|------|------|
| 獨立 critic→verifier | **HAVE** | — |
| Native spawn dual-review 一等公民 | PARTIAL（skill 有、CLI 仍 interim） | **CLI missing**（中） |
| 三方 / security 平行 | MISSING | skill-doc + agents |
| Codex 作為預設 dual path | 刻意不用（ask only） | 設計 |

**結論：** **PARTIAL**。概念 **HAVE**；OMC 的「多評審矩陣」OMG 尚未產品化。

---

## 10. Agent catalog 規模

| | OMC 4.15.5 | OMG |
|--|------------|-----|
| **Plugin agents** | **19**：analyst, architect, code-reviewer, code-simplifier, critic, debugger, designer, document-specialist, executor, explore, git-master, planner, qa-tester, scientist, security-reviewer, test-engineer, tracer, verifier, writer | **4**：omg-orchestrator, omg-executor, omg-critic, omg-verifier |
| **+ host built-ins** | Task 生態 + tiers | Grok：`explore` / `plan` / `general-purpose` + omg-* |
| **角色覆蓋** | 專科極全（designer/security/scientist/…） | 編排四角色 + host 通用 |

**比例：** OMG 專科 agent ≈ **OMC 的 1/5**（4 vs 19），若只算「具名專科」差距更大。

**影響：**  
- ultrawork/ralph/team 在 OMC 可 **按 stage 換專科**；OMG 多半 **general-purpose / omg-executor** 扛全部實作。  
- 這是 **skill-doc + agent files** 缺口，不一定要 19 個；0.3 高價值補集：**debugger-ish**、**security-reviewer-ish**、**test-engineer-ish**（皆 leaf + capability_mode）。

---

## 11. 協議厚度對照（量化感）

| 區域 | OMC skill-body 量級 | OMG skill + 對應 CLI |
|------|---------------------|----------------------|
| Parallel exec | ~150 行 skill | ~140 行 skill + integrate/accept CLI |
| Persistence | ~260 行 skill + Stop hooks | ~100 行 skill + modes loop + accept |
| Plan consensus | ~290 plan + ~140 ralplan + gate | ~95 skill + **ralplan.py FSM** |
| Requirements interview | **~800** deep-interview | **0** |
| Full auto | ~225 autopilot | ~53 pipeline skill + **pipeline.py** |
| Cancel | **~380** | ~98 + simple cancel_run |
| Team | **~1000+** | experimental fanout only |
| Ask | ~65 | ~42 + **broker 安全實作** |
| Dual-review | 散落多 skill | 專用 skill + dual_review.py interim |

**觀察：** OMG 多處 **「skill 薄、CLI 厚」**（ralplan/pipeline/accept/ask）；OMC 多處 **「skill 厚、hook 厚」**。對 Grok host（Stop 非阻塞）這是正確分層，但 **文件深度分數**會讓人誤以為 OMG「沒功能」——其實是 **強制力搬到 CLI**。

---

## 12. 依 gap 類型的優先建議（僅研究，非 roadmap 定案）

### skill-doc-only（便宜）

1. 擴 **omg-ralph** 文檔：對齊 OMC checklist 的可選章節（deslop 若有 skill、post-accept 流程）。  
2. **ralplan stage prompts** 注入 RALPLAN-DR 短模板（Principles/Drivers/Options）。  
3. **omg-ultrawork** 補 task-graph / wave 範例（不必 tier 模型）。  
4. **pipeline** skill 補各 stage 失敗/resume 表（runtime 已有 history）。

### CLI missing（中價）

1. **multi-PID cancel** 對齊 process fanout `workers/*.pid.json`。  
2. **dual-review native** CLI 路徑（spawn 契約或明確 deprecate sequential）。  
3. **薄 deep-interview** 或 `omg interview` 狀態檔（非 800 行數學版）。  
4. 可選 **pre-exec vague gate**（keyword → 強制 ralplan）。

### host NEVER / OUT_OF_SCOPE

- Stop continuation pin（已 CONSENSUS：不建）  
- 全量 omc-teams / tmux multi-CLI 作為 default workers  
- OMC 級 team 1045 行協議照搬  

---

## 13. 對 BRIEF 四問的 skill-depth 答覆（本 advisor 視角）

1. **是否已有基本 OMC 功能？**  
   **核心執行三角（ulw / ralph / ralplan）+ cancel + ask + accept：基本有。**  
   深度上：**ulw≈parity、ralplan runtime 強、ralph/cancel/autopilot/team/interview 仍明顯較薄或缺。**

2. **don’t-stop 在 Grok 上？**  
   Skill 層已誠實寫成 **CLI outer loop**（omg-ralph / pipeline）；**不要**在 skill 假裝 Stop pin。深度差距是 **host NEVER**，不是漏寫一段 markdown。

3. **真 parity 仍缺？**  
   deep-interview、team 編排、autopilot Phase3–4 厚度、agent catalog、cancel 級聯、native dual-review。  
   **不是**缺 `omg-ultrawork` 這個名字。

4. **0.3 建什麼？**  
   從 skill-depth 看：**先 CLI 多 PID cancel + dual-review 誠實化 + 薄 interview**；**不要**先寫 1000 行 team skill。Agent catalog 小補集比複製 OMC 19 agents 划算。

---

## 14. 證據路徑速查

| 主題 | 路徑 |
|------|------|
| OMC ultrawork body | `…/skill-bodies/ultrawork/SKILL.md` |
| OMC ralph body | `…/skill-bodies/ralph/SKILL.md` |
| OMC plan / ralplan / deep-interview / cancel / team / autopilot | `…/skill-bodies/{plan,ralplan,deep-interview,cancel,team,autopilot}/SKILL.md` |
| OMG skills | `skills/omg-{ultrawork,ralph,ralplan,cancel,ask,pipeline,dual-review,using}/SKILL.md` |
| OMG agents | `agents/omg-{orchestrator,executor,critic,verifier}.md` |
| OMG CLI | `omg_cli/{modes,ralplan,pipeline,fanout,dual_review,acceptance,ask/broker}.py` |
| BRIEF | `docs/research/omc-parity-council/BRIEF.md` |

---

## 15. 一頁總結表（給 synthesis 用）

| Feature | OMC depth | OMG depth | Status | Gap 主因 |
|---------|-----------|-----------|--------|----------|
| Parallel fan-out (ulw) | 4 | 4 | HAVE | skill-doc-only（tier/graph） |
| Persistence (ralph) | 5 | 3 | PARTIAL | host NEVER + skill 薄 |
| Plan consensus (ralplan) | 5 | 4 | PARTIAL | skill-doc（DR/UI） |
| Autopilot / pipeline | 5 | 3 | PARTIAL | stages/agents |
| Dual-review | 4 | 3 | PARTIAL | native CLI |
| Ask advisors | 3 | 3 | HAVE | provider 寬度 |
| Team / tmux | 5 | 2 | MISSING/OUT | 產品邊界 |
| Cancel | 5 | 3 | PARTIAL | multi-PID / modes |
| Deep interview | 5 | 0 | MISSING | CLI+skill |
| Accept / verified | 4（散落） | **5**（accept 硬閘） | HAVE | OMG 更硬 |
| Agent catalog | 5 | 2 | PARTIAL | agent files |

**最硬結論：**  
OMG 在 **「CLI 擁有 verified / FSM」** 上已有 **parity 甚至更嚴**；在 **「skill-body 協議頁面與專科 agent 生態」** 上大約 **OMC 的 30–50%**。深度債主要是 **interview + team + cancel 級聯 + reviewer 矩陣**，不是缺 ultrawork 這個 skill 檔名。
