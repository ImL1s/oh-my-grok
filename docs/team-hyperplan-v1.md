# Team Hyperplan Composition Contract V1 (#69 PR13)

Hermetic Hyperplan contract: deterministic DAG compiler, fail-closed
manifest persistence, offline result production, **shared composition
task-driver admission/collection**, and **worker-scoped lane claim/submit**
under the canonical Team run root. Composition worker/pane/Jobs/provider
**automatic** execution remains unsupported (`execution_supported=false`).

Authoritative modules: `omg_cli.team.compositions.hyperplan`,
`omg_cli.team.compositions.task_driver`, and
`omg_cli.team.compositions.lane_protocol`.

```bash
omg team hyperplan plan --spec SPEC.json [--json]
omg team hyperplan materialize --spec SPEC.json --run RUN_ID [--json]
omg team hyperplan validate-decision --run RUN_ID --input DECISION.json [--json]
omg team hyperplan produce-decision --run RUN_ID --input RESULT_BUNDLE.json [--json]
omg team hyperplan admit-tasks --run RUN_ID --team-id TEAM_ID [--json]
omg team hyperplan collect-tasks --run RUN_ID --team-id TEAM_ID [--json]
omg team hyperplan claim-lane --run RUN_ID --team-id TEAM_ID --lane-id LANE [--json]
omg team hyperplan submit-lane-result --run RUN_ID --team-id TEAM_ID --claim-file CLAIM.json --result RESULT.json [--json]
```

`plan` performs **zero** filesystem mutation. `materialize`,
`validate-decision`, `produce-decision`, `admit-tasks`, and `collect-tasks`
are **leader-only** and fail closed for a worker process or nested first-party
launch **before persist**; `claim-lane` / `submit-lane-result` remain
**worker-only**. `materialize` atomically writes
only:

`.omg/state/runs/<run>/team/compositions/hyperplan-v1.json`

`admit-tasks` revalidates the materialized manifest, compiles a deterministic
PR11 `TaskBatchV1` (`source.kind=hyperplan_v1`, `task_key=lane_id`,
`requires_code_change=false`), and calls `admit_task_batch_v1`. It does **not**
launch workers. Leader-only.

`claim-lane` / `submit-lane-result` are **worker-only**. They require a complete
Team worker environment (`OMG_TEAM_WORKER` + identity + run/team + leader/state
roots + owner token), resolve the authoritative leader root, bind `lane_id` to
the immutable PR12/PR11 task, call existing `claim-task` /
`transition-task-status`, and never write task files directly. `claim-lane`
returns a bounded `CompositionLaneClaimV1` envelope (goal/evidence + validated
dependency outputs; never the leader conversation). Human output redacts the
claim token; machine `--json` may expose it for the worker. Submit consumes a
claim file (no `--claim-token` argv). Rejected/blocked lane results still
transition the Team task to `completed`. `execution_supported=false` retained.

`collect-tasks` remains leader-only: requires a committed batch with exact lane
coverage, every mapped task `completed` + claim-free + still bound, parses
worker `LaneTaskResultV1` JSON (workers must not supply lane/digest/writer
identity), builds the existing result bundle, and invokes produce-decision
persistence (bundle first, decision commit marker last). Failures write no
authoritative decision.

`produce-decision` derives a decision from a bounded
`HyperplanResultBundleV1` (exactly one receipt per manifest lane;
CLI-computed digests). Under the composition lock it writes
`hyperplan-v1-result-bundle.json`, then atomically writes
`hyperplan-v1-decision.json` **last** as the commit marker. It never
creates Team tasks, launches panes/Jobs/providers, or invokes MCP.

`validate-decision` validates a supplied decision artifact against the
materialized manifest (same approval gates). It may persist only when no
result-bundle exists (hand-authored path). If
`hyperplan-v1-result-bundle.json` is present, persist is refused unless the
decision already matches the produce commit marker (idempotent re-check) —
validate-decision never overwrites a produce-written decision. It never
invents or silently approves a decision and never writes `passes` /
`verified`.

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

Persisted manifests are recompiled from their normalized specs; the entire
canonical derived core must match (forged lane/dependency drift is refused
even if digests were re-stamped).

## Result bundle → Decision

`compile_hyperplan_decision_v1(manifest, bundle)` is pure.
`produce_hyperplan_decision_v1(root, run_id, bundle)` is the locked
persistence wrapper.

The bundle is exact-key bounded and bound to `composition_id` +
`composition_digest`. Each receipt carries `lane_id`, `status`,
`artifact_kind`, schema-checked `payload`, and a CLI-computed canonical
`digest`. Critic receipts bind their declared dimension and bounded
findings (finding IDs globally unique). Synthesis must disposition every
critic finding exactly once and cannot invent IDs. Verification must cover
every manifest lane exactly once.

Derived (never accepted as top-level caller assertions): lane coverage,
conflicts (from synthesis `open_conflicts`), `required_repairs` /
`unresolved_risks` (from finding dispositions), source digests (composition,
result bundle, every lane receipt), limitations
(`execution_supported=false`, `hermetic_result_production_v1`), and verdict.

`approved` requires all receipts complete, empty conflicts/repairs/risks, no
accepted blocking finding, synthesis approval, verifier approval, and zero
verifier blockers. Incomplete lanes derive `rejected`. Contradictory claims
(e.g. verifier approval with blockers, or synthesis approval with open
conflicts) fail closed rather than being silently normalized.

## Fail-closed invariants

- Idempotent materialize / produce for identical digests
- Same `composition_id` with a different digest → refuse
- Conflicting existing result bundle/decision → refuse
- Corrupt / truncated / symlinked / foreign-writer artifacts → refuse
- Failure between bundle write and decision commit marker → no authoritative
  decision
- Missing / cancelled run → refuse
- Never sets `verified` / `passes`
- Never launches panes, Jobs, providers, Antigravity, or MCP
- Worker claim/submit refuse leader / partial / non-Team spawn contexts
- Concurrent same-lane claims yield exactly one winner
- Same-result submit is idempotent; conflicting submit is refused

## Honesty

Hyperplan V1 **result production landed** under #69 PR10. Catalog v4
atomic task-batch DAG admission landed under #69 PR11. Shared composition
task driver (admit-tasks / collect-tasks) landed under #69 PR12. Composition
lane worker protocol (claim-lane / submit-lane-result) landed under #69
PR13. Does **not** close #69: Hyperplan execution (auto workers / providers /
panes / Jobs), model synthesis, Security Research composition execution, live
Antigravity evidence, and full OMX remain open. Security Research hermetic
result production landed under #69 PR9. Manifests retain
`execution_supported=false`. No `live_*` maturity claims.

Refs #69.
