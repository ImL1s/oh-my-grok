# oh-my-grok 安全模型

English | [简体中文](./security-model.zh.md) | [繁體中文](./security-model.zh-TW.md)

隔離宣稱的**權威對照表**。README、skills、doctor footer 應連到這裡，而不是自行發明更強的措辭。

最後更新：2026-07-23 · Plugin 版本：**0.6.0**

## 分層表（強 → 弱）

| 層 | 機制 | 硬度 | 能擋什麼 | 殘餘／失敗模式 |
|-------|-----------|----------|---------------|-------------------------|
| **1. capability_mode** | Host 對 `spawn_subagent` 的 tool-kind 過濾 | **偏硬（host）** | `read-write` 實作者：**無 Execute** → 無 `run_terminal_command` → 該 worker 不能跑 `python -c`／`npx`／agent CLI。critic／verifier 的 `read-only`：不能寫 + 無 Execute。 | 省略 mode 會退回 agent 預設（`general-purpose` ≈ 全開）。`read-write` 仍含 Task／spawn — depth=1 需要 `disallowedTools`／父層政策。 |
| **2. Agent／headless 工具過濾** | frontmatter `disallowedTools`；父層 `--disallowed-tools` | **被遵守時偏硬** | 額外拒絕 executor 的 shell／spawn；RO 階段在 dual-review／ralplan 注入 shell deny。 | 錯的 tool id、TUI 忽略 headless flags，或 leader 仍有 shell。 |
| **3. OS sandbox** | Grok `--sandbox`／自訂 deny paths | **啟用時近 kernel** | 對 Grok process 的路徑拒絕（例如 `.omg/state/**`）。 | 預設關閉；macOS 子行程網路限制有限；外層 `omg` CLI 在子 sandbox 之外。 |
| **4. Permission rules** | `--allow`／`--deny` | **閘門，不是移除** | 可拒絕仍出現在 toolset 裡的呼叫。 | wrapper／直譯器殘餘；不是通用 allowlist 引擎。 |
| **5. PreToolUse hooks** | 全域：`$GROK_HOME/hooks` 下自洽的 `omg_pretool_deny_standalone.py`（來自 `omg_cli.deny`）；邏輯 = `omg_cli.deny` | **軟（fail-open）** | hook 健康且 host 尊重 deny 時，命令位置拒絕 `claude`／`codex`／…（stdout JSON deny，永遠 exit 0，`-I -S \|\| true` launcher）。Subagent **繼承**父層 PreToolUse（host 來源 + 單元測試）。 | Timeout／崩潰／缺 binary／畸形 JSON → **工具仍可能執行**。絕不要當硬 sandbox 行銷。 |
| **6. Acceptance allowlist** | `omg_cli.command_policy` + `omg accept` | **CLI 閘門（操作者意圖）** | 只有凍結的 argv 家族可跑進 `verified`：`true`／`false`／`pytest`／`python -m pytest\|unittest`／專案 `.py`；拒絕 `python -c`、shell、`npx`、agent CLI。 | 核准的 runner 仍會執行**儲存庫程式碼**。不是 OS sandbox。 |
| **7. Ask broker** | `omg ask` 僅子行程 env + 固定 providers；預設 stdin prompt | **使用者觸發路徑** | 只有人類跑 CLI 時才找外部顧問；`OMG_ALLOW_EXTERNAL_CLI` 不匯出到父 shell；prompt 本體不在 argv（`OMG_ASK_STDIN=1`）；除非 `OMG_ASK_ALLOW_EXTRA=1` 否則關閉自由 `--extra`。 | Provider 可能忽略 stdin；永不自動灌進 pipeline。 |
| **8. Prompt／skills HARD RULES** | Skills、agent 本文、CLI 注入提醒 | **僅慣例** | 文件要求的 `capability_mode`、depth=1、不用外部 workers。 | 模型可以忽略文字。 |

## 主要產品契約

1. **Workers 不要有 shell** — 以 `capability_mode=read-write` spawn 實作者；critic／verifier／explore 用 `read-only`。這是對直譯器逃逸的主回答。
2. **Depth = 1** — 子代不得再 spawn；`omg-executor` 同時 disallow `spawn_subagent` **與** `run_terminal_command`／`run_terminal_cmd`。
3. **只有 `omg` CLI** 在語意 acceptance 後可寫 `.omg/state/` 下的 `passes`／`verified`。
4. **Hooks 是縱深防禦** — fail-open；live canary 用 `scripts/canary_pretool.py`（PATH shim，永不真的叫 claude／codex）。

## In-session MCP server（`omg mcp-server`）

聚焦的讀取 + proposal 表面（不是 OMC ~54-tool 對等）。MCP process **就是** omg-cli 程式碼，因此「verified 只有 CLI」不會自我強制 — 靠三道機制守住：

| # | 機制 | 能擋什麼 |
|---|-----------|---------------|
| 1 | 策展過的工具 **allowlist** | 沒有 accept／set_verified／state_write／python_repl／… 工具 |
| 2 | **結構性拒絕**（`OMG_MCP_SERVER=1`） | in-process 對 `set_verified` + `register_cli_acceptance_token` 拋錯 |
| 3 | 每個寫入 handler 的 **路徑禁閉** | 不能寫進 `.omg/state/**`；拒絕 `..`／symlink 逃逸 |

若將來加入 kick-a-run 工具，必須 spawn **全新**、沒有 MCP env 標記的 `omg` 子行程 — 永不在 MCP server in-process 跑 acceptance／FSM。

Plugin 的 `.mcp.json` 只是慣例式註冊。`configured` 與本機 `loadable` **不代表** Grok 在目前 session 已 enabled／observed／verified 該 server。那些宣稱需要新鮮的 host 觀測。

## Repository workflow 邊界

`repository-workflow/v1` 由產品擁有。定義依 name + version 不可變；planner 固定 task ID、actor 身分、generation、permission request 與 dependency wave。CLI **不 spawn** shell 或外來 agent：由 Grok 的 leader／skill 執行原生 `spawn_subagent`，再把綁定 task-ID 的 receipt 交給 `omg workflow run`。

有效權限是 repository 政策、host 能力與 launch-receipt 權限的交集。MCP server 與寫入路徑需要分開的 allowlist。缺／重複／外來 receipt、actor 不符、權限拒絕，或沒有已驗證 receipt 的外部效應，都會擋住 shipment。需要獨立的 verifier 與 skeptic 身分。

Grok `/create-workflow`、`.grok/workflows/*.rhai` 與原生 dashboard 屬 `optional_unclaimed`。Help 文字或本機檔案不是穩定 schema 或新鮮呼叫的證據。OMG 永不探測未文件化的 localhost／私有 sidecar。

## Recovery、memory、tracking、compaction、notifications

- Recovery 只開啟一般非 symlink 來源、複製有界後綴、再檢查檔案身分、寫入不可變證據、redact context，並保留 broken-chain／unknown-record 警告。這是刻意的部分恢復。
- Project memory 會 redact 值，並優先保留使用者事實而非 scanner／import 資料。Tracker projection 與 compaction checkpoint 以 generation 圍籬。
- Notification adapter 只出站、有界、適用處做 SSRF 檢查，且明確非權威。它們不能設定 `passes`、`verified`、workflow 終態或 release 狀態。
- `.lsp.json` 由 host 擁有註冊。OMG 只驗證設定與本機命令是否存在；不代理語意 LSP 操作，也不推斷健康。

## Acceptance 政策（摘要）

Acceptance 子行程 env（`omg_cli.acceptance.sanitized_env`）會剝除 `OMG_ALLOW_*` 以及常見劫持鍵（`PYTHONSTARTUP`、`PYTHONPATH`、`GIT_DIR`／`GIT_WORK_TREE`、`LD_PRELOAD`／`DYLD_*`、`NODE_OPTIONS`／`NODE_PATH`、`npm_config_*`）。PATH／HOME／VIRTUAL_ENV 會保留，好讓 venv runner 能運作。
**殘餘：** 核准的 runner 仍會執行儲存庫程式碼；不是 OS sandbox。
操作者弱化：`OMG_ACCEPT_KEEP_PYTHONPATH=1` 會在 scrub 後重新加入 PYTHONPATH。

**UltraQA freeze（v0.3.2+）：** `omg qa freeze` 套用與 acceptance **相同** 的命令政策（在 freeze 時 fail-closed）。提示會導向 `python3 -m pytest`／專案 `.py` — 這**不會**擴大 allowlist。未加引號的 pytest marker token（`-m not live`）可能為 UX 合併成單一 markexpr；合併不是政策繞過。

**Auto PRD／complete 短路（v0.3.2+）：** 缺少 `prd.json` 時，只可從 **CLI 蓋章且乾淨** 的 UltraQA 物化（永不覆寫既有操作者 PRD）。`omg autopilot complete` 可在 run 已是磁碟 `verified` 時短路（只做 phase 同步）— **不會**在沒有先前 CLI accept 路徑時建立 `verified`。

**Goal verify 多行程殘餘：** 當連結的 run 已是磁碟 `verified` 時，`omg goal verify` 可接受磁碟 CLI acceptance stamp（`require_token=False`）。這比同行程 `set_verified` token 弱 — 把 goal 升格視為多行程磁碟信任，而非 process-token 等級。見 `omg_cli/goals.py` 的 verify 路徑。

見 `omg_cli/command_policy.py`（`POLICY_VERSION`）。

| Family | Allowed | Denied |
|--------|---------|--------|
| `true` / `false` | yes | — |
| `pytest` | any args | — |
| `python` / `python3` / `python3.N` | `-m pytest`、`-m unittest`，或專案下 `.py` | `-c`、`-e`、其他 `-m` module、`python3evil` |
| `npm` | `test`、`run test`、`run pytest` | 其他 scripts |
| `git` | 唯讀：`status`/`diff`/`log`/`show`/`rev-parse`/`rev-list`/`describe`/`ls-files`/`ls-tree`/`cat-file`；`branch`/`tag`/`stash` 僅 list | `clean`/`push`/`reset`/`checkout`/`restore`/`rebase`/`merge`/`pull`/`fetch`/`remote`/`config`/`add`/`commit`/…；mutate flags（`branch -D`、`tag -d`、`stash drop`）；`-c` config 注入 |
| `make` | 只允許 allowlisted targets（`test`/`check`/`lint`/`unit`/`units`/`pytest`/`ci`/`verify`） | 裸 `make`；未知 targets；`-f`/`--file`/`-C`/`--directory`/`--eval`（含黏著形式） |
| `cargo` | `test`/`check`/`clippy`/`fmt` | `run`/`install`/`publish`/`bench`/`script`/`build`；亦拒 `--manifest-path`/`--config`/`--target-dir`/`-C` |
| `go` | `test`/`vet`/`fmt`/`version` | `run`/`generate`/`get`/`install`/`mod`；`-exec`/`--exec`/`-toolexec`/`--toolexec` |
| `dart` | `test`/`analyze`/`format` | `run`/`compile`/`pub` |
| `flutter` | `test`/`analyze` | `run`/`pub`/其他 |
| `npx` / shells / `claude` / `codex` / `rm` / `sudo` | — | **永遠拒絕** |
| `--allow-cmd NAME` | 擴充 basename 集合 | floors 仍適用 |
| `--no-allowlist` | 僅 TTY 的 break-glass | floors 仍適用；非 TTY 拒絕 |

在 basename allowlist 之外，acceptance 還對每個 family 套用 **argv grammar**（`POLICY_VERSION` ≥ 2）：git 僅檢查（無裸 `stash`、無建立 branch／tag），make 需要 allowlisted target 且無 makefile／dir 覆寫，cargo／go／dart／flutter 只允許測試／分析類子命令，使凍結的 runner 不能變成 install、publish 或長跑行程啟動器。

**Canary 通過條件**（`scripts/canary_pretool.py --live`／`omg_cli/canary_classify.py`）：

| Status | Exit | Meaning |
|--------|------|---------|
| `DENIED_PARENT_AND_CHILD` | 0 | 父與子都顯示 host 簽章 `oh-my-grok: external agent CLI blocked` |
| `DENIED_PARENT_HOST_CHILD_CAPABILITY` | 0 | 父有 host 簽章 **且** 子 **沒有 shell 工具**（capability 隔離）+ 無 marker |
| `DENIED_CLAIMED_NO_HOOK_ORACLE` | 2 | 只有模型「denied」散文 — **不算** suite 綠 |
| `REAL_CLI_RAN_*`／有 marker | 1 | Soft-gate 失敗 |

沒有 host 或 capability 證據的自由模型表演，不得讓 suite 變綠。

### Spawn 軟性 fail-closed（Option A，已出貨）

PreToolUse matcher 包含 `spawn_subagent|Task`。hook 執行時，`omg_cli.deny.decide_spawn_subagent` **拒絕**下列 spawn：

- 省略 `capability_mode`／`capabilityMode`，或
- 設成 `execute`／`all`，或
- 與角色表不符（`general-purpose`／`omg-executor` → `read-write`；`explore`／critic／verifier → `read-only`）。

這仍是 **soft-gate**（hook 崩潰／timeout 時 host fail-open）。主要隔離仍是正確設定時的 host `capability_mode`。逃生口：僅 process env `OMG_ALLOW_UNSAFE_SPAWN=1`。

**Deny UX（2026-07-20）：** 缺／錯 mode **不得**讓 leader 放棄多 agent 工作。Deny `reason` 字串含 `RETRY IMMEDIATELY` 與建議的 `capability_mode`，好讓模型在同一回合重 spawn，而不是退回 solo-only。Skills／AGENTS／orchestrator 也硬編碼該重試協定。

`--yes` 只跳過確認 UX — **永不**跳過政策。

## Canary

```bash
python3 scripts/canary_pretool.py --dry
# optional live (skips if no grok):
python3 scripts/canary_pretool.py --live
```

程序與 host 來源證據：[`docs/research/subagent-pretooluse-spike.md`](research/subagent-pretooluse-spike.md)。

### 全域 PreToolUse 安裝（soft-gate 要有效就必須）

2026-07-19 live 顯示 plugin 內建的 `hooks/hooks.json` 可能不會出現在 session 的 `hook_execution` 紀錄。Soft-gate 要有效，需要 `$GROK_HOME/hooks/` 下的全域 hook，且終端使用者與開發路徑都要安裝：

1. `omg setup`（與 `omg install-hook`）— 終端使用者路徑 — 會安裝。
2. `scripts/install-plugin.sh` — 開發路徑 — 呼叫同一安裝器。
3. `omg doctor` 硬檢查 `global PreToolUse soft-gate` + 軟新鮮度檢查。

**Hook 必須自洽，並住在 `$GROK_HOME` 下，永不指向 checkout 路徑（2026-07-22 修復）。** 舊設計失敗根因：全域 hook 指向 `python3 "<checkout>/hooks/bin/pre_tool_use_deny.py"`，該腳本在 macOS-TCC 保護的 `~/Documents` 下，且還 `import` 了 `omg_cli`。在其他 workspace（或沒有 Documents 存取）的 grok session 無法 `open()` 它，於是 `python3` 以 **2** 結束 — 而 grok 的 hook 契約把 PreToolUse exit code 2 讀成*明確 deny*。每個工具呼叫（甚至 `ls`）都被擋。in-code fail-open 從未執行，因為 python 連檔都打不開。

自洽 standalone（`hooks/bin/omg_pretool_deny_standalone.py`，由 `scripts/generate_standalone_hook.py` 從 `omg_cli/deny.py` + `_common.hook_disabled` 產生，並由 CI `--check` 防漂移）用分層 fail-**open** 階梯關閉此問題：

1. **Wire 契約** — grok 不論 exit code 都會尊重 stdout `{"decision":"deny"}`，並把非 `{0,2}` 的 exit 當 fail-open。因此 standalone **只**用 stdout JSON 表達 deny，且 **永遠 exit 0** — 非零 exit（尤其是 2）絕不能來自我們。
2. **Launcher** — 安裝成 `python3 -I -S "<abs>" || true`。`-I -S` 隔離直譯器（無 `PYTHONPATH`／user-site／sibling-module 注入）；`|| true` 把任何直譯器／啟動失敗（例如 rc 2「打不開檔」）正規成 rc 0 → fail-open。
3. **In-code** — 整段 `try/except`，任何錯誤預設 allow。
4. **doctor** — realpath 必須在 `$GROK_HOME` 下 + 真的 `open()` + 行為性子行程 smoke（allow／deny）+ installed-vs-committed hash（過期則 WARN）。不要信任 `os.access`（它查 permission bits，不是 TCC）。

遷移：既有 checkout-path json 會在 `omg setup`／`install-hook` 時自動修復；若無法替換則**隔離**成非 `.json` 名稱（grok 只發現 `*.json`），使它不能再 deny 每個工具。這一切在 hook timeout／崩潰時仍是 **fail-open**；主要隔離仍是實作者沒有 Execute 的 `capability_mode`。

**帶外恢復**（已被舊 hook 弄磚的 session 無法透過被擋的終端跑 `omg`）：從任何一般 shell 跑 `python3 -m omg_cli.hook_install`（修復），或最後手段 `rm "${GROK_HOME:-$HOME/.grok}/hooks/omg-pretool-deny.json"` 停用 soft-gate，然後重開 grok。

## Host launcher：`omg --madmax`（break-glass）

**操作者觸發**的互動式 Grok，帶全開 host 權限：

- 注入 `--always-approve` + `--permission-mode bypassPermissions`（恰好一次）。
- 互動式且不在 `$TMUX` 內：**需要 tmux** — 每次啟動建立**新** session（`omg-<dir>-<digest>-<timestamp>`），再 attach。缺少 tmux → exit 1（不會默默降級成直接跑）。
- 在 tmux 內／headless（`-p`、`--single`，…）：in-process 跑 `grok`（不巢狀 session）。
- **不**寫 `.omg/state`，**不**碰 `verified`／acceptance／ask deny lists。
- Root `--yolo` 仍**只**是 mode 子命令升格 — 不是 madmax 別名。
- 脫離的全開 session 會在 tmux 下繼續跑，直到你 `tmux kill-session -t omg-…`。
- **Env 轉送：** madmax 透過 `tmux new-session -e KEY=value` 把 allowlisted `GROK_*`／`XAI_*`／少數 shell 變數傳進 session（不嵌進 pane 啟動命令字串）。值仍可能出現在 session 生命期的 **tmux server process** 環境 — 在多使用者機器上，優先用 host 身分／profile 密鑰，而不是一次性 env dump。

這是刻意的 break-glass，不是 sandbox。緩解靠文件與名稱前綴（`omg-`）— 不是 PreToolUse。

## 實驗性 team plane：`omg team`（D1 零設定 + D3 multi-CLI + D2 staged driver + D4 scale／resume／ralph）

由 **`OMG_EXPERIMENTAL_TMUX_TEAM=1`** 閘控。生命週期：`start`／`run`／`scale`／`resume`／`status`／`collect`／`stop`。

| 宣稱 | 現實 |
|-------|---------|
| 零設定 panes | **只有 grok**（省略 `--routing` 時走 D1，經 madmax `build_pane_command`） |
| Multi-CLI panes | 同一閘門下，當 `--routing` 映射 role→`{provider,model?}` 時**存在**（providers：grok／codex／agy／cursor／gemini） |
| 隔離 | **僅整合**隔離：ownership manifest + 每任務 git worktree + `seal` + `integrate` — **不是**執行 sandbox。D4 scale／resume／ralph **不**新增隔離宣稱。 |
| Kill 路徑 | `stop`／scale-down **只**殺記錄的 tmux session／window 名稱 + 記錄的 `pgid` — **沒有**自我匹配的 `pkill -f` |
| `verified` | `collect`／`stop`／**`run`**／**`scale`**／**`resume`**／ralph loop **永不**設定；仍只在 `omg accept` 之後 |
| Nested | 在 spawned-worker 脈絡（`OMG_TEAM_WORKER`／相關標記）內拒絕 start／run／scale／resume |
| Routing floors | Reviewer／verifier → 只允許結構化 verdict providers（`grok`／`codex`／`claude`／`gemini`；**禁止 cursor**）；未知角色 fail-closed；姿勢由角色推導（永不自由填） |
| `omg team run` | **僅 staged DRIVER**（`team-plan→team-prd→team-exec→team-verify→team-fix`）。**不**重做 ralplan／dual_review／planner／verifier — 序列化 team plane，並經 POST-A2 `parse_verdict_file` 閘控持久的 `stages/team-verifier.*`。分解是 leader／ralplan 的工作（`--tasks-json`／`--tasks-path`）。除了「把它們串起來」外，沒有 autopilot 對等。 |
| `omg team scale` | 在 run-dir **scale lock** 下動態 `--add N`／`--remove N`；受 `max_workers_cap()` 限制；window index 單調；scale-down 保留 worktree，且活躍 pane 不少於 1 |
| `omg team resume` | leader 重啟後冪等活體對帳進 `team.json`；若不是 team run 則 fail-closed |
| `omg team run --ralph` | 同一 staged driver 外層有界 max_iter loop（ralph 紀律）；`linked_ralph` ↔ `linked_team`；只有真實 team-verify APPROVE 才算完成 — **不是**第二道隔離邊界 |

### 各 provider 姿勢強制（不均一）

姿勢由角色推導（`omg_cli/team/roles.py` → `role_posture`），並由 `build_executor_argv`（`omg_cli/team/providers.py`）套用。強制強度**依 provider 而異**：

| Provider | 唯讀強制 |
|----------|------------------------|
| **grok** | CLI 強制（`--permission-mode plan` vs `bypassPermissions`） |
| **codex** | CLI 強制（`-s read-only` vs `workspace-write`） |
| **agy** | `--sandbox` **僅盡力**（兩種姿勢都有 `--dangerously-skip-permissions` 以利 headless 自主）— OMG **不**強制 agy 的 sandbox；請引用 agy 真實的 `--sandbox` 語意，不是硬 jail |
| **cursor** | `--mode ask`（唯讀）vs 預設 agent mode（讀寫）；**禁止**擔任 reviewer／verifier（沒有結構化 verdict mode） |
| **gemini** | **無** — 唯讀與讀寫 argv 相同；gemini pane（含 gemini reviewer）**只**被整合邊界包住，**不是** CLI sandbox |

這正是契約寫成 **「整合隔離，不是執行隔離」** 的原因。有 shell 能力的 executor pane 以操作者級機器存取執行；只有 worktree 所有權 + seal + integrate 限制什麼能進 leader tree，且 `verified` 仍只有 CLI（`omg accept`）。

不要宣稱跨 provider 均一 sandbox、OMC multi-CLI team 對等，或 multi-CLI panes 是執行 sandbox。

## 不要宣稱

- 「Workers 不能跑外部 CLI，因為 PreToolUse 擋了」卻**不**說明 fail-open 殘餘與 capability_mode 為主。
- 「Acceptance allowlist 是 sandbox。」
- 「`--permission-mode plan` 是所有 session 的硬唯讀鎖。」
- 「Live canary 通過就永遠證明硬隔離」（Grok 升級後要重跑）。
- 「`omg --madmax` 有 sandbox」或「madmax 是 mode FSM／會設 verified。」
- 「`omg team` multi-CLI panes 是執行 sandbox／跨 provider 均一 CLI sandbox。」（只有整合隔離；見姿勢表。）
- 「`omg team run` 是完整 planner／verifier／autopilot 對等 mode。」（它是既有車道上的薄 staged driver。）
- 「`omg team scale`／`resume`／`--ralph` 新增執行 sandbox 或新隔離邊界。」（只是生命週期；同一套整合隔離契約。）
- 「agy `--sandbox` 是 OMG 強制的硬唯讀 jail。」
- 「gemini reviewer panes 有 CLI sandbox。」
- 「`.mcp.json`／`.lsp.json` 檔證明 host 已 enabled 或 verified。」
- 「本機 `.rhai` 或 `/create-workflow` help 文字證明原生 workflow 對等。」
- 「Notifications 或原生 dashboard 對 run／release 狀態有權威。」

## 相關

- 隔離研究：`.omg/research/council-v021/`（本機）／`docs/research/council-v021-synthesis.md`
- 安裝：`scripts/install-plugin.sh`
- Smoke：`scripts/smoke.sh`
