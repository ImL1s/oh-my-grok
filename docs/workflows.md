# Repository Workflows

English | [简体中文](./workflows.zh.md) | [繁體中文](./workflows.zh-TW.md)

OMG 0.6.0 adds a product-owned, versioned workflow contract for repeatable multi-agent reviews. A workflow is not a saved prompt. It fixes stages, dependencies, matrices, roles, capability modes, permission requests, verification commands, and the independent verifier/skeptic ship rule.

## Capability boundary

| Surface | Status | Meaning |
|---|---|---|
| `repository-workflow/v1` | Product-owned | OMG compiles, installs, plans, journals, and structurally reconciles/replays evidence. The public runner cannot currently authenticate `ship`. |
| Grok `/create-workflow` + `.grok/workflows/*.rhai` | `optional_unclaimed` | Local files or help text are not proof of a stable public schema or a successful current invocation. |

Run `omg capabilities` or `omg native-status` for independent `configured`, `installed`, `enabled`, `loadable`, `observed`, `healthy`, and `verified` claims. OMG never probes private sidecars.

## Install and inspect

Definitions are immutable by `(name, workflow_version)`. Changing bytes requires a new semantic version and migration metadata.

```bash
omg workflow install ./production-safety-review.json
omg workflow list
omg workflow show production-safety-review --version 1.0.0
```

Installed definitions live under `.omg/workflows/registry/`. Same-version byte drift, cycles, nested workflows, mutable actor identity, and verifier/author identity reuse fail closed.

## Plan

Input is JSON and must satisfy the definition's input schema.

```bash
printf '%s\n' '{"candidate_commit":"abc123"}' > /tmp/workflow-input.json
omg workflow plan production-safety-review \
  --version 1.0.0 --input /tmp/workflow-input.json --generation 0 \
  > /tmp/workflow-plan.json
```

The plan fixes a digest, deterministic task IDs, dependency waves, maximum parallelism, actor identities, permission requests, and generation. Planning does not launch agents or execute shell commands.

## Execute with native subagents

The leader reads `/tmp/workflow-plan.json`, spawns each ready task through Grok's native `spawn_subagent`, and always supplies the task's exact `capability_mode`. Children remain depth 1. The host or skill collects one JSON receipt per task. OMG's CLI never substitutes Claude, Codex, Cursor, AGY, or another shell agent.

Receipts may be a task-ID map or an array containing `task_id`. A structurally successful
receipt is deliberately not minimal. It is an exact
`workflow_task_receipt` object binding the repository, run, definition, plan,
task, stage, matrix index, actor, and generation. It also carries:

- `launch_provenance`: claimed Grok provider plus launch, session, and agent
  instance IDs, bound to a canonical structural receipt at
  `.omg/artifacts/workflow-launches/<run-id>/<task-id>.json`;
- one ordered verification receipt for every declared `verification_argv`,
  with the exact argv, exit code zero, bounded stdout/stderr sizes and hashes,
  and the launch-receipt hash;
- one artifact receipt for every required artifact, with the exact confined
  path, size, SHA256, declared schema and schema digest, current JSON content,
  and the launch-receipt hash;
- a canonical hash over the complete task receipt.

An explicit empty verification or artifact receipt array is valid only when
the corresponding declaration is empty. Verifier and skeptic tasks use
`status: "approved"`. These checks reject malformed, stale, or reused data,
but caller-produced IDs, files, modes, and plain hashes are not authority.

## Reconcile and decide

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

Permission admission is the intersection of repository policy, host capabilities, and claimed launch permissions. MCP names and write paths require explicit `--allow-mcp` / `--allow-write-path`. The CLI validates the exact receipt schema before invoking the pure resolver. The child validates it again, and the parent re-reads launch and artifact bytes before structural acceptance. Missing, extra, duplicated, stale, foreign, symlinked, hash-mismatched, or schema-mismatched evidence fails closed. Results are journaled under `.omg/artifacts/workflow-runs/<run-id>/`. The callback may read and return canonical receipt data, but may not spawn processes, access the network, load native code, mutate files, or use inherited writable descriptors. A stage with non-null `effect_type` fails closed with `E_WORKFLOW_EFFECT_EXECUTOR_UNSAFE` before its callback starts.

Grok currently exposes no public host-signed/product-authenticated spawn and
command receipt API that OMG can verify. Therefore `omg workflow run
--receipts` and the default product runner always report
`E_WORKFLOW_PRODUCT_AUTHORITY_UNAVAILABLE`, keep
`product_authority_verified: false`, and return `no_ship`/unverified even when
every caller-produced receipt claims approval. DAG planning, permission
admission, journaling, structural reconciliation, and fail-closed replay remain
usable. The runner never sets ordinary OMG run `passes` or `verified`.

## Recovery and operational surfaces

```bash
omg session allocate
omg session route --resume <uuid> --fork-session --new-session-id <uuid>
omg recover ~/.grok/sessions/example.jsonl
omg memory put architecture "Python CLI plus Grok plugin"
omg tracker status --run <run-id>
omg compact show .omg/state/compaction/<key>/checkpoint.json
omg notify status
```

Recovery retains at most the newest 900 physical lines and 900 parsed records in immutable evidence, then keeps at most 256 complete turns within a 2 MiB context. It preserves warnings such as `W_BROKEN_CHAIN` and unknown-record summaries. Memory is redacted and deterministic. Tracker projection is passive and generation-fenced. Compaction preserves exact guidance bytes. Notifications are outbound-only and non-authoritative.
