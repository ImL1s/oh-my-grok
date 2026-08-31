# 安裝 manifest（oh-my-grok）

`omg setup --runtime grok|antigravity|both --scope project|user` 會把 runtime、
scope、交易 ID、目標、雜湊與所有權寫入同一份版本化 manifest。project scope
不會從寬鬆的目前目錄猜測；user scope 不會建立目前專案的 `.omg`。

Antigravity 安裝使用官方 `agy plugin validate/install/enable/list`，再執行有
時間限制的 `agy --agent omg-explore` 並要求實際呼叫 `omg.tools.doctor` MCP
工具。`observed`、`healthy` 與 `live_verified` 是獨立層級；只有 hook 已註冊且
agent 執行成功呼叫 OMG MCP 工具時才會 `live_verified=true`。單純複製檔案不算。

同名但不同內容的外來 plugin 會保留並以 `E_CONFLICT` 中止。新安裝若在
discovery 階段失敗，會先移除該次新 plugin，再回復檔案交易；中斷交易會保留
recovery marker，下一次 setup 必須先完成 recovery。import/migrate 預設
dry-run、記錄 provenance，且不會隱含匯入憑證。uninstall 只移除 manifest
擁有且雜湊未變的內容，專案 `.omg/state` 不會連帶刪除。

完整契約與命令範例請見[英文版](install-manifest.md)。
