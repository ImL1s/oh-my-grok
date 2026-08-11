# Team Security Research Composition Contract V1 (#69 PR12)

Hermetic Security Research contract: deterministic DAG compiler, fail-closed
manifest persistence, offline result production, and **shared composition
task-driver admission/collection** under the canonical Team run root.
Composition worker/pane/Jobs/PoC execution remains unsupported
(`execution_supported=false`).

Authoritative modules: `omg_cli.team.compositions.security_research` and
`omg_cli.team.compositions.task_driver`.

```bash
omg team security-research plan --spec SPEC.json [--json]
omg team security-research materialize --spec SPEC.json --run RUN_ID [--json]
omg team security-research validate-report --run RUN_ID --input REPORT.json [--json]
omg team security-research produce-report --run RUN_ID --input RESULT_BUNDLE.json [--json]
omg team security-research admit-tasks --run RUN_ID --team-id TEAM_ID [--json]
omg team security-research collect-tasks --run RUN_ID --team-id TEAM_ID [--json]
```

`plan` performs **zero** filesystem mutation. `materialize` atomically writes
only:

`.omg/state/runs/<run>/team/compositions/security-research-v1.json`

`admit-tasks` / `collect-tasks` share the Hyperplan PR12 driver
(`source.kind=security_research_v1`). Admission does not launch workers; collection
parses `LaneTaskResultV1` and invokes produce-report persistence (bundle first,
report commit marker last). Immutable safe-PoC policy is unchanged.

`produce-report` derives a report from a bounded
`SecurityResearchResultBundleV1` (exactly one receipt per manifest lane;
CLI-computed digests). Under the composition lock it writes
`security-research-v1-result-bundle.json`, then atomically writes
`security-research-v1-report.json` **last** as the commit marker. It never
creates Team tasks, launches panes/Jobs/providers, invokes MCP, runs
commands, accesses a network, or executes a PoC.

`validate-report` validates a supplied report artifact against the
materialized manifest (same proof gates). It may persist only when no
result-bundle exists (hand-authored path). If
`security-research-v1-result-bundle.json` is present, persist is refused
unless the report already matches the produce commit marker (idempotent
re-check) — validate-report never overwrites a produce-written report. It
never invents findings and never writes `passes` / `verified`.

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

Persisted manifests are recompiled from their normalized specs; the entire
canonical derived core must match (forged lane/dependency drift is refused
even if digests were re-stamped).

## Result bundle → Report

`compile_security_research_report_v1(manifest, bundle)` is pure.
`produce_security_research_report_v1(root, run_id, bundle)` is the locked
persistence wrapper.

The bundle is exact-key bounded and bound to `composition_id` +
`composition_digest`. Each receipt carries `lane_id`, `status`,
`artifact_kind`, schema-checked `payload`, and a CLI-computed canonical
`digest`. Hunt receipts carry candidates; validators carry per-candidate
validated/falsified dispositions and admissible proof modes; consolidate
carries surviving/rejected findings; verify carries covered lanes, blockers,
and a gate recommendation (advisory — verdict is derived).

Derived (never accepted as top-level caller assertions): lane coverage,
source digests (including `result_bundle`), findings, rejected candidates,
blockers, and verdict. Surviving findings must trace to hunt candidates and
the consolidate receipt. Validator dispositions must reference known
candidates. `pass` / `pass_with_findings` require every lane complete;
otherwise the derived verdict is `block`.

## Report contract / proof gates

`SecurityResearchReportV1` must cover **all** manifest lanes, bind
`composition_id` + `composition_digest`, and bind every lane artifact digest
under `source_artifact_digests`. Verdicts:

- `pass` — no surviving findings; all lanes complete
- `pass_with_findings` — findings exist, none blocking; all lanes complete
- `block` — a blocking finding or explicit `incomplete_audit_blockers` entry;
  blocked lanes may be preserved with reasons

A surviving finding requires attacker capability, concrete attack path,
reachability, impact, CWE candidate, evidence locations, remediation, and
regression check. `high`/`critical` additionally require:

- both validators validate that same finding/candidate
- `validator_artifact_refs` equal **exactly** the unordered pair of
  `validate.primary` and `validate.independent` coverage digests (no
  substitutes, extras, or duplicates)
- `reproduced` only when both validators record `local_fixture` reproduction;
  otherwise agreeing `safe_static_proof` (`static` / `dry_run`)

CVSS is accepted only with a complete base metric vector whose metric values
are CVSS 3.1 enums (not arbitrary strings). Falsified candidates belong only
in `rejected_candidates`.

## Fail-closed invariants

- Idempotent materialize / produce for identical digests
- Same `composition_id` with a different digest → refuse
- Conflicting existing result bundle/report → refuse
- Corrupt / truncated / symlinked / foreign-writer artifacts → refuse
- Failure between bundle write and report commit marker → no authoritative
  report
- Missing / cancelled run → refuse
- Never sets `verified` / `passes`
- Never launches panes, Jobs, providers, Antigravity, MCP, or PoC execution

## Honesty

Security Research V1 **result production landed** under #69 PR9. Catalog v4
atomic task-batch DAG admission landed under #69 PR11. Shared composition
task driver (admit-tasks / collect-tasks) landed under #69 PR12. Does
**not** close #69: composition execution, PoC running, model synthesis,
Hyperplan execution, live Antigravity evidence, and full OMX remain open.
Hyperplan hermetic result production landed under #69 PR10. Manifests retain
`execution_supported=false`. No `live_*` maturity claims.

Refs #69.
