# 01 — Feature Inventory / Explore（OMC ↔ OMG parity）

**date_utc:** 2026-07-20  
**role:** Grok advisor #1 — FEATURE INVENTORY / EXPLORE  
**OMG version:** 0.2.5（`plugin.json`）  
**OMC reference:** `~/.claude/plugins/cache/omc/oh-my-claudecode/4.15.5`  
**OMG root:** `<repo-root>`  
**BRIEF:** `docs/research/omc-parity-council/BRIEF.md`

**Labels:** **HAVE** | **PARTIAL** | **MISSING** | **NEVER**（host 不可行）| **OUT_OF_SCOPE**（產品刻意不做）

---

## 0. 結論（先講清楚）

| 問題 | 答案 |
|------|------|
| OMG 是否已有 **基本 OMC 功能**？ | **大致有（core 約 70–80%）** — 平行 fan-out、CLI 外層 persistence、plan FSM、accept/verified、cancel、doctor/setup、ask broker、capability 隔離敘事與 PreToolUse soft-deny 都在。 |
| 是否「產品級 OMC 完整面」？ | **否（full surface 約 25–35%）** — 缺 deep-interview、ultraqa、ultragoal、HUD/wiki/notifications、19 角色 agent 目錄、team/tmux runtime、Stop 強制續跑、MCP state tools、ralplan pre-exec gate 等。 |
| 「don't stop until done」在 Grok？ | **只能靠 CLI outer loop**（`omg ralph` / `omg pipeline`）。Stop hook **不可 blocking** → **NEVER** 做 OMC-style Stop pin（見 `docs/research/stop-continuation/CONSENSUS.md`）。 |

嚴格：有 CLI 與 skill 名稱 ≠ 行為等價。下列矩陣以 **evidence path** 為準，不寫 marketing。

---

## 1. OMG surface inventory

### 1.1 CLI（`omg_cli/main.py` `build_parser`）

| 子命令 | 用途（code 事實） | 模組 |
|--------|-------------------|------|
| `setup` | `.omg/` dirs、AGENTS + gitignore fragment、compat banner | `setup_cmd.py` |
| `doctor` / `--strict` | plugin/hooks/skills/agents/PATH/compat；global PreToolUse hard-check | `doctor.py` |
| `state` / `--run` | 讀 active 或指定 run JSON | `state.py` |
| `cancel` / `--run` / `--grace` | SIGTERM pg → optional SIGKILL；`pid.json` starttime fail-closed | `state.py` |
| `accept` | freeze PRD commands → semantic policy → `set_verified` only with CLI stamp | `acceptance.py` + `command_policy.py` |
| `integrate` | ULW envelopes cherry-pick；ancestry/merge/`changed_files` anti-forge | `integrate.py` |
| `worker prepare\|seal` | worktree + envelope bridge（no-shell workers） | `workers.py` |
| `ulw` | 1-shot mode launch（skill fanout default；process fanout experimental） | `modes.py` / `fanout.py` |
| `ralph` | max_iter outer loop + context pack + optional accept | `modes.py` |
| `ralplan` | CLI-owned draft→critic→revise→verifier FSM | `ralplan.py` |
| `pipeline` | plan→implement→integrate→dual_review→accept→report；`--resume` | `pipeline.py` |
| `dual-review` | sequential headless critic→verifier（**interim**；不 set verified） | `dual_review.py` |
| `ask` | trusted advisor broker（child-only `OMG_ALLOW_EXTERNAL_CLI`） | `ask/broker.py` + `providers.py` |

全域 flag：`--safe` / `--yolo`；modes 另有 `--dry-run` / `--max-iter` / `--timeout`。

**README 與 main 對齊的硬關鍵字：** `ulw` / `ralph` / `ralplan` / `pipeline` / `dual-review` / `ask`。

### 1.2 Skills（`skills/*/SKILL.md` frontmatter）

| Skill | description 摘要 |
|-------|------------------|
| `omg-using` | Bootstrap router；ulw/ralph/ralplan/cancel 分派；**明確寫：不靠 Stop continuation** |
| `omg-ultrawork` | `spawn_subagent` 平行；envelope + `omg integrate` |
| `omg-ralph` | **單次 iteration**；outer CLI 擁有 loop |
| `omg-ralplan` | plan consensus；no implementation |
| `omg-pipeline` | AUTO_PILOT-like composition；prefer CLI FSM |
| `omg-dual-review` | critic→verifier；CLI path = interim sequential |
| `omg-cancel` | `omg cancel` + PID 檔；禁 self-matching `pkill -f` |
| `omg-ask` | 人類觸發 external advisors only |

### 1.3 Agents（`agents/`）

| Agent | capability / 限制 |
|-------|-------------------|
| `omg-orchestrator` | leader；可 depth=1 spawn |
| `omg-executor` | `capabilityMode: read-write`；**disallow** spawn + shell |
| `omg-critic` | `read-only`；disallow spawn/edit/shell |
| `omg-verifier` | `read-only`；disallow spawn/edit/shell；**不**寫 verified |

對比 OMC：19 agents（architect / planner / security-reviewer / test-engineer / qa-tester / designer / …）— OMG 僅 **4** 個角色，explore/plan 依賴 **Grok-native** built-ins。

### 1.4 Hooks（`hooks/hooks.json` + `hooks/bin/`）

| Event | Script | 行為 |
|-------|--------|------|
| SessionStart | `session_start.py` | ensure dirs + spool event；fail-open |
| Stop | `stop.py` | **只** append event；**永不** set verified |
| SubagentStop | `subagent_stop.py` | spool only |
| PreToolUse | `pre_tool_use_deny.py` → `omg_cli.deny` | matcher: `run_terminal_command\|Bash\|Shell\|spawn_subagent\|Task`；deny external agent CLIs + spawn capability_mode gate；**fail-open honest** |

### 1.5 `omg_cli/*.py` 模組目的（精簡）

| Module | Purpose |
|--------|---------|
| `main.py` | argparse router |
| `state.py` | run single-writer；mutex；cancel；`set_verified` gate |
| `modes.py` | ulw/ralph/ralplan `grok -p` launch；ralph context pack；HARD RULES inject |
| `ralplan.py` | plan FSM + stage artifacts + APPROVE gate |
| `pipeline.py` | multi-stage AUTO_PILOT-like FSM + report.json |
| `dual_review.py` | sequential headless dual review |
| `fanout.py` | experimental multi-PID process fanout（`OMG_EXPERIMENTAL_PROCESS_FANOUT=1`） |
| `integrate.py` | ULW cherry-pick integrator |
| `workers.py` | prepare/seal worktrees + envelopes |
| `acceptance.py` | freeze + run + CLI stamp |
| `command_policy.py` | semantic argv allowlist / floors |
| `deny.py` | PreToolUse deny regex + spawn capability contract |
| `doctor.py` | health / compat / global hook |
| `setup_cmd.py` | project scaffold |
| `compat.py` | Claude/OMC keyword leakage scan |
| `canary_classify.py` | canary result taxonomy |
| `ask/*` | advisor provider argv + broker |

### 1.6 架構契約（BRIEF + README 已定案）

- Workers = **Grok-native `spawn_subagent` only**（預設禁止 claude/codex/omc team 當 worker）
- Persistence = **CLI outer loop**，不是 chat Stop reinject
- `verified` = **CLI-only**（`omg accept`）；dual-review **不** set verified
- PreToolUse = fail-open；primary isolation = **`capability_mode`**
- **No tmux v1**；process fanout experimental only
- Stop continuation = **DO NOT BUILD 0.3.x**

---

## 2. OMC 4.15.5 reference inventory（disk）

### 2.1 Skills 目錄（`skills/` 與 `skill-bodies/`）

完整 skill 名（plugin skills + bodies 對齊，含 shim）：

`ai-slop-cleaner`, `ask`, `autopilot`, `autoresearch`, `cancel`, `ccg`, `configure-notifications`, `debug`, `deep-dive`, `deep-interview`, `deepinit`, `external-context`, `hud`, `learner`, `local-build-reminder`, `mcp-setup`, `merge-readiness`, `omc-doctor`, `omc-reference`, `omc-setup`, `omc-teams`, `plan`, `project-session-manager`, `ralph`, `ralplan`, `release`, `remember`, `sciomc`, `self-improve`, `setup`, `skill`, `skillify`, `team`, `trace`, `ultragoal`, `ultraqa`, `ultrawork`, `verify`, `visual-verdict`, `wiki`, `writer-memory`

> Plugin `skills/*/SKILL.md` 多為 **compact shim** → 真 body 在 `skill-bodies/*/SKILL.md`。

### 2.2 指定 skill 前 ~40 行摘要（skill-bodies）

| Skill | OMC how（精髓） |
|-------|-----------------|
| **autopilot** | Phase 0 expansion → plan → Ralph+ULW exec → UltraQA cycles → multi-reviewer validation → cancel cleanup；可吃 deep-interview / ralplan 產物 |
| **ralph** | PRD-driven persistence；Stop/hook 續跑（「boulder never stops」）；story-by-story；architect/critic/codex reviewer；deslop；in-session state |
| **ultrawork** | 純平行 engine（component）；model tier 路由；**不**負責 persistence |
| **ralplan** | `plan --consensus` alias；Planner→Architect→Critic；pre-exec gate 攔 vague ralph/autopilot/team |
| **team** | Claude implicit agent teams + 可選 tmux CLI workers；staged pipeline plan→prd→exec→verify→fix；worktrees；role routing |
| **ultragoal** | durable ledger under `.omc/ultragoal` + Claude `/goal` handoff（shell **不能** mutate /goal） |
| **ultraqa** | qa-tester → architect diagnose → fix，max 5 cycles；state file |
| **wiki** | MCP wiki_* + `.omc/wiki` markdown KB（Karpathy model） |
| **hud** | Claude Code `statusLine` + `omc-hud.mjs` 狀態列 |
| **deep-interview** | Socratic + ambiguity scoring ≤ threshold；spec → pending approval |
| **ask** | `omc ask` 固定路徑；多 provider（claude/codex/gemini/agy/grok/cursor） |
| **cancel** | 多 mode 智慧取消 + MCP state_clear + team shutdown + force |
| **verify** | 證據優先 completion check playbook |

### 2.3 OMC agents（19）

`analyst`, `architect`, `code-reviewer`, `code-simplifier`, `critic`, `debugger`, `designer`, `document-specialist`, `executor`, `explore`, `git-master`, `planner`, `qa-tester`, `scientist`, `security-reviewer`, `test-engineer`, `tracer`, `verifier`, `writer`

### 2.4 OMC 其他表面（parity 相關）

- **Hooks 密度高**：keyword detector、persistent-mode / Stop block、pre-tool enforcer、project-memory、wiki session hooks、HUD、delegation enforcer…（`hooks/` + `scripts/*.mjs`）
- **MCP / bridge / team runtime**：`bridge/`, `dist/team/`, `dist/mcp/`
- **Commands**：`hud`, `wiki`, `verify`, `omc-setup`, `release`, …（`commands/`）

---

## 3. Feature matrix（BRIEF shared rows + 重要 extras）

### 3.1 BRIEF 共用列

| Feature | OMC how | OMG how | Status | Evidence |
|---------|---------|---------|--------|----------|
| **Parallel fan-out (ulw)** | `ultrawork` skill：多 Task 平行 + tier 路由；component only | `omg ulw` + `omg-ultrawork`：`spawn_subagent` depth=1；envelope + `omg integrate` / `worker prepare\|seal`；experimental process fanout | **HAVE**（Grok-native 路徑完整；無 OMC model-tier 路由） | `skills/omg-ultrawork/SKILL.md`; `omg_cli/modes.py`; `integrate.py`; `workers.py`; `fanout.py` |
| **Persistence loop (ralph)** | In-session PRD loop + Stop continuation + reviewer + deslop | CLI `omg ralph` max_iter outer loop；skill = **one story / iter**；context pack；accept 後才 verified | **HAVE**（機制不同但產品目標對齊；**非** OMC Stop pin） | `skills/omg-ralph/SKILL.md`; `modes.py` `ralph_context_pack`; `stop-continuation/CONSENSUS.md` |
| **Plan consensus (ralplan)** | Planner→Architect→Critic；RALPLAN-DR；pre-exec gate | CLI FSM draft→critic→revise→verifier；APPROVE whole-word；no implement | **HAVE**（角色較扁：無獨立 Architect/Planner agent；無 vague-prompt gate） | `skills/omg-ralplan/SKILL.md`; `omg_cli/ralplan.py` |
| **Full auto pipeline (autopilot)** | 5 階段 + UltraQA + multi-perspective Phase 4 + deep-interview 銜接 | `omg pipeline`：plan→implement→integrate→dual_review→accept→report；`--resume` | **PARTIAL** | `skills/omg-pipeline/SKILL.md`; `pipeline.py` `STAGE_ORDER`; OMC `skill-bodies/autopilot/SKILL.md` |
| **Dual / multi review** | architect + security-reviewer + code-reviewer 平行；ralph critic 可選 codex | `omg dual-review` / pipeline stage：**sequential headless** critic→verifier；TUI prefer native spawn；**不** set verified；external = 人類 `omg ask` | **PARTIAL** | `skills/omg-dual-review/SKILL.md`; `dual_review.py`; agents critic/verifier |
| **Ask external advisors** | `omc ask` multi-provider + artifacts | `omg ask` codex/claude(fable)/gemini…；child-only allow env；artifact only | **HAVE** | `skills/omg-ask/SKILL.md`; `omg_cli/ask/` |
| **Team / tmux multi-process** | implicit agent teams + `omc team` / tmux CLI workers + staged pipeline | **明確 no tmux v1**；預設 `spawn_subagent`；process fanout experimental opt-in | **OUT_OF_SCOPE**（v1）/ process path **PARTIAL** experimental | README; BRIEF; `fanout.py`; OMC `skill-bodies/team/SKILL.md` |
| **Stop pin / force continue** | Stop hook `decision:block` + boulder / persistent-mode | Stop **passive spool only**；host only PreToolUse blocks | **NEVER**（0.3.x） | `hooks/bin/stop.py`; `stop-continuation/CONSENSUS.md`; BRIEF |
| **Context pack / resume** | session-scoped state MCP；ralph progress/prd；autopilot resume | ralph context pack each iter；`pipeline --resume`；run dir artifacts；**無** session MCP / multi-mode state graph | **PARTIAL** | `modes.py` `ralph_context_pack`; `pipeline.py` resume; OMC cancel/state MCP |
| **Doctor / setup** | omc-setup / omc-doctor multi-phase | `omg setup` + `omg doctor` / `--strict` | **HAVE** | `setup_cmd.py`; `doctor.py`; skills omg-using |
| **Cancel** | 多 mode 智慧 cancel + team shutdown + force + MCP | `omg cancel` PID/pg + starttime verify；skill playbook | **HAVE**（覆蓋面較窄，夠用） | `state.py` `cancel_run`; `skills/omg-cancel/SKILL.md` |
| **Acceptance / verified gate** | reviewer + tests；多路徑 completion；state tools | **更硬：** 僅 `omg accept` + frozen manifest + semantic policy + CLI stamp 可 `set_verified` | **HAVE**（OMG 在 verified 權威性上甚至更嚴格） | `acceptance.py`; `command_policy.py`; `state.py` `set_verified`; README |
| **HUD** | statusLine + omc-hud.mjs presets | 無 | **MISSING** | OMC `skill-bodies/hud/SKILL.md`；OMG 無對應 skill/CLI |
| **Wiki** | wiki_* MCP + `.omc/wiki` | 無（僅 `.omg/artifacts` 提案） | **MISSING** | OMC wiki skill；OMG no `wiki` |
| **Notifications** | configure-notifications skill | 無 | **MISSING** | OMC skill + `dist/notifications/` |
| **Deep interview** | Socratic + ambiguity math + topology gate | 無 | **MISSING** | OMC `skill-bodies/deep-interview/SKILL.md` |
| **UltraQA** | QA cycle skill + state | pipeline 無獨立 QA cycle skill；accept 是命令 gate 非 diagnose-fix loop | **MISSING** | OMC ultraqa；OMG 無 `omg-ultraqa` |
| **Ultragoal durable goals** | ledger + /goal handoff | 無（run 級 state 有，非 multi-goal durable ledger） | **MISSING** | OMC ultragoal skill + `docs/ultragoal.md` |
| **Skill management** | skillify / skill / learner / self-improve | 固定 8 個 omg-* skills；無 meta skill tooling | **MISSING** | OMC skillify 等；OMG `skills/` 僅 8 |
| **Capability isolation** | permission modes + hooks + team worktrees | `capability_mode` 契約 + agent disallowedTools + PreToolUse spawn fail-closed + accept policy | **HAVE**（honest residual documented） | `docs/security-model.md`; agents frontmatter; `deny.py` |
| **PreToolUse canary** | 大量 enforcer hooks | `scripts/canary_pretool.py` + host-signature criteria；doctor global hook check | **HAVE** | `scripts/canary_pretool.py`; `docs/security-model.md`; live suite docs |

### 3.2 Extra OMC 面（有產品意義）

| Feature | OMC | OMG | Status | Evidence |
|---------|-----|-----|--------|----------|
| **Ralplan pre-exec gate**（vague prompt 強制先 plan） | ralplan skill 內 gate | 無 keyword gate；使用者自己選 mode | **MISSING** | OMC ralplan body §Pre-Execution Gate |
| **Agent catalog depth** | 19 specialists + tier models | 4 omg-* + Grok built-ins | **PARTIAL** | OMC `agents/`; OMG `agents/` |
| **Keyword / magic mode inject** | keyword-detector + skill injector hooks | skill descriptions + CLI hard keywords；無 OMC 級 magic inject | **PARTIAL** | OMC `scripts/keyword-detector.mjs`; OMG skills only |
| **ai-slop-cleaner / deslop** | ralph mandatory pass | 無 | **MISSING** | OMC ralph body step 7.5 |
| **CCG / multi-LLM council skill** | `ccg` skill | `omg ask` 人類串；無 auto CCG skill | **PARTIAL** / **OUT** as default | OMC ccg；OMG ask skill HARD RULES |
| **Project session manager (PSM)** | worktree + tmux + multi-provider session | 無 | **MISSING** / **OUT** v1 tmux | OMC `project-session-manager/` |
| **Remember / project memory** | remember tags + project-memory hooks | 無 | **MISSING** | OMC remember + hooks |
| **Visual verdict** | visual-verdict skill | 無 | **MISSING** | OMC skill |
| **Autoresearch / sciomc / self-improve** | 研究/科學/自我改進 loops | 無 | **OUT_OF_SCOPE**（非 core coding orchestration） | OMC skill list |
| **Writer-memory** | 長篇寫作記憶 | 無 | **OUT_OF_SCOPE** | OMC writer-memory |
| **Verify skill (playbook)** | verify skill | 分散在 verifier agent + accept | **PARTIAL** | OMC verify；OMG omg-verifier + accept |
| **MCP state_read/write** | 完整 MCP state API | 檔案 JSON under `.omg/state/` only | **PARTIAL** | OMC cancel skill MCP tools；OMG `state.py` |
| **Handoffs dir** | `.omc/handoffs/` stage docs | artifacts 自由寫；無 staged handoff 協議 | **PARTIAL** | OMC team skill；OMG artifacts |
| **Multi-repo `.omc-workspace`** | documented | 未見同等 | **MISSING** | OMC REFERENCE / team caveats |
| **Process multi-PID workers** | tmux team CLI workers | experimental `ulw --fanout process` | **PARTIAL** | `fanout.py` |
| **Live gates / smoke** | 大型 test suite | `scripts/smoke.sh`, `live_suite.sh`, pytest unit | **HAVE**（規模較小） | `scripts/`; `tests/` |

---

## 4. Parity %（粗算，非精確 KPI）

### 4.1 Core workflows only（「基本 OMC」）

定義 core = 使用者最常以為 OMC「會做事」的路徑：

`ulw` · `ralph` · `ralplan` · autopilot/pipeline · dual-review · ask · cancel · doctor/setup · accept/verified · capability isolation · context/resume · stop-continue 行為對等

| 計分 | 項目 |
|------|------|
| 滿分項（~1.0） | ulw, ralph*, ralplan*, ask, cancel, doctor/setup, accept/verified, capability, canary |
| 半項（~0.5） | pipeline vs autopilot, dual-review vs multi-review, context/resume |
| 0 / NEVER | Stop pin；team 不計入「基本」若接受 Option B |

**粗算：≈ 72–78% core functional parity**  
（若把 Stop pin 與 team 硬算進 core，會掉到 ≈ 55–60%。BRIEF 已定 Stop NEVER、team no-tmux → 應用 **~75%**。）

\*機制不同（CLI outer loop vs in-session Stop）但 **使用者可達成「做到 verified 為止」**。

### 4.2 Full OMC surface

~40 skills + 19 agents + dense hooks + MCP + HUD/wiki/notify/memory/PSM/team/ultragoal/ultraqa/deep-interview/…

**粗算：≈ 25–35% full surface**  
（OMG 刻意 Option B：plugin+CLI、Grok-native workers、無 Rust fork、無 tmux v1。）

---

## 5. Top 10 MISSING by user value

依「Grok 使用者會痛」排序（含 NEVER 但必須說清楚的替代）：

| # | Gap | Status | 為何痛 | 建議方向（inventory only） |
|---|-----|--------|--------|----------------------------|
| 1 | **In-session「don't stop」= OMC Stop pin** | **NEVER** | 使用者在 TUI 打 ralph 期望 boulder；host 無法 block Stop | 產品文案強制：`omg ralph` / `omg pipeline`；skill 已寫；勿假實作 |
| 2 | **Deep interview / 需求澄清** | **MISSING** | vague goal 直接 pipeline → 錯建浪費 | 0.3 高價值；可簡化版（無完整 ambiguity math） |
| 3 | **UltraQA-class diagnose→fix cycle** | **MISSING** | accept 只跑命令；失敗無 architect 診斷 loop | pipeline 可選 stage 或 `omg qa` |
| 4 | **Native dual-review（spawn 而非 sequential headless）+ 多視角 review** | **PARTIAL** | CLI dual-review 是 interim；缺 security/architect 第三視角 | 完成 native spawn path；可選 `omg ask` 當外部 critic |
| 5 | **Ralplan / pipeline 前 vague-prompt gate** | **MISSING** | 直接 ulw/ralph 空目標 | 輕量 keyword + anchors 檢查（不必抄完整 OMC gate） |
| 6 | **Durable multi-session goals（ultragoal-like）** | **MISSING** | run 結束進度易斷；跨 session 大項目 | ledger under `.omg/` + resume 文案；勿綁 Claude `/goal` |
| 7 | **Agent catalog（architect / security / test-engineer…）** | **PARTIAL** | 複雜任務只有 4 角色 | 按需加 omg-architect 等 prompt agents（仍 Grok-native） |
| 8 | **HUD / 即時 run 可見度** | **MISSING** | 長 ralph/pipeline 黑盒 | 若 Grok 有 statusLine/API 再做；否則 `omg state` 輪詢 + report 強化 |
| 9 | **Project memory / wiki / remember** | **MISSING** | 跨 session 決策遺失 | 低優先；artifacts 先夠用 |
| 10 | **Team / multi-process orchestration** | **OUT_OF_SCOPE** v1 | 超大平行、異質 CLI workers | 維持 spawn_subagent；process fanout 僅實驗；勿默認 tmux |

**Honorable mentions（非 top10 但審計常提）：** deslop/ai-slop-cleaner、notifications、skillify、PSM、multi-repo workspace marker、CCG auto-council skill。

---

## 6. 對 BRIEF 四題的 inventory 側答案

1. **基本 OMC 功能？**  
   **Yes for core orchestration paths**（ulw/ralph/ralplan/pipeline/accept/cancel/doctor/ask/isolation），**No for lifestyle/platform features**（HUD/wiki/memory/team/deep-interview/Stop pin）。

2. **Don't-stop on Grok？**  
   **CLI outer loop only**（已實作）。Stop pin = **NEVER** until host blocking Stop。

3. **真實 product 仍缺？**  
   見 §5：需求澄清、QA cycle、native/multi review、vague gate、durable goals、agent 深度、可觀測性。pipeline 已是 autopilot **骨架**，不是完整 OMC autopilot 行為樹。

4. **0.3.x roadmap 訊號（inventory，非 planner 定案）**  
   - **Build：** deep-interview 精簡、QA cycle、native dual-review、vague gate、agent 擴充、pipeline/resume 打磨  
   - **Never / defer：** Stop continuation、tmux team、全量 wiki/HUD（除非 host 有原生 status 鉤子）、OMC 級 MCP state 生態 clone

---

## 7. 原始證據索引

| 區 | Path |
|----|------|
| BRIEF | `<repo-root>/docs/research/omc-parity-council/BRIEF.md` |
| OMG README | `<repo-root>/README.md` |
| CLI router | `<repo-root>/omg_cli/main.py` |
| Security | `<repo-root>/docs/security-model.md` |
| Stop consensus | `<repo-root>/docs/research/stop-continuation/CONSENSUS.md` |
| Hooks | `<repo-root>/hooks/hooks.json` |
| OMC plugin | `~/.claude/plugins/cache/omc/oh-my-claudecode/4.15.5/` |
| OMC skill bodies | `…/4.15.5/skill-bodies/{autopilot,ralph,ultrawork,ralplan,team,ultragoal,ultraqa,wiki,hud,deep-interview,ask,cancel,verify}/SKILL.md` |

---

**Inventory complete.** 後續 architect / critic / planner 應以本矩陣 **HAVE/PARTIAL/MISSING/NEVER** 為準，勿把 skill 命名當行為等價。
