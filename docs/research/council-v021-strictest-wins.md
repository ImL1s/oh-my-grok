# Council v0.2.1 — Strictest Wins 合成裁決

**日期:** 2026-07-19  
**Repo 快照:** oh-my-grok `main` @ `cca7d34` · `plugin.json` version **0.2.2**  
**輸入:**

| 來源 | 路徑 | 立場摘要 |
|------|------|----------|
| Fable plan | `.omg/research/council-v021/fable-plan.md` | 20 gaps 可關；骨架可出；P0→P2 依序施工 |
| Codex plan | `.omg/research/council-v021/codex-plan.md` | **NO-GO** 發布 v0.2.1/0.2.2；骨架 ≠ 封口 |
| 先前 orchestrator 合成 | `docs/research/council-v021-synthesis.md` | 列已落地 + 誠實 residual（未等 Codex 完稿） |
| 現況掃描 | `omg_cli/*`、`agents/*`、`skills/*`、`scripts/smoke.sh`、`README.md` | 見 §2 Implemented-now |

**裁決規則（strictest wins）:** 兩邊有共識則採共識；有衝突時採**更嚴的一方**（通常 Codex）。「文件寫了 / dry-run 骨架有了」不算 gap 關閉；需 fail-closed 測試、live canary 或 feature-gate。

---

## 1. Combined verdict（綜合裁決）

### 1.1 一句話

**目前 NO-GO 以 v0.2.1 規格宣告「20 gaps 已關」；亦 NO-GO 以 `plugin.json` 0.2.2 暗示 isolation/pipeline 已就緒。**  
骨架進度真實且可貴（P0 state/accept 大半、ask/pipeline/dual-review CLI、smoke、flock、range cherry-pick），但 Codex 指出的 release blockers **多數仍成立**，且 0.2.2 新增的 **process fanout** 在 strictest 規則下是 **regression surface**（與 README「fan-out only via `spawn_subagent`」硬規則衝突）。

### 1.2 共識（Fable ∩ Codex）

雙方一致、必須遵守：

1. **Hooks 是 fail-open soft-gate** — 不得 market 成 hard sandbox。  
2. **Worker 硬邊界 = `capability_mode` 移除 Execute**（`read-write` / `read-only`），不是 regex deny。  
3. **`.omg/state` / `verified` / `passes` 僅 omg CLI 可寫。**  
4. **外部 advisor 僅 `omg ask`、user-invoked**；不進 worker 路徑、不自動 ingest pipeline。  
5. **無 tmux control plane**；平行主路徑是 Grok `spawn_subagent` depth=1。  
6. **不 fork grok-build**；只用官方 flags / config / plugin surface。  
7. **Live PreToolUse canary 尚未有 dated 證據**（spike 表仍空）。  
8. **Acceptance allowlist ≠ sandbox** — 核准 runner 仍可執行 repo code。

### 1.3 衝突 → Strictest 勝出

| 議題 | Fable | Codex | **Strictest 裁決** |
|------|-------|-------|-------------------|
| 可否標記 v0.2.1 完成 | 可關、按 PR 順序出 | **NO-GO** | **NO-GO** 直到 §4 checklist 全綠 |
| Process fanout (`--fanout process`) | 次要 supervisor，可出 experimental | 違反 hard rule；workers 保留 shell；應移除/gate | **移除或 feature-gate 出公開 surface**；不得當 gap 15/16 正解 |
| 無 shell worker 如何 commit | skill + envelope；worker 可能需 shell | 必須 **`omg worker prepare/seal`** broker | **必須 seal bridge**；不可要求 read-write worker 自己 `git commit` |
| Tool ID | `run_terminal_command` | headless flag 應為 **`run_terminal_cmd`** | **未 live 驗證前不得宣稱 shell 已移除**；雙 ID 都要測 |
| Acceptance | basename allowlist 足夠 v0.2.1 | 需 **semantic argv grammar**；`python -c` / `npx` 預設問題 | **語意 policy 為 P0**；basename 僅過渡 |
| Kill 安全 | starttime 比對後 kill | 缺 starttime / ps 失敗 **不得 signal** | **Fail-closed kill**（現況 legacy / ps-fail 仍 True → 開著） |
| Pipeline | plan→exec→accept 可出 | 缺 intake / seal / integrate / report；未整合不可 review | **完整 FSM 或 gate「不完整」階段** |
| Dual-review | 兩次 grok / skill 可 | 非 native spawn；公開則 P0 | **公開 surface 須 native 或 feature-gate** |
| Ask | 固定 argv 表 | prompt-in-argv、`--extra`、輸出 RAM | **stdin/固定 adapter/cap 或 gate** |
| Personas | host 原生；文件+範本 | setup 安裝 templates；plugin 不含 persona | **setup-owned templates + live discovery** 才算關 |

### 1.4 與「版本號」的關係

- `plugin.json` **0.2.2**、README 已寫 v0.2.1/0.2.2 功能表 → **行銷/版本敘事已超前於 gap closure**。  
- Strictest：**版本前進不是證據**。下一版建議標為 **0.2.3 = 誠實收斂**（修 blockers + gate 未完成 surface），而非再 bump 新功能。

### 1.5 對「出貨」的定義（本文件）

| 標籤 | 意義 |
|------|------|
| **Ship in 0.2.3** | 必須修、gate 或誠實降級後才可 tag |
| **Deferred** | 明確延後，且不得在 README 宣稱已完成 |

---

## 2. Gap 1–20 對照表

欄位說明：

- **Fable** — Fable 建議/樂觀狀態  
- **Codex** — Codex 現況與完成條件（較嚴）  
- **Implemented-now** — 本 repo 現況（2026-07-19 程式碼掃描）  
- **Still open** — strictest 下仍未關的部分  

| # | 主題 | Fable | Codex | Implemented-now | Still open |
|---|------|-------|-------|-----------------|------------|
| **1** | install + smoke | `install.sh` + dry/live matrix | scripted install；smoke fail-fast；fresh HOME | `scripts/smoke.sh` dry 路徑有；**無** `install.sh`；doctor/validate 用 `\|\| true` 吞失敗；無 live matrix | 一鍵 install；smoke **fail-fast**；live opt-in；install smoke 測試 |
| **2** | `doctor --strict` 綠燈 | 修 parser + fixtures | inventory + effective components + compat cells；fresh-HOME green | doctor 能 list/inspect；compat 檔案掃描；**本機 strict 未證明 e2e 綠**；無完整 fixture 綠燈路徑 | installed+trusted+effective inventory strict-green 證據；compat 以 `externalCompat` 為準 |
| **3** | PreToolUse parent/child canary | 腳本 + 預期會觸發 | **shim** canary、dated JSON、永不真跑 claude/codex | spike 有 host source 證據；**live 表空**；示範指令仍像真 CLI | dated live canary + marker absence；hook crash fail-open 負向證據 |
| **4** | Worker 無 shell 外 agent | capability + frontmatter + argv `--disallowed-tools` | spawn **強制** `capability_mode`；tools allowlist；sandbox deny state | skills/agents **文件層** capability；executor 只 `disallowedTools: spawn_subagent`（**未** ban shell）；`build_grok_argv(disallow_shell)` 僅 dual-review/ralplan RO；`DISALLOW_SHELL_TOOLS=run_terminal_command`（tool ID 爭議） | 程式保證 spawn 參數；executor 無 Execute；正確 headless tool ID live 證據；state path deny |
| **5** | Interpreter escape | capability 主力 + deny.shlex 輔助 | **不靠更大 regex**；worker 無 shell + semantic acceptance | deny 仍 regex（cmd pos / sh -c）；**無** python -c / npx 語意擋；acceptance 允許 python/npx basename | semantic command policy；worker 無 shell 閉環（含 seal）；誠實 security-model 文件 |
| **6** | 誠實定位 | README 三層模型 | canonical `docs/security-model.md` + 全站一致 | README Isolation stack 已較誠實；**無** security-model.md；skills 仍可能過度暗示 hard ban | 單一 truth table；contract test 禁未限定「hard blocked」 |
| **7** | Accept allowlist | basename + `--allow-cmd` 入 manifest | executable + **argv grammar**；`--no-allowlist` 封閉/TTY break-glass | basename allowlist + always-deny + shells 拒 argv0；`--allow-cmd`；**公開 `--no-allowlist`**；**npx/node/python 在預設 allow**；**未**擋 `python -c` | 語意 policy；override 進 SHA；移除/封閉 `--no-allowlist`；拒 interpreter -c/-e |
| **8** | `--review` / `--yes` | TTY y/N；非 TTY 需 yes | 印 cwd/manifest/policy；TTY 確認；yes 不繞 policy | `--review` 印 argv；非 TTY 需 `--yes`；**缺**完整 digest/policy 展示與 TTY 單次確認（實作偏「review 也要 --yes」） | UX 對齊：TTY 確認、digest/policy 快照測試 |
| **9** | create_run flock | flock + Windows fallback | 完整 transaction；**no-fcntl 不可 silent unlock** | POSIX `fcntl.flock` 有；concurrent test 有；**fcntl=None → `_create_run_unlocked`** | no-fcntl fail-closed 或 O_EXCL lease；lock metadata |
| **10** | integrate worktree 護欄 | project / `.omg/worktrees` + `git worktree list` | worktree **identity**（真 worktree top、common dir）；broker 簽發 path | resolve 後 prefix allowlist；symlink 外逃可擋；**未**驗 git worktree list / common-dir | 真 worktree 身份；非 worktree 子目錄拒；broker path |
| **11** | multi-commit | `base..head` range pick + `--require-squash` | ancestor、topo order、拒 merge、**changed_files 比對** | `base_sha..head_sha` cherry-pick + partial reset；**無** ancestor 預檢、merge 拒、changed_files 強制比對、`--require-squash` flag | ancestry/order/merge/changed_files；require-squash |
| **12** | pid + starttime kill | 寫 pid.json；不符不 kill | 缺 starttime / ps 失敗 → **不 signal**；legacy 不自動 kill | 寫 `{pid,starttime,pgid}`；**legacy 無 starttime → True**；**ps 失敗 → True**（best-effort kill） | fail-closed kill；Linux `/proc`；legacy migration |
| **13** | multi-PID workers | `workers/*.pid.json` + cancel 迭代 | **通用** register/unregister；**不**靠違規 process-fanout 證明 | fanout 寫 `workers/*.pid.json`；cancel 掃 workers；sequential launch 仍覆寫 leader pid 邏輯殘存 | CLI-owned registry lifecycle；與 fanout 脫鉤；native child PID 誠實不可見 |
| **14** | `omg ask` | 固定 argv、child env、artifact | immutable adapter；**prompt 不進 argv**；去任意 `--extra`；串流 cap；agy≠gemini 錯 alias | broker 存在；child `OMG_ALLOW_EXTERNAL_CLI`；shell=False；artifact；**prompt 在 argv**；**`--extra` 有限過濾仍 passthrough**；`agy→gemini` alias；輸出完整 capture 後截斷 | stdin/file prompt；固定 adapter；移除 free `--extra`；串流上限；alias 修正 |
| **15** | 無 tmux 平行 | spawn 主 + `omg swarm` 次 | **僅** native spawn + **prepare/seal**；禁 N× grok -p | 預設 skill=spawn；**`--fanout process` 公開**且 `disallow_shell=False` | **gate/移除 process fanout**；worker prepare/seal；3-worker live 平行證據 |
| **16** | v0.x ship 哪條平行 | Primary+Secondary experimental | **只 ship native**；隱藏 N× supervisor | README **同時**寫 spawn-only **與** process fanout | 文件/CLI 單一真相；contract test 無公開 N× worker |
| **17** | `omg pipeline` FSM | interview?→ralplan→ulw\|ralph→accept→report | intake→plan→execute→**seal→integrate**→review→accept→**report**；共享 run_id | `plan → implement → dual_review → accept`；可 resume 骨架；**無** intake/seal/integrate/report/needs_input | 完整 stage；ULW 未 integrate 不進 review/accept；report artifact |
| **18** | pipeline 權威邊界 | CLI FSM；skill 只教 | CLI 唯一 writer；skill 不複製 state machine | CLI + `skills/omg-pipeline` 對齊方向；FSM 未完整 | stage 名稱 contract test；skill 禁寫 state |
| **19** | dual-review 原生 | skill / 可選 CLI；Grok agents | **單一 leader + spawn_subagent**；child IDs/capability 證據；否則 gate | `omg dual-review` = **兩次** headless `grok -p` + agent body prompt；`disallow_shell=True` 但 tool ID 待證 | native spawn 重構 **或** feature-gate；live toolset 負向 canary |
| **20** | personas `[[inputs]]/[[outputs]]` | host 原生；研究文件+範本 | setup 複製到 `.grok/personas|roles`；plugin **不含** persona | **無** templates/grok；**無** setup `--with-review-templates` | setup-owned TOML；discovery live；I/O 進 dual-review |

### 2.1 現況功能掃描（Implemented surface）

| 表面 | 狀態 | 備註 |
|------|------|------|
| `omg ulw` / `ralph` / `ralplan` | 有 | modes + skills |
| `omg accept` | 有 | allowlist / review / yes / in-process token |
| `omg integrate` | 有 | path allowlist + range cherry-pick + atomic reset |
| `omg cancel` / `state` / `doctor` / `setup` | 有 | cancel 含 workers 掃描骨架 |
| `omg ask` | 有 | codex/claude/gemini；user broker |
| `omg pipeline` | 有 | plan→implement→dual_review→accept |
| `omg dual-review` | 有 | 雙 headless session，非 spawn |
| `omg ulw --fanout process` | 有（**爭議**） | multi-PID；workers 保留 shell |
| `omg worker prepare/seal` | **無** | Codex 關鍵 bridge 未做 |
| `scripts/install*.sh` | **無** | 僅 `smoke.sh` |
| `docs/security-model.md` | **無** | |
| persona/role templates | **無** | |
| Live canary script (shim) | **無** | spike 手測程序 only |

---

## 3. Ship in 0.2.3 vs Deferred

版本敘事建議：**0.2.3 = strictest blockers 收斂 + 公開 surface 誠實化**（可含小功能，但以 gate/修正確性為主）。

### 3.1 Ship in 0.2.3（必須）

| 項目 | 對應 gap | 理由（strictest） |
|------|----------|-------------------|
| **Feature-gate 或移除 `--fanout process`** | 15, 16, 13 | 違反「fan-out only via spawn_subagent」；測試鎖定 worker 有 shell = isolation regression |
| **Fail-closed PID kill** | 12 | 缺 starttime / ps 失敗不得 signal；legacy plain pid 僅顯示或 break-glass |
| **Accept semantic floor** | 5, 7 | 至少：拒 `python|node -c/-e`、預設移出/嚴格 `npx`、shell wrappers；`--no-allowlist` TTY-only 或移除 |
| **`--review` UX 對齊** | 8 | TTY 確認路徑清晰；digest/policy 可見；yes 不繞 policy |
| **Integrate hardening 最小集** | 10, 11 | ancestor 檢查 + changed_files 比對（或明確 fail）；可選 `--require-squash` |
| **no-fcntl 不 silent unlock** | 9 | O_EXCL lease 或 fail closed + 文件 POSIX-only |
| **disallow shell tool ID 釐清** | 4, 19 | live 驗證 `run_terminal_cmd` vs `run_terminal_command`；RO 階段雙寫或改正確 ID |
| **Smoke fail-fast + install 腳本** | 1 | dry matrix 失敗 → 非零；`install-plugin.sh` 固定 validate→install→inventory |
| **Doctor strict 可重現綠燈路徑** | 2 | fixture 或 documented fresh-HOME 步驟；compat 不誤殺無效 cell |
| **README / 版本誠實化** | 6, 16 | 刪除或標記 experimental 的過大 claim；對齊 isolation 真相表 |
| **Pipeline：ULW 必經 integrate 或明確 skip flag** | 17 | 未整合成果不得默認進 dual-review/accept |
| **Ask：去危險 passthrough 或 gate** | 14 | 最少：限制/移除 `--extra`；文件 prompt-in-argv residual；修正誤導 alias |
| **Dual-review：gate 或標 experimental + tool ID 修** | 19 | 未 native 前不得宣稱 spawn dual-agent |
| **Process registry 與 fanout 脫鉤** | 13 | register API 給合法 leader/ask；cancel 不依賴違規 fanout 當唯一 writer |

### 3.2 Deferred（明確延後 + 原因）

| 項目 | 對應 gap | 延後原因 |
|------|----------|----------|
| **完整 `omg worker prepare/seal` + 3-worker live matrix** | 4, 5, 15 | 正確設計但工作量 L；0.2.3 可先 **禁 process fanout + 文件 capability 預設**，seal 進 0.2.4 |
| **Shim live canary 進 CI** | 3 | 需 trusted plugin + 本機 grok session；0.2.3 交付腳本 + 空表結構，CI live 可 optional job |
| **OS custom sandbox profile deny `.omg/state/**`** | 4 | host profile 未穩定文件化；defense-in-depth 非封口前提 |
| **完整 pipeline intake / needs_input / report digests** | 17, 18 | FSM 擴充 L；0.2.3 先補 integrate 順序硬條件即可 |
| **Ask stdin secrecy + 串流 cap + immutable adapters 全量** | 14 | L；0.2.3 先砍 elevation surface，完整 broker 0.2.4 |
| **Native spawn dual-review + persona I/O** | 19, 20 | 需 setup templates + leader 重構；0.2.3 feature-gate，0.2.4 重做 |
| **Personas/roles setup install** | 20 | plugin 官方 component 不含 persona；屬 setup 擴充 |
| **tmux display layer** | 15 | 永不做 control plane；display 非優先 |
| **seccomp / 未文件化 `--sandbox` 預設開啟** | 5 | research only |
| **自動 goal 分解 swarm** | 15 | 雙方 DO NOT DO：supervisor 不吃未切片 goal |

### 3.3 建議 0.2.3 PR 順序（strictest 壓縮 Fable PR1–5 + Codex PR1–5）

1. **release gate + honesty** — smoke fail-fast、install 腳本、README/fanout 敘事、security residual 表（gaps 1, 6, 16）  
2. **kill + lock fail-closed** — starttime、no-fcntl、workers registry 骨架脫鉤（9, 12, 13）  
3. **accept policy + review UX**（5, 7, 8）  
4. **integrate ancestry/changed_files + pipeline integrate gate**（10, 11, 17）  
5. **gate process-fanout / dual-review / ask extras；tool ID canary**（4, 14, 15, 19）  

之後 0.2.4：prepare/seal、native dual-review、personas、完整 pipeline report。

---

## 4. Release checklist status

對照 Codex「最終 release checklist」+ 現況掃描。圖例：`[x]` 已滿足 · `[~]` 部分 · `[ ]` 未滿足。

| # | 檢查項 | 狀態 | 證據 / 缺口 |
|---|--------|:----:|-------------|
| 1 | `grok plugin validate .` hard pass | [~] | smoke 有跑但 **non-fatal**（`\|\| true` / WARN） |
| 2 | fresh HOME：install `--trust` → list/inspect → `omg doctor --strict` exit 0 | [ ] | 無 scripted install；無 e2e 綠燈證明 |
| 3 | parent/child shim live canary：deny 生效、marker 不存在 | [ ] | spike live 表空；無 shim 腳本 |
| 4 | worker capability live matrix：implementer 可寫不可 Execute/spawn；reviewer 只讀 | [ ] | 僅 skill/prompt；executor 仍可 shell；無 live matrix |
| 5 | semantic acceptance + review UX 全綠；untrusted runner 風險文件化 | [~] | basename allowlist + review flags 有；**語意 -c/-e 未擋**；npx 在預設 allow |
| 6 | ULW 3-worker prepare/edit/seal/integrate 實測；multi-commit/conflict 證據 | [~] | range cherry-pick + rollback **單元測試向**；**無** prepare/seal、無 3-worker live |
| 7 | 公開 CLI **無** N× full Grok process workers；平行僅 native spawn | [ ] | **`omg ulw --fanout process` 公開**（main.py + fanout.py） |
| 8 | PID reuse / unknown identity 不誤殺；registry 取消 CLI-owned processes | [~] | 有 starttime 欄位；**legacy/ps-fail 仍可能 kill**；registry 不完整 |
| 9 | pipeline Ralph/ULW complete/resume/needs_input/report；僅 CLI accept 可 verified | [~] | plan→implement→dual_review→accept 有；**缺** needs_input/report/integrate 順序；verified 仍僅 accept token 路徑（好） |
| 10 | ask 與 dual-review 完成安全重構，或 v0.x **明確隱藏/feature-gate** | [ ] | 兩者皆**公開**且未達 Codex 完成條件 |
| 11 | README/doctor/skills 與 canonical security model 一致 | [~] | README 較誠實；**無** `docs/security-model.md`；fanout 敘事自相矛盾 |
| 12 | Hooks / capability **不**被 market 成 hard sandbox | [~] | README 有 fail-open；部分 skill 用語仍需掃 |

### 4.1 GO / NO-GO

| 問題 | 裁決 |
|------|------|
| 可否宣告 **v0.2.1 gaps 全關**？ | **NO-GO** |
| 可否以 **0.2.2 為 isolation-complete release**？ | **NO-GO** |
| 可否在 0.2.3 **只**做文件 bump 不修 blockers？ | **NO-GO** |
| 0.2.3 若完成 §3.1 且 checklist 1–8、10–11 達標（9 允許 report 延後但 integrate gate 必須）？ | **條件 GO** |

### 4.2 不得宣稱的句子（DO NOT CLAIM）

直到對應證據存在：

- 「workers cannot run external CLIs」（除非 live capability matrix）  
- 「PreToolUse blocks all children」（除非 dated canary + 仍須附 fail-open）  
- 「process fanout is the supported parallel path」  
- 「dual-review is native multi-agent isolation」  
- 「acceptance allowlist is a security sandbox」  
- 「`plugin.json` 0.2.x 表示 council gaps 已關閉」

---

## 5. 附錄：與前一版 synthesis 的 delta

| `council-v021-synthesis.md`（orchestrator 早先） | 本文件（strictest） |
|------------------------------------------------|---------------------|
| 列 v0.2.1/0.2.2 已實作骨架 | 骨架承認，但 **≠ release** |
| process fanout 當 v0.2.2 功能 | **blocker / 應 gate** |
| residuals 以 hooks fail-open 為主 | 加上 kill fail-open、tool ID、seal 缺失、pipeline 缺口、ask/dual 公開未完成 |
| 未併入 Codex NO-GO | **採 Codex NO-GO 為總裁決**，Fable 施工順序作 backlog 參考 |

---

## 6. 參考路徑（絕對）

- `<repo-root>/.omg/research/council-v021/fable-plan.md`
- `<repo-root>/.omg/research/council-v021/codex-plan.md`
- `<repo-root>/docs/research/council-v021-synthesis.md`
- `<repo-root>/docs/research/subagent-pretooluse-spike.md`
- `<repo-root>/omg_cli/{main,state,acceptance,integrate,modes,fanout,pipeline,dual_review}.py`
- `<repo-root>/omg_cli/ask/{broker,providers}.py`
- `<repo-root>/scripts/smoke.sh`
- `<repo-root>/plugin.json`（version 0.2.2）

---

*本文件僅研究/裁決產物；未改產品程式碼。*
