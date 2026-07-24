# oh-my-grok 安全模型

English | [简体中文](./security-model.zh.md) | [繁體中文](./security-model.zh-TW.md)

隔离宣称的**权威对照表**。README、skills、doctor footer 应连到这里，而不是自行发明更强的措辞。

最后更新：2026-07-23 · Plugin 版本：**0.6.0**

## 分层表（强 → 弱）

| 层 | 机制 | 硬度 | 能挡什么 | 残余／失败模式 |
|-------|-----------|----------|---------------|-------------------------|
| **1. capability_mode** | Host 对 `spawn_subagent` 的 tool-kind 过滤 | **偏硬（host）** | `read-write` 实作者：**无 Execute** → 无 `run_terminal_command` → 该 worker 不能跑 `python -c`／`npx`／agent CLI。critic／verifier 的 `read-only`：不能写 + 无 Execute。 | 省略 mode 会退回 agent 预设（`general-purpose` ≈ 全开）。`read-write` 仍含 Task／spawn — depth=1 需要 `disallowedTools`／父层政策。 |
| **2. Agent／headless 工具过滤** | frontmatter `disallowedTools`；父层 `--disallowed-tools` | **被遵守时偏硬** | 额外拒绝 executor 的 shell／spawn；RO 阶段在 dual-review／ralplan 注入 shell deny。 | 错的 tool id、TUI 忽略 headless flags，或 leader 仍有 shell。 |
| **3. OS sandbox** | Grok `--sandbox`／自订 deny paths | **启用时近 kernel** | 对 Grok process 的路径拒绝（例如 `.omg/state/**`）。 | 预设关闭；macOS 子行程网络限制有限；外层 `omg` CLI 在子 sandbox 之外。 |
| **4. Permission rules** | `--allow`／`--deny` | **闸门，不是移除** | 可拒绝仍出现在 toolset 里的呼叫。 | wrapper／直译器残余；不是通用 allowlist 引擎。 |
| **5. PreToolUse hooks** | 全域：`$GROK_HOME/hooks` 下自洽的 `omg_pretool_deny_standalone.py`（来自 `omg_cli.deny`）；逻辑 = `omg_cli.deny` | **软（fail-open）** | hook 健康且 host 尊重 deny 时，命令位置拒绝 `claude`／`codex`／…（stdout JSON deny，永远 exit 0，`-I -S \|\| true` launcher）。Subagent **继承**父层 PreToolUse（host 来源 + 单元测试）。 | Timeout／崩溃／缺 binary／畸形 JSON → **工具仍可能执行**。绝不要当硬 sandbox 行销。 |
| **6. Acceptance allowlist** | `omg_cli.command_policy` + `omg accept` | **CLI 闸门（操作者意图）** | 只有冻结的 argv 家族可跑进 `verified`：`true`／`false`／`pytest`／`python -m pytest\|unittest`／专案 `.py`；拒绝 `python -c`、shell、`npx`、agent CLI。 | 核准的 runner 仍会执行**储存库程式码**。不是 OS sandbox。 |
| **7. Ask broker** | `omg ask` 仅子行程 env + 固定 providers；预设 stdin prompt | **使用者触发路径** | 只有人类跑 CLI 时才找外部顾问；`OMG_ALLOW_EXTERNAL_CLI` 不汇出到父 shell；prompt 本体不在 argv（`OMG_ASK_STDIN=1`）；除非 `OMG_ASK_ALLOW_EXTRA=1` 否则关闭自由 `--extra`。 | Provider 可能忽略 stdin；永不自动灌进 pipeline。 |
| **8. Prompt／skills HARD RULES** | Skills、agent 本文、CLI 注入提醒 | **仅惯例** | 文件要求的 `capability_mode`、depth=1、不用外部 workers。 | 模型可以忽略文字。 |

## 主要产品契约

1. **Workers 不要有 shell** — 以 `capability_mode=read-write` spawn 实作者；critic／verifier／explore 用 `read-only`。这是对直译器逃逸的主回答。
2. **Depth = 1** — 子代不得再 spawn；`omg-executor` 同时 disallow `spawn_subagent` **与** `run_terminal_command`／`run_terminal_cmd`。
3. **只有 `omg` CLI** 在语意 acceptance 后可写 `.omg/state/` 下的 `passes`／`verified`。
4. **Hooks 是纵深防御** — fail-open；live canary 用 `scripts/canary_pretool.py`（PATH shim，永不真的叫 claude／codex）。

## In-session MCP server（`omg mcp-server`）

聚焦的读取 + proposal 表面（不是 OMC ~54-tool 对等）。MCP process **就是** omg-cli 程式码，因此“verified 只有 CLI”不会自我强制 — 靠三道机制守住：

| # | 机制 | 能挡什么 |
|---|-----------|---------------|
| 1 | 策展过的工具 **allowlist** | 没有 accept／set_verified／state_write／python_repl／… 工具 |
| 2 | **结构性拒绝**（`OMG_MCP_SERVER=1`） | in-process 对 `set_verified` + `register_cli_acceptance_token` 抛错 |
| 3 | 每个写入 handler 的 **路径禁闭** | 不能写进 `.omg/state/**`；拒绝 `..`／symlink 逃逸 |

若将来加入 kick-a-run 工具，必须 spawn **全新**、没有 MCP env 标记的 `omg` 子行程 — 永不在 MCP server in-process 跑 acceptance／FSM。

Plugin 的 `.mcp.json` 只是惯例式注册。`configured` 与本机 `loadable` **不代表** Grok 在目前 session 已 enabled／observed／verified 该 server。那些宣称需要新鲜的 host 观测。

## Repository workflow 边界

`repository-workflow/v1` 由产品拥有。定义依 name + version 不可变；planner 固定 task ID、actor 身份、generation、permission request 与 dependency wave。CLI **不 spawn** shell 或外来 agent：由 Grok 的 leader／skill 执行原生 `spawn_subagent`，再把绑定 task-ID 的 receipt 交给 `omg workflow run`。

有效权限是 repository 政策、host 能力与 launch-receipt 权限的交集。MCP server 与写入路径需要分开的 allowlist。缺／重复／外来 receipt、actor 不符、权限拒绝，或没有已验证 receipt 的外部效应，都会挡住 shipment。需要独立的 verifier 与 skeptic 身份。

Grok `/create-workflow`、`.grok/workflows/*.rhai` 与原生 dashboard 属 `optional_unclaimed`。Help 文字或本机档案不是稳定 schema 或新鲜呼叫的证据。OMG 永不探测未文件化的 localhost／私有 sidecar。

## Recovery、memory、tracking、compaction、notifications

- Recovery 只开启一般非 symlink 来源、复制有界后缀、再检查档案身份、写入不可变证据、redact context，并保留 broken-chain／unknown-record 警告。这是刻意的部分恢复。
- Project memory 会 redact 值，并优先保留使用者事实而非 scanner／import 资料。Tracker projection 与 compaction checkpoint 以 generation 围篱。
- Notification adapter 只出站、有界、适用处做 SSRF 检查，且明确非权威。它们不能设定 `passes`、`verified`、workflow 终态或 release 状态。
- `.lsp.json` 由 host 拥有注册。OMG 只验证设定与本机命令是否存在；不代理语意 LSP 操作，也不推断健康。

## Acceptance 政策（摘要）

Acceptance 子行程 env（`omg_cli.acceptance.sanitized_env`）会剥除 `OMG_ALLOW_*` 以及常见劫持键（`PYTHONSTARTUP`、`PYTHONPATH`、`GIT_DIR`／`GIT_WORK_TREE`、`LD_PRELOAD`／`DYLD_*`、`NODE_OPTIONS`／`NODE_PATH`、`npm_config_*`）。PATH／HOME／VIRTUAL_ENV 会保留，好让 venv runner 能运作。
**残余：** 核准的 runner 仍会执行储存库程式码；不是 OS sandbox。
操作者弱化：`OMG_ACCEPT_KEEP_PYTHONPATH=1` 会在 scrub 后重新加入 PYTHONPATH。

**UltraQA freeze（v0.3.2+）：** `omg qa freeze` 套用与 acceptance **相同** 的命令政策（在 freeze 时 fail-closed）。提示会导向 `python3 -m pytest`／专案 `.py` — 这**不会**扩大 allowlist。未加引号的 pytest marker token（`-m not live`）可能为 UX 合并成单一 markexpr；合并不是政策绕过。

**Auto PRD／complete 短路（v0.3.2+）：** 缺少 `prd.json` 时，只可从 **CLI 盖章且干净** 的 UltraQA 物化（永不覆写既有操作者 PRD）。`omg autopilot complete` 可在 run 已是磁盘 `verified` 时短路（只做 phase 同步）— **不会**在没有先前 CLI accept 路径时建立 `verified`。

**Goal verify 多行程残余：** 当连结的 run 已是磁盘 `verified` 时，`omg goal verify` 可接受磁盘 CLI acceptance stamp（`require_token=False`）。这比同行程 `set_verified` token 弱 — 把 goal 升格视为多行程磁盘信任，而非 process-token 等级。见 `omg_cli/goals.py` 的 verify 路径。

见 `omg_cli/command_policy.py`（`POLICY_VERSION`）。

| Family | Allowed | Denied |
|--------|---------|--------|
| `true` / `false` | yes | — |
| `pytest` | any args | — |
| `python` / `python3` / `python3.N` | `-m pytest`、`-m unittest`，或专案下 `.py` | `-c`、`-e`、其他 `-m` module、`python3evil` |
| `npm` | `test`、`run test`、`run pytest` | 其他 scripts |
| `git` | 只读：`status`/`diff`/`log`/`show`/`rev-parse`/`rev-list`/`describe`/`ls-files`/`ls-tree`/`cat-file`；`branch`/`tag`/`stash` 仅 list | `clean`/`push`/`reset`/`checkout`/`restore`/`rebase`/`merge`/`pull`/`fetch`/`remote`/`config`/`add`/`commit`/…；mutate flags（`branch -D`、`tag -d`、`stash drop`）；`-c` config 注入 |
| `make` | 只允许 allowlisted targets（`test`/`check`/`lint`/`unit`/`units`/`pytest`/`ci`/`verify`） | 裸 `make`；未知 targets；`-f`/`--file`/`-C`/`--directory`/`--eval`（含黏着形式） |
| `cargo` | `test`/`check`/`clippy`/`fmt` | `run`/`install`/`publish`/`bench`/`script`/`build`；亦拒 `--manifest-path`/`--config`/`--target-dir`/`-C` |
| `go` | `test`/`vet`/`fmt`/`version` | `run`/`generate`/`get`/`install`/`mod`；`-exec`/`--exec`/`-toolexec`/`--toolexec` |
| `dart` | `test`/`analyze`/`format` | `run`/`compile`/`pub` |
| `flutter` | `test`/`analyze` | `run`/`pub`/其他 |
| `npx` / shells / `claude` / `codex` / `rm` / `sudo` | — | **永远拒绝** |
| `--allow-cmd NAME` | 扩充 basename 集合 | floors 仍适用 |
| `--no-allowlist` | 仅 TTY 的 break-glass | floors 仍适用；非 TTY 拒绝 |

在 basename allowlist 之外，acceptance 还对每个 family 套用 **argv grammar**（`POLICY_VERSION` ≥ 2）：git 仅检查（无裸 `stash`、无建立 branch／tag），make 需要 allowlisted target 且无 makefile／dir 覆写，cargo／go／dart／flutter 只允许测试／分析类子命令，使冻结的 runner 不能变成 install、publish 或长跑行程启动器。

**Canary 通过条件**（`scripts/canary_pretool.py --live`／`omg_cli/canary_classify.py`）：

| Status | Exit | Meaning |
|--------|------|---------|
| `DENIED_PARENT_AND_CHILD` | 0 | 父与子都显示 host 签章 `oh-my-grok: external agent CLI blocked` |
| `DENIED_PARENT_HOST_CHILD_CAPABILITY` | 0 | 父有 host 签章 **且** 子 **没有 shell 工具**（capability 隔离）+ 无 marker |
| `DENIED_CLAIMED_NO_HOOK_ORACLE` | 2 | 只有模型“denied”散文 — **不算** suite 绿 |
| `REAL_CLI_RAN_*`／有 marker | 1 | Soft-gate 失败 |

没有 host 或 capability 证据的自由模型表演，不得让 suite 变绿。

### Spawn 软性 fail-closed（Option A，已出货）

PreToolUse matcher 包含 `spawn_subagent|Task`。hook 执行时，`omg_cli.deny.decide_spawn_subagent` **拒绝**下列 spawn：

- 省略 `capability_mode`／`capabilityMode`，或
- 设成 `execute`／`all`，或
- 与角色表不符（`general-purpose`／`omg-executor` → `read-write`；`explore`／critic／verifier → `read-only`）。

这仍是 **soft-gate**（hook 崩溃／timeout 时 host fail-open）。主要隔离仍是正确设定时的 host `capability_mode`。逃生口：仅 process env `OMG_ALLOW_UNSAFE_SPAWN=1`。

**Deny UX（2026-07-20）：** 缺／错 mode **不得**让 leader 放弃多 agent 工作。Deny `reason` 字串含 `RETRY IMMEDIATELY` 与建议的 `capability_mode`，好让模型在同一回合重 spawn，而不是退回 solo-only。Skills／AGENTS／orchestrator 也硬编码该重试协定。

`--yes` 只跳过确认 UX — **永不**跳过政策。

## Canary

```bash
python3 scripts/canary_pretool.py --dry
# optional live (skips if no grok):
python3 scripts/canary_pretool.py --live
```

程序与 host 来源证据：[`docs/research/subagent-pretooluse-spike.md`](research/subagent-pretooluse-spike.md)。

### 全域 PreToolUse 安装（soft-gate 要有效就必须）

2026-07-19 live 显示 plugin 内建的 `hooks/hooks.json` 可能不会出现在 session 的 `hook_execution` 纪录。Soft-gate 要有效，需要 `$GROK_HOME/hooks/` 下的全域 hook，且终端使用者与开发路径都要安装：

1. `omg setup`（与 `omg install-hook`）— 终端使用者路径 — 会安装。
2. `scripts/install-plugin.sh` — 开发路径 — 呼叫同一安装器。
3. `omg doctor` 硬检查 `global PreToolUse soft-gate` + 软新鲜度检查。

**Hook 必须自洽，并住在 `$GROK_HOME` 下，永不指向 checkout 路径（2026-07-22 修复）。** 旧设计失败根因：全域 hook 指向 `python3 "<checkout>/hooks/bin/pre_tool_use_deny.py"`，该脚本在 macOS-TCC 保护的 `~/Documents` 下，且还 `import` 了 `omg_cli`。在其他 workspace（或没有 Documents 存取）的 grok session 无法 `open()` 它，于是 `python3` 以 **2** 结束 — 而 grok 的 hook 契约把 PreToolUse exit code 2 读成*明确 deny*。每个工具呼叫（甚至 `ls`）都被挡。in-code fail-open 从未执行，因为 python 连档都打不开。

自洽 standalone（`hooks/bin/omg_pretool_deny_standalone.py`，由 `scripts/generate_standalone_hook.py` 从 `omg_cli/deny.py` + `_common.hook_disabled` 产生，并由 CI `--check` 防漂移）用分层 fail-**open** 阶梯关闭此问题：

1. **Wire 契约** — grok 不论 exit code 都会尊重 stdout `{"decision":"deny"}`，并把非 `{0,2}` 的 exit 当 fail-open。因此 standalone **只**用 stdout JSON 表达 deny，且 **永远 exit 0** — 非零 exit（尤其是 2）绝不能来自我们。
2. **Launcher** — 安装成 `python3 -I -S "<abs>" || true`。`-I -S` 隔离直译器（无 `PYTHONPATH`／user-site／sibling-module 注入）；`|| true` 把任何直译器／启动失败（例如 rc 2“打不开档”）正规成 rc 0 → fail-open。
3. **In-code** — 整段 `try/except`，任何错误预设 allow。
4. **doctor** — realpath 必须在 `$GROK_HOME` 下 + 真的 `open()` + 行为性子行程 smoke（allow／deny）+ installed-vs-committed hash（过期则 WARN）。不要信任 `os.access`（它查 permission bits，不是 TCC）。

迁移：既有 checkout-path json 会在 `omg setup`／`install-hook` 时自动修复；若无法替换则**隔离**成非 `.json` 名称（grok 只发现 `*.json`），使它不能再 deny 每个工具。这一切在 hook timeout／崩溃时仍是 **fail-open**；主要隔离仍是实作者没有 Execute 的 `capability_mode`。

**带外恢复**（已被旧 hook 弄砖的 session 无法透过被挡的终端跑 `omg`）：从任何一般 shell 跑 `python3 -m omg_cli.hook_install`（修复），或最后手段 `rm "${GROK_HOME:-$HOME/.grok}/hooks/omg-pretool-deny.json"` 停用 soft-gate，然后重开 grok。

## Host launcher：`omg --madmax`（break-glass）

**操作者触发**的互动式 Grok，带全开 host 权限：

- 注入 `--always-approve` + `--permission-mode bypassPermissions`（恰好一次）。
- 互动式且不在 `$TMUX` 内：**需要 tmux** — 每次启动建立**新** session（`omg-<dir>-<digest>-<timestamp>`），再 attach。缺少 tmux → exit 1（不会默默降级成直接跑）。
- 在 tmux 内／headless（`-p`、`--single`，…）：in-process 跑 `grok`（不巢状 session）。
- **不**写 `.omg/state`，**不**碰 `verified`／acceptance／ask deny lists。
- Root `--yolo` 仍**只**是 mode 子命令升格 — 不是 madmax 别名。
- 脱离的全开 session 会在 tmux 下继续跑，直到你 `tmux kill-session -t omg-…`。
- **Env 转送：** madmax 透过 `tmux new-session -e KEY=value` 把 allowlisted `GROK_*`／`XAI_*`／少数 shell 变数传进 session（不嵌进 pane 启动命令字串）。值仍可能出现在 session 生命期的 **tmux server process** 环境 — 在多使用者机器上，优先用 host 身份／profile 密钥，而不是一次性 env dump。

这是刻意的 break-glass，不是 sandbox。缓解靠文件与名称前缀（`omg-`）— 不是 PreToolUse。

## 实验性 team plane：`omg team`（D1 零设定 + D3 multi-CLI + D2 staged driver + D4 scale／resume／ralph）

由 **`OMG_EXPERIMENTAL_TMUX_TEAM=1`** 闸控。生命周期：`start`／`run`／`scale`／`resume`／`status`／`collect`／`stop`。

| 宣称 | 现实 |
|-------|---------|
| 零设定 panes | **只有 grok**（省略 `--routing` 时走 D1，经 madmax `build_pane_command`） |
| Multi-CLI panes | 同一闸门下，当 `--routing` 映射 role→`{provider,model?}` 时**存在**（providers：grok／codex／agy／cursor／gemini） |
| 隔离 | **仅整合**隔离：ownership manifest + 每任务 git worktree + `seal` + `integrate` — **不是**执行 sandbox。D4 scale／resume／ralph **不**新增隔离宣称。 |
| Kill 路径 | `stop`／scale-down **只**杀记录的 tmux session／window 名称 + 记录的 `pgid` — **没有**自我匹配的 `pkill -f` |
| `verified` | `collect`／`stop`／**`run`**／**`scale`**／**`resume`**／ralph loop **永不**设定；仍只在 `omg accept` 之后 |
| Nested | 在 spawned-worker 脉络（`OMG_TEAM_WORKER`／相关标记）内拒绝 start／run／scale／resume |
| Routing floors | Reviewer／verifier → 只允许结构化 verdict providers（`grok`／`codex`／`claude`／`gemini`；**禁止 cursor**）；未知角色 fail-closed；姿势由角色推导（永不自由填） |
| `omg team run` | **仅 staged DRIVER**（`team-plan→team-prd→team-exec→team-verify→team-fix`）。**不**重做 ralplan／dual_review／planner／verifier — 序列化 team plane，并经 POST-A2 `parse_verdict_file` 闸控持久的 `stages/team-verifier.*`。分解是 leader／ralplan 的工作（`--tasks-json`／`--tasks-path`）。除了“把它们串起来”外，没有 autopilot 对等。 |
| `omg team scale` | 在 run-dir **scale lock** 下动态 `--add N`／`--remove N`；受 `max_workers_cap()` 限制；window index 单调；scale-down 保留 worktree，且活跃 pane 不少于 1 |
| `omg team resume` | leader 重启后幂等活体对账进 `team.json`；若不是 team run 则 fail-closed |
| `omg team run --ralph` | 同一 staged driver 外层有界 max_iter loop（ralph 纪律）；`linked_ralph` ↔ `linked_team`；只有真实 team-verify APPROVE 才算完成 — **不是**第二道隔离边界 |

### 各 provider 姿势强制（不均一）

姿势由角色推导（`omg_cli/team/roles.py` → `role_posture`），并由 `build_executor_argv`（`omg_cli/team/providers.py`）套用。强制强度**依 provider 而异**：

| Provider | 只读强制 |
|----------|------------------------|
| **grok** | CLI 强制（`--permission-mode plan` vs `bypassPermissions`） |
| **codex** | CLI 强制（`-s read-only` vs `workspace-write`） |
| **agy** | `--sandbox` **仅尽力**（两种姿势都有 `--dangerously-skip-permissions` 以利 headless 自主）— OMG **不**强制 agy 的 sandbox；请引用 agy 真实的 `--sandbox` 语意，不是硬 jail |
| **cursor** | `--mode ask`（只读）vs 预设 agent mode（读写）；**禁止**担任 reviewer／verifier（没有结构化 verdict mode） |
| **gemini** | **无** — 只读与读写 argv 相同；gemini pane（含 gemini reviewer）**只**被整合边界包住，**不是** CLI sandbox |

这正是契约写成 **“整合隔离，不是执行隔离”** 的原因。有 shell 能力的 executor pane 以操作者级机器存取执行；只有 worktree 所有权 + seal + integrate 限制什么能进 leader tree，且 `verified` 仍只有 CLI（`omg accept`）。

不要宣称跨 provider 均一 sandbox、OMC multi-CLI team 对等，或 multi-CLI panes 是执行 sandbox。

## 不要宣称

- “Workers 不能跑外部 CLI，因为 PreToolUse 挡了”却**不**说明 fail-open 残余与 capability_mode 为主。
- “Acceptance allowlist 是 sandbox。”
- “`--permission-mode plan` 是所有 session 的硬只读锁。”
- “Live canary 通过就永远证明硬隔离”（Grok 升级后要重跑）。
- “`omg --madmax` 有 sandbox”或“madmax 是 mode FSM／会设 verified。”
- “`omg team` multi-CLI panes 是执行 sandbox／跨 provider 均一 CLI sandbox。”（只有整合隔离；见姿势表。）
- “`omg team run` 是完整 planner／verifier／autopilot 对等 mode。”（它是既有车道上的薄 staged driver。）
- “`omg team scale`／`resume`／`--ralph` 新增执行 sandbox 或新隔离边界。”（只是生命周期；同一套整合隔离契约。）
- “agy `--sandbox` 是 OMG 强制的硬只读 jail。”
- “gemini reviewer panes 有 CLI sandbox。”
- “`.mcp.json`／`.lsp.json` 档证明 host 已 enabled 或 verified。”
- “本机 `.rhai` 或 `/create-workflow` help 文字证明原生 workflow 对等。”
- “Notifications 或原生 dashboard 对 run／release 状态有权威。”

## 相关

- 隔离研究：`.omg/research/council-v021/`（本机）／`docs/research/council-v021-synthesis.md`
- 安装：`scripts/install-plugin.sh`
- Smoke：`scripts/smoke.sh`
