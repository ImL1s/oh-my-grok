# OMG OMX-like Team Launch UX P0 實作計畫

- 日期：2026-07-25
- 狀態：Proposed
- 第一階段範圍：只做 `oh-my-grok`；OMA／OMCU 僅保留可重用的 CLI 與 state contract，不在本 P0 一起修改
- 目標命令：`omg team [N[:role]] "<goal>"`
- 完成門檻：真實 `grok` worker、真實 tmux split panes、真實 git worktrees、可由 worker 使用的 mailbox/task API、可讀取的 durable state，以及 `status`／`resume`／`stop` 全部通過非 dry-run live smoke

## 一句話結論

**不能；截至 2026-07-25，Grok 內沒有 `/team` skill／routing，shell 端 `omg team` 只接受實驗性 verbose subcommands，現有 tmux plane、P0 API 與 worktree/state 又尚未接成一條可由真實 worker ACK、執行、續跑及停止的 OMX-like launch path。**

## Review baseline

目前 repo 是 clean `main`，本次直接讀取並驗證下列實作：

- `omg_cli/main.py`
  - `omg team` parser 只接受 `start|run|scale|resume|status|collect|stop|api`。
  - `start` 強制要求 `--goal` 與 `--tasks-json`。
  - `resume` 目前只重讀 `team.json` 並 reconcile pane liveness。
- `omg_cli/team/plane.py`
  - live start 受 `OMG_EXPERIMENTAL_TMUX_TEAM=1` gate。
  - 現行 topology 是一個 detached tmux session、每個 task 一個 `new-window`；不是 leader window 內的可見 split panes。
  - worker env 只有一般 allowlist 與 `OMG_TEAM_WORKER=1`，沒有 team/run/worker/state-root identity。
  - worker prompt 明寫「Coordinate via artifacts only」，沒有 mailbox ACK、claim、transition contract。
- `omg_cli/team/api.py`
  - 已有 11 個 P0 mailbox/task ops，但 state 是另一組 run-scoped store。
  - `execute_team_api()` 會全面拒絕 `OMG_TEAM_WORKER` context，因此實際 team worker 不能用該 API。
  - `get-summary` 尚未連到 pane/process liveness。
- `omg_cli/workers.py` 與 `omg_cli/team/worktree.py`
  - 已有真實 git worktree、seal、delivery、integration hardening。
  - 兩套 ownership/worktree contract 並存；canonical shorthand 在只有 `N + goal` 時，無法事先提供 non-empty `owned_files`。
- `templates/omg-rules.md`、`skills/omg-using/SKILL.md`、`docs/skills*.md`
  - 沒有 team routing／team skill。
  - 全域規則把所有 fan-out 都導向 `spawn_subagent`，沒有「使用者明確要求 durable tmux team」的例外。
- `tests/test_team_plane.py`、`tests/test_team_pipeline.py`
  - 檔案本身明確標示 `No live tmux`／`dry-run only`。
- `scripts/smoke.sh`、`scripts/live_suite.sh`、`.github/workflows/ci.yml`
  - 沒有一條會啟動真實 Grok team panes 的 gate。

Fresh baseline evidence：

```text
$ python3 -m pytest -q \
    tests/test_team_plane.py tests/test_team_pipeline.py tests/test_team_api.py \
    tests/test_skill_inventory.py tests/test_plugin_session_discovery.py
115 passed in 39.22s

$ python3 -m omg_cli.main team 3:executor "fix flaky tests"
omg team: error: argument team_action: invalid choice: '3:executor'
```

這代表既有 hermetic contract 是綠的，但也直接證明 user-facing shorthand 尚不存在；前者不能拿來替代 live parity。

## P0 user contract

### Shell grammar

```text
omg team [N[:role]] "<goal>"
omg team status [TEAM|RUN] [--json]
omg team resume [TEAM|RUN] [--json]
omg team stop [TEAM|RUN] [--force] [--json]
omg team shutdown [TEAM|RUN] [--force] [--json]   # stop alias，與 OMX 用語對齊
```

規則：

- `N` 預設為 `3`，範圍使用既有 `max_workers_cap()`；超界 fail closed。
- `role` 預設為 `executor`，必須通過 `omg_cli/team/roles.py` 驗證。
- `3:role` 選的是 worker role／prompt；provider 預設仍是 `grok`。
- routed providers 繼續使用明確的 `--routing`，不得把 role 字串偷換成 provider。
- shell command 缺少 `<goal>` 時回傳 usage error；不得從 shell history 猜 goal。
- 保留既有 `team start|run|scale|collect|api` 作為 advanced/compat surface；不能破壞已存在的 scripts。

為了不讓 Python `argparse` subparser 與 shorthand 衝突，新增一個公開 `launch` action，並在 parse 前做純 argv normalization：

```text
team 3:executor "fix flaky tests"
  -> team launch --workers 3 --role executor --goal "fix flaky tests"
```

保留字 `start|run|scale|resume|status|collect|stop|shutdown|api|launch` 不得被當成 goal。

### In-session grammar

保證可安裝、可測的 plugin surface：

```text
/oh-my-grok:omg-team 3:executor fix flaky tests
team 3:executor fix flaky tests
```

新增 `skills/omg-team/SKILL.md`，其唯一 launch authority 是呼叫 canonical `omg team ...`；skill 本身不得用 `spawn_subagent` 假裝 team，也不得自己手寫 `.omg/state/`。

對 literal `/team 3` 要分兩層誠實處理：

1. 先用目前 Grok plugin SDK／live session 證明是否支援 unnamespaced slash alias。
2. 若 host 支援 alias，註冊 `/team` 並加入 live session acceptance。
3. 若 host 在 model routing 前就拒絕未知 slash command，P0 只能宣稱 namespaced skill 與 natural-language keyword；不得把 `/oh-my-grok:omg-team` 文件改名後宣稱 literal `/team` 已完成。

在 Grok session 中若 skill invocation 沒帶 goal，可使用該 user turn 的剩餘內容／明確 active task；無明確 task 時只問一次 goal。shell 端則永遠要求 goal。

### Tmux behavior

- 已在 tmux 內：
  - 保留 leader pane。
  - 在同一個 leader window 用 `split-window` 建立 `N` 個 worker panes。
  - 記錄每個 exact `pane_id`、`pane_pid`、session/window identity 與 team owner token。
  - 套用穩定 layout；不得殺到 leader 或非本 team pane。
- 不在 tmux、且是 interactive TTY：
  - 建立 named session 與 control/leader pane，再 split `N` 個 worker panes。
  - launch 成功後 attach 該 session；detach 後 CLI 才結束。
- 非 interactive context：
  - 只有明確 `--detach` 才允許建立 detached team。
  - 回傳 exact `tmux attach-session -t <name>` command。
  - 不得把「無法 attach」默默降級成 dry-run 或 `spawn_subagent`。

### Start transaction

`omg_cli/team/runtime.py` 成為 canonical orchestrator，啟動順序固定為：

1. 驗證 git repo、clean leader preflight、tmux、provider binary、worker count、role、routing。
2. 產生 stable `team_name`、`run_id`、leader root hash、base SHA 與 owner token。
3. 在 canonical run-scoped team root 原子建立：
   - `config.json`
   - `manifest.json`
   - `tasks/*.json`
   - `mailbox/*.json`
   - `workers/<worker>/identity.json`
   - `workers/<worker>/inbox.md`
   - worktree receipts
4. 建立每個 worker 的 dedicated git worktree。
5. 將完整 assignment 寫入 inbox；pane 只收到啟動 prompt，不靠大量 `send-keys` 傳工作內容。
6. 建立 split panes，向每個 process 注入經 allowlist 處理的：
   - `OMG_TEAM_RUN_ID`
   - `OMG_TEAM_ID`
   - `OMG_TEAM_WORKER_ID`
   - `OMG_TEAM_STATE_ROOT`
   - `OMG_TEAM_LEADER_ROOT`
   - `OMG_TEAM_OWNER_TOKEN`
7. worker 啟動後必須：
   - 讀自己的 inbox。
   - 透過 `omg team api send-message` 對 `leader-fixed` 寫入 `ACK`。
   - claim task、transition 到 `in_progress`。
   - 在自己的 worktree 工作、測試、commit。
   - transition 到 terminal state，附上 commit/test evidence，再通知 leader。
8. leader bounded wait：
   - exact panes 仍存在。
   - process identity 符合。
   - 每個 worker 都有 durable ACK。
9. 只有上述條件成功後才能把 team 狀態改成 `running` 並印出 `Team started`。

若任一步失敗，回傳非零並進入 `failed_start`；只清理由本 transaction 精確建立且仍可證明 ownership 的 panes/session。dirty worktree 與診斷 state 必須保留，不得為了「乾淨」而刪掉證據。

### Task decomposition 與 worktree scope

shorthand 只有 `N + goal`，所以不能要求使用者先寫 `--tasks-json`。新增 `omg_cli/team/decomposition.py`：

1. 優先解析明確 numbered／bulleted items。
2. 其次拆分可獨立的 conjunction clauses。
3. atomic goal fallback 建立 diagnosis／implementation／verification lanes，並用 dependency 欄位避免把必須序列化的工作偽裝成 parallel。
4. 若 invocation 帶有已核准 plan/task artifact，優先採用該 artifact，而不是重新猜測。

canonical team worktree 使用 `omg_cli/team/worktree.py`，不放寬 legacy `omg_cli/workers.py` 的 non-empty ownership invariant。將 team worktree receipt 升為可辨識的 scope mode：

- `declared`：advanced task 已提供 `owned_paths`，維持目前嚴格檢查。
- `discover`：launch 時可先建立未綁 paths 的 worker worktree；seal 時在 team lock 下把實際 changed paths 原子綁到 delivery。
- 兩個 worktrees 若 claim／修改重疊 path，第二個 seal 或 integration 必須回 `E_TEAM_PATH_CONFLICT`，不得 last-writer-wins。

這仍然只是 git integration isolation，不是 execution sandbox；文件與 status 必須保留這個界線。

### State 與 API

canonical authority 保持 run-scoped：

```text
.omg/state/runs/<run-id>/team/<team-key>/
```

另加一個非權威 lookup index：

```text
.omg/state/team/<team-name>/ref.json
```

用途只是讓 `status|resume|stop <team-name>` resolve 到 canonical `run_id + team_id`；所有 mutation 仍在 run-scoped lock 下完成，避免第三套 authoritative store。

`start_team()` 不得再只寫孤立的 `team.json`；launch transaction 必須直接初始化 `api.py` 使用的 config/tasks/mailboxes/workers。legacy `team.json` 若保留，應是 canonical state 的 compatibility view。

移除「只要是 worker 就全面拒絕 API」的 blanket gate，改為 identity-bound operation matrix：

| Operation | Leader | Worker |
|---|---:|---:|
| `read-config`, `get-summary` | allow | allow |
| `send-message` | allow | allow；`from_worker` 強制等於 env identity |
| `mailbox-list`, `mailbox-mark-delivered` | allow | 只允許自己的 mailbox |
| `claim-task` | allow | allow；claim owner 強制等於 env identity |
| `transition-task-status`, `release-task-claim`, `renew-task-claim` | allow | 只允許自己持有的 claim |
| `create-task`, `write-worker-inbox` | allow | deny |
| launch／stop／cleanup | allow | deny |

worker 傳入 payload 的 `run_id/team_id/worker_id` 必須與 immutable identity receipt、leader root hash、owner token 一致；不能只相信 CLI input 字串。

### Lifecycle

`status` 的單一 snapshot 至少包含：

- team/run identity、goal、requested/actual worker count、role/provider。
- `starting|running|degraded|completed|failed|stopped`。
- task counts 與每個 task 的 owner/status/dependency。
- mailbox pending/ACK counts。
- exact pane/process liveness。
- worktree path/branch/base/head/dirty/delivery state。
- 建議的 attach、inspect、resume command。

`resume` 不是只寫 liveness：

- team 還活著：inside tmux 用 `switch-client/select-window`，outside tmux 用 exact attach。
- worker 已死但 task 尚未 terminal：
  - 驗證原 worktree receipt 與 dirty state。
  - 可安全續跑時以新 generation 重啟該 worker。
  - dirty／identity drift 時保留 worktree並回報 blocked recovery，不得覆寫。
- session 不存在但 durable state 完整：重建 owned panes，重新派送未完成 task。

`stop`／`shutdown`：

- 先寫 durable shutdown request，給 worker bounded graceful window。
- 再只 kill owner token、exact pane ID/PID/session identity 都吻合的剩餘 process。
- 有 `in_progress` task 時，非 `--force` 要回傳非零與阻塞原因。
- 預設保留 state、mailbox 與 worktrees以供 readback；真正 cleanup 是另一個明確動作。
- 永遠不用 `pkill -f`。

## Gap table：相對 OMX `$team` 的 user-facing launch must-haves

| Must-have | OMX `$team` reference | OMG 目前 | P0 缺口與驗收 |
|---|---|---|---|
| 一行 launch grammar | `omx team 3:executor "..."` | 只有 verbose subcommands；shorthand parse 直接失敗 | `omg team 3:executor "..."` 可 parse、launch，legacy grammar 不破壞 |
| In-session entry | `$team` workflow／keyword | 無 `omg-team` skill，rules/router 無 team | 新增 `omg-team` skill + natural-language route；literal `/team` 只在 host alias live proof 後宣稱 |
| 可見 tmux split panes | leader window + worker panes | detached session + 每 task `new-window` | inside tmux 使用 `split-window`；outside TTY 建 session 後 attach |
| 真實 agent process | Codex/Claude workers 在 pane 中執行 | plane 可以組 provider argv，但最後一次 smoke 沒有 live proof | 每 pane 有 exact PID/command，實際 `grok` 完成可觀察的 repo task |
| Dedicated worktrees | 每 worker 自動 worktree | 有 worktree primitives，但 shorthand 無法提供 `owned_files` | 每 worker 真 git worktree；discover scope 在 seal 時綁 changed paths，衝突 fail closed |
| Durable launch state | launch 前建立 config/manifest/tasks/mailbox/workers | plane `team.json`、API store、native state 分離 | canonical runtime 在起 pane 前原子初始化單一 team control plane |
| Worker mailbox/task API | worker ACK、claim、transition | P0 ops 存在，但 worker context 被 blanket deny | identity-bound worker allowlist；live ACK/claim/complete |
| Goal → task board | count + goal 即可啟動 | 要求 caller 先供 `--tasks-json` | deterministic decomposition／approved artifact handoff；dependency 不偽平行 |
| Startup readiness | panes ready 後才報 started | 有 pane metadata，沒有 mailbox ACK gate | exact pane/process + N 個 durable ACK 後才 `running` |
| `status` | 聚合 tasks/workers/mailbox/worktrees | 主要讀 `team.json`；API summary liveness 固定 false | 一個 snapshot 聚合所有 authoritative stores與 live proof |
| `resume` | 依 durable state 恢復／重連 | 只 reconcile liveness | attach/switch；安全時 relaunch dead incomplete worker，否則 blocked readback |
| `stop/shutdown` | graceful coordination + exact teardown | 有 hardened kill，但未連 task/mailbox shutdown | shutdown request、terminal gate、exact teardown、state/worktree 保留 |
| 真實 parity evidence | live panes + state + work output | tests 明寫 no live tmux／dry-run only | dedicated no-dry-run Grok live smoke；無此 evidence 不得移除 experimental 標籤 |

## Ordered PR slices

### PR 1 — 鎖定 user contract 與 CLI grammar

Files：

- Add `omg_cli/team/cli.py`
- Modify `omg_cli/main.py`
- Add `tests/test_team_cli.py`
- Modify `tests/test_cli_router.py`
- Modify `docs/plans/2026-07-25-omx-team-launch-ux-p0.md` only if review decisions change

內容：

- 實作 argv normalization 與公開 `launch` parser。
- 定義 count/role/goal validation、reserved action、legacy compatibility。
- `status|resume|stop|shutdown` 接受 team/run positional identity，同時保留 `--run`。
- 此 slice 只鎖 grammar，不宣稱 live team 已完成。

Acceptance：

```bash
python3 -m pytest -q tests/test_team_cli.py tests/test_cli_router.py
python3 -m omg_cli.main team --help
python3 -m omg_cli.main team 0:executor x       # expected non-zero
python3 -m omg_cli.main team 3:unknown x        # expected non-zero
```

Evidence 必須顯示 help 的 canonical usage 為 `omg team [N[:role]] "<goal>"`，而不是只有 verbose `start --tasks-json`。

### PR 2 — 統一 canonical state、task board 與 worktrees

Files：

- Add `omg_cli/team/runtime.py`
- Add `omg_cli/team/state.py`
- Add `omg_cli/team/decomposition.py`
- Modify `omg_cli/team/api.py`
- Modify `omg_cli/team/worktree.py`
- Modify `omg_cli/contracts/state_schemas.py` if receipt schema validation needs version 2
- Add `tests/test_team_runtime.py`
- Add `tests/test_team_decomposition.py`
- Modify `tests/test_team_api.py`
- Modify `tests/test_team_worktree.py`

內容：

- 建立 canonical launch transaction 與 team-name lookup index。
- launch 前初始化 config/manifest/tasks/mailbox/worker inbox。
- 加入 worker identity + operation matrix。
- 加入 `declared|discover` scope；重疊 changed path fail closed。
- legacy `omg_cli/workers.py` ownership invariant 不修改。

Acceptance：

```bash
python3 -m pytest -q \
  tests/test_team_runtime.py tests/test_team_decomposition.py \
  tests/test_team_api.py tests/test_team_worktree.py
```

必備 assertions：

- 第一個 worker process launch callback 被呼叫前，所有 state與 worktree receipts 已存在。
- worker 可以 ACK/claim/transition 自己的 task，但不能偽造其他 worker identity 或寫 arbitrary inbox。
- 兩個 discover worktrees 改同一路徑時，第二個 seal/integrate 明確失敗。
- team-name index 只能 resolve，不能成為第二個 mutation authority。

### PR 3 — 真實 split-pane transport 與 startup ACK

Files：

- Add `omg_cli/team/tmux.py`
- Modify `omg_cli/team/runtime.py`
- Modify `omg_cli/team/plane.py`，讓 legacy start 轉接 shared tmux primitives
- Modify `omg_cli/team/providers.py`
- Modify canonical worker prompt builder
- Add `tests/test_team_tmux.py`
- Add `tests/fixtures/team_worker.py`
- Add `scripts/live_team_transport_smoke.py`

內容：

- inside tmux 使用同 window `split-window`，outside TTY 建 session + attach。
- 每個 pane 存 exact identity 與 owner token。
- worker prompt 實作 inbox → ACK → claim → work → commit → transition。
- `Team started` 前 bounded wait N 個 ACK。
- start failure exact rollback；不可 fallback 到 dry-run或 `spawn_subagent`。

Acceptance：

```bash
python3 -m pytest -q tests/test_team_tmux.py tests/test_team_runtime.py
python3 scripts/live_team_transport_smoke.py --workers 2
```

fixture smoke 只證明 transport/state transaction，不算 Grok parity；它必須證明同一 window 有 2 個新增 pane、2 個真 process、2 個 worktrees與2個 ACK。

### PR 4 — `status`／`resume`／`stop` lifecycle

Files：

- Modify `omg_cli/team/runtime.py`
- Modify `omg_cli/team/state.py`
- Modify `omg_cli/team/api.py`
- Modify `omg_cli/team/tmux.py`
- Modify `omg_cli/main.py`
- Add `tests/test_team_lifecycle.py`
- Modify `tests/test_team_plane.py`

內容：

- status 聚合 task/mailbox/pane/process/worktree。
- resume 支援 attach/switch、dead incomplete worker generation restart與 blocked dirty recovery。
- stop/shutdown 先 durable request，再 exact graceful/forced teardown。
- legacy `team resume --run` 與 `team stop --run` 仍可用。

Acceptance：

```bash
python3 -m pytest -q \
  tests/test_team_lifecycle.py tests/test_team_plane.py tests/test_team_api.py
python3 scripts/live_team_transport_smoke.py --workers 2 --exercise-resume-stop
```

必備 assertions：

- kill 一個 fixture worker 後，`status` 顯示 degraded，`resume` 只重啟該 worker並增加 generation。
- dirty worktree 不會被 resume 覆寫。
- non-force stop 在 active claim 時失敗；force stop 只殺本 team exact panes。
- stop 後 state/mailbox/worktree receipts 仍可讀。

### PR 5 — Grok in-session skill、routing 與 docs

Files：

- Add `skills/omg-team/SKILL.md`
- Modify `skills/omg-using/SKILL.md`
- Modify `templates/omg-rules.md`
- Modify `templates/AGENTS.fragment.md`
- Modify `docs/skills.md`
- Modify `docs/skills.zh.md`
- Modify `docs/skills.zh-TW.md`
- Modify `README.md`
- Modify `plugin.json`
- Modify `scripts/check_docs_links.py`
- Modify `tests/test_skill_inventory.py`
- Modify `tests/test_plugin_session_discovery.py`
- Modify `tests/test_docs_cli_drift.py`
- Regenerate `omg_capabilities.lock.json`

內容：

- skill 明確區分 durable tmux team 與 `spawn_subagent`/ULW。
- rules 增加精準例外：一般 fan-out 仍是 `spawn_subagent`；使用者明確要求 `team`／tmux panes 時呼叫 canonical `omg team`。
- skills count 由 15 更新為 16，三語 docs 同步。
- 設定完成後 `omg setup` 必須可 idempotently 更新 global rules。
- 查證並測試 literal `/team` alias；無 host proof 就只文件化 namespaced skill + natural route。

Acceptance：

```bash
python3 scripts/generate_capabilities_lock.py
python3 scripts/generate_capabilities_lock.py --check
python3 scripts/check_docs_links.py
python3 -m pytest -q \
  tests/test_skill_inventory.py tests/test_plugin_session_discovery.py \
  tests/test_docs_cli_drift.py tests/test_guidance.py
```

另需 fresh installed-plugin readback，證明 `omg-team` 出現在 Grok session discovery；repo 內只有 `SKILL.md` 不算安裝成功。

### PR 6 — 真實 Grok live smoke、promotion 與 release gate

Files：

- Add `scripts/live_team_smoke.py`
- Modify `scripts/live_suite.sh`
- Modify `.github/workflows/release.yml`
- Modify `docs/RELEASE.md`
- Modify `docs/RELEASE.zh.md`
- Modify `docs/RELEASE.zh-TW.md`
- Add sanitized evidence under `docs/research/live/`
- Modify `omg_cli/team/plane.py`／`omg_cli/team/api.py` gate policy only after smoke passes

Live scenario 必須使用真實 `grok`，不得設定 fixture executor，不得使用 `--dry-run`：

```bash
python3 scripts/live_team_smoke.py \
  --workers 2 \
  --role executor \
  --goal "worker 1 and worker 2 each complete an assigned marker change and test"
```

script 自己 fail closed 驗證：

- canonical command 真的是 `omg team 2:executor "<goal>"`。
- `dry_run == false`。
- 同一 team window 的 exact worker pane count 是 2。
- pane command/process identity 是 production `grok` provider，不是 fixture或 shell placeholder。
- 2 個 dedicated `git worktree` 存在且 base SHA 正確。
- 2 個 durable ACK。
- task board 出現 claim → `in_progress` → terminal transitions。
- worker 真的產生 commits／files／test evidence；不是只輸出文字。
- `status --json` 與 filesystem/tmux readback 相符。
- 至少殺掉一個 worker並由 `resume` 正確恢復未完成工作。
- `stop`／`shutdown` 後 owned panes/session 消失，其他 tmux panes不受影響。
- durable state、mailbox與worktree evidence仍可讀。

只有此 live smoke 通過後：

- canonical Grok shorthand 才能移除 `OMG_EXPERIMENTAL_TMUX_TEAM=1` gate。
- routed multi-CLI、legacy staged `team run` 可以繼續保留 experimental gate。
- release notes 才能寫「OMX-like team launch P0」。

若 credentials、quota 或 host slash alias 阻擋 live gate，PR 可合併為 experimental，但不得宣稱 user-facing parity 或把 dry-run結果當 promotion evidence。

## Final acceptance commands

```bash
# Current CI-equivalent gates
python3 scripts/check_parity_inventory.py
python3 scripts/check_traceability.py
python3 scripts/generate_standalone_hook.py --check
python3 scripts/generate_capabilities_lock.py --check
python3 scripts/check_docs_links.py
python3 -m pytest -q -m "not live" --tb=short
python3 -m compileall -q omg_cli
./scripts/smoke.sh

# Canonical UX
omg team --help
omg team 3:executor "fix flaky tests"
omg team status <team-name> --json
omg team resume <team-name> --json
omg team stop <team-name>

# Mandatory release/promotion proof
python3 scripts/live_team_smoke.py \
  --workers 2 \
  --role executor \
  --goal "complete two independent marker changes with tests"
```

`ruff`／`mypy` 的 CI file list必須加入本 P0 新增的 runtime、CLI、state、tmux 與 tests；不能因目前 CI 只列舊檔而漏掉新核心檔案。

## 明確不做的事

- 不把 `team api` 有 11 個 ops 說成 launch UX 已完成。
- 不把 `--dry-run`、mocked `subprocess`、fake tmux或 fixture worker 當成 Grok live proof。
- 不把 detached `new-window` metadata 說成使用者已看到的 OMX-style split panes。
- 不把 `madmax` 當 team；`madmax` 是 host launcher／permission posture，不提供 team state、mailbox、tasks與worktree lifecycle。
- 不把 `spawn_subagent` 當 team；它是 Grok native depth-1 fan-out，沒有 durable tmux panes與 team resume/shutdown contract。
- 不把 `omg ulw` 或 `team run --ralph` 改名成 team parity；workflow persistence 與 durable pane team 是不同產品面。
- 不在 tmux 失敗時自動降級成 dry-run、ULW或 `spawn_subagent`，然後仍回傳成功。
- 不在 worker API 仍被 blanket deny 時聲稱 mailbox/task coordination 已接通。
- 不建立第三套 authoritative team state；lookup index 只做 identity resolution。
- 不放寬 legacy ownership，使空 `owned_files` 等同於可修改整個 repo。
- 不以 `mkdir` 或 copy checkout 假裝 git worktree；必須有 `git worktree list`、branch/base SHA與receipt proof。
- 不因新增 skill 檔就聲稱 literal `/team` 可用；必須有 installed Grok session readback。
- P0 不追 full OMX 33-op API parity、dynamic scaling parity、OMA／OMCU shorthand或所有 provider 的 interactive feature parity；這些是後續 slices，不能阻塞 OMG 的核心 launch UX。

## Stop condition

本 P0 只有在「一行 shorthand → 真實可見 tmux workers → dedicated worktrees → worker ACK/claim/work/commit → durable status → resume → exact stop」由同一個非 dry-run live run 證明後才算完成。任何一段只有文件、state skeleton、mock、dry-run或人工推測，整體 verdict 仍是 **experimental，尚未 OMX-like launch parity**。
