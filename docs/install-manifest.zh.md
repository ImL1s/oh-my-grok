# 安装 manifest（oh-my-grok）

`omg setup --runtime grok|antigravity|both --scope project|user` 会把 runtime、
scope、事务 ID、目标、哈希和所有权写入同一份版本化 manifest。project scope
不会从宽松的当前目录猜测；user scope 不会创建当前项目的 `.omg`。

Antigravity 安装使用官方 `agy plugin validate/install/enable/list`，再执行有
时间限制的 `agy --agent omg-explore` 并要求实际调用 `omg.tools.doctor` MCP
工具。`observed`、`healthy` 与 `live_verified` 是独立层级；只有 hook 已注册且
agent 成功调用 OMG MCP 工具时才会 `live_verified=true`。单纯复制文件不算。

同名但内容不同的外来 plugin 会被保留，并以 `E_CONFLICT` 中止。新安装若在
discovery 阶段失败，会先移除本次新 plugin，再回滚文件事务；中断事务会保留
recovery marker，下一次 setup 必须先完成 recovery。import/migrate 默认
dry-run、记录 provenance，且不会隐式导入凭证。uninstall 只移除 manifest
拥有且哈希未变化的内容，项目 `.omg/state` 不会被连带删除。

完整契约和命令示例见[英文版](install-manifest.md)。
