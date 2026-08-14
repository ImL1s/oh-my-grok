# oh-my-grok 文件（繁體中文）

English | [简体中文](./README.zh.md) | [繁體中文](./README.zh-TW.md)

English index: [`README.md`](./README.md)

Grok Build plugin + `omg` CLI 的使用者文件。  
**版本：** 見 [`plugin.json`](../plugin.json) · **Changelog：** [`CHANGELOG.md`](../CHANGELOG.md)

## 從這裡開始

| 文件 | 內容 |
|------|------|
| [./readme/README.zh-TW.md](./readme/README.zh-TW.md) | 安裝、心智模型、預設流程、skills 快捷表 |
| [../README.md](../README.md) | 英文完整 README（CLI 細節較全） |
| [skills.zh-TW.md](./skills.zh-TW.md) | **全部 skills 用法**（觸發詞、CLI、範例） |
| [skills.md](./skills.md) | 英文 skills 目錄 |
| [autopilot.zh-TW.md](./autopilot.zh-TW.md) | Autopilot 深講 |
| [autopilot.md](./autopilot.md) | Autopilot（英文） |
| [workflows.zh-TW.md](./workflows.zh-TW.md) | 版本化 repository workflows、receipt 與 ship gate |
| [workflows.md](./workflows.md) | Repository workflows（英文） |
| [security-model.md](./security-model.md) · [security-model.zh.md](./security-model.zh.md) · [security-model.zh-TW.md](./security-model.zh-TW.md) | 隔離誠實說明 |
| [architecture/agent-model-routing.zh-TW.md](./architecture/agent-model-routing.zh-TW.md) · [architecture/agent-model-routing.md](./architecture/agent-model-routing.md) · [architecture/agent-model-routing.zh.md](./architecture/agent-model-routing.zh.md) | 繁中**投影**；英文為 **canonical**；請勿另維一份 matrix |
| [hash-edit.md](./hash-edit.md) | Hash-anchored 編輯 V1 + `omg edit plan\|apply`（補充宿主編輯；不把未觀測宿主編輯當成 hash-anchored；不宣稱 `omo.edit.hash_anchored`；英文） |
| [visual-contract-v1.md](./visual-contract-v1.md) | Visual Contract V1（純比較 + `omg visual compare`；無 approved/passes/verified；無圖像 I/O；英文） |
| [hooks-lifecycle.md](./hooks-lifecycle.md) | 生命週期登錄表（#72；英文）：Grok PreToolUse/Stop 可攔截；SessionStart 被動；不注入 UserPromptSubmit |
| [tools-sidecar.md](./tools-sidecar.md) | Tools sidecar（#73；英文）：`omg tools`；不是 Grok 原生 LSP |
| [RELEASE.md](./RELEASE.md) · [RELEASE.zh.md](./RELEASE.zh.md) · [RELEASE.zh-TW.md](./RELEASE.zh-TW.md) | 維護者發版流程 |

## Skills 快速對照

| 想要… | Skill | CLI |
|-------|--------|-----|
| 哪個 mode？ | `omg-using` | `omg doctor` / `omg resume` |
| 全自動做到完 | `omg-autopilot` | `omg autopilot *` |
| 平行切片 | `omg-ultrawork` | `omg ulw` + worker/integrate |
| 堅持做到 verified | `omg-ralph` | `omg ralph` |
| 只做計畫 | `omg-ralplan` | `omg ralplan` |
| 釐清模糊目標 | `omg-deep-interview` | `omg interview *` |
| 多 story ledger | `omg-ultragoal` | `omg goal *` |
| QA 迴圈 | `omg-ultraqa` | `omg qa *` |
| 雙重審查 | `omg-dual-review` | `omg dual-review` / `omg review` |
| Pipeline FSM | `omg-pipeline` | `omg pipeline` |
| 外部顧問 | `omg-ask` | `omg ask` |
| 取消 | `omg-cancel` | `omg cancel` |
| Wiki / HUD / LSP | `omg-wiki` / `omg-hud` / `omg-lsp` | `omg wiki` / `hud` / `lsp` |
| 可重跑分階段審查 | repository workflow | `omg workflow install|list|show|plan|run` |
| 恢復、記憶、觀測 | 產品服務 | `omg recover` / `memory` / `tracker` / `compact` |

完整表格與可複製範例：**[skills.zh-TW.md](./skills.zh-TW.md)**。

## 研究文件（非日常）

歷史 parity / stop-continuation / live gates 在 [`research/`](./research/)。  
日常請用上面的產品文件。
