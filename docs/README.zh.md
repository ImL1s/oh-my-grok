# oh-my-grok 文档（简体中文）

English | [简体中文](./README.zh.md) | [繁體中文](./README.zh-TW.md)

English index: [`README.md`](./README.md)

Grok Build plugin + `omg` CLI 的使用者文档。  
**版本：** 见 [`plugin.json`](../plugin.json) · **Changelog：** [`CHANGELOG.md`](../CHANGELOG.md)

## 从这里开始

| 文件 | 内容 |
|------|------|
| [./readme/README.zh.md](./readme/README.zh.md) | 安装、心智模型、预设流程、skills 快捷表 |
| [../README.md](../README.md) | 英文完整 README（CLI 细节较全） |
| [skills.zh.md](./skills.zh.md) | **全部 skills 用法**（触发词、CLI、范例） |
| [skills.md](./skills.md) | 英文 skills 目录 |
| [autopilot.zh.md](./autopilot.zh.md) | Autopilot 深讲 |
| [autopilot.md](./autopilot.md) | Autopilot（英文） |
| [workflows.zh.md](./workflows.zh.md) | 版本化 repository workflows、receipt 与 ship gate |
| [workflows.md](./workflows.md) | Repository workflows（英文） |
| [security-model.md](./security-model.md) · [security-model.zh.md](./security-model.zh.md) · [security-model.zh-TW.md](./security-model.zh-TW.md) | 隔离诚实说明 |
| [RELEASE.md](./RELEASE.md) · [RELEASE.zh.md](./RELEASE.zh.md) · [RELEASE.zh-TW.md](./RELEASE.zh-TW.md) | 维护者发版流程 |

## Skills 快速对照

| 想要… | Skill | CLI |
|-------|--------|-----|
| 哪个 mode？ | `omg-using` | `omg doctor` / `omg resume` |
| 全自动做到完 | `omg-autopilot` | `omg autopilot *` |
| 平行切片 | `omg-ultrawork` | `omg ulw` + worker/integrate |
| 坚持做到 verified | `omg-ralph` | `omg ralph` |
| 只做计划 | `omg-ralplan` | `omg ralplan` |
| 厘清模糊目标 | `omg-deep-interview` | `omg interview *` |
| 多 story ledger | `omg-ultragoal` | `omg goal *` |
| QA 循环 | `omg-ultraqa` | `omg qa *` |
| 双重审查 | `omg-dual-review` | `omg dual-review` / `omg review` |
| Pipeline FSM | `omg-pipeline` | `omg pipeline` |
| 外部顾问 | `omg-ask` | `omg ask` |
| 取消 | `omg-cancel` | `omg cancel` |
| Wiki / HUD / LSP | `omg-wiki` / `omg-hud` / `omg-lsp` | `omg wiki` / `hud` / `lsp` |
| 可重跑分阶段审查 | repository workflow | `omg workflow install|list|show|plan|run` |
| 恢复、记忆、观测 | 产品服务 | `omg recover` / `memory` / `tracker` / `compact` |

完整表格与可复制范例：**[skills.zh.md](./skills.zh.md)**。

## 研究文件（非日常）

历史 parity / stop-continuation / live gates 在 [`research/`](./research/)。  
日常请用上面的产品文件。
