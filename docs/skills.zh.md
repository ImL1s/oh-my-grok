# Skills 目录（oh-my-grok）

English | [简体中文](./skills.zh.md) | [繁體中文](./skills.zh-TW.md)

English: [`skills.md`](./skills.md)

**15 个 in-session skills**，路径：[`skills/omg-*/SKILL.md`](../skills/)。  
概念类似 OMC skill zoo，执行面是 **Grok-native**：playbook + `omg` CLI 盖章。

> **两种表面（类似 OMC 的 CLI vs `/skill`）**  
> - **终端机 CLI：** shell 里跑 `omg …`（状态、accept、modes）。  
> - **Session skill：** 安装 plugin 后，在 Grok Build 对话里用自然语言或 `/oh-my-grok:<skill>`。  
> OMG 差异：很多流程**同时**有 skill playbook **与** 真实 CLI 子命令（`omg autopilot`、`omg ralph`…）。

---

## 如何呼叫 skill

| 方式 | 范例 |
|------|------|
| 自然语言（推荐） | `autopilot 完成登入重构` · `ulw 修好这三个 package` · `ralph 做到完` |
| Skill id（Grok plugin） | `/oh-my-grok:omg-autopilot` · `/oh-my-grok:omg-ultrawork` |
| 只在终端机 | `omg ralph "…"` / `omg ulw "…"`（不必进 chat skill） |

**路由：** 不确定用哪个 → 载入 **`omg-using`**（或问“omg 怎么用”）。

**所有 skill 的 HARD RULES：**

1. 只透过 Grok `spawn_subagent` 扇出（depth 1）。
2. 一律设 `capability_mode`（实作 `read-write` / 审查 `read-only`）。
3. 只有 **`omg` CLI** 可以写 `.omg/state/` 下的 `verified` / `passes`。
4. 中止用 `omg cancel` — 禁止会自我匹配的 `pkill -f`。
5. **没有** OMC 式 Stop hard-pin — 对话中断就再呼叫 skill 或说 **继续 / continue**。

---

## In-session 快捷表（OMC 风格）

| 触发词 / 说法 | Skill | 终端机 CLI | 做什么 |
|---------------|--------|------------|--------|
| omg 怎么用、第一次 | `omg-using` | `omg doctor` · `omg setup` · `omg resume` | 路由 + 健康检查 |
| autopilot、full auto、帮我做完 | `omg-autopilot` | `omg autopilot *` | interview→…→verified |
| ulw、ultrawork、平行 | `omg-ultrawork` | `omg ulw` + worker + integrate | 平行 fan-out |
| ralph、不要停、做到完 | `omg-ralph` | `omg ralph` | 单 story 外层循环 |
| ralplan、plan 共识 | `omg-ralplan` | `omg ralplan` | 计划→critic→verifier（不写码） |
| deep interview、厘清需求 | `omg-deep-interview` | `omg interview *` | 需求闸门 |
| ultragoal、多 story、goal ledger | `omg-ultragoal` | `omg goal *` | 持久 ledger（无 host `/goal`） |
| ultraqa、修测试、重跑 | `omg-ultraqa` | `omg qa *` | freeze→run→repair（**≠ verified**） |
| dual-review、不要 self-approve | `omg-dual-review` | `omg dual-review` · `omg review` | critic→verifier |
| pipeline | `omg-pipeline` | `omg pipeline` | plan→implement→accept FSM |
| ask codex / 第二意见 | `omg-ask` | `omg ask` | 人类触发的外部顾问 |
| cancel、中止 | `omg-cancel` | `omg cancel` | 安全中止 |
| wiki、专案记忆 | `omg-wiki` | `omg wiki *` | 本地 markdown wiki |
| hud、statusline | `omg-hud` | `omg hud` | 一行状态 |
| lsp、symbols | `omg-lsp` | `omg lsp *` | 检查 host-owned `.lsp.json`；无语意 proxy |

**多关键字同时出现时的优先序**（见 `omg-using`）：  
`cancel` > `ralplan` > `autopilot` > `ultragoal` > `ralph` > `ulw`。

---

## 建议 skill 链

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

（规范 playbook 以各 `SKILL.md` 为准；以下是操作者摘要。）

### `omg-using` — 引导 / 路由

| | |
|--|--|
| **何时** | 第一次用、“哪个 skill？”、中断后 continue |
| **呼叫** | `omg 怎么用` · `/oh-my-grok:omg-using` |
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

### `omg-autopilot` — 完整生命周期

| | |
|--|--|
| **何时** | 厘清→计划→实作→审查→QA→verified |
| **呼叫** | `autopilot …` · `full auto` · `/oh-my-grok:omg-autopilot` |
| **CLI** | `omg autopilot start\|transition\|status\|complete` |
| **深讲** | [`autopilot.zh.md`](./autopilot.zh.md) · [EN](./autopilot.md) |
| **SKILL** | [`skills/omg-autopilot/SKILL.md`](../skills/omg-autopilot/SKILL.md) |

```bash
omg autopilot start "完成功能 X 並含測試"
# 或：omg autopilot start "…" --skip-interview
omg autopilot status --run RUN
omg autopilot complete --run RUN
```

阶段：`interview → ralplan → implement → review → (rework) → qa → acceptance → verified`  
无 Stop pin — 对话中断请说 **继续**。

---

### `omg-ultrawork` — 平行执行

| | |
|--|--|
| **何时** | 独立切片、平行 agent |
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

### `omg team` — 实验性 tmux team plane（D1 零设定 + D3 multi-CLI + D2 分阶段 driver + D4 scale/resume/ralph）

| | |
|--|--|
| **何时** | 选择性多 pane ULW + 真实 worktree；测试用 hermetic dry-run |
| **闸门** | `OMG_EXPERIMENTAL_TMUX_TEAM=1`（未设则拒绝） |
| **CLI** | `omg team start\|run\|scale\|resume\|status\|collect\|stop` |
| **诚实范围** | 零设定 = grok panes；`--routing` 启 multi-CLI（含角色地板）。**整合**隔离（ownership + seal + integrate）— **不是**执行沙箱。`collect` / `run` / `scale` / `resume` 永不写 `verified`。scale/resume/ralph 是**同一** team plane 的生命周期延伸（无新隔离宣称）。 |

**`omg team run`** 是 team plane 上的**分阶段 DRIVER**（不是新的 planner/verifier）：

`team-plan → team-prd → team-exec → team-verify → team-fix`（终态：`complete` / `failed` / `blocked`）。

- **team-plan / team-prd** — 穿透标记；任务拆解属 **leader / ralplan**，`run` 只吃 `--tasks-json` 或 `--tasks-path`。
- **team-exec** — `start_team` 再 `collect_team`（dry-run 只 start，不碰 tmux/subprocess）。
- **team-verify** — 以 POST-A2 `parse_verdict_file` 闸 `stages/team-verifier.md|json`；APPROVE → `complete`，否则 → `team-fix`。**不**代写 verdict。
- **team-fix** — `--max-fix`（预设 3）上限；超限 → `failed`。
- **`--ralph [--max-iter N]`**（D4）— 外层**有界**持久循环（预设 max_iter=3）；`team.json` 记 `linked_ralph`、`stages/team-ralph.json` 记 `linked_team`；仍只靠真实 team-verify APPROVE 进 complete，**永不**写 `verified`。
- 进 exec/fix 会作废旧 verify 戳记；`verified` 仍只经 `omg accept`。

**生命周期（D4）：**

- **`omg team scale --run ID --add N|--remove N [--dry-run]`** — 动态加/减 pane（run 目录 scale lock；`--add` 受 `max_workers_cap()` 与单调 window index 限制；`--remove` 优雅排空，只杀记录的 pgid + window，**不**杀 session、**禁止** `pkill -f`，标记 `scaled_down` 并保留 worktree；active 不可低于 1）。
- **`omg team resume --run ID`** — leader 重启后重读 `team.json`、对账 pane 存活；只做幂等 status 写入。

```bash
export OMG_EXPERIMENTAL_TMUX_TEAM=1
omg team start --goal "平行修 A/B" --tasks-json '[{"task_id":"t1","owned_files":["a.py"]},{"task_id":"t2","owned_files":["b.py"]}]' --dry-run
omg team run --goal "x" --tasks-json '[{"task_id":"t1","owned_files":["a.py"]}]' --dry-run --max-fix 3
omg team run --goal "x" --tasks-json '[{"task_id":"t1","owned_files":["a.py"]}]' --ralph --max-iter 2 --dry-run
omg team scale --run RUN --add 2 --dry-run
omg team resume --run RUN
omg team status --run RUN --json
omg team collect --run RUN   # seal_all_tasks + integrate；永不 verified
omg team stop --run RUN      # 只殺記錄的 session + pgid（禁止 pkill -f）
```

---

### `omg-ralph` — 持久循环（单 story）

| | |
|--|--|
| **何时** | 不要停到 verified；多轮同一目标 |
| **呼叫** | `ralph` · `做到完` · `/oh-my-grok:omg-ralph` |
| **CLI** | `omg ralph "goal"`（`--max-iter N`） |
| **SKILL** | [`skills/omg-ralph/SKILL.md`](../skills/omg-ralph/SKILL.md) |

```bash
omg ralph "完成 auth 遷移" --max-iter 5
```

Skill = **单次 iteration** playbook；**CLI 外层** 拥有 max-iter 与重启。

---

### `omg-ralplan` — 计划共识（不写产品码）

| | |
|--|--|
| **何时** | 写码前先对齐计划 |
| **呼叫** | `ralplan` · `plan 共识` · `/oh-my-grok:omg-ralplan` |
| **CLI** | `omg ralplan "…"` |
| **SKILL** | [`skills/omg-ralplan/SKILL.md`](../skills/omg-ralplan/SKILL.md) |

```bash
omg ralplan "auth 重構共識計畫" --safe
# FSM: draft → critic → revise → verifier → APPROVE
# 之後：omg ulw / omg ralph / omg autopilot
```

---

### `omg-deep-interview` — 需求闸门

| | |
|--|--|
| **何时** | 目标模糊、范围不清 |
| **呼叫** | `deep interview` · `厘清需求` · `/oh-my-grok:omg-deep-interview` |
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
| **何时** | 多个持久 story、depends_on、跨 session |
| **呼叫** | `ultragoal` · `goal ledger` · `/oh-my-grok:omg-ultragoal` |
| **CLI** | `omg goal init\|status\|link-run\|start-story\|checkpoint\|block-story\|resume-story\|complete-story\|verify\|repair` |
| **SKILL** | [`skills/omg-ultragoal/SKILL.md`](../skills/omg-ultragoal/SKILL.md) |

Grok **没有** host `/goal` — ledger 只在 `.omg/ultragoal/`。  
`omg goal verify` 需要已透过 accept/complete **verified** 的 linked run。

---

### `omg-ultraqa` — QA 修复循环

| | |
|--|--|
| **何时** | 对抗式 QA、重测到绿、review 之后 |
| **呼叫** | `ultraqa` · `修测试` · `/oh-my-grok:omg-ultraqa` |
| **CLI** | `omg qa freeze\|run\|status` |
| **SKILL** | [`skills/omg-ultraqa/SKILL.md`](../skills/omg-ultraqa/SKILL.md) |

```bash
omg qa freeze --run RUN --scenarios-json \
  '[{"id":"unit","command":"python3 -m pytest -q -m '"'"'not live'"'"'"}]'
omg qa run --run RUN
omg qa status --run RUN
```

**QA clean ≠ verified。** 接着 `omg accept` 或 `omg autopilot complete`。  
Freeze 会拒绝 `grep` / `test` / `omg` / `python -c`（v0.3.2+ 有 tip）。

---

### `omg-dual-review` — critic → verifier

| | |
|--|--|
| **何时** | 不要 self-approve；独立审查 |
| **呼叫** | `dual-review` · `/oh-my-grok:omg-dual-review` |
| **CLI** | `omg dual-review "…"` · `omg review --run RUN …` |
| **SKILL** | [`skills/omg-dual-review/SKILL.md`](../skills/omg-dual-review/SKILL.md) |

**不会** 设 `verified`。CLI 路径为序列 headless Grok（相对原生平行 dual-review 为永久 PARTIAL）。

---

### `omg-pipeline` — 脚本化 plan→accept

| | |
|--|--|
| **何时** | CLI 组合流程、不必完整 autopilot skill |
| **呼叫** | `pipeline` · `/oh-my-grok:omg-pipeline` |
| **CLI** | `omg pipeline "goal"` |
| **SKILL** | [`skills/omg-pipeline/SKILL.md`](../skills/omg-pipeline/SKILL.md) |

```bash
omg pipeline "goal"
omg pipeline "goal" --plan-only
omg pipeline "goal" --skip-plan --implement ulw
```

人在循环、多阶段对话 → 优先 **`omg-autopilot`**。

---

### `omg-ask` — 外部顾问（仅人类触发）

| | |
|--|--|
| **何时** | Codex / Claude / Gemini 第二意见 |
| **呼叫** | `ask codex …` · `/oh-my-grok:omg-ask` |
| **CLI** | `omg ask codex\|claude\|gemini "…"` |
| **SKILL** | [`skills/omg-ask/SKILL.md`](../skills/omg-ask/SKILL.md) |

```bash
omg ask codex "review this patch"
omg ask claude "對這份 plan 的第二意見"
```

**不是** 预设产品 worker。使用者没要求时 agent 不应自行 shell 顾问 CLI。

---

### `omg-cancel` — 中止

| | |
|--|--|
| **何时** | 卡住、目标错了、杀 worker |
| **呼叫** | `cancel` · `stop omg` · `/oh-my-grok:omg-cancel` |
| **CLI** | `omg cancel` · `omg cancel --run ID` |
| **SKILL** | [`skills/omg-cancel/SKILL.md`](../skills/omg-cancel/SKILL.md) |

```bash
omg state
omg cancel
omg cancel --run 20260720T…-…
```

---

### `omg-wiki` — 本地知识库

| | |
|--|--|
| **何时** | 记录决策、搜寻旧笔记 |
| **呼叫** | `wiki` · `/oh-my-grok:omg-wiki` |
| **CLI** | `omg wiki list\|ingest\|query` |
| **SKILL** | [`skills/omg-wiki/SKILL.md`](../skills/omg-wiki/SKILL.md) |

```bash
omg wiki list
omg wiki ingest --title "Auth 決策" --text "…" --tags "arch"
omg wiki query "auth"
```

不是 run / `verified` 权威来源。

---

### `omg-hud` — 状态列

| | |
|--|--|
| **何时** | 一行 mode\|status\|stage |
| **呼叫** | `hud` · `/oh-my-grok:omg-hud` |
| **CLI** | `omg hud` · `omg hud --run RUN` · `omg hud --json` |
| **SKILL** | [`skills/omg-hud/SKILL.md`](../skills/omg-hud/SKILL.md) |

---

### `omg-lsp` — host-owned LSP 注册

| | |
|--|--|
| **何时** | 检查公开 `.lsp.json` 注册与本机 server command 是否可用 |
| **呼叫** | `lsp` · `/oh-my-grok:omg-lsp` |
| **CLI** | `omg lsp status` · `omg lsp check path.py` · `omg lsp symbols path.py` · `omg lsp diagnostics path.py` |
| **SKILL** | [`skills/omg-lsp/SKILL.md`](../skills/omg-lsp/SKILL.md) |

`omg lsp status` 只验证 host-owned 注册，不会启动 server。它会回报
`semantic_proxy_count: 0`；configured 但未由 host 观测，不代表 healthy。
`check`、`symbols`、`diagnostics` 会回传 `semantic_proxy_unsupported` 并以
exit code 1 结束。语意语言操作请使用 Grok host tools；repository 查找则用
`read_file` / `grep`。

---

### 会话内 MCP（`omg mcp-server`）— 聚焦 ops 表面

**聚焦**的会话内 read + proposal MCP 表面，**不是** OMC ~54-tool 对等。
只暴露读取与非权威 proposal 写入；`passes` / `verified` / accept **永远不是**
MCP tool（仅 CLI，且在 `OMG_MCP_SERVER=1` 时**结构性拒绝**）；语意 LSP
操作不会注册；没有 code-exec / 状态突变 / 权威写入工具。
这是 in-session **workflow** 能力对齐，不是 tool 数量对齐。

```bash
grok mcp add omg omg -- mcp-server
omg mcp-install --print-only
omg mcp-server                 # stdio JSON-RPC（會設 OMG_MCP_SERVER=1）
```

| Tool | 类型 | 后端 |
|------|------|------|
| `omg_state_status` | 读 | `hud.hud_pack` |
| `omg_state_read` / `omg_state_list_active` | 读 | state load |
| `omg_note_read` / `omg_note_write` | 读 / proposal | `.omg/notepad.md` |
| `omg_wiki_*` | 读 / proposal | `.omg/wiki/` |
| `omg_project_memory_*` | 读 / proposal | `.omg/project-memory.json` |
| `omg_artifact_write` | 仅 proposal | `.omg/artifacts/` |
| `omg_resume_context` | 读 | resume pack + RESUME.md |

**三道安全机制：** (1) 策展 allowlist；(2) `OMG_MCP_SERVER=1` 时
`set_verified` / `register_cli_acceptance_token` 直接 raise；(3) 写入路径
禁闭（拒 `.omg/state/**` 与 traversal）。

**刻意排除（OMC 有、OMG 没有）：** `state_write`、`state_clear`、`python_repl`、
`ast_grep_replace`、所有语意 LSP 操作（包括 goto/hover/rename/
find_references/symbols/diagnostics）、
`shared_memory`、`session_search`、`merge_readiness`，以及任何 accept/verify 工具。

---

### 产品服务与 repository workflows（0.6.0）

这些是 CLI contract，不是新增 chat skill。Skill 可以呼叫它们，但权威状态与
证据仍由 CLI artifact 管理。

| 指令 | Contract |
|---|---|
| `omg session allocate\|route` | 精确 create/resume/continue/fork argv；child UUID 不可重用。 |
| `omg recover` | 不可变、受限 JSONL suffix；部分恢复保留 broken-chain/未知纪录警告。 |
| `omg memory put\|search\|show\|export\|import\|rescan` | Redacted、确定性的专案 facts。 |
| `omg tracker status\|project\|reconcile` | Passive、generation-fenced lifecycle projection。 |
| `omg compact create\|show\|render` | Lossless guidance checkpoint / restore。 |
| `omg notify status\|send\|process` | 只出站、非权威 delivery queue。 |
| `omg workflow install\|list\|show\|plan\|run` | 不可变 registry、确定 waves、receipt-bound ship gate。 |
| `omg parity run\|release-readback` | 委派 frozen W0 manifest engine，并验 exact bundle。 |
| `omg capabilities` / `omg native-status` | 分开的 capability tiers；不探测私有 sidecar。 |

Workflow plan 不会启动外部 CLI。Leader 应使用 Grok 原生 `spawn_subagent`、传入
精确 `capability_mode`，再把绑定 task ID 的 receipts 交给 `omg workflow run`。
详见 [workflows.zh.md](./workflows.zh.md)。

## Agents（skills 会用到的角色）

| Agent | 典型 `capability_mode` | 角色 |
|-------|------------------------|------|
| `omg-orchestrator` | leader | 拆解与协调 |
| `omg-executor` | `read-write`（无 shell） | 实作 |
| `omg-debugger` | `read-write`（无 shell） | 根因 / 回归 / build 修复 |
| `omg-designer` | `read-write`（无 shell） | UI/UX 实作 |
| `omg-writer` | `read-write`（无 shell） | README / API 文件 / 注解 |
| `omg-test-engineer` | `read-write`（无 shell） | 测试策略 / 覆盖 / flaky 加固 |
| `omg-critic` / `omg-verifier` | `read-only` | 挑战 / 证据 |
| `omg-code-reviewer` / `omg-architect` | `read-only` | 结构化审查 |
| `omg-security-reviewer` | `read-only` | OWASP / secrets / 不安全模式 |
| `omg-qa-tester` / `omg-analyst` | 见 taxonomy | QA 情境 / interview 分析 |

团队路由用的 posture / class 地板在 `omg_cli/team/roles.py`
（`role_posture`、`role_class`、`is_reviewer_or_verifier`）。
Grok 内建（`explore`、`plan`、`general-purpose`）仍补临时缺口。

---

## Skill ↔ CLI 对照

| Skill | 主要 CLI | 会设 `verified`？ |
|-------|----------|-------------------|
| omg-using | doctor / setup / resume | 否 |
| omg-autopilot | `autopilot *` + accept/complete | 仅经 complete/accept |
| omg-ultrawork | `ulw` / worker / integrate | 否（要 accept） |
| omg-ralph | `ralph` | 经外层 accept |
| omg-ralplan | `ralplan` | 否 |
| omg-deep-interview | `interview *` | 否 |
| omg-ultragoal | `goal *` | linked run accept + `goal verify` |
| omg-ultraqa | `qa *` | **永不** |
| omg-dual-review | `dual-review` / `review` | **永不** |
| omg-pipeline | `pipeline` | 最终 accept 阶段 |
| omg-ask | `ask` | 否 |
| omg-cancel | `cancel` | 否 |
| omg-wiki / hud / lsp | wiki / hud / lsp | 否 |
| *（MCP 表面）* | `mcp-server` / `mcp-install` | **永不**（结构性拒绝） |

---

## 相关文件

- [README.zh.md](./readme/README.zh.md) — 安装与中文入门  
- [README.md](../README.md) — 英文主 README  
- [autopilot.zh.md](./autopilot.zh.md) — Autopilot 深讲  
- [security-model.md](./security-model.md) — 隔离诚实说明（英文）  
- [research/](./research/) — 研究纪录（非日常产品文件）  
