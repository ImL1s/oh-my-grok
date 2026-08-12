# 双主机 agent / model 路由（投影）

[English (canonical)](./agent-model-routing.md) | [简体中文](./agent-model-routing.zh.md) | [繁體中文](./agent-model-routing.zh-TW.md)

**英文页是唯一规范来源。** 本页是投影，不是第二份架构。

支持矩阵、capability id、ownership 清单、route-kind 对照、human+JSON 示例一律以英文为准：

- [Decision（必备基线）](./agent-model-routing.md#decision-mandatory-baseline)
- [英文支持矩阵（唯一规范表）](./agent-model-routing.md#normative-support-matrix)
- [Ownership boundary](./agent-model-routing.md#ownership-boundary)
- [Native model route vs external executor](./agent-model-routing.md#native-model-route-vs-external-executor)
- [CLI / UX surfaces honesty](./agent-model-routing.md#cli--ux-surfaces-honesty)

**跟踪：** [oh-my-grok#133](https://github.com/ImL1s/oh-my-grok/issues/133) · 实现（本页不宣称已出货）：[#131](https://github.com/ImL1s/oh-my-grok/issues/131) · UX：[#134](https://github.com/ImL1s/oh-my-grok/issues/134)

## 这是什么

本投影只摘要 oh-my-grok（OMG）agent／model **政策**如何对应 native host。它不实现 route 选择、Team 执行或凭证。运行时行为属 #131；host-neutral CLI／UX 投影属 #134。

## 必备基线与可选主机

- 原版 Grok Build 是必备 **first-class baseline**。
- Medley 是 **optional**／可选兼容主机，**不是** hard dependency。
- Medley **absent**／没有 Medley 时，agents / skills / workflows / evidence / acceptance 仍可用。
- 安装 OMG **不会**安装 Medley；安装 Medley **不是**标准 OMG 的前提。
- 增强 native 路由与 Medley 端 negotiation 为**计划中（#131）**，**尚未出货**。
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

## CLI 诚实（shipped vs contract-only）

| 表面 | 状态 |
|------|------|
| `omg doctor` / `omg doctor --json` | **已出货** — 只回报现行 host/session capabilities；**不决定** Medley 路由可用性 |
| `omg team status` / `omg team status --json` | **已出货** — 默认 `--json` **没有** `route` / `route.kind` |
| `omg agents list` | **contract-only**，planned #131/#134，**今日不可运行** |
| `omg agents explain` | **contract-only**，planned #131/#134，**今日不可运行** |

human+JSON 成对示例（含 stock host 的 unsupported／unavailable）只以英文页为准，见 [CLI / UX surfaces honesty](./agent-model-routing.md#cli--ux-surfaces-honesty)。本投影不复制那四组完整区块。

## 请读英文页

规范来源：[`agent-model-routing.md`](./agent-model-routing.md)

Issue（稳定 GitHub URL）：

- [oh-my-grok#133](https://github.com/ImL1s/oh-my-grok/issues/133)
- [oh-my-grok#131](https://github.com/ImL1s/oh-my-grok/issues/131)
- [oh-my-grok#134](https://github.com/ImL1s/oh-my-grok/issues/134)
- [ImL1s/medley#287](https://github.com/ImL1s/medley/issues/287)
- [ImL1s/medley#289](https://github.com/ImL1s/medley/issues/289)

繁体投影：[agent-model-routing.zh-TW.md](./agent-model-routing.zh-TW.md)
