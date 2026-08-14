# 双主机 agent / model 路由（投影）

[English (canonical)](./agent-model-routing.md) | [简体中文](./agent-model-routing.zh.md) | [繁體中文](./agent-model-routing.zh-TW.md)

**英文页是唯一规范来源。** 本页是投影，不是第二份架构。

支持矩阵、capability id、ownership 清单、route-kind 对照、human+JSON 示例一律以英文为准：

- [Decision（必备基线）](./agent-model-routing.md#decision-mandatory-baseline)
- [英文支持矩阵（唯一规范表）](./agent-model-routing.md#normative-support-matrix) — 八行表格仅英文页维护，本投影不复制
- [Ownership boundary](./agent-model-routing.md#ownership-boundary)
- [Native model route vs external executor](./agent-model-routing.md#native-model-route-vs-external-executor)
- [Legacy provider fields and route schema v1](./agent-model-routing.md#legacy-provider-fields-and-route-schema-v1)
- [Advisory plane vs task execution](./agent-model-routing.md#advisory-plane-vs-task-execution)
- [CLI / UX surfaces honesty](./agent-model-routing.md#cli--ux-surfaces-honesty)
- [Presentation ownership and accessibility](./agent-model-routing.md#presentation-ownership-and-accessibility)

**跟踪：** [oh-my-grok#133](https://github.com/ImL1s/oh-my-grok/issues/133) · 实现（本页不宣称已出货）：[#131](https://github.com/ImL1s/oh-my-grok/issues/131) · UX：[#134](https://github.com/ImL1s/oh-my-grok/issues/134) · 顾问平面：[#138](https://github.com/ImL1s/oh-my-grok/issues/138)

## 这是什么

本投影只摘要 oh-my-grok（OMG）agent／model **政策**如何对应 native host。它不实现 route 选择、Team 执行或凭证。运行时行为属 #131；host-neutral CLI／UX 投影属 #134。

## 必备基线与可选主机

- 原版 Grok Build 是必备 **first-class baseline**。
- Medley 是 **optional**／可选兼容主机，**不是** hard dependency。
- Medley **absent**／没有 Medley 时，agents / skills / workflows / evidence / acceptance 仍可用。
- Medley **absent** 的 hermetic 证据见英文页与 [`tests/test_stock_host_medley_absent.py`](../../tests/test_stock_host_medley_absent.py)。本投影不复制测试。
- 安装 OMG **不会**安装 Medley；安装 Medley **不是**标准 OMG 的前提。
- 增强 native 路由的 **Grok 侧** `omg agents list` / `explain` 已出货（read-only）。
  Medley exact／candidates／receipts 与 `/agents` TUI 仍为**计划中**
  （[medley#287](https://github.com/ImL1s/medley/issues/287) /
  [medley#290](https://github.com/ImL1s/medley/issues/290)），**尚未出货**。
- 现行 `omg doctor` 只回报现行 host/session capabilities，**不决定** Medley 路由可用性。
- External Team CLI executor（codex、agy、cursor、gemini）是另一组可选依赖，不是 Medley API／access route。

Stock Grok Build 是 **supported**，不是 legacy 或降级模式。本文件不宣称 OMG、Medley 或 xAI 之间有隶属关系。

## 用语：unsupported vs unavailable

两层词汇不可混用，也**不要**把这两个词用斜线写成可以互换。

- Stock host 上，Medley-only **capability outcome** 是 **unsupported**。
- 同一情境下，**route-specific facts**（receipt、ordered candidates、access／readiness）是 **unavailable**。
- 两者都不是安装 **failed**。
- 只有 **supported** 才授权使用该 capability。

完整 outcome 表与 capability 词汇见英文页 [Support states](./agent-model-routing.md#support-states)。本投影不复制 capability id 清单，也不重写英文支持矩阵。

## 政策 route class vs 已出货 Presentation kind

政策 route class（本页／#131 合约用语）：

- `native` — host 执行 child session／model route
- `external_executor` — OMG 启动并监督 CLI worker

已出货 Presentation `route.kind`（仅 `omg team status --presentation`）：

- `external_executor`
- `unknown`
- `native_host_receipt`

规则：

- 政策 `native` **不等于** `native_host_receipt`。
- 不要发明 `external_cli_executor` 或 `execution_kind`。
- 没有已出货的 `route.kind = "native"`。
- 默认锁定的 `omg team status --json` **没有** `route` / `route.kind`。

完整对照与 ownership 清单以英文为准：[Native model route vs external executor](./agent-model-routing.md#native-model-route-vs-external-executor)、[Ownership boundary](./agent-model-routing.md#ownership-boundary)。

HTTP `429` 不得单独授权换 provider 重送。细节见英文页 [Initial selection, retry, route fallback, worker replacement](./agent-model-routing.md#initial-selection-retry-route-fallback-worker-replacement)。

## Legacy provider 与 route schema v1

已出货 Presentation（不是 #131）：`route.schema` = 1；`route.kind` 才是判别栏。
`executor` 与 `provider` 可 **dual-carried**。`provider` **仅**在已 stamp 的
v1 dual-carry 路由可读；没有 stamp 的旧列投影 `unknown` / `provider` null，
**永不猜测**。
**永不**从 provider 文字推断 native／external。Reader 保留 unknown。
schema 变更需要另一次 versioned migration。

英文规范：[Legacy provider fields and route schema v1](./agent-model-routing.md#legacy-provider-fields-and-route-schema-v1)

## 顾问平面 vs 任务执行

政策 `native` / `external_executor` 只分类 **task_execution**，不是每一个 OMG 监督的 CLI。已出货的 `omg ask` 是 **advisory**，不是 Team executor，也不是 Medley API route。

三个正交维度（规范以英文为准）：

- `runtime_kind` = `native_host` | `external_cli`
- `purpose` = `advisory` | `task_execution`（权威任务参与；与 read-only / read-write posture **独立**。read-only / read-write posture 是 **requested OMG posture**，仅在所选 runtime/provider 提供 **qualified enforcement** 时生效；否则为 **unproven** / **unsupported**，或 route 被 blocked。不要把 role floor 当成 sandbox）
- `lifecycle` = `foreground` | `background_job` | `team_member`

`external_cli` + `advisory` **不是** external Team executor。`omg ask` 产物（`.omg/artifacts/ask-*.md`）与 consultation／council 产物都是 advisory／非权威，**永不**写入 acceptance / `verified`。本页不宣称已出货 council runtime。

英文规范：[Advisory plane vs task execution](./agent-model-routing.md#advisory-plane-vs-task-execution)。跟踪：[oh-my-grok#138](https://github.com/ImL1s/oh-my-grok/issues/138)。

## CLI 诚实（shipped vs contract-only）

| 表面 | 状态 |
|------|------|
| `omg doctor` / `omg doctor --json` | **已出货** — 只回报现行 host/session capabilities；**不决定** Medley 路由可用性 |
| `omg team status` / `omg team status --json` | **已出货** — 默认 `--json` **没有** `route` / `route.kind` |
| `omg agents list` | **已出货** — Grok baseline inspect；Medley facts 为 unsupported／unavailable |
| `omg agents explain <agent-or-profile>` | **已出货** — 同上。Medley `/agents` TUI（#290）仍是 **contract-only**，**今日不可运行** |

human+JSON 成对示例（含 stock host 的 unsupported／unavailable）只以英文页为准，见 [CLI / UX surfaces honesty](./agent-model-routing.md#cli--ux-surfaces-honesty)。本投影不复制那四组完整区块。

## UX 归属与无障碍

路由／后端完成 **不是** UI／TUI 完成。OMG **不拥有**任意 stock-host renderer／panel。

- Stock Grok Build：只经 host 已支持的宣告式 Agents／Tasks／child surfaces
- OMG：[#134](https://github.com/ImL1s/oh-my-grok/issues/134) 的 policy／Team／external-executor 投影（planned / contract-only）
- Medley：[ImL1s/medley#290](https://github.com/ImL1s/medley/issues/290) Agents／lifecycle TUI；[ImL1s/medley#207](https://github.com/ImL1s/medley/issues/207) provider／route／statusline

增强栏位 capability-gated；stock host 以 **unsupported**／**unavailable** 诚实回报。
narrow-width／no-color／无障碍是 **contract target**，本页不宣称已出货 runtime。

英文规范：[Presentation ownership and accessibility](./agent-model-routing.md#presentation-ownership-and-accessibility)。

## 请读英文页

规范来源：[`agent-model-routing.md`](./agent-model-routing.md)

Issue（稳定 GitHub URL）：

- [oh-my-grok#133](https://github.com/ImL1s/oh-my-grok/issues/133)
- [oh-my-grok#131](https://github.com/ImL1s/oh-my-grok/issues/131)
- [oh-my-grok#134](https://github.com/ImL1s/oh-my-grok/issues/134)
- [oh-my-grok#138](https://github.com/ImL1s/oh-my-grok/issues/138)
- [ImL1s/medley#287](https://github.com/ImL1s/medley/issues/287)
- [ImL1s/medley#289](https://github.com/ImL1s/medley/issues/289)
- [ImL1s/medley#207](https://github.com/ImL1s/medley/issues/207)
- [ImL1s/medley#290](https://github.com/ImL1s/medley/issues/290)

繁体投影：[agent-model-routing.zh-TW.md](./agent-model-routing.zh-TW.md)
