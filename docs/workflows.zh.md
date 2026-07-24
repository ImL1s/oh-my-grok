# Repository Workflows

English | [简体中文](./workflows.zh.md) | [繁體中文](./workflows.zh-TW.md)

OMG 0.6.0 提供产品自行拥有、可版本化的 workflow contract。它不是保存 prompt，而是固定 stages、依赖、matrix、角色、`capability_mode`、权限、验证命令，以及独立 verifier / skeptic 的 ship 规则。

## 能力边界

| 表面 | 状态 | 意义 |
|---|---|---|
| `repository-workflow/v1` | OMG 产品拥有 | 可编译、安装、规划、journal，并做结构化 reconcile / replay；目前 public runner 无法验证 `ship` authority。 |
| Grok `/create-workflow` 与 `.grok/workflows/*.rhai` | `optional_unclaimed` | 只有本地档案或 help 文字，不代表公开 schema 已稳定，也不代表本次呼叫成功。 |

用 `omg capabilities` 或 `omg native-status` 查看分开的 `configured`、`installed`、`enabled`、`loadable`、`observed`、`healthy`、`verified` 证据。OMG 不碰私有 sidecar。

## 安装与规划

同一 `(name, workflow_version)` 的内容不可变；改内容必须升 semantic version。

```bash
omg workflow install ./production-safety-review.json
omg workflow list
omg workflow show production-safety-review --version 1.0.0

printf '%s\n' '{"candidate_commit":"abc123"}' > /tmp/workflow-input.json
omg workflow plan production-safety-review \
  --version 1.0.0 --input /tmp/workflow-input.json --generation 0 \
  > /tmp/workflow-plan.json
```

Plan 会固定 digest、task ID、依赖 waves、平行上限、actor identity、权限与 generation；不会启动 agent，也不会执行 shell。

## 用 Grok 原生 subagents 执行

Leader 读取 plan，依 wave 用 Grok `spawn_subagent` 派工，并传入每个 task 的精确 `capability_mode`。Child 保持 depth 1。OMG CLI 不会用 Claude、Codex、Cursor、AGY 或其他 shell agent 代打。

Host / skill 收集每个 task 的 JSON receipt。结构上成功的 receipt 不是精简结果，而是 exact `workflow_task_receipt`：完整绑定 repository、run、definition、plan、task、stage、matrix、actor 与 generation，并包含：

- `launch_provenance`：声称的 Grok provider、launch / session / agent instance ID，以及 `.omg/artifacts/workflow-launches/<run-id>/<task-id>.json` 内结构化 receipt 的 canonical hash；
- 每个 `verification_argv` 各一笔、顺序一致的验证 receipt，包含完全相同的 argv、零 exit code、受限 stdout / stderr 大小与 SHA256，以及 launch receipt hash；
- 每个 required artifact 各一笔 receipt，包含精确受限路径、size、SHA256、宣告 schema 与 schema digest、目前 JSON 内容，以及 launch receipt hash；
- 整份 task receipt 的 canonical hash。

只有宣告本来为空时，对应 receipt array 才可明确为空。Verifier 与 skeptic 的 `status` 必须是 `approved`。这些检查可拒绝格式错误、过期或重用资料，但 caller 自行建立的 ID、档案、mode 或 plain hash 不会因此成为 authority。

## 对账与发布判断

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

Permission admission 是 repository policy、host capability、声称 launch permission 的交集。MCP 与写入路径还要明确的 `--allow-mcp` / `--allow-write-path`。CLI 会先验 exact schema，child 会再验一次，parent 接受前还会重读 launch 与 artifact bytes。缺栏、多栏、重复、过期、外来、symlink、hash / schema 不符一律 fail closed。Callback 是 pure receipt resolver：只可读取并回传 canonical receipt 资料，不可启动程序、连网、载入 native code、修改档案或使用继承的可写 descriptor。`effect_type` 非空的 stage 会在 callback 启动前以 `E_WORKFLOW_EFFECT_EXECUTOR_UNSAFE` fail closed。

目前 Grok 没有 OMG 可验证的公开 host-signed / product-authenticated spawn 与 command receipt API。因此 `omg workflow run --receipts` 与预设 product runner 即使收到全部 caller 自制的 APPROVE receipt，也会回报 `E_WORKFLOW_PRODUCT_AUTHORITY_UNAVAILABLE`、维持 `product_authority_verified: false`，并输出 `no_ship` / unverified。DAG planning、permission admission、journal、结构化 reconcile 与 fail-closed replay 仍可使用；runner 不会设定一般 OMG run 的 `passes` / `verified`。

## 恢复与操作表面

```bash
omg session allocate
omg session route --resume <uuid> --fork-session --new-session-id <uuid>
omg recover ~/.grok/sessions/example.jsonl
omg memory put architecture "Python CLI plus Grok plugin"
omg tracker status --run <run-id>
omg compact show .omg/state/compaction/<key>/checkpoint.json
omg notify status
```

Recovery 只保留最新至多 900 个 physical lines 与 900 笔 parsed records，并在 2 MiB context 内保留至多 256 个完整 turns；输出是不可变证据，且保留 `W_BROKEN_CHAIN` 与未知纪录摘要。Memory 会 redact 且输出确定。Tracker 是 passive、generation-fenced。Compaction 保留 guidance 原始 bytes。Notifications 只出站、没有权威性。
