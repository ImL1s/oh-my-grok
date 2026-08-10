# Team Hyperplan Composition Contract V1 (#69 PR7)

Non-executing Hyperplan scaffold: deterministic DAG compiler + fail-closed
manifest/decision persistence under the canonical Team run root.

Authoritative module: `omg_cli.team.compositions.hyperplan`.

```bash
omg team hyperplan plan --spec SPEC.json [--json]
omg team hyperplan materialize --spec SPEC.json --run RUN_ID [--json]
omg team hyperplan validate-decision --run RUN_ID --input DECISION.json [--json]
```

`plan` performs **zero** filesystem mutation. `materialize` atomically writes
only:

`.omg/state/runs/<run>/team/compositions/hyperplan-v1.json`

`validate-decision` validates a supplied artifact against that manifest and
stores `.omg/state/runs/<run>/team/compositions/hyperplan-v1-decision.json`.
It never invents or silently approves a decision.

## Spec → Manifest

`HyperplanSpecV1` requires `schema_version=1`, exactly one of `goal` or
`plan_artifact`, and **3–8** unique critique dimensions (safe ids). Optional
`limits` / `evidence` descriptors are bounded; unknown fields are refused.

`compile_hyperplan_v1()` is pure and always stamps `execution_supported=false`.
It emits:

- one adversarial `critic` lane per distinct dimension (sorted)
- one read-only `planner` `synthesize` lane depending on every critic
- one read-only `verifier` `verify` lane depending on synthesis + all critics

Every lane has `requires_code_change=false`, `allow_implementation=false`,
empty `owned_files`, `posture=read-only`, and an explicit
`expected_artifact` schema. No worktree / provider / pane / Jobs fields.

## Decision contract

`HyperplanDecisionV1` must cover **all** manifest lanes, bind
`composition_id` + `composition_digest`, and include
`source_artifact_digests.composition`. `verdict=approved` additionally
requires empty `conflicts`, `required_repairs`, and `unresolved_risks`, plus
every lane `status=complete`. Incomplete coverage can only be stored under
`verdict=rejected` (or fails closed).

## Fail-closed invariants

- Idempotent materialize for the same spec digest
- Same `composition_id` with a different digest → refuse
- Corrupt / truncated / symlinked / foreign-writer manifests → refuse
- Missing / cancelled run → refuse
- Never sets `verified` / `passes`
- Never launches panes, Jobs, providers, Antigravity, or MCP

## Honesty

Hyperplan V1 **contract/scaffolding landed** under #69 PR7. Does **not**
close #69: execution, result production, model synthesis, security
compositions, live Antigravity evidence, catalog v4, and full OMX remain
open. No `live_*` maturity claims.

Refs #69.
