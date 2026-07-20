# Code Review — Spawn fail-closed deny UX（RETRY IMMEDIATELY）

**日期 (UTC):** 2026-07-20  
**Repo:** `<repo-root>`  
**BASE_SHA:** `60d0882`（origin/main HEAD）  
**HEAD:** working tree（未提交的 spawn retry UX）  
**審查角色:** code-reviewer（唯讀產品碼；僅寫本報告）  
**範圍:** Spawn deny reason → 強制同 turn retry；docs/skills/agents 一致；維持 fail-closed 安全

---

## Code Review Summary

**Files Reviewed:** 11（+ tests）  
**Total Issues:** 7  

### By Severity
- CRITICAL: 0（必須修）
- HIGH / Important: 2（應修 / 殘差 — 皆不單獨阻擋本 UX patch）
- MEDIUM / Minor: 5（建議修）
- LOW: 0（併入 Minor）

### Spec compliance（Stage 1）— PASS

| # | 需求 | 結果 | 證據 |
|---|------|------|------|
| 1 | 缺/錯 `capability_mode` 被 deny 時，必須指示 **同 turn retry** 正確 mode；不得放棄 multi-agent | **PASS** | `spawn_deny_reason` 一律含 `RETRY IMMEDIATELY` + `Do NOT abandon multi-agent` + 具體 `capability_mode={suggested!r}` |
| 2 | 仍 **fail-closed** deny（不可 auto-allow 缺 mode） | **PASS** | `decide_spawn_subagent`：missing/invalid/execute/all/mismatch → `decision: deny`；僅 `OMG_ALLOW_UNSAFE_SPAWN=1` 可繞過 |
| 3 | Tests 覆蓋 explore (RO) + general-purpose (RW) 的 RETRY 文案 | **PASS** | `test_spawn_missing_mode_explore_suggests_read_only`、`test_spawn_missing_capability_mode_denied`、`test_spawn_general_purpose_requires_read_write`、`test_spawn_explore_requires_read_only` |
| 4 | Docs/skills/agents 一致 | **PASS（主路徑）** | AGENTS.md、templates/AGENTS.fragment.md、agents/omg-orchestrator.md、skills omg-using/ralph/ultrawork、modes HARD_RULES + capability contract、docs/security-model.md §Deny UX。**部分缺口：** ralplan / dual-review / pipeline skills 未寫 RETRY |
| 5 | 無 product lie；reason 不得宣稱 host 硬 rewrite tool args | **PASS** | Reason 只說再 call `spawn_subagent` + `Minimal fix: add parameter capability_mode=…`。security-model 仍標 PreToolUse soft / fail-open |

---

## Strengths（優點）

1. **安全姿態正確。** fail-closed gate 未改；只改 deny reason 與 prompt 面，教 leader retry。沒有 silent auto-inject / auto-allow 缺 `capability_mode`（那會是安全回歸）。
2. **Reason 集中建構。** `spawn_deny_reason(kind=…)` + `suggested_capability_mode()` 讓 missing / invalid / execute_all / mismatch 共用同一套 retry 協議，不易漂移。
3. **建議 mode 可操作。** `!r` 產生 `capability_mode='read-only'` / `'read-write'`；explore → RO、general-purpose → RW 明確，測試有 assert。
4. **多層訊息（不只 reason）。** `modes.py` HARD_RULES、AGENTS fragment、orchestrator、主 skills（using/ralph/ultrawork）重複 RETRY，降低單一表面被忽略的風險。
5. **安全文件誠實。** `docs/security-model.md` 仍寫 soft-gate residual；Deny UX 註明是 UX 而非更強隔離。
6. **CamelCase / Task alias 仍可用。** `_spawn_fields` + Task→spawn 映射有 `test_spawn_task_alias_and_camel_case_keys`。
7. **未宣稱 host arg rewrite。** 與既有 isolation 研究一致（CLI 不 rewrite spawn args）；文案與實作相符。

---

## Issues

### Critical
*（無）*

### Important

[IMPORTANT] 仍缺 spawn-deny reason + model retry 的 live host 證明  
File: `docs/research/omc-parity-council/03-critic-gaps.md`（P0 item 4）；屬 Option A 殘差，非本 patch 引入  
Confidence: HIGH  
Issue: Unit tests 證明 `decide_spawn_subagent` reason 文案；仍 **無 live canary** 證明 (a) host 會把 PreToolUse reason 丟給 leader，(b) leader 真的會 re-spawn。RETRY-only UX 必要但不足以當產品信心上限。  
Fix: 新增 `scripts/canary_spawn_cap.py`（或擴充 canary）：缺 mode spawn 時 reason 含 `RETRY IMMEDIATELY` + `capability_mode=`；可選 live：leader 補 mode 後成功。宣稱語言維持 unit-only 直到有 host oracle。

[IMPORTANT] 全域 PreToolUse soft-gate 殘差（非 omg 專案 + fail-open）  
File: `docs/security-model.md:53-86`、`hooks/hooks.json:36-46`、`decide_pre_tool_use` except→allow  
Confidence: HIGH  
Issue: 全域安裝（`~/.grok/hooks/omg-pretool-deny.json`）會讓 **所有** Grok session 吃到 spawn fail-closed。hook crash/timeout 仍 **fail-open**。RETRY UX 在 deny 有觸發時幫助恢復，但不修「hook 死掉」或非 omg 專案的摩擦。屬既有殘差；本 patch 未惡化。  
Fix（backlog，不擋 RETRY UX 落地）: 文件標註非 omg 影響；保留 `OMG_ALLOW_UNSAFE_SPAWN=1` 顯式逃生；doctor 已 hard-check 全域 matcher — install 後維持綠燈。

### Minor

[MINOR] `test_spawn_execute_mode_denied` 未 assert RETRY 文案  
File: `tests/test_deny.py:186-197`  
Confidence: HIGH  
Issue: execute/all 路徑 *有* 走 `spawn_deny_reason(..., kind="execute_all")`，因此含 `RETRY IMMEDIATELY` + 建議 mode，但測試只 assert `decision == "deny"`。之後若 execute_all 剝掉 retry 字串，CI 不會抓。  
Fix:
```python
assert "RETRY IMMEDIATELY" in d.get("reason", "")
assert "read-write" in d.get("reason", "")  # general-purpose suggested
assert "execute" in d.get("reason", "").lower() or "not allowed" in d.get("reason", "")
```
並 parametrize `capability_mode=all`。

[MINOR] 缺 empty `subagent_type` / invalid mode / `OMG_ALLOW_UNSAFE_SPAWN` 單元測試  
File: `tests/test_deny.py`（缺 case）；邏輯在 `omg_cli/deny.py:116-120,193-205`  
Confidence: HIGH  
Issue: 行為以靜態追蹤合理但無守護：
- 空 type + 缺 mode → deny，建議 `read-write`，label `(missing subagent_type)`
- 空 type + 明確 RO/RW → allow（`required is None`）
- invalid mode（`foo`）→ deny + RETRY + suggested
- `OMG_ALLOW_UNSAFE_SPAWN=1` → allow + reason  
Fix: 補 3–4 支短測試。

[MINOR] 次級 skills 未寫 RETRY 協議  
File: `skills/omg-ralplan/SKILL.md`（~L48–63）、`skills/omg-dual-review/SKILL.md`（~L22）、`skills/omg-pipeline/SKILL.md`（無 capability/RETRY）  
Confidence: HIGH  
Issue: 主 multi-agent skills + orchestrator + AGENTS 已更新；ralplan/dual-review 仍要求 `capability_mode` 但 **沒寫** deny 後 RETRY。pipeline 無 capability spawn 說明。只載入這些 skill body 的 leader 仍可能 deny 後 solo-fallback。  
Fix: 各 skill HARD RULES / capability 段加一行（抄 omg-using）：
> If spawn DENIED for capability_mode: **RETRY IMMEDIATELY** same turn with the required mode. Do not abandon multi-agent.

[MINOR] 子字串 role 啟發式可能對怪異 type 名誤建議 mode  
File: `omg_cli/deny.py:121-128,137-140`  
Confidence: MEDIUM  
Issue: `"explore" in st` / `"executor" in st` 使 `explore-executor` 歸 RO；未知 `foo-review-bar` 可能建議 RO。對出貨 type 罕見；只影響 deny 文案 + role table。  
Fix: 優先 exact frozenset；文件註明 substring 為 best-effort；可選：衝突 keyword 時不套 substring。

[MINOR] 本審查 lane 無法 in-process 執行 pytest  
File: N/A（環境）  
Confidence: HIGH  
Issue: 本 reviewer subagent 無可用 shell，未能跑 `python3 -m pytest tests/test_deny.py -q` 或 `git diff 60d0882`。邏輯已對現有 `deny.py` + `test_deny.py` **靜態核對**（所有 RETRY assert 與實作一致）。  
Fix: parent/CI 必須跑：
```bash
cd <repo-root>
python3 -m pytest tests/test_deny.py -q
git -C <repo-root> diff 60d0882 --stat
```
pytest 綠燈視為 commit 前 hard gate。

---

## Edge-case 評估（靜態 walkthrough）

| Case | 預期 | Code path | 狀態 |
|------|------|-----------|------|
| explore，缺 mode | deny + RETRY + `capability_mode='read-only'` | missing → `suggested=read-only` | OK（有測） |
| general-purpose，缺 mode | deny + RETRY + `read-write` | missing → suggested RW | OK（有測） |
| general-purpose + read-only | deny mismatch + RETRY + RW | mismatch | OK（有測） |
| explore + read-write | deny mismatch + RETRY + RO | mismatch | OK（有測） |
| explore + read-only | allow | required match | OK（有測） |
| omg-executor + read-write | allow | required match | OK（有測） |
| Task + camelCase keys | critic RO 時 allow | `_spawn_fields` + Task alias | OK（有測） |
| execute mode | deny + RETRY + suggested（非 execute） | execute_all | 邏輯 OK；**測試弱** |
| all mode | 同 execute | execute_all | 邏輯 OK；**未測** |
| invalid mode `foo` | deny + RETRY + suggested | invalid | 邏輯 OK；**未測** |
| 空 subagent_type，無 mode | deny；建議 RW；label `(missing subagent_type)` | st empty → required None → default RW | 邏輯 OK；**未測** |
| 空 type + 明確 RO | allow | required None | 邏輯 OK；**未測** |
| `OMG_ALLOW_UNSAFE_SPAWN=1` | 一律 allow | early return | 邏輯 OK；**未測** |
| `read_only` / `read_write` underscore | normalize 後比對 | L207–210 | OK |
| 僅空白 mode | 視為 missing | strip → empty | OK |
| Hook exception | allow（fail-open） | `decide_pre_tool_use` except | 既有殘差 |

---

## RETRY-only UX 夠不夠？

**對本 patch 的目標：夠，且是正確的第一步。**

| 方案 | 安全 | Leader 恢復 | 產品誠實 |
|------|------|-------------|---------|
| **A. 僅 RETRY 文案（本變更）** | 維持 fail-closed | 依賴 model 讀 reason + skills | 誠實 |
| B. 缺 mode 就 auto-allow 預設 | 削弱 Option A | 高恢復 | 隔離敘事錯誤 |
| C. Host/CLI rewrite spawn args | 更強 UX | 最高恢復 | 需真實 host 支援；本變更未宣稱 |

**即使 RETRY UX 落地後仍屬 Important 的殘差：**
1. Soft-gate fail-open / 依賴全域安裝  
2. 無 live spawn-deny host oracle  
3. Model 仍可忽略文字（convention 層）  
4. 次級 skills（ralplan/dual/pipeline）訊息不完整  

以上都不使 RETRY 文案本身錯誤或不安全；只限制你能宣傳「leaders 一定恢復」的強度。

---

## Positive Observations（摘要）

- 安全與 UX 正確分離：deny 仍是 deny；reason 教恢復。  
- 建議 mode 依角色，不是泛用「隨便設一個 mode」。  
- Docs 仍寫 fail-open residual；主隔離是 host `capability_mode`。  
- 兩條關鍵產品路徑（explore RO / general-purpose RW）測試有 assert 關鍵字串。

---

## Recommendation

### Verdict: **Ready to proceed**

本 RETRY UX 實作本身無 CRITICAL / HIGH confidence **阻擋**缺陷。Spec 1–5 在主路徑滿足。可落地此 messaging fix；Important 殘差當 backlog（live canary、全域 soft-gate 誠實）；Minor 測試/文件若成本低可同 commit 順手補。

### 具體修正清單（可選：commit 前 / 下一 PR）

1. **（Minor，便宜）** 擴充 `test_spawn_execute_mode_denied`（+ `all`）assert `RETRY IMMEDIATELY` 與建議 `read-write`。  
2. **（Minor，便宜）** 補測：空 type；invalid mode；`OMG_ALLOW_UNSAFE_SPAWN=1`。  
3. **（Minor，便宜）** `omg-ralplan` / `omg-dual-review` / `omg-pipeline` 各加一行 RETRY。  
4. **（Important，backlog）** Live spawn-capability canary，deny reason 需 host signature。  
5. **（Hard gate）** Parent lane：`python3 -m pytest tests/test_deny.py -q` 綠燈才能 commit（本 lane 僅靜態驗證）。

### Open Questions（低信心 — 不阻擋）

- Grok host 是否穩定把多句 PreToolUse `reason` 完整塞進 model context（不截斷）？需一次 live 觀察。  
- 同 turn 多個平行 spawn 中只有一個 deny 時，RETRY 是否干擾 batching？多半 OK；建議 live ULW 確認。

---

## 審查檔案清單

| Path | 在本變更中的角色 |
|------|------------------|
| `omg_cli/deny.py` | `suggested_capability_mode`、`spawn_deny_reason`，接入所有 spawn deny |
| `tests/test_deny.py` | explore + general-purpose 的 RETRY assert |
| `AGENTS.md` / `templates/AGENTS.fragment.md` | 專案 hard rules：deny 後 RETRY |
| `agents/omg-orchestrator.md` | Spawn policy：一次 deny 不可當 multi-agent 取消 |
| `skills/omg-using`、`omg-ralph`、`omg-ultrawork` | HARD RULES / capability defaults |
| `omg_cli/modes.py` | HARD_RULES_REMINDER + capability spawn contract 注入 |
| `docs/security-model.md` | Option A 下 Deny UX 註記 |

**Repo 外（已註明，未審）：** `~/.agents/skills` 的 dual-review / multi-llm-council Fable argv notes。

---

## Final checklist

- [x] Spec compliance 先於 style  
- [x] Logic / security / edge cases 有 file:line 證據  
- [x] 每個 issue 有 severity + confidence + fix  
- [x] 含 positive observations  
- [x] Verdict 清楚：**Ready to proceed**  
- [ ] 本 lane 執行 `pytest tests/test_deny.py` — **僅靜態驗證；parent 必跑**  
- [x] Reviewer 未改產品碼（僅本報告路徑）
