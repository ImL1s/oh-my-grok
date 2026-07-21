# Skills 目錄（oh-my-grok）

English: [`skills.md`](./skills.md)

**15 個 in-session skills**，路徑：[`skills/omg-*/SKILL.md`](../skills/)。  
概念類似 OMC skill zoo，執行面是 **Grok-native**：playbook + `omg` CLI 蓋章。

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
5. **沒有** OMC 式 Stop hard-pin — 對話中斷就再呼叫 skill 或說 **繼續 / continue**。

---

## In-session 快捷表（OMC 風格）

| 觸發詞 / 說法 | Skill | 終端機 CLI | 做什麼 |
|---------------|--------|------------|--------|
| omg 怎麼用、第一次 | `omg-using` | `omg doctor` · `omg setup` · `omg resume` | 路由 + 健康檢查 |
| autopilot、full auto、幫我做完 | `omg-autopilot` | `omg autopilot *` | interview→…→verified |
| ulw、ultrawork、平行 | `omg-ultrawork` | `omg ulw` + worker + integrate | 平行 fan-out |
| ralph、不要停、做到完 | `omg-ralph` | `omg ralph` | 單 story 外層迴圈 |
| ralplan、plan 共識 | `omg-ralplan` | `omg ralplan` | 計畫→critic→verifier（不寫碼） |
| deep interview、釐清需求 | `omg-deep-interview` | `omg interview *` | 需求閘門 |
| ultragoal、多 story、goal ledger | `omg-ultragoal` | `omg goal *` | 持久 ledger（無 host `/goal`） |
| ultraqa、修測試、重跑 | `omg-ultraqa` | `omg qa *` | freeze→run→repair（**≠ verified**） |
| dual-review、不要 self-approve | `omg-dual-review` | `omg dual-review` · `omg review` | critic→verifier |
| pipeline | `omg-pipeline` | `omg pipeline` | plan→implement→accept FSM |
| ask codex / 第二意見 | `omg-ask` | `omg ask` | 人類觸發的外部顧問 |
| cancel、中止 | `omg-cancel` | `omg cancel` | 安全中止 |
| wiki、專案記憶 | `omg-wiki` | `omg wiki *` | 本地 markdown wiki |
| hud、statusline | `omg-hud` | `omg hud` | 一行狀態 |
| lsp、symbols | `omg-lsp` | `omg lsp *` | 誠實本地 probe（非完整 LSP MCP） |

**多關鍵字同時出現時的優先序**（見 `omg-using`）：  
`cancel` > `ralplan` > `autopilot` > `ultragoal` > `ralph` > `ulw`。

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
| **CLI** | `omg autopilot start\|transition\|status\|complete` |
| **深講** | [`autopilot.zh-Hant.md`](./autopilot.zh-Hant.md) · [EN](./autopilot.md) |
| **SKILL** | [`skills/omg-autopilot/SKILL.md`](../skills/omg-autopilot/SKILL.md) |

```bash
omg autopilot start "完成功能 X 並含測試"
# 或：omg autopilot start "…" --skip-interview
omg autopilot status --run RUN
omg autopilot complete --run RUN
```

階段：`interview → ralplan → implement → review → (rework) → qa → acceptance → verified`  
無 Stop pin — 對話中斷請說 **繼續**。

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
| **CLI** | `omg goal init\|status\|link-run\|start-story\|checkpoint\|block-story\|resume-story\|complete-story\|verify\|repair` |
| **SKILL** | [`skills/omg-ultragoal/SKILL.md`](../skills/omg-ultragoal/SKILL.md) |

Grok **沒有** host `/goal` — ledger 只在 `.omg/ultragoal/`。  
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
| **CLI** | `omg ask codex\|claude\|gemini "…"` |
| **SKILL** | [`skills/omg-ask/SKILL.md`](../skills/omg-ask/SKILL.md) |

```bash
omg ask codex "review this patch"
omg ask claude "對這份 plan 的第二意見"
```

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

### `omg-lsp` — 語言 probe（誠實）

| | |
|--|--|
| **何時** | symbols / check；**不是** 完整 LSP MCP |
| **呼叫** | `lsp` · `/oh-my-grok:omg-lsp` |
| **CLI** | `omg lsp status` · `omg lsp check path.py` |
| **SKILL** | [`skills/omg-lsp/SKILL.md`](../skills/omg-lsp/SKILL.md) |

優先用 Grok `read_file` / `grep`。本機有 pyright 才有 check。

---

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

團隊路由用的 posture / class 地板在 `omg_cli/team/roles.py`
（`role_posture`、`role_class`、`is_reviewer_or_verifier`）。
Grok 內建（`explore`、`plan`、`general-purpose`）仍補臨時缺口。

---

## Skill ↔ CLI 對照

| Skill | 主要 CLI | 會設 `verified`？ |
|-------|----------|-------------------|
| omg-using | doctor / setup / resume | 否 |
| omg-autopilot | `autopilot *` + accept/complete | 僅經 complete/accept |
| omg-ultrawork | `ulw` / worker / integrate | 否（要 accept） |
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

---

## 相關文件

- [README.zh-TW.md](../README.zh-TW.md) — 安裝與中文入門  
- [README.md](../README.md) — 英文主 README  
- [autopilot.zh-Hant.md](./autopilot.zh-Hant.md) — Autopilot 深講  
- [security-model.md](./security-model.md) — 隔離誠實說明（英文）  
- [research/](./research/) — 研究紀錄（非日常產品文件）  
