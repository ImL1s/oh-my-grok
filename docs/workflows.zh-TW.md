# Repository Workflows

English | [简体中文](./workflows.zh.md) | [繁體中文](./workflows.zh-TW.md)

OMG 0.6.0 提供產品自行擁有、可版本化的 workflow contract。它不是保存 prompt，而是固定 stages、依賴、matrix、角色、`capability_mode`、權限、驗證命令，以及獨立 verifier / skeptic 的 ship 規則。

## 能力邊界

| 表面 | 狀態 | 意義 |
|---|---|---|
| `repository-workflow/v1` | OMG 產品擁有 | 可編譯、安裝、規劃、journal，並做結構化 reconcile / replay；目前 public runner 無法驗證 `ship` authority。 |
| Grok `/create-workflow` 與 `.grok/workflows/*.rhai` | `optional_unclaimed` | 只有本地檔案或 help 文字，不代表公開 schema 已穩定，也不代表本次呼叫成功。 |

用 `omg capabilities` 或 `omg native-status` 查看分開的 `configured`、`installed`、`enabled`、`loadable`、`observed`、`healthy`、`verified` 證據。OMG 不碰私有 sidecar。

## 安裝與規劃

同一 `(name, workflow_version)` 的內容不可變；改內容必須升 semantic version。

```bash
omg workflow install ./production-safety-review.json
omg workflow list
omg workflow show production-safety-review --version 1.0.0

printf '%s\n' '{"candidate_commit":"abc123"}' > /tmp/workflow-input.json
omg workflow plan production-safety-review \
  --version 1.0.0 --input /tmp/workflow-input.json --generation 0 \
  > /tmp/workflow-plan.json
```

Plan 會固定 digest、task ID、依賴 waves、平行上限、actor identity、權限與 generation；不會啟動 agent，也不會執行 shell。

## 用 Grok 原生 subagents 執行

Leader 讀取 plan，依 wave 用 Grok `spawn_subagent` 派工，並傳入每個 task 的精確 `capability_mode`。Child 保持 depth 1。OMG CLI 不會用 Claude、Codex、Cursor、AGY 或其他 shell agent 代打。

Host / skill 收集每個 task 的 JSON receipt。結構上成功的 receipt 不是精簡結果，而是 exact `workflow_task_receipt`：完整綁定 repository、run、definition、plan、task、stage、matrix、actor 與 generation，並包含：

- `launch_provenance`：聲稱的 Grok provider、launch / session / agent instance ID，以及 `.omg/artifacts/workflow-launches/<run-id>/<task-id>.json` 內結構化 receipt 的 canonical hash；
- 每個 `verification_argv` 各一筆、順序一致的驗證 receipt，包含完全相同的 argv、零 exit code、受限 stdout / stderr 大小與 SHA256，以及 launch receipt hash；
- 每個 required artifact 各一筆 receipt，包含精確受限路徑、size、SHA256、宣告 schema 與 schema digest、目前 JSON 內容，以及 launch receipt hash；
- 整份 task receipt 的 canonical hash。

只有宣告本來為空時，對應 receipt array 才可明確為空。Verifier 與 skeptic 的 `status` 必須是 `approved`。這些檢查可拒絕格式錯誤、過期或重用資料，但 caller 自行建立的 ID、檔案、mode 或 plain hash 不會因此成為 authority。

## 對帳與發布判斷

```bash
omg workflow run production-safety-review \
  --version 1.0.0 --input /tmp/workflow-input.json \
  --receipts /tmp/workflow-receipts.json --generation 0 \
  --repository-permission read_repository \
  --repository-permission run_declared_verification \
  --repository-permission emit_declared_artifact \
  --host-capability read_repository \
  --host-capability run_declared_verification \
  --host-capability emit_declared_artifact \
  --launch-permission read_repository \
  --launch-permission run_declared_verification \
  --launch-permission emit_declared_artifact
```

Permission admission 是 repository policy、host capability、聲稱 launch permission 的交集。MCP 與寫入路徑還要明確的 `--allow-mcp` / `--allow-write-path`。CLI 會先驗 exact schema，child 會再驗一次，parent 接受前還會重讀 launch 與 artifact bytes。缺欄、多欄、重複、過期、外來、symlink、hash / schema 不符一律 fail closed。Callback 是 pure receipt resolver：只可讀取並回傳 canonical receipt 資料，不可啟動程序、連網、載入 native code、修改檔案或使用繼承的可寫 descriptor。`effect_type` 非空的 stage 會在 callback 啟動前以 `E_WORKFLOW_EFFECT_EXECUTOR_UNSAFE` fail closed。

目前 Grok 沒有 OMG 可驗證的公開 host-signed / product-authenticated spawn 與 command receipt API。因此 `omg workflow run --receipts` 與預設 product runner 即使收到全部 caller 自製的 APPROVE receipt，也會回報 `E_WORKFLOW_PRODUCT_AUTHORITY_UNAVAILABLE`、維持 `product_authority_verified: false`，並輸出 `no_ship` / unverified。DAG planning、permission admission、journal、結構化 reconcile 與 fail-closed replay 仍可使用；runner 不會設定一般 OMG run 的 `passes` / `verified`。

## 恢復與操作表面

```bash
omg session allocate
omg session route --resume <uuid> --fork-session --new-session-id <uuid>
omg recover ~/.grok/sessions/example.jsonl
omg memory put architecture "Python CLI plus Grok plugin"
omg tracker status --run <run-id>
omg compact show .omg/state/compaction/<key>/checkpoint.json
omg notify status
```

Recovery 只保留最新至多 900 個 physical lines 與 900 筆 parsed records，並在 2 MiB context 內保留至多 256 個完整 turns；輸出是不可變證據，且保留 `W_BROKEN_CHAIN` 與未知紀錄摘要。Memory 會 redact 且輸出確定。Tracker 是 passive、generation-fenced。Compaction 保留 guidance 原始 bytes。Notifications 只出站、沒有權威性。
