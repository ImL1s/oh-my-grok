# Skills 目錄（oh-my-grok）

English | [简体中文](./skills.zh.md) | [繁體中文](./skills.zh-TW.md)

English: [`skills.md`](./skills.md)

**45 個 in-session skills**，路徑：[`skills/omg-*/SKILL.md`](../skills/)
（原 16 + Wave B 13 + Wave C 16）。  
概念類似 OMC skill zoo，執行面是 **Grok-native**：playbook + `omg` CLI 蓋章。
**不是** live-verified。Antigravity 檔案仍是**投影**。

機器目錄（別名、分類、pipeline、續跑策略）：[`skills/catalog.json`](../skills/catalog.json) ·
生成表 [`docs/parity/skills-catalog.md`](./parity/skills-catalog.md) ·
[简体](./parity/skills-catalog.zh.md) ·
[繁體](./parity/skills-catalog.zh-TW.md)。
檢視：`omg skill list|show|resolve|resources`（永不寫 `verified`）。
Grok `<workflow_routing>` 由此 catalog 生成（無 UserPromptSubmit 注入）。
Antigravity 投影**不是**已安裝 AG 外掛。

> **兩種表面（類似 OMC 的 CLI vs `/skill`）**  
> - **終端機 CLI：** shell 裡跑 `omg …`（狀態、accept、modes）。  
> - **Session skill：** 安裝 plugin 後，在 Grok Build 對話裡用自然語言或 `/oh-my-grok:<skill>`。  
> OMG 差異：很多流程**同時**有 skill playbook **與** 真實 CLI 子命令（`omg autopilot`、`omg ralph`…）。

---

## 如何呼叫 skill

| 方式 | 範例 |
|------|------|
| 自然語言（推薦） | `autopilot 完成登入重構` · `ulw 修好這三個 package` · `ralph 做到完` |
| Skill id（Grok plugin） | `/oh-my-grok:omg-autopilot` · `/oh-my-grok:omg-ultrawork` |
| 只在終端機 | `omg ralph "…"` / `omg ulw "…"`（不必進 chat skill） |

**路由：** 不確定用哪個 → 載入 **`omg-using`**（或問「omg 怎麼用」）。

**所有 skill 的 HARD RULES：**

1. 只透過 Grok `spawn_subagent` 扇出（depth 1）。
2. 一律設 `capability_mode`（實作 `read-write` / 審查 `read-only`）。
3. 只有 **`omg` CLI** 可以寫 `.omg/state/` 下的 `verified` / `passes`。
4. 中止用 `omg cancel` — 禁止會自我匹配的 `pkill -f`。
5. grok **≥0.2.107** 有 Stop pin（每 turn 上限 **8**，fail-open）— 超 cap 或 turn 結束後，再呼叫 skill、`/loop`，或說 **繼續 / continue**。

---

## In-session 快捷表（OMC 風格）

| 觸發詞 / 說法 | Skill | 終端機 CLI | 做什麼 |
|---------------|--------|------------|--------|
| omg 怎麼用、第一次 | `omg-using` | `omg doctor` · `omg setup` · `omg resume` | 路由 + 健康檢查 |
| autopilot、full auto、幫我做完 | `omg-autopilot` | `omg autopilot *` | interview→…→verified |
| ulw、ultrawork、平行 | `omg-ultrawork` | `omg ulw` + worker + integrate | 平行 fan-out |
| team N、tmux team、多 pane | `omg-team` | `omg team …` | 持久 tmux panes — slash **僅** `/oh-my-grok:omg-team`（無裸 `/team`） |
| ralph、不要停、做到完 | `omg-ralph` | `omg ralph` | 單 story 外層迴圈 |
| ralplan、plan 共識 | `omg-ralplan` | `omg ralplan` | 計畫→critic→verifier（不寫碼） |
| deep interview、釐清需求 | `omg-deep-interview` | `omg interview *` | 需求閘門 |
| ultragoal、多 story、goal ledger | `omg-ultragoal` | `omg goal *` | 持久 ledger + host `/goal` session 壓力 |
| ultraqa、修測試、重跑 | `omg-ultraqa` | `omg qa *` | freeze→run→repair（**≠ verified**） |
| dual-review、不要 self-approve | `omg-dual-review` | `omg dual-review` · `omg review` | critic→verifier |
| pipeline | `omg-pipeline` | `omg pipeline` | plan→implement→accept FSM |
| ask codex / 第二意見 | `omg-ask` | `omg ask` | 人類觸發的外部顧問 |
| cancel、中止 | `omg-cancel` | `omg cancel` | 安全中止 |
| wiki、專案記憶 | `omg-wiki` | `omg wiki *` | 本地 markdown wiki |
| hud、statusline | `omg-hud` | `omg hud` | 一行狀態 |
| lsp、symbols | `omg-lsp` | `omg lsp *` | 檢查 host-owned `.lsp.json`；無語意 proxy |

**多關鍵字同時出現時的優先序**（catalog 驅動；與 Grok 規則相同）：  
`cancel` > `ralplan` > `autopilot` > `ultragoal` > `ralph` > `ulw`，然後其餘 continuation owner，然後其它。

### Wave B/C 選擇器（configured playbook — 非 live-verified）

| 何時 | Skill | 誠實 CLI |
|------|-------|----------|
| 規劃前最佳實踐簡報 | `omg-best-practice-research` | `omg ask`（顧問） |
| 追蹤 / lifecycle 投影 | `omg-trace` | `omg tracker status\|project\|reconcile` |
| Deep-dive 調研 | `omg-deep-dive` | 僅製品 |
| 吸入外部事實 | `omg-external-context` | `omg memory *` |
| 紅綠一條切片 | `omg-tdd` | `omg qa *`（永不 verified） |
| 修好損壞的 build | `omg-build-fix` | `omg qa *` |
| 對 diff 做安全車道 | `omg-security-review` | `omg review` / `omg dual-review` |
| 給 visual envelope 打分 | `omg-visual-verdict` | `omg visual compare`（僅 compare；無 capture/Ralph） |
| 深度工作區 init | `omg-deepinit` | `omg setup`（`init-deep` 別名） |
| 具名 session | `omg-project-session-manager` | `omg session allocate\|route`（`psm` 別名） |
| MCP 註冊 | `omg-mcp-setup` | `omg mcp-install` |
| 通知佇列 | `omg-configure-notifications` | `omg notify status\|send\|process` |
| 檢視 skill 目錄 | `omg-skill` | `omg skill list\|show\|resolve\|resources` |
| 更嚴的計畫閘 | `omg-prometheus-strict` | `omg ralplan` |
| Team Hyperplan | `omg-hyperplan` | `omg team hyperplan`（Team #69；fixture execute） |
| 有界調研循環 | `omg-autoresearch` | 製品；可選 `omg ask` |
| 對著 goal ledger 調研 | `omg-autoresearch-goal` | `omg goal *` |
| 平行調研扇出 | `omg-parallel-research` | 可選 `omg ask`（`sciomc` 別名） |
| 改進寫下來 | `omg-self-improve` | 僅製品 — **沒有** learning loop |
| 寫作者筆記 | `omg-writer-memory` | `omg memory *` / `omg note` |
| 問 visual 持久循環 | `omg-visual-ralph` | 僅 `omg visual compare`；循環仍是 `omg ralph` |
| 清 AI slop | `omg-ai-slop-cleaner` | 製品報告；改檔用 `omg edit plan\|apply`（無 `comments` 子命令） |
| 註解稽核 | `omg-comment-checker` | 製品報告（`omg edit` 無 comments 子命令） |
| Team security-research | `omg-security-research` | `omg team security-research`（Team #69） |
| UI 切片 | `omg-design` | `omg-designer` spawn；無額外 CLI |
| 發布檢查單 | `omg-release` | `omg parity *`（本 skill 永不 verified） |
| Git 衛生 | `omg-git-master` | 宿主 git；無 `omg git` 孿生 |
| 搭 ralph PRD | `omg-ralph-init` | 然後 `omg ralph` |
| 低 token 姿態 | `omg-ecomode` | 無 CLI 孿生；`capability_mode` none |

Host-native 名字（`plan`、`goal`、`loop`、`compact`、`help`、`agents`、`mcp`、`skills`、`plugin`）保持別名/宿主所有 — 永不做成外掛目錄。`ulw-loop` 仍是別名。

---

## 建議 skill 鏈

```text
模糊想法
  → omg-using → omg-deep-interview → omg-ralplan → omg-autopilot
     （或 plan 後改 omg-ralph / omg-ultrawork）

多檔、彼此獨立的切片
  → omg-ultrawork → omg integrate → omg accept

單一 story、多輪做到 verified
  → omg-ralph  （CLI 擁有 max-iter 外層迴圈）

同一對話內完整生命週期
  → omg-autopilot  （中斷就 continue）

跨天多 story
  → omg-ultragoal + 每 story 的 ralph/ulw/autopilot

寫完後品質
  → omg-dual-review → omg-ultraqa → omg accept / omg autopilot complete
```

---

## 各 skill 摘要

（規範 playbook 以各 `SKILL.md` 為準；以下是操作者摘要。）

### `omg-using` — 引導 / 路由

| | |
|--|--|
| **何時** | 第一次用、「哪個 skill？」、中斷後 continue |
| **呼叫** | `omg 怎麼用` · `/oh-my-grok:omg-using` |
| **CLI** | `omg doctor` · `omg setup` · `omg state` · `omg resume` |
| **SKILL** | [`skills/omg-using/SKILL.md`](../skills/omg-using/SKILL.md) |

```bash
omg doctor
omg setup
# 重新開 session 後：先讀 .omg/state/RESUME.md，再：
omg resume
omg resume --clear   # 成功接續後清除
```

---

### `omg-autopilot` — 完整生命週期

| | |
|--|--|
| **何時** | 釐清→計畫→實作→審查→QA→verified |
| **呼叫** | `autopilot …` · `full auto` · `/oh-my-grok:omg-autopilot` |
| **CLI** | `omg autopilot start\|transition\|status\|await\|complete\|run` |
| **深講** | [`autopilot.zh-TW.md`](./autopilot.zh-TW.md) · [EN](./autopilot.md) |
| **SKILL** | [`skills/omg-autopilot/SKILL.md`](../skills/omg-autopilot/SKILL.md) |

```bash
omg autopilot start "完成功能 X 並含測試"
# 或：omg autopilot start "…" --skip-interview
omg autopilot run "完成功能 X 並含測試" --unattended   # 無人值守外層 (#40)
omg autopilot run --resume RUN --unattended            # cap / 崩潰後恢復
omg autopilot status --run RUN
omg autopilot await --run RUN --set   # 破壞性/憑證確認時暫停
omg autopilot complete --run RUN
```

階段：`interview → ralplan → implement → review → (rework) → qa → acceptance → verified`  
grok **≥0.2.107** 有 Stop pin（每 turn 上限 **8**，fail-open）— 超 cap 見 [autopilot.zh-TW.md](./autopilot.zh-TW.md#stop-pin-誠實說明)（`omg autopilot run --resume … --unattended`、`/loop`、外層 `omg ralph`）。

---

### `omg-ultrawork` — 平行執行

| | |
|--|--|
| **何時** | 獨立切片、平行 agent |
| **呼叫** | `ulw` · `ultrawork` · `/oh-my-grok:omg-ultrawork` |
| **CLI** | `omg ulw` · `omg worker own\|prepare\|seal\|join` · `omg integrate` |
| **SKILL** | [`skills/omg-ultrawork/SKILL.md`](../skills/omg-ultrawork/SKILL.md) |

```bash
omg ulw "平行修 A/B/C"
omg worker own --run RUN --tasks-json '[{"task_id":"t1","owned_files":["a.py"]}]'
omg worker prepare-owned --run RUN
omg worker seal --run RUN --task t1
omg worker join --run RUN
omg integrate --run RUN
omg accept --yes
```

---

### `omg team` — tmux team plane（預設開啟；D1 零設定 + D3 multi-CLI + D2 分階段 driver + D4 scale/resume/ralph）

| | |
|--|--|
| **何時** | 多 pane ULW + 真實 worktree；測試用 hermetic dry-run / fixture smoke |
| **閘門** | **預設開啟。** 關閉：`OMG_DISABLE_TMUX_TEAM=1`（舊 `OMG_EXPERIMENTAL_TMUX_TEAM=0` 也會關） |
| **Skill** | `omg-team` — session slash **僅** `/oh-my-grok:omg-team`；自然語言 `team N …` |
| **CLI** | `omg team launch`（argv 簡寫 `N`/`N:role`+goal → launch；`--io-mode auto\|interactive\|headless`）；亦 `start\|run\|scale\|resume\|status\|collect\|stop\|api\|supervisor\|panes\|capture\|focus\|key\|input\|watch\|view\|hyperplan\|security-research` |

**啟動就緒（#99）：** pane supervisor 證明 provider 真正可用（`pane_created` →
`provider_spawned` → `provider_ready` → `task_dispatched`；可選 `mailbox_ack`）。
舊版 `worker-ready` v1 收據僅為 `wrapper_ready_legacy`，不能產生
`startup_status=running`。認證/信任提示 → `blocked_start`。`--no-wait` →
`unverified_start`。`--io-mode interactive` 不等 supervisor ACK，由 leader
在同一逾時內等待 pane TTY 上的 `TUI_READY:<nonce>`，並在 pane/PID/start
TOCTOU 再證明後才提升 `input_ready`；就緒後提交 attempt-scoped inbox
instruction（不是 TUI seed）。interactive 團隊的 `scale --add` 用 interactive
argv/wrapper，不會長出 headless supervisor pane。Grok 1.0.4 無原生 ready 行，
由 `interactive_wrapper` 僅在 grok 開始讀 TTY 後列印。TUI 首輪種子是位置參數
`grok "<text>"`（沒有 `--prompt` 旗標）。逾時 / identity mismatch fail-closed，
不會靜默降為 headless。Fixture 不得宣稱 `LIVE_TEAM_INTERACTIVE_TTY_OK`。

**靜默 bootstrap（#100）：** worker pane 成功啟動不印 JSON / nested-`.omg` 警告；
失敗僅一行提示。詳情見 `workers/<id>/bootstrap.log`，用
`omg team status <run> --full` 檢視（不要把 pane scrollback 當權威錯誤來源）。
| **誠實範圍** | 零設定 = grok panes；`--routing` 啟 multi-CLI（含角色地板）。**整合**隔離（ownership + seal + integrate）— **不是**執行沙箱。`collect` / `run` / `scale` / `resume` 永不寫 `verified`。scale/resume/ralph 是**同一** team plane 的生命週期延伸（無新隔離宣稱）。Live 升格證據：`scripts/live_team_smoke.py --live` → `LIVE_TEAM_SMOKE_OK`（2026-07-30 本地；不進 CI）。**無裸 `/team` slash alias** — 2026-07-25 host 探測：plugin skill 為 `/name` 或 `/plugin:name`，無 frontmatter 可為 `omg-team` 註冊 unnamespaced `/team`；其他 plugin 已占用 `team` skill 名。 |

**視窗拓樸（#96）：** 既有 tmux pane 內啟動預設 `view_mode=same_window`（leader 左、workers 右堆疊；detached split + `main-vertical`）。`--dedicated-window` 使用獨立 Team window；tmux 外 / `--detach` 為 `detached_session`。same_window 的 `stop`/失敗回滾只清 worker panes，不殺 shared leader window。plan-only / dry-run / live JSON 皆帶 `view_mode`；缺 mode 的舊 run 維持 dedicated/detached 行為。

**`omg team run`** 是 team plane 上的**分階段 DRIVER**（不是新的 planner/verifier）：

`team-plan → team-prd → team-exec → team-verify → team-fix`（終態：`complete` / `failed` / `blocked`）。

- **team-plan / team-prd** — 穿透標記；任務拆解屬 **leader / ralplan**，`run` 只吃 `--tasks-json` 或 `--tasks-path`。
- **team-exec** — `start_team` 再 `collect_team`（dry-run 只 start，不碰 tmux/subprocess）。
- **team-verify** — 以 POST-A2 `parse_verdict_file` 閘 `stages/team-verifier.md|json`；APPROVE → `complete`，否則 → `team-fix`。**不**代寫 verdict。
- **team-fix** — `--max-fix`（預設 3）上限；超限 → `failed`。
- **`--ralph [--max-iter N]`**（D4）— 外層**有界**持久迴圈（預設 max_iter=3）；`team.json` 記 `linked_ralph`、`stages/team-ralph.json` 記 `linked_team`；仍只靠真實 team-verify APPROVE 進 complete，**永不**寫 `verified`。
- 進 exec/fix 會作廢舊 verify 戳記；`verified` 仍只經 `omg accept`。

**生命週期（D4）：**

- **`omg team scale --run ID --add N|--remove N [--dry-run]`** — 在 run 目錄 **scale lock** 下動態加/減 pane。Live scale-up 在副作用前先發布按 generation 分隔的不可變 **WAL**，再以 `@omg_scale_nonce` + rename 綁定 window，並 **fail-closed ownership readback**（精確 `display-message`；不單靠可變的 `session:index`）。未提交的 scale-up WAL 或未來 **identity-receipt** generation 會擋住 dry-run add、remove、resume/relaunch、collect/join/integrate、stop，直到原 op recovery 完成。`--add` 受 `max_workers_cap()` 與單調 window index 限制；`--remove` 首次優雅排空（idle/newest），recovery 綁定 **receipt 受害者**（錯誤的 `--remove N` 會 fail-closed 並帶 generation + task id），只殺記錄的 pgid + 已認證 pane（**不**殺 session、**禁止** `pkill -f`），標記 `scaled_down` 並保留 worktree；active 不可低於 1。Meta 提交若失去成功路徑，以 identity readback 分為 committed / not_committed / unknown（不以 volatile 的 `last_scale.actions` 單獨判定）。**不是**執行 sandbox — 見 `docs/security-model.md`。
- **`omg team resume --run ID`** — 同一 scale lock 下重讀 `team.json`；若 relaunch WAL 待處理，先做精確 relaunch recovery，再做 pane 存活對帳（冪等 status 寫入）。pane 對帳/relaunch 之後（仍持同一把 lock）再對帳 Team API task claims：保留未過期且一致的 claim（位元組級不變）；僅將已過期且一致的 claim 重置為 `pending`（version+1，舊 token 被圍欄）。缺失 API board 為非物化 no-op；claim 損壞在任何 mutation 前 fail-closed。resume 輸出附加 `claim_reconcile`（僅 ID/計數，不含 token）。仍符合 receipt 身分的 remain-on-exit dead pane，process 不在時可清理後提交為 `needs_collect`。**預設絕不 attach / 切換 tmux client**（腳本安全）。加 `--view` 在釋放 lifecycle lock 後恢復精確 Team window/leader（同 session `select-*`、跨 session `switch-client`、TTY 外 `attach-session`；`--takeover` 才加 `-d`；`--json` 永不執行 view）。`omg team view` 只恢復視圖不做 reconcile/relaunch；`--print` 只列印 argv。AVAILABLE 時啟動/複用 jobs 平面內部 ACP stdio sidecar（no-replay receipt；非 session/close）。reconcile / provider-session / tmux-view 結果分開回報。公開的 `omg team api reconcile` / MCP 對等物仍需未來 catalog 版本 — 見 `docs/team-operation-catalog-v1.md`。

```bash
omg team start --goal "平行修 A/B" --tasks-json '[{"task_id":"t1","owned_files":["a.py"]},{"task_id":"t2","owned_files":["b.py"]}]' --dry-run
omg team start --goal "…" --tasks-json '[{"task_id":"t1","owned_files":["a.py"],"provider":"fake"}]' --worker-topology=job --dry-run
omg team run --goal "x" --tasks-json '[{"task_id":"t1","owned_files":["a.py"]}]' --dry-run --max-fix 3
omg team run --goal "x" --tasks-json '[{"task_id":"t1","owned_files":["a.py"]}]' --ralph --max-iter 2 --dry-run
omg team scale --run RUN --add 2 --dry-run
omg team resume --run RUN
omg team resume --run RUN --view
omg team view --run RUN --print
omg team status --run RUN --json
omg team collect --run RUN   # seal_all_tasks + integrate；永不 verified
omg team stop --run RUN      # 只殺記錄的 session + pgid（禁止 pkill -f）
# Hyperplan / Security Research V1 fixture execute（#69 PR14；compile 仍 execution_supported=false）:
omg team hyperplan execute --run RUN --team-id TEAM --executor fixture|grok --input RESULT_BUNDLE.json --json
omg team security-research execute --run RUN --team-id TEAM --executor fixture|grok --input RESULT_BUNDLE.json --json
# 關閉 team plane：export OMG_DISABLE_TMUX_TEAM=1
```

---

### `omg-ralph` — 持久迴圈（單 story）

| | |
|--|--|
| **何時** | 不要停到 verified；多輪同一目標 |
| **呼叫** | `ralph` · `做到完` · `/oh-my-grok:omg-ralph` |
| **CLI** | `omg ralph "goal"`（`--max-iter N`） |
| **SKILL** | [`skills/omg-ralph/SKILL.md`](../skills/omg-ralph/SKILL.md) |

```bash
omg ralph "完成 auth 遷移" --max-iter 5
```

Skill = **單次 iteration** playbook；**CLI 外層** 擁有 max-iter 與重啟。

---

### `omg-ralplan` — 計畫共識（不寫產品碼）

| | |
|--|--|
| **何時** | 寫碼前先對齊計畫 |
| **呼叫** | `ralplan` · `plan 共識` · `/oh-my-grok:omg-ralplan` |
| **CLI** | `omg ralplan "…"` |
| **SKILL** | [`skills/omg-ralplan/SKILL.md`](../skills/omg-ralplan/SKILL.md) |

```bash
omg ralplan "auth 重構共識計畫" --safe
# FSM: draft → critic → revise → verifier → APPROVE
# 之後：omg ulw / omg ralph / omg autopilot
```

---

### `omg-deep-interview` — 需求閘門

| | |
|--|--|
| **何時** | 目標模糊、範圍不清 |
| **呼叫** | `deep interview` · `釐清需求` · `/oh-my-grok:omg-deep-interview` |
| **CLI** | `omg interview start\|answer\|status\|pressure-pass\|close` |
| **SKILL** | [`skills/omg-deep-interview/SKILL.md`](../skills/omg-deep-interview/SKILL.md) |

```bash
omg interview start "重建 billing" --profile standard
omg interview status --run RUN
omg interview answer --run RUN --question-id Q1 --text "…"
omg interview pressure-pass --run RUN --text "假設與風險…"
omg interview close --run RUN
```

---

### `omg-ultragoal` — 多 story ledger

| | |
|--|--|
| **何時** | 多個持久 story、depends_on、跨 session |
| **呼叫** | `ultragoal` · `goal ledger` · `/oh-my-grok:omg-ultragoal` |
| **CLI** | `omg goal init\|status\|set-host\|link-run\|start-story\|checkpoint\|block-story\|resume-story\|complete-story\|verify\|repair` |
| **SKILL** | [`skills/omg-ultragoal/SKILL.md`](../skills/omg-ultragoal/SKILL.md) |

Grok **有** slash `/goal`（session 範圍、單 goal、設定即替換；Active 繞過 Stop；
重啟後降為 paused，用 `/goal resume`）。多 story ledger 在 `.omg/ultragoal/`
經 `omg goal *`（無 OMX `get_goal`/`create_goal` tool API）。  
`omg goal set-host --goal GOAL` 列印 `/goal …` handoff（僅 prompt turn）。  
`omg goal verify` 需要已透過 accept/complete **verified** 的 linked run。

---

### `omg-ultraqa` — QA 修復迴圈

| | |
|--|--|
| **何時** | 對抗式 QA、重測到綠、review 之後 |
| **呼叫** | `ultraqa` · `修測試` · `/oh-my-grok:omg-ultraqa` |
| **CLI** | `omg qa freeze\|run\|status` |
| **SKILL** | [`skills/omg-ultraqa/SKILL.md`](../skills/omg-ultraqa/SKILL.md) |

```bash
omg qa freeze --run RUN --scenarios-json \
  '[{"id":"unit","command":"python3 -m pytest -q -m '"'"'not live'"'"'"}]'
omg qa run --run RUN
omg qa status --run RUN
```

**QA clean ≠ verified。** 接著 `omg accept` 或 `omg autopilot complete`。  
Freeze 會拒絕 `grep` / `test` / `omg` / `python -c`（v0.3.2+ 有 tip）。

---

### `omg-dual-review` — critic → verifier

| | |
|--|--|
| **何時** | 不要 self-approve；獨立審查 |
| **呼叫** | `dual-review` · `/oh-my-grok:omg-dual-review` |
| **CLI** | `omg dual-review "…"` · `omg review --run RUN …` |
| **SKILL** | [`skills/omg-dual-review/SKILL.md`](../skills/omg-dual-review/SKILL.md) |

**不會** 設 `verified`。CLI 路徑為序列 headless Grok（相對原生平行 dual-review 為永久 PARTIAL）。

---

### `omg-pipeline` — 腳本化 plan→accept

| | |
|--|--|
| **何時** | CLI 組合流程、不必完整 autopilot skill |
| **呼叫** | `pipeline` · `/oh-my-grok:omg-pipeline` |
| **CLI** | `omg pipeline "goal"` |
| **SKILL** | [`skills/omg-pipeline/SKILL.md`](../skills/omg-pipeline/SKILL.md) |

```bash
omg pipeline "goal"
omg pipeline "goal" --plan-only
omg pipeline "goal" --skip-plan --implement ulw
```

人在迴圈、多階段對話 → 優先 **`omg-autopilot`**。

---

### `omg-ask` — 外部顧問（僅人類觸發）

| | |
|--|--|
| **何時** | Codex / Claude / Gemini 第二意見 |
| **呼叫** | `ask codex …` · `/oh-my-grok:omg-ask` |
| **CLI** | `omg ask list-advisors` · `omg ask explain <id>` · `omg ask codex\|claude\|gemini\|agy "…"` |
| **SKILL** | [`skills/omg-ask/SKILL.md`](../skills/omg-ask/SKILL.md) |

```bash
omg ask list-advisors
omg ask explain fable
omg ask codex "review this patch"
omg ask claude "對這份 plan 的第二意見"
```

`list-advisors` / `explain` 是**離線登錄**目錄事實（每個 harness 都是 `unproven`；二進位為 `not_probed`）。不宣稱合格，也不執行 provider。

**不是** 預設產品 worker。使用者沒要求時 agent 不應自行 shell 顧問 CLI。

---

### `omg-cancel` — 中止

| | |
|--|--|
| **何時** | 卡住、目標錯了、殺 worker |
| **呼叫** | `cancel` · `stop omg` · `/oh-my-grok:omg-cancel` |
| **CLI** | `omg cancel` · `omg cancel --run ID` |
| **SKILL** | [`skills/omg-cancel/SKILL.md`](../skills/omg-cancel/SKILL.md) |

```bash
omg state
omg cancel
omg cancel --run 20260720T…-…
```

---

### `omg-wiki` — 本地知識庫

| | |
|--|--|
| **何時** | 記錄決策、搜尋舊筆記 |
| **呼叫** | `wiki` · `/oh-my-grok:omg-wiki` |
| **CLI** | `omg wiki list\|ingest\|query` |
| **SKILL** | [`skills/omg-wiki/SKILL.md`](../skills/omg-wiki/SKILL.md) |

```bash
omg wiki list
omg wiki ingest --title "Auth 決策" --text "…" --tags "arch"
omg wiki query "auth"
```

不是 run / `verified` 權威來源。

---

### `omg-hud` — 狀態列

| | |
|--|--|
| **何時** | 一行 mode\|status\|stage |
| **呼叫** | `hud` · `/oh-my-grok:omg-hud` |
| **CLI** | `omg hud` · `omg hud --run RUN` · `omg hud --json` |
| **SKILL** | [`skills/omg-hud/SKILL.md`](../skills/omg-hud/SKILL.md) |

---

### `omg-lsp` — host-owned LSP 註冊

| | |
|--|--|
| **何時** | 檢查公開 `.lsp.json` 註冊與本機 server command 是否可用 |
| **呼叫** | `lsp` · `/oh-my-grok:omg-lsp` |
| **CLI** | `omg lsp status` · `omg lsp validate` · legacy: `check`/`symbols`/`diagnostics` → `E_LSP_HOST_OWNED` |
| **SKILL** | [`skills/omg-lsp/SKILL.md`](../skills/omg-lsp/SKILL.md) |

`omg lsp status` / `omg lsp validate` 只檢查 host-owned `.lsp.json`，不會啟動
server。status 會回報 `semantic_proxy_count: 0`；configured 但未由 host 觀測，
不代表 healthy。legacy `check`/`symbols`/`diagnostics` 一律回傳
`E_LSP_HOST_OWNED` / `semantic_proxy_unsupported` 並以 exit code 1 結束（#28）。
語意語言操作請使用 Grok host tools；repository 查找用 `read_file` / `grep`。

---

### 會話內 MCP（`omg mcp-server`）— 聚焦 ops 表面

**聚焦**的會話內 read + proposal MCP 表面，**不是** OMC ~54-tool 對等。
只暴露讀取與非權威 proposal 寫入；`passes` / `verified` / accept **永遠不是**
MCP tool（僅 CLI，且在 `OMG_MCP_SERVER=1` 時**結構性拒絕**）；語意 LSP
操作不會註冊；沒有 code-exec / 狀態突變 / 權威寫入工具。
這是 in-session **workflow** 能力對齊，不是 tool 數量對齊。

```bash
grok mcp add omg omg -- mcp-server
omg mcp-install --print-only
omg mcp-server                 # stdio JSON-RPC（會設 OMG_MCP_SERVER=1）
```

| Tool | 類型 | 後端 |
|------|------|------|
| `omg_state_status` | 讀 | `hud.hud_pack` |
| `omg_state_read` / `omg_state_list_active` | 讀 | state load |
| `omg_note_read` / `omg_note_write` | 讀 / proposal | `.omg/notepad.md` |
| `omg_wiki_*` | 讀 / proposal | `.omg/wiki/` |
| `omg_project_memory_*` | 讀 / proposal | `.omg/project-memory.json` |
| `omg_artifact_write` | 僅 proposal | `.omg/artifacts/` |
| `omg_resume_context` | 讀 | resume pack + RESUME.md |

**三道安全機制：** (1) 策展 allowlist；(2) `OMG_MCP_SERVER=1` 時
`set_verified` / `register_cli_acceptance_token` 直接 raise；(3) 寫入路徑
禁閉（拒 `.omg/state/**` 與 traversal）。

**刻意排除（OMC 有、OMG 沒有）：** `state_write`、`state_clear`、`python_repl`、
`ast_grep_replace`、所有語意 LSP 操作（包括 goto/hover/rename/
find_references/symbols/diagnostics）、
`shared_memory`、`session_search`、`merge_readiness`，以及任何 accept/verify 工具。

---

### 產品服務與 repository workflows（0.6.0）

這些是 CLI contract，不是新增 chat skill。Skill 可以呼叫它們，但權威狀態與
證據仍由 CLI artifact 管理。

| 指令 | Contract |
|---|---|
| `omg session allocate\|route\|search\|friction\|replay\|observatory\|retain\|ag-history\|acp-resume` | Host-session argv，加上已脫敏的 search/friction/replay/observatory/retention；AG history 為唯讀 unsupported stub。Replay 永不重放命令。`acp-resume` 重用 host ACP initialize+session/resume，只產出無正文 receipt（hermetic `OMG_ACP_BIN` 不是 live Grok；無 session/close；restore-code 一律拒絕；不匯入 AG history）。Refs #74。 |
| `omg trace timeline` | 唯讀、有界的 lifecycle timeline（`--run` / `--session`）。永不列印原始 prompt。Refs #74。 |
| `omg recover` | 不可變、受限 JSONL suffix；部分恢復保留 broken-chain/未知紀錄警告。 |
| `omg memory put\|search\|show\|export\|import\|rescan\|layers` | Redacted、確定性的專案 facts，外加唯讀 memory-layer 目錄（不會合併成一份 unbounded memory.json）。 |
| `omg tracker status\|project\|reconcile` | Passive、generation-fenced lifecycle projection。 |
| `omg compact create\|show\|render` | Lossless guidance checkpoint / restore。 |
| `omg notify status\|send\|process` | 只出站、非權威 delivery queue。 |
| `omg workflow install\|list\|show\|plan\|run` | 不可變 registry、確定 waves、receipt-bound ship gate。 |
| `omg parity run\|release-readback\|release-bundle\|release-evidence\|check\|gaps\|refresh` | 委派 frozen W0 manifest engine，並產生／驗 exact bundle 與 completion evidence。 |
| `omg capabilities` / `omg native-status` | 分開的 capability tiers，外加唯讀 `agents_catalog`、`skills_catalog`、`hooks_registry` 與 `tools_sidecar`；不探測私有 sidecar。 |
| `omg agents list\|explain` | Dual-host agent/model 政策檢視（#131）與 host-neutral UX（#134：`--width`／`NO_COLOR`）。Stock Grok Build 使用顯式 inherit；Medley caps 為 unsupported（不是安裝失敗），除非 `--host-inspect` / `OMG_MEDLEY_INSPECT` 提供 Medley inspect。未設 inspect 時 `inspect_source=absent`、`attempt` 為 null，不嘗試 Medley #18 fallback。不做付費探測。Medley TUI 仍為 #290。 |
| `omg skill list\|show\|resolve\|resources` | 唯讀 skill 目錄檢視（#70）。永不寫 `verified`。宿主名 `plan`/`goal` 只作為別名解析。 |
| `omg provider antigravity capabilities\|doctor\|run` | Antigravity（`agy`）探測 + 無頭執行（#67-A/B）：能力信封、doctor、與 `ProviderAdapter.run`（text/json/stream-json）。`omg ask agy` 已切換（#67-C）；Team 窗格經 `build_launch_envelope`（#67-D；supervisor 持有 PTY/PID/readiness）。不宣稱 `live_call_ready`。 |
| `omg visual compare\|capture\|verdict\|ralph\|overlay` | Visual Contract V1（#75）。`compare` 包裝 `compare()`（`--input` JSON；scored/blocked；契約仍不解像素）。`capture` 使用 `capture.command`，否則 `OMG_VISUAL_CAPTURE`，否則 **blocked**（不是假通過；不要求 Playwright）。`verdict` 包裝 `compare()`，在 `.omg/artifacts/visual/<run_id>/` 寫入描述符／findings／分數歷史；`reviewer_status` 要求獨立唯讀 reviewer（否則 `E_VISUAL_REVIEWER`）。`ralph` 為有界 capture/verdict/repair-prompt 迴圈（不 spawn agent）。`overlay` 用標準庫解 PNG，寫入數值 stats + `overlay.png`（`pixel_decode: true`；`--descriptor-only` 跳過解碼）。永不寫入 `passes`/`verified`。本切片無 live 截圖 smoke、無 AG vision 模型。見 [visual-contract-v1.md](./visual-contract-v1.md)。 |
| `omg tools doctor\|serve\|lsp\|ast\|codegraph\|research` | OMG 自有 sidecar（#73）：語意 LSP / AST-grep / CodeGraph（玩具級 import/symbol 掃描，並寫 hermetic SCIP protobuf Index；JSON-lite 仍為 `not_scip`；Homebrew MIP `scip` 不當作 Sourcegraph SCIP）/ 可選網路研究。**不是** Grok 原生 LSP（`omg lsp` 仍為 host-owned）。**不是** live Antigravity 證據。`omg mcp-server` 仍禁止 `lsp.*`。見 [tools-sidecar.md](./tools-sidecar.md)。 |
| `omg edit plan\|apply\|verify\|comments\|simplify` | Hash-anchored 編輯 CLI 加上 comment/simplifier 衛生（#76）：`plan` 唯讀；`apply` 在 Team 所有權 / `OMG_CAPABILITY_MODE=read-only` 門之後走 `apply_hash_edit`；`verify` 重讀並重規劃、不寫檔（stale/ok）；`comments` 預設只報告，除非 `--fix`；`simplify` 預設關閉，除非 `--enable` 或 `.omg/simplify.json`（同一次 `--apply-edits` 中後續失敗會回滾先前已套用的描述符）。可選 `--provider grok` 只產生提案：同樣的 assignment/guard，再跑 Jobs grok（只准 hash-edit descriptors JSON；不套用；不寫 `verified`）。不寫 `passes`/`verified`。不宣稱 `omo.edit.hash_anchored` 宿主對等。見 `docs/hash-edit.md`。 |

Workflow plan 不會啟動外部 CLI。Leader 應使用 Grok 原生 `spawn_subagent`、傳入
精確 `capability_mode`，再把綁定 task ID 的 receipts 交給 `omg workflow run`。
詳見 [workflows.zh-TW.md](./workflows.zh-TW.md)。

## Agents（skills 會用到的角色）

| Agent | 典型 `capability_mode` | 角色 |
|-------|------------------------|------|
| `omg-orchestrator` | leader | 拆解與協調 |
| `omg-executor` | `read-write`（無 shell） | 實作 |
| `omg-debugger` | `read-write`（無 shell） | 根因 / 回歸 / build 修復 |
| `omg-designer` | `read-write`（無 shell） | UI/UX 實作 |
| `omg-writer` | `read-write`（無 shell） | README / API 文件 / 註解 |
| `omg-test-engineer` | `read-write`（無 shell） | 測試策略 / 覆蓋 / flaky 加固 |
| `omg-critic` / `omg-verifier` | `read-only` | 挑戰 / 證據 |
| `omg-code-reviewer` / `omg-architect` | `read-only` | 結構化審查 |
| `omg-security-reviewer` | `read-only` | OWASP / secrets / 不安全模式 |
| `omg-qa-tester` / `omg-analyst` | 見 taxonomy | QA 情境 / interview 分析 |

機器可讀外掛 hook 登錄表：[`hooks/registry.json`](../hooks/registry.json)
（loader `omg_cli/hooks_registry.py`；`omg capabilities` → `hooks_registry` 或
`omg doctor`）。[`docs/parity/projections/antigravity/hooks/`](./parity/projections/antigravity/hooks/)
下的 Antigravity 檔案 **只是投影** — 不是已安裝的 AG 外掛，也不是 live AG 證據。
見 [hooks-lifecycle.md](./hooks-lifecycle.md)。

機器可讀外掛 skill 目錄：[`skills/catalog.json`](../skills/catalog.json)
（loader `omg_cli/skills_catalog.py`；`omg skill list|show|resolve|resources` 或
`omg capabilities` 的 `skills_catalog`）。
[`docs/parity/projections/antigravity/skills/`](./parity/projections/antigravity/skills/)
下的 Antigravity `SKILL.md` **只是投影**。Grok 現有 45 個 in-session playbook（**不是** live-verified）。

機器可讀外掛 agent 目錄：[`agents/catalog.json`](../agents/catalog.json)
（loader `omg_cli/agents_catalog.py`；用 `omg capabilities` 的 `agents_catalog` 檢視）。
`scripts/generate_agents_catalog.py --check` 也會檢查可安裝的 `agents/*.md`
內 Agy `tools:` 是否漂移。工具清單由同一份目錄權限推導：唯讀角色只有讀取／搜尋工具，
讀寫角色才有編輯與 `run_command`，且只有讀寫視覺實作者有 `generate_image`。
agent frontmatter 若省略、使用 snake_case 別名（`capability_mode` / `permission_mode`），或與目錄的 `capabilityMode` / `permissionMode` 不一致則 fail-closed。Agent markdown 以 `O_NOFOLLOW|O_NONBLOCK`（POSIX）或 Windows `CreateFileW` / `NtCreateFile` `FILE_FLAG_OPEN_REPARSE_POINT` 開啟並從 pinned handle 讀取。
[`docs/parity/projections/antigravity/agents/`](./parity/projections/antigravity/agents/)
下的 Antigravity `agent.md` **只是投影** — 不是已安裝的 AG 插件，也不是 live AG 證據；
可安裝的 dual-host 定義是根目錄的 `agents/*.md`。
團隊路由地板仍在 `omg_cli/team/roles.py`。Dual-host model policy（#131）透過
`agents/model_policies.json` 與 `omg agents list|explain` 消費此目錄
（Grok baseline 已出貨；可選 Medley inspect 經 `--host-inspect` / `OMG_MEDLEY_INSPECT`；inspect 缺席時 `attempt` 為 null 且不嘗試 Medley #18 fallback；live spawn/TUI 仍為 Refs）。
Grok 內建（`explore`、`plan`、`general-purpose`）是政策 profiles，不是第二份 registry。

---

## Skill ↔ CLI 對照

| Skill | 主要 CLI | 會設 `verified`？ |
|-------|----------|-------------------|
| omg-using | doctor / setup / resume | 否 |
| omg-autopilot | `autopilot *` + accept/complete | 僅經 complete/accept |
| omg-ultrawork | `ulw` / worker / integrate | 否（要 accept） |
| omg-team | `team` / launch / status / stop / api | 否（要 accept） |
| omg-ralph | `ralph` | 經外層 accept |
| omg-ralplan | `ralplan` | 否 |
| omg-deep-interview | `interview *` | 否 |
| omg-ultragoal | `goal *` | linked run accept + `goal verify` |
| omg-ultraqa | `qa *` | **永不** |
| omg-dual-review | `dual-review` / `review` | **永不** |
| omg-pipeline | `pipeline` | 最終 accept 階段 |
| omg-ask | `ask` | 否 |
| omg-cancel | `cancel` | 否 |
| omg-wiki / hud / lsp | wiki / hud / lsp | 否 |
| omg-skill | `skill list\|show\|resolve\|resources` | **永不** |
| omg-visual-verdict / visual-ralph | `visual compare\|capture\|verdict\|ralph\|overlay` | **永不** |
| omg-hyperplan / security-research | `team hyperplan` / `team security-research` | **永不** |
| omg-mcp-setup | `mcp-install` | **永不** |
| omg-configure-notifications | `notify *` | **永不** |
| omg-writer-memory | `memory *` / `note` | **永不** |
| *（MCP 表面）* | `mcp-server` / `mcp-install` | **永不**（結構性拒絕） |

---

## 相關文件

- [README.zh-TW.md](./readme/README.zh-TW.md) — 安裝與中文入門  
- [README.md](../README.md) — 英文主 README  
- [autopilot.zh-TW.md](./autopilot.zh-TW.md) — Autopilot 深講  
- [security-model.md](./security-model.md) — 隔離誠實說明（英文）  
- [research/](./research/) — 研究紀錄（非日常產品文件）  
