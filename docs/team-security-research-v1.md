# Team Security Research Composition Contract V1 (#69 PR8)

Non-executing Security Research scaffold: deterministic DAG compiler +
fail-closed manifest/report persistence under the canonical Team run root.

Authoritative module: `omg_cli.team.compositions.security_research`.

```bash
omg team security-research plan --spec SPEC.json [--json]
omg team security-research materialize --spec SPEC.json --run RUN_ID [--json]
omg team security-research validate-report --run RUN_ID --input REPORT.json [--json]
```

`plan` performs **zero** filesystem mutation. `materialize` atomically writes
only:

`.omg/state/runs/<run>/team/compositions/security-research-v1.json`

`validate-report` validates a supplied artifact against that manifest and
stores `.omg/state/runs/<run>/team/compositions/security-research-v1-report.json`.
It never invents findings and never writes `passes` / `verified`.

## Spec → Manifest

`SecurityResearchSpecV1` requires `schema_version=1`, exactly one of `target` or
`target_artifact`, and **3–8** unique attack surfaces (safe ids). Optional
`limits` / `evidence` descriptors are bounded; unknown fields are refused.

`compile_security_research_v1()` is pure and always stamps
`execution_supported=false` plus an **immutable** safe-PoC policy (static /
dry-run / local-fixture only; network, third-party targets, destructive
actions, target-side persistence, and ambient/real credentials forbidden).

For N surfaces it emits exactly **N+4** read-only lanes:

- `hunt.<surface>` × N — role `security-reviewer`
- `validate.primary` and `validate.independent` — role `verifier`, each
  depending on all hunters
- `consolidate` — role `security-reviewer`, depending on hunters + validators
- `verify` — role `verifier`, depending on every prior lane

Every lane has `requires_code_change=false`, `allow_implementation=false`,
empty `owned_files`, `posture=read-only`, and an explicit
`expected_artifact` schema. No worktree / provider / pane / Jobs / command
fields.

## Report contract

`SecurityResearchReportV1` must cover **all** manifest lanes, bind
`composition_id` + `composition_digest`, and bind every lane artifact digest
under `source_artifact_digests`. Verdicts:

- `pass` — no surviving findings; all lanes complete
- `pass_with_findings` — findings exist, none blocking; all lanes complete
- `block` — a blocking finding or explicit `incomplete_audit_blockers` entry;
  blocked lanes may be preserved with reasons

A surviving finding requires attacker capability, concrete attack path,
reachability, impact, CWE candidate, evidence locations, remediation, and
regression check. `high`/`critical` additionally require both validator
artifact references and `reproduced` or `safe_static_proof`. CVSS is accepted
only with a complete base metric vector. Falsified candidates belong only in
`rejected_candidates`.

## Fail-closed invariants

- Idempotent materialize for the same spec digest
- Same `composition_id` with a different digest → refuse
- Corrupt / truncated / symlinked / foreign-writer manifests → refuse
- Missing / cancelled run → refuse
- Never sets `verified` / `passes`
- Never launches panes, Jobs, providers, Antigravity, MCP, or PoC execution

## Honesty

Security Research V1 **contract/scaffolding landed** under #69 PR8. Does **not**
close #69: execution, PoC running, model synthesis, Hyperplan execution/result
production, live Antigravity evidence, catalog v4, and full OMX remain open.
No `live_*` maturity claims.

Refs #69.
