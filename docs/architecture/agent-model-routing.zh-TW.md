# 雙主機 agent / model 路由（投影）

[English (canonical)](./agent-model-routing.md) | [简体中文](./agent-model-routing.zh.md) | [繁體中文](./agent-model-routing.zh-TW.md)

**英文頁是唯一規範來源。** 本頁是投影，不是第二份架構。

支援矩陣、capability id、ownership 清單、route-kind 對照、human+JSON 範例一律以英文為準：

- [Decision（必備基線）](./agent-model-routing.md#decision-mandatory-baseline)
- [英文支援矩陣（唯一規範表）](./agent-model-routing.md#normative-support-matrix)
- [Ownership boundary](./agent-model-routing.md#ownership-boundary)
- [Native model route vs external executor](./agent-model-routing.md#native-model-route-vs-external-executor)
- [Advisory plane vs task execution](./agent-model-routing.md#advisory-plane-vs-task-execution)
- [CLI / UX surfaces honesty](./agent-model-routing.md#cli--ux-surfaces-honesty)

**追蹤：** [oh-my-grok#133](https://github.com/ImL1s/oh-my-grok/issues/133) · 實作（本頁不宣稱已出貨）：[#131](https://github.com/ImL1s/oh-my-grok/issues/131) · UX：[#134](https://github.com/ImL1s/oh-my-grok/issues/134) · 顧問平面：[#138](https://github.com/ImL1s/oh-my-grok/issues/138)

## 這是什麼

本投影只摘要 oh-my-grok（OMG）agent／model **政策**如何對應 native host。它不實作 route 選擇、Team 執行或憑證。執行行為屬 #131；host-neutral CLI／UX 投影屬 #134。

## 必備基線與可選主機

- 原版 Grok Build 是必備 **first-class baseline**。
- Medley 是 **optional**／可選相容主機，**不是** hard dependency。
- Medley **absent**／沒有 Medley 時，agents / skills / workflows / evidence / acceptance 仍可用。
- 安裝 OMG **不會**安裝 Medley；安裝 Medley **不是**標準 OMG 的前提。
- 增強 native 路由與 Medley 端 negotiation 為**規劃中（#131）**，**尚未出貨**。
- 現行 `omg doctor` 只回報現行 host/session capabilities，**不決定** Medley 路由可用性。
- External Team CLI executor（codex、agy、cursor、gemini）是另一組可選依賴，不是 Medley API／access route。

Stock Grok Build 是 **supported**，不是 legacy 或降級模式。本文件不宣稱 OMG、Medley 或 xAI 之間有隸屬關係。

## 用語：unsupported vs unavailable

兩層詞彙不可混用，也**不要**把這兩個詞用斜線寫成可以互換。

- Stock host 上，Medley-only **capability outcome** 是 **unsupported**。
- 同一情境下，**route-specific facts**（receipt、ordered candidates、access／readiness）是 **unavailable**。
- 兩者都不是安裝 **failed**。
- 只有 **supported** 才授權使用該 capability。

完整 outcome 表與 capability 詞彙見英文頁 [Support states](./agent-model-routing.md#support-states)。本投影不複製 capability id 清單，也不重寫英文支援矩陣。

## 政策 route class vs 已出貨 Presentation kind

政策 route class（本頁／#131 合約用語）：

- `native` — host 執行 child session／model route
- `external_executor` — OMG 啟動並監督 CLI worker

已出貨 Presentation `route.kind`（僅 `omg team status --presentation`）：

- `external_executor`
- `unknown`
- `native_host_receipt`

規則：

- 政策 `native` **不等於** `native_host_receipt`。
- 不要發明 `external_cli_executor` 或 `execution_kind`。
- 沒有已出貨的 `route.kind = "native"`。
- 預設鎖定的 `omg team status --json` **沒有** `route` / `route.kind`。

完整對照與 ownership 清單以英文為準：[Native model route vs external executor](./agent-model-routing.md#native-model-route-vs-external-executor)、[Ownership boundary](./agent-model-routing.md#ownership-boundary)。

HTTP `429` 不得單獨授權換 provider 重送。細節見英文頁 [Initial selection, retry, route fallback, worker replacement](./agent-model-routing.md#initial-selection-retry-route-fallback-worker-replacement)。

## 顧問平面 vs 任務執行

政策 `native` / `external_executor` 只分類 **task_execution**，不是每一個 OMG 監督的 CLI。已出貨的 `omg ask` 是 **advisory**，不是 Team executor，也不是 Medley API route。

三個正交維度（規範以英文為準）：

- `runtime_kind` = `native_host` | `external_cli`
- `purpose` = `advisory` | `task_execution`
- `lifecycle` = `foreground` | `background_job` | `team_member`

`external_cli` + `advisory` **不是** external Team executor。`omg ask` 產物（`.omg/artifacts/ask-*.md`）與 consultation／council 產物都是 advisory／非權威，**永不**寫入 acceptance / `verified`。本頁不宣稱已出貨 council runtime。

英文規範：[Advisory plane vs task execution](./agent-model-routing.md#advisory-plane-vs-task-execution)。追蹤：[oh-my-grok#138](https://github.com/ImL1s/oh-my-grok/issues/138)。

## CLI 誠實（shipped vs contract-only）

| 表面 | 狀態 |
|------|------|
| `omg doctor` / `omg doctor --json` | **已出貨** — 只回報現行 host/session capabilities；**不決定** Medley 路由可用性 |
| `omg team status` / `omg team status --json` | **已出貨** — 預設 `--json` **沒有** `route` / `route.kind` |
| `omg agents list` | **contract-only**，planned #131/#134，**今日不可跑** |
| `omg agents explain` | **contract-only**，planned #131/#134，**今日不可跑** |

human+JSON 成對範例（含 stock host 的 unsupported／unavailable）只以英文頁為準，見 [CLI / UX surfaces honesty](./agent-model-routing.md#cli--ux-surfaces-honesty)。本投影不複製那四組完整區塊。

## 請讀英文頁

規範來源：[`agent-model-routing.md`](./agent-model-routing.md)

Issue（穩定 GitHub URL）：

- [oh-my-grok#133](https://github.com/ImL1s/oh-my-grok/issues/133)
- [oh-my-grok#131](https://github.com/ImL1s/oh-my-grok/issues/131)
- [oh-my-grok#134](https://github.com/ImL1s/oh-my-grok/issues/134)
- [oh-my-grok#138](https://github.com/ImL1s/oh-my-grok/issues/138)
- [ImL1s/medley#287](https://github.com/ImL1s/medley/issues/287)
- [ImL1s/medley#289](https://github.com/ImL1s/medley/issues/289)

簡體投影：[agent-model-routing.zh.md](./agent-model-routing.zh.md)
