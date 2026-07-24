# oh-my-grok (OMG)

English: [README.md](../../README.md) · [简体中文](./README.zh.md) · [繁體中文](./README.zh-TW.md)

<p align="center">
  <img src="../../assets/omg-character.png" alt="oh-my-grok character" width="300">
  <br>
  <em>先把 Grok 拉起来 — 再交给 OMG 管流程、证据与 verified 完成。</em>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green.svg" alt="License: MIT"></a>
  <img src="https://img.shields.io/badge/python-%3E%3D3.11-blue" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/host-Grok%20Build-black" alt="Grok Build">
  <img src="https://img.shields.io/badge/docs-zh-TW-informational" alt="zh-TW docs">
</p>

**[Grok Build](https://github.com/xai-org/grok-build) 的多 agent 编排。**  
与 [oh-my-claudecode](https://github.com/Yeachan-Heo/oh-my-claudecode)（OMC）等为同一类 *orchestration 想法*，执行面是 **Grok-native**。

_非官方社群 plugin — 与 xAI / OMC 维护者无关。_

_不必背完整 Grok flag。用 `omg` + skills：厘清 → 计划 → 执行 → 验证。_

**文件：** [Skills 目录](../skills.zh.md) · [Autopilot](../autopilot.zh.md) · [文档索引](../README.zh.md) · [安全模型](../security-model.zh.md) · [Changelog](../../CHANGELOG.md)

---

## 心智模型

OMG **不取代** Grok Build。

| 层 | 职责 |
|----|------|
| **Grok** | Agent 工作（`spawn_subagent`、工具、session） |
| **Plugin skills / agents** | Playbook 与角色提示 |
| **`omg` CLI** | Run 状态、证据章、acceptance、integrate、`verified` |
| **`.omg/`** | 计划、产物、run 状态（**只有 CLI** 可写 `passes` / `verified`） |

Workers 只经 Grok **`spawn_subagent`**（depth 1）。  
**没有** OMC 式 Stop hard-pin（chat 不会被强制钉住）。中断就说 **继续** 或再呼叫 skill。  
**tmux team：** 已有实验性 multi-CLI team plane（需设定 `OMG_EXPERIMENTAL_TMUX_TEAM=1`）；它只提供 worktree／seal／integrate 的**整合隔离**，不是执行 sandbox。
**范围诚实：** core purpose 编排对等子集 — 仍不是完整 OMC skill zoo，也不宣称各 provider 有一致的执行 sandbox；详见 [`docs/security-model.md`](../security-model.md)。

版本：**0.6.0** · License: MIT

---

## 快速安装

**需求：** [Grok Build CLI](https://github.com/xai-org/grok-build)（`grok` 在 PATH）· Python **3.11+**

OMG 有 **两个表面**：Grok **plugin**（skills/agents/hooks）+ **`omg` CLI**（状态、accept、verified）。完整产品两个都要。

### 方便的完整安装（推荐）

```bash
# 0) 安裝 Grok CLI
curl -fsSL https://x.ai/cli/install.sh | bash

# 1) 從 GitHub latest release 安裝完整產品
# installer 只會從解析出的同一個 immutable tag 下載 archive + SHA256SUMS
curl -fsSL https://raw.githubusercontent.com/ImL1s/oh-my-grok/main/scripts/install.sh | bash
omg --version

# 2) 專案初始化
cd /path/to/your-project
omg setup
omg doctor --strict
```

### 手动 pin GitHub 版本

```bash
TAG=v0.6.0
curl -fLO "https://github.com/ImL1s/oh-my-grok/releases/download/${TAG}/oh-my-grok-0.6.0.tar.gz"
curl -fLO "https://github.com/ImL1s/oh-my-grok/releases/download/${TAG}/SHA256SUMS"
shasum -a 256 -c SHA256SUMS
curl -fsSLo install.sh "https://raw.githubusercontent.com/ImL1s/oh-my-grok/${TAG}/scripts/install.sh"
bash install.sh --offline --archive ./oh-my-grok-0.6.0.tar.gz \
  --checksums ./SHA256SUMS --source-tag "${TAG}"
omg doctor --strict
```

方便路径会先解析一次 GitHub `latest`，验证 semantic tag，再从该 tag 下载两个资产；切换 plugin / CLI、strict doctor、receipt、失败 rollback 都在同一 transaction。Contributor 仍可 clone 固定 tag 后执行 `./scripts/install-plugin.sh`。

### 只装 plugin（半套）

```bash
grok plugin install ImL1s/oh-my-grok@v0.6.0 --trust
```

不会自动把 `omg` 放上 PATH，也不保证 global PreToolUse soft-gate。日常请用完整安装。

```bash
omg doctor
omg ulw "noop" --dry-run
```

---

## 预设流程

非琐碎任务建议：

```text
1. omg interview start "…"     # 模糊時釐清  （skill: omg-deep-interview）
2. omg ralplan "…"             # 只做計畫共識 （skill: omg-ralplan）
3. omg ulw / omg ralph / omg autopilot …
4. omg accept --yes            # 唯一可設 verified 的路徑之一
   # 或：omg autopilot complete --run RUN
```

| 需求 | 用 |
|------|-----|
| 平行独立切片 | `omg ulw` + worker + integrate · skill `omg-ultrawork` |
| 坚持做到 verified | `omg ralph` · skill `omg-ralph` |
| 只要计划 | `omg ralplan` · skill `omg-ralplan` |
| Session 内全自动 | skill **`omg-autopilot`** + `omg autopilot *` |
| 需求不清 | `omg interview` · skill `omg-deep-interview` |
| 中止 | `omg cancel` · skill `omg-cancel` |

**QA clean ≠ verified。** UltraQA 绿了还要 accept/complete。

**v0.3.2 小提示：**

- Freeze 只允许 pytest / 专案 `.py` / `true`/`false`；`grep`/`omg`/`python -c` 在 **freeze** 就挡。  
- Marker 请加引号：`python3 -m pytest -q -m 'not live'`  
- Clean UltraQA 后 **可省略 prd.json**（accept/complete 会 materialize）  
- 已 `accept` 再 `complete` 会 **short-circuit**（不重跑整轮测试）

---

## Skills（in-session）— 类似 OMC `/skill`

完整 **15 个 skill** 的触发词、CLI、范例：  
**→ [docs/skills.zh-TW.md](../skills.zh-TW.md)** · [英文版](../skills.md)

### CLI vs skill

| 表面 | 在哪 | 怎么呼叫 |
|------|------|----------|
| **终端机 CLI** | shell | `omg setup` · `omg ulw "…"` · `omg accept --yes` |
| **Session skill** | Grok Build 对话 | 自然语言或 `/oh-my-grok:omg-autopilot` |

### 快捷表

| 你说… | Skill | 终端机 CLI |
|-------|--------|------------|
| omg 怎么用 | `omg-using` | `omg doctor` · `omg resume` |
| autopilot / full auto / 帮我做完 | `omg-autopilot` | `omg autopilot *` |
| ulw / 平行 | `omg-ultrawork` | `omg ulw` + worker + integrate |
| ralph / 不要停 | `omg-ralph` | `omg ralph "…"` |
| ralplan / 计划共识 | `omg-ralplan` | `omg ralplan "…"` |
| deep interview / 厘清 | `omg-deep-interview` | `omg interview *` |
| ultragoal / 多 story | `omg-ultragoal` | `omg goal *` |
| ultraqa / 修测试 | `omg-ultraqa` | `omg qa *` |
| dual-review | `omg-dual-review` | `omg dual-review` · `omg review` |
| pipeline | `omg-pipeline` | `omg pipeline "…"` |
| ask codex / 第二意见 | `omg-ask` | `omg ask …` |
| cancel | `omg-cancel` | `omg cancel` |
| wiki / hud / lsp | `omg-wiki` · `omg-hud` · `omg-lsp` | `omg wiki` · `hud` · `lsp` |

**多关键字优先序：** cancel → ralplan → autopilot → ultragoal → ralph → ulw。

### 常见 skill 链

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

深讲：[docs/autopilot.zh-TW.md](../autopilot.zh-TW.md)

---

## HARD RULES

1. 只透过 Grok **`spawn_subagent`** 扇出（depth = 1）。  
2. **不要** 把 `claude` / `codex` / `omc team` / `agy` / `cursor-agent` 当预设 worker（顾问走 `omg ask`，需使用者触发）。  
3. **只有 `omg` CLI** 可设 `passes` / `verified`。  
4. 取消用 **`omg cancel`** — 禁止会自杀的 `pkill -f`。  

主隔离是 **`capability_mode`**；PreToolUse 是 **fail-open soft-gate**。详见 [docs/security-model.md](../security-model.md)。

---

## 常用 CLI

```text
omg {setup,doctor,state,cancel,resume,wiki,hud,lsp,interview,goal,accept,
     session,recover,memory,tracker,compact,notify,native-status,workflow,
     capabilities,parity,integrate,worker,team,review,qa,autopilot,ulw,ralph,
     ralplan,ask,pipeline,dual-review,mcp-server,mcp-install} ...
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

omg workflow install ./production-safety-review.json
omg workflow plan production-safety-review --input ./input.json
omg native-status
omg capabilities

omg session allocate
omg recover ~/.grok/sessions/example.jsonl
omg memory search architecture

omg accept --yes
omg state --human
omg cancel
```

Repository workflow 的 receipt、权限交集与 ship gate 见 [docs/workflows.zh-TW.md](../workflows.zh-TW.md)。更多 CLI 细节与 flags 见英文 [README.md](../../README.md#commands)。

---

## 开发与测试（贡献者）

```bash
cd /path/to/oh-my-grok
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
PYTHONPATH=. python3 -m pytest -q -m "not live"
./scripts/smoke.sh
```

---

## License

[MIT](../../LICENSE) · Copyright (c) 2026 ImL1s

[CONTRIBUTING.md](CONTRIBUTING.md) · [SECURITY.md](SECURITY.md) · [CHANGELOG.md](../../CHANGELOG.md)
