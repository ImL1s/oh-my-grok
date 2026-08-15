# Skills 目录（oh-my-grok）

English | [简体中文](./skills.zh.md) | [繁體中文](./skills.zh-TW.md)

English: [`skills.md`](./skills.md)

**16 个 in-session skills**，路径：[`skills/omg-*/SKILL.md`](../skills/)。  
概念类似 OMC skill zoo，执行面是 **Grok-native**：playbook + `omg` CLI 盖章。

机器目录（别名、分类、pipeline、续跑策略）：[`skills/catalog.json`](../skills/catalog.json) ·
生成表 [`docs/parity/skills-catalog.md`](./parity/skills-catalog.md)。
检视：`omg skill list|show|resolve|resources`（永不写 `verified`）。
仅目录项已分类。Antigravity 投影**不是**已安装 AG 插件。

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
5. grok **≥0.2.107** 有 Stop pin（每 turn 上限 **8**，fail-open）— 超 cap 或 turn 结束后，再呼叫 skill、`/loop`，或说 **继续 / continue**。

---

## In-session 快捷表（OMC 风格）

| 触发词 / 说法 | Skill | 终端机 CLI | 做什么 |
|---------------|--------|------------|--------|
| omg 怎么用、第一次 | `omg-using` | `omg doctor` · `omg setup` · `omg resume` | 路由 + 健康检查 |
| autopilot、full auto、帮我做完 | `omg-autopilot` | `omg autopilot *` | interview→…→verified |
| ulw、ultrawork、平行 | `omg-ultrawork` | `omg ulw` + worker + integrate | 平行 fan-out |
| team N、tmux team、多 pane | `omg-team` | `omg team …` | 持久 tmux panes — slash **仅** `/oh-my-grok:omg-team`（无裸 `/team`） |
| ralph、不要停、做到完 | `omg-ralph` | `omg ralph` | 单 story 外层循环 |
| ralplan、plan 共识 | `omg-ralplan` | `omg ralplan` | 计划→critic→verifier（不写码） |
| deep interview、厘清需求 | `omg-deep-interview` | `omg interview *` | 需求闸门 |
| ultragoal、多 story、goal ledger | `omg-ultragoal` | `omg goal *` | 持久 ledger + host `/goal` session 压力 |
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
| **CLI** | `omg autopilot start\|transition\|status\|await\|complete\|run` |
| **深讲** | [`autopilot.zh.md`](./autopilot.zh.md) · [EN](./autopilot.md) |
| **SKILL** | [`skills/omg-autopilot/SKILL.md`](../skills/omg-autopilot/SKILL.md) |

```bash
omg autopilot start "完成功能 X 並含測試"
# 或：omg autopilot start "…" --skip-interview
omg autopilot run "完成功能 X 並含測試" --unattended   # 无人值守外层 (#40)
omg autopilot run --resume RUN --unattended            # cap / 崩溃后恢复
omg autopilot status --run RUN
omg autopilot await --run RUN --set   # 破坏性/凭证确认时暂停
omg autopilot complete --run RUN
```

阶段：`interview → ralplan → implement → review → (rework) → qa → acceptance → verified`  
grok **≥0.2.107** 有 Stop pin（每 turn 上限 **8**，fail-open）— 超 cap 见 [autopilot.zh.md](./autopilot.zh.md#stop-pin-诚实说明)（`omg autopilot run --resume … --unattended`、`/loop`、外层 `omg ralph`）。

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

### `omg team` — tmux team plane（默认开启；D1 零设定 + D3 multi-CLI + D2 分阶段 driver + D4 scale/resume/ralph）

| | |
|--|--|
| **何时** | 多 pane ULW + 真实 worktree；测试用 hermetic dry-run / fixture smoke |
| **闸门** | **默认开启。** 关闭：`OMG_DISABLE_TMUX_TEAM=1`（旧 `OMG_EXPERIMENTAL_TMUX_TEAM=0` 也会关） |
| **Skill** | `omg-team` — session slash **仅** `/oh-my-grok:omg-team`；自然语言 `team N …` |
| **CLI** | `omg team launch`（argv 简写 `N`/`N:role`+goal → launch；`--io-mode auto\|interactive\|headless`）；亦 `start\|run\|scale\|resume\|status\|collect\|stop\|api\|supervisor\|panes\|capture\|focus\|key\|input\|watch\|view\|hyperplan\|security-research` |

**启动就绪（#99）：** pane supervisor 证明 provider 真的可用（`pane_created` →
`provider_spawned` → `provider_ready` → `task_dispatched`；可选 `mailbox_ack`）。
旧版 `worker-ready` v1 收据仅为 `wrapper_ready_legacy`，不能产生
`startup_status=running`。认证/信任提示 → `blocked_start`。`--no-wait` →
`unverified_start`。`--io-mode interactive` 不等 supervisor ACK，由 leader
在同一超时内等待 pane TTY 上的 `TUI_READY:<nonce>` 后再提升 `input_ready`；
超时 fail-closed，不会静默降为 headless。

**静默 bootstrap（#100）：** worker pane 成功启动不打印 JSON / nested-`.omg` 警告；
失败仅一行提示。详情见 `workers/<id>/bootstrap.log`，用
`omg team status <run> --full` 查看（不要把 pane scrollback 当权威错误源）。
| **诚实范围** | 零设定 = grok panes；`--routing` 启 multi-CLI（含角色地板）。**整合**隔离（ownership + seal + integrate）— **不是**执行沙箱。`collect` / `run` / `scale` / `resume` 永不写 `verified`。scale/resume/ralph 是**同一** team plane 的生命周期延伸（无新隔离宣称）。Live 升格证据：`scripts/live_team_smoke.py --live` → `LIVE_TEAM_SMOKE_OK`（2026-07-30 本地；不进 CI）。**无裸 `/team` slash alias** — 2026-07-25 host 探测：plugin skill 为 `/name` 或 `/plugin:name`，无 frontmatter 可为 `omg-team` 注册 unnamespaced `/team`；其他 plugin 已占用 `team` skill 名。 |

**窗口拓扑（#96）：** 既有 tmux pane 内启动默认 `view_mode=same_window`（leader 左、workers 右堆叠；detached split + `main-vertical`）。`--dedicated-window` 使用独立 Team window；tmux 外 / `--detach` 为 `detached_session`。same_window 的 `stop`/失败回滚只清 worker panes，不杀 shared leader window。plan-only / dry-run / live JSON 皆带 `view_mode`；缺 mode 的旧 run 维持 dedicated/detached 行为。

**`omg team run`** 是 team plane 上的**分阶段 DRIVER**（不是新的 planner/verifier）：

`team-plan → team-prd → team-exec → team-verify → team-fix`（终态：`complete` / `failed` / `blocked`）。

- **team-plan / team-prd** — 穿透标记；任务拆解属 **leader / ralplan**，`run` 只吃 `--tasks-json` 或 `--tasks-path`。
- **team-exec** — `start_team` 再 `collect_team`（dry-run 只 start，不碰 tmux/subprocess）。
- **team-verify** — 以 POST-A2 `parse_verdict_file` 闸 `stages/team-verifier.md|json`；APPROVE → `complete`，否则 → `team-fix`。**不**代写 verdict。
- **team-fix** — `--max-fix`（预设 3）上限；超限 → `failed`。
- **`--ralph [--max-iter N]`**（D4）— 外层**有界**持久循环（预设 max_iter=3）；`team.json` 记 `linked_ralph`、`stages/team-ralph.json` 记 `linked_team`；仍只靠真实 team-verify APPROVE 进 complete，**永不**写 `verified`。
- 进 exec/fix 会作废旧 verify 戳记；`verified` 仍只经 `omg accept`。

**生命周期（D4）：**

- **`omg team scale --run ID --add N|--remove N [--dry-run]`** — 在 run 目录 **scale lock** 下动态加/减 pane。Live scale-up 在副作用前先发布按 generation 分隔的不可变 **WAL**，再以 `@omg_scale_nonce` + rename 绑定 window，并 **fail-closed ownership readback**（精确 `display-message`；不单靠可变的 `session:index`）。未提交的 scale-up WAL 或未来 **identity-receipt** generation 会挡住 dry-run add、remove、resume/relaunch、collect/join/integrate、stop，直到原 op recovery 完成。`--add` 受 `max_workers_cap()` 与单调 window index 限制；`--remove` 首次优雅排空（idle/newest），recovery 绑定 **receipt 受害者**（错误的 `--remove N` 会 fail-closed 并带 generation + task id），只杀记录的 pgid + 已认证 pane（**不**杀 session、**禁止** `pkill -f`），标记 `scaled_down` 并保留 worktree；active 不可低于 1。Meta 提交若失去成功路径，以 identity readback 分为 committed / not_committed / unknown（不以 volatile 的 `last_scale.actions` 单独判定）。**不是**执行 sandbox — 见 `docs/security-model.md`。
- **`omg team resume --run ID`** — 同一 scale lock 下重读 `team.json`；若 relaunch WAL 待处理，先做精确 relaunch recovery，再做 pane 存活对账（幂等 status 写入）。pane 对账/relaunch 之后（仍持同一把 lock）再对账 Team API task claims：保留未过期且一致的 claim（字节级不变）；仅将已过期且一致的 claim 重置为 `pending`（version+1，旧 token 被围栏）。缺失 API board 为非物化 no-op；claim 损坏在任何 mutation 前 fail-closed。resume 输出附加 `claim_reconcile`（仅 ID/计数，不含 token）。仍符合 receipt 身份的 remain-on-exit dead pane，process 不在时可清理后提交为 `needs_collect`。**默认绝不 attach / 切换 tmux client**（脚本安全）。加 `--view` 在释放 lifecycle lock 后恢复精确 Team window/leader（同 session `select-*`、跨 session `switch-client`、TTY 外 `attach-session`；`--takeover` 才加 `-d`；`--json` 永不执行 view）。`omg team view` 只恢复视图不做 reconcile/relaunch；`--print` 只打印 argv。AVAILABLE 时启动/复用 jobs 平面内部 ACP stdio sidecar（no-replay receipt；非 session/close）。reconcile / provider-session / tmux-view 结果分开回报。公开的 `omg team api reconcile` / MCP 对等物仍需未来 catalog 版本 — 见 `docs/team-operation-catalog-v1.md`。

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
omg team hyperplan execute --run RUN --team-id TEAM --executor fixture --input RESULT_BUNDLE.json --json
omg team security-research execute --run RUN --team-id TEAM --executor fixture --input RESULT_BUNDLE.json --json
# 关闭 team plane：export OMG_DISABLE_TMUX_TEAM=1
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
| **CLI** | `omg goal init\|status\|set-host\|link-run\|start-story\|checkpoint\|block-story\|resume-story\|complete-story\|verify\|repair` |
| **SKILL** | [`skills/omg-ultragoal/SKILL.md`](../skills/omg-ultragoal/SKILL.md) |

Grok **有** slash `/goal`（session 范围、单 goal、设置即替换；Active 绕过 Stop；
重启后降为 paused，用 `/goal resume`）。多 story ledger 在 `.omg/ultragoal/`
经 `omg goal *`（无 OMX `get_goal`/`create_goal` tool API）。  
`omg goal set-host --goal GOAL` 打印 `/goal …` handoff（仅 prompt turn）。  
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
| **CLI** | `omg ask list-advisors` · `omg ask explain <id>` · `omg ask codex\|claude\|gemini\|agy "…"` |
| **SKILL** | [`skills/omg-ask/SKILL.md`](../skills/omg-ask/SKILL.md) |

```bash
omg ask list-advisors
omg ask explain fable
omg ask codex "review this patch"
omg ask claude "對這份 plan 的第二意見"
```

`list-advisors` / `explain` 是**离线注册表**目录事实（每个 harness 都是 `unproven`；二进制为 `not_probed`）。不宣称合格，也不执行 provider。

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
| **CLI** | `omg lsp status` · `omg lsp validate` · legacy: `check`/`symbols`/`diagnostics` → `E_LSP_HOST_OWNED` |
| **SKILL** | [`skills/omg-lsp/SKILL.md`](../skills/omg-lsp/SKILL.md) |

`omg lsp status` / `omg lsp validate` 只检查 host-owned `.lsp.json`，不会启动
server。status 会回报 `semantic_proxy_count: 0`；configured 但未由 host 观测，
不代表 healthy。legacy `check`/`symbols`/`diagnostics` 一律返回
`E_LSP_HOST_OWNED` / `semantic_proxy_unsupported` 并以 exit code 1 结束（#28）。
语意语言操作请使用 Grok host tools；repository 查找用 `read_file` / `grep`。

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
| `omg parity run\|release-readback\|release-bundle\|release-evidence\|check\|gaps\|refresh` | 委派 frozen W0 manifest engine，并产生／验 exact bundle 与 completion evidence。 |
| `omg capabilities` / `omg native-status` | 分开的 capability tiers，外加只读 `agents_catalog`、`skills_catalog`、`hooks_registry` 与 `tools_sidecar`；不探测私有 sidecar。 |
| `omg agents list\|explain` | Dual-host agent/model 政策检视（#131）与 host-neutral UX（#134：`--width`／`NO_COLOR`）。Stock Grok Build 使用显式 inherit；Medley caps 为 unsupported（不是安装失败）。不做付费探测。Medley TUI 仍为 #290。 |
| `omg skill list\|show\|resolve\|resources` | 只读 skill 目录检视（#70）。永不写 `verified`。宿主名 `plan`/`goal` 只作为别名解析。 |
| `omg provider antigravity capabilities\|doctor\|run` | Antigravity（`agy`）探测 + 无头执行（#67-A/B）：能力信封、doctor、与 `ProviderAdapter.run`（text/json/stream-json）。`omg ask agy` 已切换（#67-C）；Team 窗格经 `build_launch_envelope`（#67-D；supervisor 持有 PTY/PID/readiness）。不宣称 `live_call_ready`。 |
| `omg visual compare` | Visual Contract V1 `compare()` 包装（#75）：读取 `--input` JSON，发出 scored/blocked 信封。调用方自行比较 `aggregate` 与 `threshold`。不写入 `passes`/`verified`，不解图像，不调用 agent。Capture、overlay/diff、独立 reviewer、visual-Ralph 仍属后续 #75。见 [visual-contract-v1.md](./visual-contract-v1.md)。 |
| `omg tools doctor\|serve\|lsp\|ast\|codegraph\|research` | OMG 自有 sidecar（#73）：语义 LSP / AST-grep / CodeGraph / 可选网络研究。**不是** Grok 原生 LSP（`omg lsp` 仍为 host-owned）。**不是** live Antigravity 证据。`omg mcp-server` 仍禁止 `lsp.*`。见 [tools-sidecar.md](./tools-sidecar.md)。 |
| `omg edit plan\|apply` | Hash-anchored 编辑 CLI（#76）：`plan` 只读；`apply` 走 `apply_hash_edit`（再读、再规划、原子替换）。不写 `passes`/`verified`。不宣称 `omo.edit.hash_anchored` 宿主对等。见 `docs/hash-edit.md`。 |

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

机器可读插件 skill 目录：[`skills/catalog.json`](../skills/catalog.json)
（loader `omg_cli/skills_catalog.py`；`omg skill list|show|resolve|resources` 或
`omg capabilities` 的 `skills_catalog`）。
[`docs/parity/projections/antigravity/skills/`](./parity/projections/antigravity/skills/)
下的 Antigravity `SKILL.md` **只是投影**。Grok 仍只有 16 个 in-session playbook。

机器可读插件 agent 目录：[`agents/catalog.json`](../agents/catalog.json)
（loader `omg_cli/agents_catalog.py`；用 `omg capabilities` 的 `agents_catalog` 检视）。
agent frontmatter 若省略或与目录的 `capabilityMode` / `permissionMode` 不一致则 fail-closed。
[`docs/parity/projections/antigravity/agents/`](./parity/projections/antigravity/agents/)
下的 Antigravity `agent.md` **只是投影** — 不是已安装的 AG 插件，也不是 live AG 证据。
团队路由地板仍在 `omg_cli/team/roles.py`。Dual-host model policy（#131）透过
`agents/model_policies.json` 与 `omg agents list|explain` 消费此目录
（Grok baseline 已出货；Medley exact/receipts 仍为 Refs）。
Grok 内建（`explore`、`plan`、`general-purpose`）是政策 profiles，不是第二份 registry。

---

## Skill ↔ CLI 对照

| Skill | 主要 CLI | 会设 `verified`？ |
|-------|----------|-------------------|
| omg-using | doctor / setup / resume | 否 |
| omg-autopilot | `autopilot *` + accept/complete | 仅经 complete/accept |
| omg-ultrawork | `ulw` / worker / integrate | 否（要 accept） |
| omg-team | `team` / launch / status / stop / api | 否（要 accept） |
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
