# External free audit — Codex

> 審計日期：2026-07-20（Asia/Taipei）  
> OMG 基準：oh-my-grok 0.2.5，HEAD <code>60d0882491ce3bd2a9dc22ef2f40852c58fcbf68</code>  
> Host：<code>grok 0.2.106 (bde89716f679) [stable]</code>  
> OMC 參考：oh-my-claudecode 4.15.5  
> 性質：獨立、唯讀產品／架構／證據審計；未啟動任何 OMG workflow mode，未修改產品原始碼。

## Executive verdict

### Verdict on「基本都有了？」

**ONLY_IF，而且條件非常窄。**

若「基本」只指以下最小骨架：

1. 有一個 CLI 可以啟動 Grok headless process；
2. 有 run state、PID、cancel；
3. 有一個 bounded outer loop；
4. 有 frozen acceptance command 與 CLI-only verified gate；
5. 有 prompt／agent capability 契約以及一層 fail-open PreToolUse 防線；

那麼答案是 **YES，基本骨架已存在**。

若「基本 OMC 功能」包含使用者通常會合理期待的產品行為：

- 真正可證明的平行 fan-out、隔離 worktree、回收結果與自動 integrate；
- 同一工作上下文持續多輪，而不是每輪重開全新 Grok session；
- 不會把「Do not APPROVE」判成 APPROVE 的 plan／review gate；
- plan → implement → integrate → review → accept 的 live end-to-end；
- 可乾淨歸因為 OMG、沒有 OMC／Claude compatibility 污染的 live 證據；
- deep interview、QA repair loop、durable goal、team runtime 等 OMC-class product surface；

答案是 **NO**。

我的產品判定不是「差一點就 parity」，而是：

> **OMG 已有一個值得保留的 CLI control-plane 原型，accept／cancel／state 的方向正確；但 plan consensus、dual review、pipeline 與 live proof 目前尚未達到可信產品 gate。0.3 的第一任務不是擴功能，而是消滅假綠與建立乾淨、可歸因的證據。**

尤其有兩個會直接改變 council 結論的事實：

1. quota-heavy live log 中，verifier 明寫 **REQUEST CHANGES** 與 **Do not APPROVE**，CLI 卻輸出 <code>verdict=APPROVE</code>，suite 最後仍寫 <code>status=ok</code>。
2. 當前 <code>grok inspect --json</code> 顯示 Grok 同時載入 OMG、OMC 4.15.5、ralph-loop、Claude hooks 與大量 Claude skills；<code>omg doctor</code> 的 plugin isolation 掃描卻回報 Claude plugins 目錄沒有 plugin-like entries。現有 live evidence 沒有保存乾淨 discovery snapshot，因此不能視為 OMG-only 證據。

這兩點都不是「polish」，而是 release-blocking trust defects。

---

## Audit method and evidence hierarchy

本報告不以檔名、skill 名稱或 council 投票數當成功證據，而依以下順序判斷：

1. **原始 live transcript／artifact**：模型實際說什麼、CLI 最後判什麼、suite 是否真的 assert 語意。
2. **runtime source**：終態由哪段程式決定、非零 exit code 是否能壓過 artifact、artifact 是否有 provenance。
3. **host 官方文件**：哪些 hook 能 block、session／subagent resume 能力是否原生存在。
4. **fresh diagnostic／probe**：doctor、grok inspect、純函式與 temp-dir 重現。
5. **unit tests**：只證明測到的 contract；不能壓過相反的 live evidence。
6. **README／prior synthesis**：是 claim，不是 ground truth。

本輪 fresh evidence：

| Probe | 結果 |
|---|---|
| <code>PYTHONPATH=. python3 -m pytest -q -m 'not live'</code> | **286 passed**；但下述假綠仍可直接重現，代表 coverage 有洞 |
| <code>python3 -m omg_cli.main doctor</code> | exit 0；hard checks pass，但 Claude hooks／CLAUDE.md 僅 WARN |
| <code>python3 -m omg_cli.main doctor --strict</code> | exit 1；Claude hooks 與 OMC routing markers 使 isolation strict fail |
| <code>grok inspect --json</code> | Claude compat cells 全部由 default 啟用；OMC、ralph-loop 等 plugins 與 skills 實際載入 |
| dual missing-binary temp probe | stage launch rc=127；CLI 生成的 non-dry stub 含 APPROVE；parser 回傳 **APPROVE** |
| ralplan negation probe | <code>Do not APPROVE this plan yet.</code> 被判為 **approved=True** |
| acceptance restart probe | 同 process accept 後 verified=True；清除 process-local token，再做一次 status write 後 verified=False |

測試綠與產品可信是兩件不同的事。這個 repo 正好提供了一個很清楚的例子：286 tests 全綠，live suite 也綠，但 review verdict 仍然假綠。

---

## Independent scorecard

| Metric | Score 0–10 | Evidence-backed assessment |
|---|---:|---|
| Core orchestration | **4/10** | CLI state、cancel、accept、FSM 外殼存在；但 ULW 未閉環、Ralph 無 session continuity、ralplan／dual verdict 不可信、pipeline 繼承其缺陷 |
| Full OMC surface | **2/10** | OMG 8 skills／4 agents／4 hook events；OMC 4.15.5 有 41 skills／19 agents／219-line lifecycle hook graph，且有 team、ultraqa、ultragoal、wiki、HUD、notification、memory 等真正 runtime |
| Trust / isolation honesty | **5/10** | security-model 對 fail-open、Stop、leader shell 很誠實；accept gate 很強；但 false APPROVE、doctor discovery blind spot、污染 live harness 與 parent env bypass 拉低分數 |
| Live proof quality | **3/10** | canary、accept、cancel 有價值；但 dual live 假綠未被 suite 擋下，summary 只有 status=ok，且未固定 clean host inventory；pipeline／ralplan／ask／multi-worker ULW 均無 L2 |
| Don’t-stop UX | **3/10** | 有 bounded outer loop 與 run artifacts；但預設 3 輪、每輪 fresh session、無通用 resume、無 recovery policy，離「持續到驗收或清楚 blocker」仍遠 |

這些分數刻意不使用「有幾個同名 command」當分母。產品能力應以閉環與 failure semantics 計分：能不能安全開始、保留上下文、判斷失敗、修復、重試、驗收、恢復與停止。

---

## What the product actually is today

OMG 目前不是一個單一閉合的 orchestration engine，而是三個部分鬆散相接：

1. **TUI／skill orchestration layer**  
   skill 告訴 leader 何時 spawn、用什麼 capability_mode、如何拆工作。這一層大量依賴模型遵守文字契約。

2. **Headless process supervisor**  
   <code>omg ulw</code>、<code>omg ralph</code>、ralplan、pipeline 會開新的 <code>grok</code> process、寫 PID／state／prompt artifact。這一層不直接持有 native subagent IDs，也不觀察 TUI spawn graph。

3. **CLI authority layer**  
   worker prepare／seal、integrate、accept、cancel 與 verified single-writer。這一層是目前最接近產品級的部分。

最大的 seam 是：第一層答應「平行、多 agent、隔離」，第二層只知道「某個 Grok process exit 了」，第三層又需要 envelope／PRD／acceptance 才能完成。現在沒有一個 supervisor 能證明三層真的走完同一個 run。

所以「CLI 有 ulw／ralph／ralplan／pipeline」不能直接推出「那些能力已產品化」。有 state machine 的函式，不等於 state transitions 都有可信 oracle。

---

## Feature matrix

狀態只用 brief 要求的四類：**HAVE / PARTIAL / MISSING / NEVER**。PARTIAL 可能代表「實作存在但未閉環」，也可能代表「目前 gate 有已知可信度缺陷」。

| Feature | Status | Independent assessment |
|---|---|---|
| Parallel fan-out | **PARTIAL** | skill 可要求 native spawn；host 支援 background subagent；但 bare ULW 是一個 leader process，live ULW 主動走 solo，無 N-worker／worktree／seal／integrate 證據 |
| Persist loop | **PARTIAL** | Ralph 有 max_iter 外迴圈與 acceptance；預設 3 輪、每輪 fresh Grok session，只有單輪 live smoke |
| Plan consensus | **PARTIAL** | 有 FSM；但全文 whole-word APPROVE parser 接受否定句，且先接受 artifact 再檢查非零 rc；無 live ralplan |
| Full pipeline | **PARTIAL** | 有 plan→implement→integrate→dual→accept FSM 與 pipeline resume；但依賴有缺陷的 ralplan／dual，且無 end-to-end live |
| Dual review | **PARTIAL（P0 broken gate）** | sequential critic→verifier 存在且不直接 set verified；但 missing artifact stub 與否定句可變 APPROVE，live 已假綠 |
| Ask broker | **HAVE（窄義）** | user-invoked fixed-provider broker、child-only allow env、artifact-only；不是自動 multi-review，也尚無 host live gate |
| Team / tmux | **MISSING** | 有 experimental process fanout，但不是 team runtime；建議維持 intentional missing |
| Stop pin | **NEVER（現行 host）** | Grok 官方文件明定 Stop passive；只有 PreToolUse blocking |
| Resume / context | **PARTIAL** | pipeline 可按 run state resume；Ralph 有 filesystem pack；但未用 host 原生 sessionId／--resume，無跨 mode 通用 resume |
| Doctor | **PARTIAL** | 能檢查安裝、global hook、strict compat warning；但未以實際 grok inspect discovery graph 判定 foreign plugin contamination |
| Cancel | **HAVE** | PID starttime／process group fail-closed；quota-heavy 有 live SIGTERM killpg 證據 |
| Accept / verified | **HAVE（有 caveat）** | frozen manifest、semantic argv policy、writer stamp、process-local token；是 OMG 最強能力，但 token 的跨 process durability 要定義清楚 |
| Capability isolation | **PARTIAL** | 正確傳 read-write／read-only 時 host tool removal 有 live 證據；hook 仍 fail-open、leader 有 shell、process fanout 有 shell、當前 host 又載入 foreign orchestration |
| Deep interview | **MISSING** | 無 ambiguity／requirements convergence workflow |
| UltraQA | **MISSING** | accept 是測試 gate，不是 diagnose→fix→retest loop |
| Ultragoal | **MISSING** | scaffold directory 不是 durable multi-session goal ledger |
| HUD | **MISSING** | OMG 無；Grok 本身已有 Tasks pane／Todo pane，應優先整合而非重造 |
| Wiki | **MISSING** | 無 knowledge lifecycle／session hooks／query surface |
| Notifications | **MISSING（OMG layer）** | Grok 原生已有 turn_complete、approval_required、task_complete、agent_error 等通知；不應複製 OMC notification stack |

---

## Critical finding 1 — dual-review can fail open as APPROVE

這是本次審計最嚴重的缺陷。

### Source-level chain

1. <code>omg_cli/dual_review.py:85–124</code> 的 prose parser 掃描**整份文字**。只要沒有 FAILED／REQUEST_CHANGES，而任何地方出現 case-sensitive whole-word APPROVE，就回傳 APPROVE。原始碼甚至在 113 行註明：「do not APPROVE lightly」仍會 match。
2. <code>omg_cli/dual_review.py:293–312</code> 在 non-dry stage 沒有產生指定 artifact 時，自動寫一份 stub；stub 文字包含：
   <code>Verifier acceptance requires explicit APPROVE in real runs.</code>
3. 這份 stub 隨即被同一個 parser 判成 APPROVE。
4. <code>omg_cli/dual_review.py:441–466</code> 雖然記錄 verifier exit code，卻在決定 verdict 時沒有讓非零 rc 優先失敗。
5. <code>omg_cli/dual_review.py:492–506</code> 仍把 dual-review run 寫成 completed，並印出 verdict。

Fresh temp-dir probe 已重現：

| Field | Value |
|---|---|
| stage launch rc | 127（PATH 中沒有 grok） |
| generated artifact | 系統 stub，含 whole-word APPROVE |
| parsed verdict | **APPROVE** |

換句話說，**review process 根本沒啟動成功，也可以被判為 APPROVE**。

### Existing live evidence already proves the bug

<code>docs/research/live/suite-20260719T190456Z-quota-heavy.log:144–188</code> 中 verifier 明確寫：

- Verdict：REQUEST CHANGES
- critic artifact 是 stub
- fake-completion pattern：fail
- Do not APPROVE
- parent 應重跑 critic

但同一份 log 的第 189 行是：

<code>omg dual-review: ... verdict=APPROVE</code>

而第 240–241 行仍寫：

- summary status=ok
- live_suite OK

這不是「artifact 比 summary 更可信」就能帶過。CLI verdict 是 pipeline 的控制輸入；<code>pipeline.py:732–733</code> 看到 APPROVE 就離開 review loop、進 accept stage。雖然 dual APPROVE 不會直接 set verified，卻能跳過應有的 re-implement／re-review，破壞 pipeline gate。

### Why tests did not catch it

<code>tests/test_dual_review.py:16–31</code> 測了：

- 純 APPROVE；
- REQUEST CHANGES；
- REQUEST CHANGES 與 APPROVE 共存時，REQUEST_CHANGES 優先；
- FAILED 優先。

它沒有測：

- 只有「Do not APPROVE」；
- 系統 non-dry stub；
- launch rc=127 + missing artifact；
- stale artifact；
- live verifier 明確反對但正文提到 APPROVE。

<code>scripts/live_suite.sh:154–168</code> 對 dual 的硬 assert 只有「不能寫 verified=true」。它沒有 assert 預期 verdict、stage rc、artifact provenance 或 critic 非 stub。因此錯誤的 APPROVE 仍被 suite 接受。

**判定：P0 release blocker。修好前不得宣稱 dual-review 是一個 gate，也不得把 pipeline 當可批准自動流程。**

---

## Critical finding 2 — ralplan consensus gate has the same class of failure

Council 把 plan consensus 標成 HAVE；我不同意。

<code>omg_cli/ralplan.py:256–296</code> 的 <code>artifact_contains_approve</code> 對 markdown／text 的最後判定也是全文 whole-word APPROVE。Fresh probe：

<code>Do not APPROVE this plan yet.</code> → <code>True</code>

更嚴重的是終態順序：

- <code>ralplan.py:540–552</code> 執行 stage，得到 <code>last_rc</code>。
- <code>ralplan.py:554–559</code> verifier 若 artifact 含 APPROVE，立刻 <code>accepted=True</code> 並 break。
- <code>ralplan.py:569–572</code> 才檢查 non-zero launch。

因此，只要 verifier path 已有 stale／預先存在／錯誤內容含 APPROVE 的 artifact，即使當輪 process launch 失敗，FSM 仍可能先 accepted。

缺少的安全條件包括：

- artifact 必須由本次 invocation 在 stage start 後建立；
- artifact 必須綁 run_id／round／role；
- non-zero exit、timeout、missing artifact 必須絕對壓過文字 verdict；
- verdict 必須是 schema 中的 terminal field，不是正文單字搜尋；
- 舊 artifact 必須在 stage 開始前清除或以 invocation-specific path 隔離。

所以 ralplan 目前是 **FSM implementation HAVE，可信 consensus PARTIAL**。它不是可當 pipeline plan gate 的 HAVE。

---

## Critical finding 3 — current host is not an OMG-only environment

### What doctor says

Fresh <code>omg doctor</code>：

- OMG plugin、hooks、global PreToolUse、skills、agents 都 OK；
- Claude settings hooks 與 CLAUDE.md markers 是 WARN；
- standard mode exit 0；
- strict mode把上述風險升為 FAIL、exit 1；
- compat plugin scan 同時回報 <code>~/.claude/plugins</code> 沒有 plugin-like entries。

strict mode能提醒「不乾淨」是好事，但 plugin inventory 的實際判斷仍有盲點。

<code>omg_cli/compat.py:155–164</code> 明確把 <code>cache</code>、<code>marketplaces</code> 當 bookkeeping denylist；<code>scan_claude_plugins</code> 因而不報它們。這對 Claude 自己的 bookkeeping 或許合理，對「Grok 最終到底載入了什麼」卻不可靠。

### What Grok actually discovers

Fresh <code>grok inspect --json</code> 顯示：

- <code>compat.claude.skills/rules/agents/mcps/hooks/sessions</code> 全部 enabled，source=default；
- plugins 包含 oh-my-grok，也包含：
  - oh-my-claudecode 4.15.5 cache；
  - ralph-loop；
  - codex；
  - warp；
  - 其他 Claude marketplace/cache plugins；
- skills 同時包含 OMC 的 ralph、ultrawork、ultraqa、ultragoal、omc-plan，以及 OMG 的 omg-* skills；
- hooks 包含 <code>~/.claude</code> 的 PreToolUse、OMG global hook、OMC plugin hooks 等。

Grok 官方文件 <code>05-configuration.md:381–414</code> 說明所有 harness compatibility cells 預設為 true；<code>10-hooks.md:58–72</code> 也明確說 Claude hook source 會預設載入。

所以這不是理論污染，而是當前 discovery graph 的事實。

### Impact on live proof

現有 live suite 沒有在 summary 保存：

- Grok version；
- OMG commit／installed plugin hash；
- effective compat cells；
- loaded plugins／hooks／skills allowlist；
- global hook hash；
- foreign orchestration absence。

因此，既有 L2 可以證明「在當時那個整體 Grok 環境中發生了某件事」，不能乾淨證明「只靠 OMG 會發生」。canary 的 deny reason 確實是 OMG signature，這一點仍可信；但 ULW／Ralph／dual 等模型行為的歸因不乾淨。

更直接地說：**如果 parity audit 的被測物與 reference OMC 同時載入同一個 host，證據就沒有對照組。**

---

## Ralph and “don’t stop” — outer loop exists, continuity does not

Council 認為 persistence HAVE，因為有 CLI max_iter + context pack。這個說法只對了一半。

### What exists

<code>omg_cli/modes.py:607–817</code>：

- Ralph 預設 max_iter=3；
- 每輪啟動 Grok；
- 每輪後嘗試 frozen acceptance；
- acceptance 通過才 verified；
- launch non-zero 會停止；
-迭代結束但沒 acceptance 時，狀態是 completed／verified=false；require_acceptance 預設會讓 CLI exit 1。

這個 outer-loop 與「model 說做完就算做完」相比，方向正確。

### What is missing

<code>modes.py:292–382</code> 的 argv 固定用 <code>--output-format plain</code>；<code>modes.py:517–604</code> 每輪 <code>subprocess.Popen</code> 一個新 Grok process。程式沒有：

- capture JSON response；
-保存 sessionId；
-下一輪傳 <code>--resume</code>；
-使用 <code>--continue</code>；
-保存／resume child subagent identity。

Grok 官方文件則已提供這些能力：

- <code>14-headless-mode.md:236–284</code>：JSON output 可取 sessionId，下一次用 <code>--resume</code>；
- <code>14-headless-mode.md:494–498</code>：headless 預設 fresh session，要維持 context 必須 resume／continue；
- <code>17-sessions.md:181–204, 294–300</code>：headless multi-step automation 應保存 sessionId；
- <code>16-subagents.md:143–184</code>：completed subagent 可用 <code>resume_from</code> 保留 transcript、tool state 與 model。

所以目前每輪所謂 context persistence 主要是「把少量 filesystem facts 重新塞進新 prompt」，不是「延續同一工作記憶」。它可能在簡單 PRD story 有效，但不能等同 OMC in-session persistence，也沒有利用 Grok 已經提供的最佳 primitive。

此外，<code>passes=i-1</code> 是輪數 bookkeeping，不是 acceptance pass count。對人類顯示時若不說清楚，容易把「跑過幾輪」誤讀成「通過幾關」。

### Correct status

Ralph 是：

- **outer supervisor primitive：HAVE**
- **reliable persistence product：PARTIAL**
- **in-session Stop pin：NEVER**

---

## Parallel ULW — machinery exists, happy path is not closed

目前 bare <code>omg ulw</code> 的實際路徑是：

1. 建立 run、記 base SHA；
2. 開一個 Grok leader process；
3. prompt 建議 leader 用 spawn_subagent；
4. process exit 後，若無 acceptance，寫 completed／verified=false；
5. **不自動 integrate**。

<code>omg_cli/fanout.py:1–18</code> 的 process fanout 是 experimental；<code>fanout.py:305–317</code> 明說 worker 預設保留 shell，capability_mode 只是 prompt-level；<code>fanout.py:385–432</code> 只以各 process exit code 決定 completed，沒有要求 envelope、integrate 或 acceptance。

另一方面，<code>omg_cli/workers.py</code> 與 <code>integrate.py</code> 的 prepare／seal／ancestry／changed_files machinery 是有實質價值的。但目前 leader／operator 要自己把它串起來：

- prepare worktree；
- spawn child 到正確 cwd；
- join；
- seal；
-產生 run-scoped envelopes；
- integrate；
- accept。

既有 live ULW fixture 只要求建立一個小檔案，skill 明確允許 tiny task 由 leader solo；log 也真的說「不需要 fan-out」。cap-spawn 只證明一個 child 在正確 capability_mode 下沒有 shell，不證明平行、不證明 worktree、不證明 integrate。

因此 Council 說 Parallel PARTIAL 是對的；任何「ultrawork 已 live-proven」說法都不成立。

---

## Pipeline — useful FSM shell, currently not a trustworthy autopilot

Pipeline 的 stage order 與 state persistence值得保留：

<code>plan → implement → integrate → dual_review → accept</code>

也有一些正確選擇：

- dual APPROVE 不等於 verified；
- acceptance 是最後權威；
- re-implement 後會再 integrate；
- pipeline state 可 resume；
- report artifact 有 CLI writer。

但它有四類產品風險：

### 1. Inherits both unsafe verdict gates

plan 依賴 ralplan；review 依賴 dual_review。兩者都可能 false APPROVE。因此 FSM 結構完整不代表 stage gates 可信。

### 2. Global, not run-scoped envelopes

<code>pipeline.py:183–228</code> 從全域 <code>.omg/artifacts/ulw-results/*.json</code> 判斷是否要 integrate、讀 head SHA；沒有以 run_id 隔離。別的 run 遺留 envelope 可能影響本 run 的 integrate／resume decision。

### 3. Misnamed no-op environment guard

<code>pipeline.py:175–180</code> 的 <code>_assert_no_allow_env</code> 在 parent 已 export <code>OMG_ALLOW_EXTERNAL_CLI=1</code> 時只執行 <code>pass</code>。名稱與 docstring 說「Hard guard」，實際上沒有拒絕、沒有清除，也沒有 warning。由於 Grok child 會繼承 parent env，PreToolUse external CLI deny 會被 bypass。

### 4. Resume is state resume, not session resume

<code>pipeline.py:390–408, 542–564</code> 能依 pipeline JSON 跳 stage；但內部 Grok calls 仍 fresh session。另有 config drift 風險：resume 時部分 CLI flags 可與原 run state 不一致。應以 frozen run config 為預設，變更需要顯式 override。

再加上沒有 L2 pipeline happy path／failure path，現在最誠實的定位是「可測的 orchestration FSM prototype」，不是 autopilot product。

---

## Accept / verified — strongest part, but clarify durability semantics

這一層是 OMG 相對 OMC 最有辨識度的優點：

- PRD acceptance commands 被 freeze；
- semantic argv policy 阻擋 shell、external agent CLI、python -c、npx 等；
- result 要有 writer=omg-cli、passed=true、matching manifest SHA；
- <code>set_verified</code> 還需要同 process 的 acceptance token；
- dual／review artifact 不能直接 set verified。

<code>state.py:632–655</code> 與 <code>acceptance.py:102–137, 600–662</code> 的 anti-forgery intent 清楚。

但 process-local token 同時帶來一個 durability 語意：

1. process A 執行 acceptance、set_verified，disk status 為 verified=true；
2. process A 結束，token 消失；
3. process B 對同 run 呼叫一般 <code>write_status</code>；
4. <code>state.py:408–411</code> 再檢查 trusted acceptance，因無 in-process token而把 verified 清成 false。

Fresh temp probe 已得到 <code>before_restart=True</code>、<code>after_later_write=False</code>。

這未必是 security bug；它可能是「terminal verified run 不應再被 mutate」的刻意結果。但 contract 必須明確：

- 若 verified 是 durable terminal fact，後續 status write 不應因 process restart 自動撤銷；
- 若 verified 只在持有 ephemeral token 的 process 內有效，disk 上的 verified=true 就不是 durable truth；
-較乾淨的設計是 terminal run immutable，或以 CLI private signing key／OS-protected receipt驗證 disk artifact，而不是把「重啟後無 token」等同「artifact 不可信」。

在 contract 釐清前，Accept / verified 仍可列 HAVE，但要加 caveat。

另外，allowlisted pytest／project scripts 仍會執行 repo code。security-model 已正確承認這不是 sandbox，這份誠實應保留。

---

## Capability isolation — useful host primitive, not a product-wide guarantee

Grok 官方 <code>16-subagents.md:160–171</code> 定義：

- read-only：讀，無寫、無 shell；
- read-write：讀寫，無 shell；
- execute：讀與 shell，無寫；
- all：全權。

當 OMG leader 正確傳 <code>capability_mode=read-write</code> 時，quota-heavy 的 child 確實沒有 terminal tool；這是有價值的 live proof。

但產品整體仍是 PARTIAL：

- capability_mode 是 optional host parameter；OMG 用 soft PreToolUse gate補強；
- hook timeout／crash／malformed output全部 fail-open（Grok <code>10-hooks.md:148–152, 195–205</code>）；
- unknown agent type只要帶 read-only／read-write 就會被 allow（<code>deny.py:168–171</code>）；
- parent leader 有 shell；
- process fanout worker 有 shell；
- parent 若有 <code>OMG_ALLOW_EXTERNAL_CLI=1</code>，deny path直接 allow；
- current host 還載入 foreign hooks／skills／plugins。

安全說法應是：

> 正確使用 Grok capability_mode 時，worker tool surface 受 host 限制；OMG hook 是 fail-open defense-in-depth，不能把它宣傳成 hard sandbox。

這與目前 security-model 的核心文案一致。問題在於實際 host hygiene 與 doctor oracle沒有跟上。

---

## OMC comparison — the missing value is lifecycle depth, not names

OMC 4.15.5 的 on-disk inventory：

- 41 skill directories；
- 19 agent definitions；
- hooks.json 219 行；
- UserPromptSubmit keyword／skill injection；
- SessionStart memory／wiki；
- PreToolUse enforcement；
- PostToolUse verification／memory／rules；
- Subagent start／stop tracking與 deliverable verification；
- PreCompact memory／wiki checkpoint；
- Stop context guard、drift guard、persistent modes、simplifier；
- SessionEnd async memory／wiki。

<code>scripts/persistent-mode.mjs:1097–1151</code> 同時讀 Ralph、ultragoal、autopilot、ultrapilot、ultrawork、ultraqa、pipeline、team 等 state；<code>1170–1243</code> 對 Ralph 做 session／stale／context／hard-limit判定後回傳 <code>decision:block</code>；<code>1409–1478</code> 對 pipeline／team 也有 phase-aware continuation。

這不代表 OMG 應照搬。Grok Stop passive，所以那套控制面無法移植；OMC 的大量 surface 也有 maintenance cost。真正應比較的是目的：

- context／memory lifecycle；
- stage feedback與 recovery；
- task ownership；
- verification depth；
- durable resume；
- team observability；
- failure-aware continuation；
- knowledge capture。

OMG 目前主要做到 state／accept／cancel／部分 orchestration shell，尚未做到上述 lifecycle depth。把 skill 名稱補齊不會改變這個差距。

---

## Challenges to multi-Grok SYNTHESIS

### What the council got right

| Council conclusion | My assessment |
|---|---|
| 不能說「OMC 功能基本都有了」 | **正確** |
| Full OMC surface 約只有一小部分 | **方向正確**；精確百分比沒有穩定分母 |
| Stop pin 在現行 Grok host 不可做 | **正確且應堅持** |
| PreToolUse 是 soft、capability_mode 才是主要 worker isolation | **正確** |
| ULW live 是 solo smoke，不是 parallel proof | **正確** |
| pipeline／ralplan／ask／multi-iter Ralph 缺 live | **正確** |
| dual APPROVE 不應 set verified | **正確** |
| 不要為 parity 複製整個 skill zoo／tmux／HUD | **正確** |

### Where the synthesis is wrong or too soft

| Synthesis claim | Problem | Corrected judgment |
|---|---|---|
| Dual sequential live：full + heavy 都 REQUEST_CHANGES | **事實錯誤**。heavy log 第 189 行是 verdict=APPROVE，而 verifier正文是 REQUEST CHANGES／Do not APPROVE | Dual live 已證明 **false-green defect**，不是 narrow pass |
| Plan consensus HAVE／live missing | Parser 與 rc ordering 可以假接受；不是只有 live 缺口 | **PARTIAL, P0 unsafe gate** |
| Persistence HAVE | 有 bounded loop，但每輪 fresh session、預設 3 輪、只有 single-iter live | **PARTIAL** |
| Capability isolation HAVE | 正確 args 時 host filter HAVE；產品仍依賴 soft spawn gate、leader shell、foreign compat surface | **PARTIAL** |
| Doctor HAVE | 安裝檢查存在；但 doctor plugin scan 看不到 Grok 實際載入的 OMC/cache/marketplace graph | **PARTIAL** |
| Trust honesty 7–8/10 | 文件誠實，但控制面可 false APPROVE、live suite 可 false green | **約 5/10** |
| Live narrow core PASS | canary／accept／cancel可 narrow pass；dual 不可算 pass，且 host inventory 未固定 | **混合：部分 pass，整體 proof quality 3/10** |
| Ralph context pack + RESUME.md 是主要 P0 | Host 已有 headless sessionId／--resume 與 subagent resume_from；文件 pack只能是 fallback／derived view | **優先用 native session continuity** |

### What the synthesis missed

1. **Non-dry dual stub 本身含 APPROVE**，launch rc=127 也能 approve。
2. **Ralplan 先接受 artifact 再處理 non-zero rc**，stale artifact 可越過 launch failure。
3. **Quota-heavy live 已經出現假綠，而 live verifier報告抄錯最終 verdict。**
4. **live_suite 對 dual 不驗 verdict，只驗 verified 沒被 set。**
5. **當前 Grok discovery 同時載入 OMC 與 OMG**，證據缺乏 OMG-only attribution。
6. **doctor 排除 cache／marketplaces，因此「plugins empty」與 grok inspect 實況矛盾。**
7. **Pipeline 的 _assert_no_allow_env 是 no-op**，parent env可關掉 external CLI deny。
8. **Pipeline envelope lookup不是 run-scoped**，跨 run stale artifact可能影響 integrate。
9. **Verified 的 process-local token有跨 process撤銷語意**，需要 terminal immutability contract。
10. **Grok 已有 Tasks pane、notifications、session resume、subagent resume_from、worktree apply**；roadmap 不應先重造 RESUME.md／假 HUD。

Council 最大的問題不是整體方向錯，而是對「實作存在」給了太多信用，沒有逐一追到 terminal oracle。當 verifier 自己的文字說不要批准，CLI 卻批准，任何 inventory 百分比都應暫停計算。

---

## Don’t-stop design — Grok-native recommendation

### Host constraint

Grok 官方 <code>10-hooks.md:82–99</code> 明定只有 PreToolUse blocking，Stop 是 passive；<code>203–205</code> 又說 passive hook stdout ignored。因此：

- Stop hook可以記錄、通知、寫 derived handoff；
- Stop hook不能 veto、不能 reinject user message、不能強制下一 turn；
-印出 <code>decision:block</code> 只會造成假象；
-除非 host contract 改變並有 canary，0.3.x 不應重開這條路。

### Recommended control plane

「不要停」應該是一個**外部、durable、session-aware supervisor**，不是 hook 魔法。

    NEW
      → START_SESSION
      → RUN_ONE_BOUNDED_UNIT
      → COLLECT_STRUCTURED_RESULT
      → RUN_ACCEPTANCE
          ├─ pass → VERIFIED（terminal）
          ├─ recoverable fail → RESUME_SAME_SESSION → next unit
          ├─ context/session lost → NEW_SESSION + signed context pack → next unit
          ├─ hard blocker / budget exhausted → BLOCKED（resumable, not completed）
          └─ user cancel → CANCELLED（terminal）

### Concrete contract

1. **First turn captures native session identity**  
   用 <code>--output-format json</code>；保存 sessionId、Grok version、model、cwd、prompt hash、run config。

2. **Subsequent turns resume the same session**  
   預設 <code>--resume sessionId</code>；只有 session missing／corrupt／context policy要求時才開新 session。fallback 要在 state 寫清楚原因。

3. **One bounded unit per iteration**  
   Story／stage要有 machine-readable input與 expected artifact；不能只叫模型「繼續做到完」。

4. **Structured result, not prose token scanning**  
   每個 stage 回傳 exact JSON schema，例如 role、run_id、round、invocation_id、verdict、findings、artifact_hash。非零 rc、timeout、missing／stale schema一律 FAILED。

5. **Acceptance is the only completion oracle**  
   process exit 0、模型說 done、review APPROVE 都不是產品完成。只有 frozen acceptance pass可 VERIFIED。

6. **Failure policy is explicit**  
   recoverable failure有 retry class與上限；auth／quota／missing authority變 BLOCKED；不能把 budget exhaustion寫成 completed。

7. **Resume is universal**  
   <code>omg resume RUN_ID</code> 應可路由 Ralph／pipeline／ULW recovery；顯示原 session、current stage、last evidence、next action、為何 fallback。

8. **RESUME.md 只能是 derived view**  
   可以為人類生成，但權威必須是 versioned JSON／ledger；避免檔案與 state drift。

9. **Use host-native child continuity**  
   TUI flow對 completed child用 <code>resume_from</code>；寫入工作用 host worktree isolation與 apply。不要自己發明另一套 child transcript system。

10. **Use host-native observability**  
    Grok 已有 Ctrl+B Tasks pane、Ctrl+T Todo pane，以及 notification events。OMG 只需提供 run/stage metadata與 completion通知，不需複製 tmux HUD。

### Honest user promise

可以承諾：

> 啟動 OMG supervisor 後，它會在每輪保留／恢復上下文，直到 CLI acceptance 通過、使用者取消，或產生一個具體可 resume 的 blocker；不會因模型一句「完成」就結束。

不能承諾：

> 在普通 Grok chat 裡，agent 永遠不會停止回合。

---

## 0.3 roadmap — my ordering

### P0 — before any new parity feature

| Priority | Work | Exit criteria |
|---|---|---|
| **P0-1** | Replace all prose APPROVE scanning | 共用 strict verdict schema；只接受 exact terminal field；negation／正文 mention不能通過 |
| **P0-2** | Fail closed on stage execution | rc≠0、timeout、missing／stub／stale artifact一律 FAILED；artifact綁 run/round/role/invocation/start time/hash |
| **P0-3** | Fix dual／ralplan ordering and pipeline inheritance | 非零 rc優先；清理舊 artifact；pipeline 不可吃未驗證 verdict；新增完整回歸 |
| **P0-4** | Make live suite semantic | Dual fixture必須 assert REQUEST_CHANGES；critic stub不得 pass；summary列每個 gate、rc、verdict、artifact hash，不可只寫 status=ok |
| **P0-5** | Clean-host proof and doctor oracle | live 開始前保存 <code>grok inspect --json</code>；foreign orchestration plugin／hook／skill不在 allowlist就 fail；doctor以 effective discovery graph而非目錄 heuristic判定 |
| **P0-6** | Close env and run-scope leaks | parent 有 <code>OMG_ALLOW_EXTERNAL_CLI=1</code> 時 pipeline hard fail；ULW envelopes必須 run-scoped；resume使用 frozen run config |

P0 regression minimum：

- <code>Do not APPROVE</code>；
- <code>Not approved</code>；
- generated stub含敏感單字；
- rc=127 + stale APPROVE artifact；
- timeout + artifact；
- artifact round／run mismatch；
- heavy live log全文 fixture；
- live suite expected negative verdict；
- foreign OMC plugin被 doctor／suite偵測；
- parent allow env使 pipeline拒絕。

### P1 — make the existing core a product

| Priority | Work | Why |
|---|---|---|
| **P1-1** | Session-aware Ralph／pipeline + universal resume | 直接使用 Grok JSON sessionId／--resume；讓 persistence是真的 context continuity |
| **P1-2** | ULW closed happy path | structured task list → prepare worktree → N native RW children → join → seal → run-scoped envelope → integrate-or-fail → accept |
| **P1-3** | Real L2 matrix | multi-worker ULW、2+ iteration Ralph、ralplan revise、pipeline happy＋failure＋resume、ask broker stub provider |
| **P1-4** | Verified terminal semantics | 定義 verified是否 immutable；避免另 process status write把 durable truth清掉 |
| **P1-5** | Human state UX | <code>omg state --human</code>／<code>omg resume</code> 顯示 stage、session、last evidence、next action、blocker |
| **P1-6** | Host version drift gate | summary記 Grok version、plugin commit、hook hashes；升版後要求 canary／critical live matrix重跑 |

### P2 — only after core evidence is trustworthy

| Priority | Work | Scope |
|---|---|---|
| **P2-1** | Lean deep interview | ambiguity／boundary／acceptance convergence；輸出可直接餵 pipeline 的 PRD |
| **P2-2** | UltraQA-like repair loop | diagnose → minimal fix → targeted test → regression → bounded repeat；不可只換 skill 名 |
| **P2-3** | Durable goal ledger | 若真的有跨 session、多 story需求，再做 append-only goal ledger與 checkpoint |
| **P2-4** | Thin native UI integration | Tasks pane metadata、native notification、session link；不重造 HUD |
| **P2-5** | Knowledge capture | 先做 run artifact index／decision log；只有有搜尋需求時才做 wiki surface |

### WONTFIX / NEVER for 0.3.x

| Item | Decision | Reason |
|---|---|---|
| OMC-style Stop veto／reinject | **NEVER until host changes** | Stop passive；假 block 是產品謊言 |
| tmux team clone | **WONTFIX** | Grok已有 native subagents／Tasks pane／worktrees；維護成本高、隔離面更大 |
| OMC skill／agent count parity | **WONTFIX** | 名稱數不是能力；先做 closed loops |
| Custom HUD clone | **WONTFIX** | 用 host Tasks／Todo pane與 human state summary |
| Custom notification stack | **WONTFIX** | Grok已原生支援 turn/task/error notifications與 hooks |
| Default shellful process fanout | **WONTFIX** | 與 capability isolation主張衝突；保持 experimental或移除 |
| Auto-ingest external advisor answer into verified | **WONTFIX** | advisor永遠 advisory；需要獨立 review與 acceptance |
| Copy OMC hooks into Grok | **WONTFIX** | lifecycle semantics不同，尤其 Stop／UserPromptSubmit stdout |

---

## Blind spots and product-lie risks — top 10

### 1. 「dual review 跑了」其實是 launch failure + stub APPROVE

目前是實際可重現的 P0，不是假設。最危險的話術是「不 set verified，所以沒事」；pipeline 仍以 verdict 控制流程。

### 2. 「plan consensus accepted」其實只是正文提到 APPROVE

否定句、規範文字、stale artifact都可能使 ralplan accepted。沒有 artifact provenance就沒有 consensus。

### 3. 「live suite green」只代表 script跑到底

summary只有 status=ok；許多 mode command後有 <code>|| true</code>；dual不驗 verdict。這種 suite 會把最重要的失敗吞掉。

### 4. 「OMG live evidence」可能同時受 OMC／Claude routing影響

current host確實載入 OMC plugin與 skills。若不保存 effective discovery allowlist，就無法歸因。

### 5. 「doctor 說 plugins empty」不等於 Grok 沒載 foreign plugins

filesystem heuristic排除了 cache／marketplaces；Grok inspect才是 runtime truth。doctor應以 host oracle為主。

### 6. 「Ralph persistence」其實每輪失憶

filesystem context pack不是 transcript continuity。預設 fresh session，且只有一輪 live。不要宣稱與 OMC「不要停」同等體驗。

### 7. 「ULW parallel」其實 leader solo

live fixture刻意是 tiny one-file task；沒有 child count、worktree、envelope、integrate。command 名稱不是 parallel evidence。

### 8. 「Pipeline hard guard」其實 parent bypass env被默許

<code>_assert_no_allow_env</code> 的 pass與名稱相反。使用者 shell一旦污染，child繼承 bypass。

### 9. 「verified 是 durable truth」語意未鎖

process-local token提高 anti-forgery，但後續另一 process status write會撤銷 verified。要避免 UI／resume對同一 run給出不同真相。

### 10. 「host isolation已證明」忽略版本與配置 drift

canary是某日期、某 Grok version、某 hook graph的證據，不是永遠保證。live summary必須保存 host fingerprint並在升版後重跑。

---

## Release claim policy

### 0.2.5 currently safe to say

- OMG 有 CLI run state、cancel、accept／verified、worker seal／integrate primitives。
- Grok Stop 是 passive；OMG 不提供 in-session Stop pin。
- 正確 capability_mode 下，RW child沒有 shell；PreToolUse只是 fail-open defense-in-depth。
- ULW／Ralph 有 real Grok single-task smoke；accept與 cancel有 dated live evidence。
- Pipeline／ralplan／parallel ULW／ask 尚未有完整 live product proof。

### Currently unsafe to say

- 「OMC 功能基本都有了。」
- 「dual review live verified。」
- 「ralplan consensus gate 可用。」
- 「所有 core loops 都 live。」
- 「live suite green，所以 pipeline trust gate 沒問題。」
- 「doctor 證明沒有 OMC／Claude 污染。」
- 「Ralph 會在同一上下文持續到做完。」
- 「ULW 已證明多 worker 平行與自動整合。」
- 「workers hard sandbox。」

---

## One page for the user

最直白的答案：

**oh-my-grok 現在有一個不錯的 orchestration control-plane 骨架，但還不能說「基本 OMC 功能都有了」。**

真正做得好的部分是：

- run state；
- PID／cancel；
- frozen acceptance；
- semantic command policy；
- verified 只有 CLI 能寫；
- worktree seal／integrate 的安全檢查；
- 對 Stop passive與 hook fail-open的文件誠實。

真正沒做完的不是 skill 數量，而是閉環：

- ULW 沒有證明多 worker，也不會自動把 worker結果接回主線；
- Ralph 雖然有外迴圈，但每輪都是新 Grok session，沒有真正延續 context；
- pipeline 是 FSM 外殼，卻依賴兩個現在不可信的判決 gate；
- plan／review parser會把「Do not APPROVE」中的 APPROVE 當成批准；
- dual stage即使 grok根本沒啟動，系統自建的 stub也能被解析成 APPROVE；
-這個 false green已經出現在 repo保存的 quota-heavy live log，suite仍然報 OK；
-當前 Grok同時載入 OMG與 OMC／Claude skills、hooks、plugins，doctor沒有完整看見，既有 live證據也沒有保存乾淨環境快照。

所以 0.3 不該先做更多模式。正確順序是：

1. **先修所有 false APPROVE：strict JSON verdict、非零 rc優先、missing／stale artifact fail closed。**
2. **讓 live suite真的驗語意，不是只驗 process走完；把已保存的 heavy假綠變成永久回歸。**
3. **讓 doctor與 live harness依 grok inspect建立 clean allowlist，排除 OMC／Claude污染。**
4. **再用 Grok原生 sessionId／--resume做真正的 Ralph／pipeline continuity。**
5. **再把 ULW串成 prepare→spawn N→join→seal→integrate→accept的閉環。**
6. **最後才考慮 deep interview、UltraQA、durable goal。**

「不要停到做完」在 Grok上的正解不是 Stop hook。Stop無法 block，永遠不要假裝可以。正解是外部 supervisor：保存 run與 sessionId，每輪 resume同一 session，只有 acceptance pass、user cancel或明確 blocker才能終止。RESUME.md可以給人看，但不能取代權威 state；Grok原生 Tasks pane、notifications、subagent resume_from與worktree apply應優先利用。

我的 ship/no-ship 結論：

> **把 OMG 當 0.2.5 prototype／alpha：可以。把 accept／cancel／state 當可靠基礎：可以。把 dual、ralplan、pipeline當可信 gate：現在不行。把「基本 OMC parity」當行銷：不行。**

P0 修完並有 clean-host、semantic live matrix後，OMG 才有資格把「核心 OMC-class orchestration」從骨架升級成產品。

---

## Evidence index

### OMG source

- <code>omg_cli/dual_review.py:85–124, 233–313, 420–507</code>
- <code>omg_cli/ralplan.py:256–303, 520–590</code>
- <code>omg_cli/modes.py:292–382, 517–604, 607–817</code>
- <code>omg_cli/pipeline.py:175–228, 390–498, 542–564, 686–850</code>
- <code>omg_cli/state.py:380–414, 620–655</code>
- <code>omg_cli/acceptance.py:102–137, 600–662</code>
- <code>omg_cli/deny.py:69–214</code>
- <code>omg_cli/fanout.py:1–18, 305–317, 385–432</code>
- <code>omg_cli/workers.py</code>
- <code>omg_cli/compat.py:155–265, 367–410</code>
- <code>hooks/hooks.json</code>
- <code>hooks/bin/stop.py</code>
- <code>scripts/live_suite.sh:71–215</code>

### Raw live evidence

- <code>docs/research/live/suite-20260719T190456Z-quota-heavy.log:138–195, 218–241</code>
- <code>docs/research/live/suite-20260719T190043Z-full.log:94–181</code>
- <code>docs/research/live/canary-20260719T190456Z.json</code>
- <code>docs/research/live/suite-20260719T190456Z-quota-heavy.summary.json</code>

### Council claims challenged

- <code>docs/research/omc-parity-council/SYNTHESIS.md:10–21, 43–65, 69–100</code>
- <code>docs/research/omc-parity-council/07-live-evidence.md:53–84, 88–107, 158–172, 196–217</code>

### Grok official host docs

- <code>~/.grok/docs/user-guide/10-hooks.md:80–99, 146–152, 188–205</code>
- <code>~/.grok/docs/user-guide/14-headless-mode.md:19–46, 236–284, 494–498</code>
- <code>~/.grok/docs/user-guide/16-subagents.md:141–196, 241–289</code>
- <code>~/.grok/docs/user-guide/17-sessions.md:1–39, 67–95, 181–204, 294–300</code>
- <code>~/.grok/docs/user-guide/05-configuration.md:381–414, 450–525</code>

### OMC 4.15.5 reference

- <code>~/.claude/plugins/cache/omc/oh-my-claudecode/4.15.5/hooks/hooks.json:1–219</code>
- <code>~/.claude/plugins/cache/omc/oh-my-claudecode/4.15.5/scripts/persistent-mode.mjs:1035–1250, 1409–1478</code>
-同一安裝的 <code>skills/</code>（41 directories）與 <code>agents/</code>（19 definitions）

### Fresh commands

- <code>PYTHONPATH=. python3 -m pytest -q -m 'not live'</code> → 286 passed
- <code>python3 -m omg_cli.main doctor</code> → exit 0 with compat WARN
- <code>python3 -m omg_cli.main doctor --strict</code> → exit 1
- <code>grok --version</code> → 0.2.106 stable
- <code>grok inspect --json</code> → effective foreign compatibility／plugin／hook／skill graph
- temp-dir direct probes for dual rc=127 stub, ralplan negation, acceptance token restart semantics
