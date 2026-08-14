# Team Security Research Composition Contract V1 (#69 PR14)

Hermetic Security Research contract: deterministic DAG compiler, fail-closed
manifest persistence, offline result production, **shared composition
task-driver admission/collection**, **worker-scoped lane claim/submit**,
and **fixture-backed auto-worker execution** under the canonical Team run
root. Compile / produce / admit / collect / claim contracts keep
`execution_supported=false`. Immutable safe-PoC policy is unchanged: this
slice never runs a PoC. Live grok / agy / antigravity / cursor
auto-execution remains unsupported.

Authoritative modules: `omg_cli.team.compositions.security_research`,
`omg_cli.team.compositions.task_driver`,
`omg_cli.team.compositions.lane_protocol`, and
`omg_cli.team.compositions.execution`.

```bash
omg team security-research plan --spec SPEC.json [--json]
omg team security-research materialize --spec SPEC.json --run RUN_ID [--json]
omg team security-research validate-report --run RUN_ID --input REPORT.json [--json]
omg team security-research produce-report --run RUN_ID --input RESULT_BUNDLE.json [--json]
omg team security-research admit-tasks --run RUN_ID --team-id TEAM_ID [--json]
omg team security-research collect-tasks --run RUN_ID --team-id TEAM_ID [--json]
omg team security-research claim-lane --run RUN_ID --team-id TEAM_ID --lane-id LANE [--json]
omg team security-research submit-lane-result --run RUN_ID --team-id TEAM_ID --claim-file CLAIM.json --result RESULT.json [--json]
omg team security-research execute --run RUN_ID --team-id TEAM_ID --executor fixture --input RESULT_BUNDLE.json [--json]
```

`plan` performs **zero** filesystem mutation. `materialize`,
`validate-report`, `produce-report`, `admit-tasks`, `collect-tasks`, and
`execute` are **leader-only** and fail closed for a worker process or nested
first-party launch **before persist**; `claim-lane` / `submit-lane-result`
remain **worker-only**. `materialize` atomically writes
only:

`.omg/state/runs/<run>/team/compositions/security-research-v1.json`

`admit-tasks` / `collect-tasks` share the Hyperplan task driver
(`source.kind=security_research_v1`). Admission does not launch workers;
collection parses `LaneTaskResultV1` and invokes produce-report persistence
(bundle first, report commit marker last). Immutable safe-PoC policy is
unchanged. Leader-only.

`claim-lane` / `submit-lane-result` are **worker-only** and share the Hyperplan
PR13 lane protocol (`CompositionLaneClaimV1` + `LaneTaskResultV1` via existing
`claim-task` / `transition-task-status`). No PoC execution, network access, or
provider launch. `execution_supported=false` retained.

`execute` is **leader-only** and **fixture-only** (shared Hyperplan driver).
It runs claim-lane / submit-lane-result with fixture workers, then
collect-tasks, and writes `security-research-v1-execution.json`
(`omg.team.composition_execution_v1`) **last**. `--input` is a
`SecurityResearchResultBundleV1` and is normalized with the same exact-key /
foreign-writer / digest / `artifact_kind` contract as `produce-report`
**before** fixture workers submit `LaneTaskResultV1` payloads. Safe-PoC
policy is unchanged: no PoC, network, or weaponized research. grok / agy /
antigravity / cursor and job topology fail closed. Compile/produce/collect
stay `execution_supported=false`.

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
`security-research-v1-result-bundle.json` is present, persist is refused unless
the report already matches the produce commit marker (idempotent re-check) —
validate-report never overwrites a produce-written report. It never invents
or silently passes a report and never writes `passes` / `verified`.

## Spec → Manifest

`SecurityResearchSpecV1` requires `schema_version=1`, exactly one of `target`
or `target_artifact`, and **1–8** unique attack surfaces (safe ids). Optional
`limits` / `evidence` descriptors are bounded; unknown fields are refused.

`compile_security_research_v1()` is pure and always stamps immutable
`safe_poc_policy` plus `execution_supported=false`. For N surfaces it emits
exactly N+4 read-only lanes:

- `hunt.<surface>` × N (depends on nothing)
- `validate.primary` and `validate.independent` (depend on every hunt)
- `consolidate` (depends on hunts + both validators)
- `verify` (depends on the full prior set)

Every lane has `requires_code_change=false`, `allow_implementation=false`,
empty `owned_files`, `posture=read-only`, and an explicit
`expected_artifact` schema. No worktree / provider / pane / Jobs fields.

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
`digest`. Hunt receipts bind their declared surface. Validators must not
invent candidate IDs. Consolidation dispositions every surviving candidate.
Verification must cover every manifest lane exactly once.

Derived (never accepted as top-level caller assertions): lane coverage,
findings / rejected candidates, incomplete-audit blockers, source digests,
limitations (`execution_supported=false`, hermetic production), and verdict.
High/critical findings require dual-validator + allowed proof kinds.
CVSS requires a complete metric vector when present.

`pass` requires all lanes complete (or explicitly blocked under policy), no
blocking survivors, and verifier pass. Incomplete / contradictory claims fail
closed. Survivors that fail validation land in `rejected_candidates`.

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
- Worker claim/submit refuse leader / partial / non-Team spawn contexts
- Execute `--input` uses the ResultBundleV1 normalizer (foreign writer /
  claimed digest / artifact_kind / unexpected fields refused)
- Idempotent re-execute with a different per-lane result digest → refuse
- Interrupted fixture execute (some lanes completed, no execution artifact)
  stays refuse-until-repair; this slice does not auto-resume mixed state

## Honesty

Security Research V1 **result production landed** under #69 PR9. Catalog v4
atomic task-batch DAG admission landed under #69 PR11. Shared composition
task driver (admit-tasks / collect-tasks) landed under #69 PR12. Composition
lane worker protocol (claim-lane / submit-lane-result) landed under #69
PR13. Fixture-backed composition execution landed under #69 PR14
(`omg.team.composition_execution_v1`; compile/produce stay
`execution_supported=false`; safe-PoC policy unchanged). Does **not** close
#69: live grok / agy / antigravity / cursor auto-workers, PoC running, job-backed
live workers, host prompt-queue / fan-out consume, model synthesis, live
Antigravity evidence, and full OMX remain open. Hyperplan hermetic result
production landed under #69 PR10. Manifests retain
`execution_supported=false`. No `live_*` maturity claims. Catalog stays v4
(execute is a CLI/Python path, not a catalog op).

Refs #69.
