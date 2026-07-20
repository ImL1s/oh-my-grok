# oh-my-grok (OMG)

English: [README.md](./README.md) · 繁中 skills：[docs/skills.zh-Hant.md](./docs/skills.zh-Hant.md)

<p align="center">
  <img src="assets/omg-character.png" alt="oh-my-grok character" width="300">
  <br>
  <em>先把 Grok 拉起來 — 再交給 OMG 管流程、證據與 verified 完成。</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-%3E%3D3.11-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/host-Grok%20Build-black" alt="Grok Build">
  <img src="https://img.shields.io/badge/docs-繁體中文-informational" alt="zh-Hant docs">
</p>

**[Grok Build](https://github.com/xai-org/grok-build) 的多 agent 編排。**  
與 [oh-my-claudecode](https://github.com/Yeachan-Heo/oh-my-claudecode)（OMC）等為同一類 *orchestration 想法*，執行面是 **Grok-native**。

_非官方社群 plugin — 與 xAI / OMC 維護者無關。_

_不必背完整 Grok flag。用 `omg` + skills：釐清 → 計畫 → 執行 → 驗證。_

**文件：** [Skills 目錄](docs/skills.zh-Hant.md) · [Autopilot](docs/autopilot.zh-Hant.md) · [文件索引](docs/README.zh-Hant.md) · [安全模型（英）](docs/security-model.md) · [Changelog（英）](CHANGELOG.md)

---

## 心智模型

OMG **不取代** Grok Build。

| 層 | 職責 |
|----|------|
| **Grok** | Agent 工作（`spawn_subagent`、工具、session） |
| **Plugin skills / agents** | Playbook 與角色提示 |
| **`omg` CLI** | Run 狀態、證據章、acceptance、integrate、`verified` |
| **`.omg/`** | 計畫、產物、run 狀態（**只有 CLI** 可寫 `passes` / `verified`） |

Workers 只經 Grok **`spawn_subagent`**（depth 1）。  
**沒有** OMC 式 Stop hard-pin（chat 不會被強制釘住）。中斷就說 **繼續** 或再呼叫 skill。  
**範圍誠實：** core purpose 編排對等子集 — 不是完整 OMC skill zoo / tmux multi-CLI team。

版本：**0.3.2** · License: MIT

---

## 快速安裝

**需求：** [Grok Build CLI](https://github.com/xai-org/grok-build)（`grok` 在 PATH）· Python **3.11+**

OMG 有 **兩個表面**：Grok **plugin**（skills/agents/hooks）+ **`omg` CLI**（狀態、accept、verified）。完整產品兩個都要。

### 完整安裝（推薦）

```bash
# 0) 安裝 Grok CLI
curl -fsSL https://x.ai/cli/install.sh | bash

# 1) 穩定路徑 clone
git clone https://github.com/ImL1s/oh-my-grok.git ~/.local/share/oh-my-grok
cd ~/.local/share/oh-my-grok
./scripts/install-plugin.sh
# 可選 pin：git checkout v0.3.2

# 2) omg 到 PATH
ln -sf "$(pwd)/bin/omg" ~/.local/bin/omg
omg --version

# 3) 專案初始化
cd /path/to/your-project
omg setup
omg doctor
```

### 只裝 plugin（半套）

```bash
grok plugin install ImL1s/oh-my-grok@v0.3.2 --trust
```

不會自動把 `omg` 放上 PATH，也不保證 global PreToolUse soft-gate。日常請用完整安裝。

```bash
omg doctor
omg ulw "noop" --dry-run
```

---

## 預設流程

非瑣碎任務建議：

```text
1. omg interview start "…"     # 模糊時釐清  （skill: omg-deep-interview）
2. omg ralplan "…"             # 只做計畫共識 （skill: omg-ralplan）
3. omg ulw / omg ralph / omg autopilot …
4. omg accept --yes            # 唯一可設 verified 的路徑之一
   # 或：omg autopilot complete --run RUN
```

| 需求 | 用 |
|------|-----|
| 平行獨立切片 | `omg ulw` + worker + integrate · skill `omg-ultrawork` |
| 堅持做到 verified | `omg ralph` · skill `omg-ralph` |
| 只要計畫 | `omg ralplan` · skill `omg-ralplan` |
| Session 內全自動 | skill **`omg-autopilot`** + `omg autopilot *` |
| 需求不清 | `omg interview` · skill `omg-deep-interview` |
| 中止 | `omg cancel` · skill `omg-cancel` |

**QA clean ≠ verified。** UltraQA 綠了還要 accept/complete。

**v0.3.2 小提示：**

- Freeze 只允許 pytest / 專案 `.py` / `true`/`false`；`grep`/`omg`/`python -c` 在 **freeze** 就擋。  
- Marker 請加引號：`python3 -m pytest -q -m 'not live'`  
- Clean UltraQA 後 **可省略 prd.json**（accept/complete 會 materialize）  
- 已 `accept` 再 `complete` 會 **short-circuit**（不重跑整輪測試）

---

## Skills（in-session）— 類似 OMC `/skill`

完整 **15 個 skill** 的觸發詞、CLI、範例：  
**→ [docs/skills.zh-Hant.md](docs/skills.zh-Hant.md)** · [英文版](docs/skills.md)

### CLI vs skill

| 表面 | 在哪 | 怎麼呼叫 |
|------|------|----------|
| **終端機 CLI** | shell | `omg setup` · `omg ulw "…"` · `omg accept --yes` |
| **Session skill** | Grok Build 對話 | 自然語言或 `/oh-my-grok:omg-autopilot` |

### 快捷表

| 你說… | Skill | 終端機 CLI |
|-------|--------|------------|
| omg 怎麼用 | `omg-using` | `omg doctor` · `omg resume` |
| autopilot / full auto / 幫我做完 | `omg-autopilot` | `omg autopilot *` |
| ulw / 平行 | `omg-ultrawork` | `omg ulw` + worker + integrate |
| ralph / 不要停 | `omg-ralph` | `omg ralph "…"` |
| ralplan / 計畫共識 | `omg-ralplan` | `omg ralplan "…"` |
| deep interview / 釐清 | `omg-deep-interview` | `omg interview *` |
| ultragoal / 多 story | `omg-ultragoal` | `omg goal *` |
| ultraqa / 修測試 | `omg-ultraqa` | `omg qa *` |
| dual-review | `omg-dual-review` | `omg dual-review` · `omg review` |
| pipeline | `omg-pipeline` | `omg pipeline "…"` |
| ask codex / 第二意見 | `omg-ask` | `omg ask …` |
| cancel | `omg-cancel` | `omg cancel` |
| wiki / hud / lsp | `omg-wiki` · `omg-hud` · `omg-lsp` | `omg wiki` · `hud` · `lsp` |

**多關鍵字優先序：** cancel → ralplan → autopilot → ultragoal → ralph → ulw。

### 常見 skill 鏈

```text
模糊想法     → deep-interview → ralplan → autopilot（或 ralph / ulw）
平行修 bug   → ultrawork → integrate → accept
必須做完     → ralph
對話內 E2E   → autopilot   （中斷就「繼續」）
跨天多 story → ultragoal + 每 story ralph/ulw
寫完後品質   → dual-review → ultraqa → accept / complete
```

### Autopilot 最短流程

```text
你:  autopilot 實作功能 X（含測試）
     # 或 /oh-my-grok:omg-autopilot …
Grok: omg autopilot start "…" → … 各階段 … → omg autopilot complete
你:  （中斷）繼續 · omg autopilot status --run RUN
```

```text
interview → ralplan → implement → review → qa → acceptance → verified
```

深講：[docs/autopilot.zh-Hant.md](docs/autopilot.zh-Hant.md)

---

## HARD RULES

1. 只透過 Grok **`spawn_subagent`** 扇出（depth = 1）。  
2. **不要** 把 `claude` / `codex` / `omc team` / `agy` / `cursor-agent` 當預設 worker（顧問走 `omg ask`，需使用者觸發）。  
3. **只有 `omg` CLI** 可設 `passes` / `verified`。  
4. 取消用 **`omg cancel`** — 禁止會自殺的 `pkill -f`。  

主隔離是 **`capability_mode`**；PreToolUse 是 **fail-open soft-gate**。詳見 [docs/security-model.md](docs/security-model.md)。

---

## 常用 CLI

```text
omg {setup,doctor,state,cancel,resume,wiki,hud,lsp,interview,goal,accept,
     integrate,worker,review,qa,autopilot,ulw,ralph,ralplan,ask,pipeline,
     dual-review} ...
```

```bash
omg setup && omg doctor

omg ralplan "auth 重構共識" --safe
omg ulw "平行修 flaky tests" --dry-run
omg ralph "完成 auth 遷移" --max-iter 5

omg autopilot start "完成功能 X 並含測試"
omg autopilot start "完成功能 X" --skip-interview
omg autopilot status --run RUN
omg autopilot complete --run RUN

omg accept --yes
omg state --human
omg cancel
```

更多 CLI 細節與 flags 見英文 [README.md](./README.md#commands)。

---

## 開發與測試（貢獻者）

```bash
cd /path/to/oh-my-grok
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
PYTHONPATH=. python3 -m pytest -q -m "not live"
./scripts/smoke.sh
```

---

## License

[MIT](LICENSE) · Copyright (c) 2026 ImL1s

[CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CHANGELOG.md](CHANGELOG.md)
