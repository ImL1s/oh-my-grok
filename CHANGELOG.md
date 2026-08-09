# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Product version source of truth: [`plugin.json`](./plugin.json).

## [Unreleased]

### Fixed
- **Parity #78-H OmO Commander/Zod discovery fail-closed:** resolve
  `addCommand(factory())` (emit `cli.mcp` from `createMcpOAuthCommand()`),
  emit Commander `.alias()` surfaces (`cli.setup`, `cli.uninstall`), and
  reject non-string-literal Zod enum elements instead of partial extract.
  Regenerated OmO mapping/proof at pin `4ca872b…`; statuses remain
  bootstrapping. Refs #78 (does not close).

### Added
- **Parity #78-H real pinned OmO discovery evidence:** committed
  discovery_rules v2 policy, surface→capability mapping, and completeness
  proof under `docs/parity/completeness/{policies,mappings,proofs}/OmO.json`
  at pin `4ca872b…` (zod schema enum + CLI/command/package extractors against
  the real oh-my-openagent tree), plus hermetic registry fixtures and
  `tests/test_parity_real_source_omo.py`. Network-free artifact consistency
  verifies without claiming source reproduction. Canonical
  `source_status.OmO` / categories / `inventory_status` stay bootstrapping —
  proof present, **no promotion**. Antigravity real-source proof remains
  open. Refs #78 (does not close).

- **Parity #78-G real pinned OMX discovery evidence:** committed
  discovery_rules v2 policy, surface→capability mapping, and completeness
  proof under `docs/parity/completeness/{policies,mappings,proofs}/OMX.json`
  at pin `435d4a9…` (catalog-manifest-first extractors), plus hermetic
  registry fixtures and `tests/test_parity_real_source_omx.py`. Network-free
  artifact consistency verifies without claiming source reproduction.
  Canonical `source_status.OMX` / categories / `inventory_status` stay
  bootstrapping — proof present, **no promotion**. Refs #78 (does not close).

- **Parity #78-F real pinned OMC discovery evidence:** committed
  discovery_rules v2 policy, surface→capability mapping, and completeness
  proof under `docs/parity/completeness/{policies,mappings,proofs}/OMC.json`
  at the existing OMC pin, plus hermetic synthetic registry fixtures and
  `tests/test_parity_real_source_omc.py`. Network-free artifact consistency
  verifies without claiming source reproduction. Canonical
  `source_status.OMC` / categories / `inventory_status` stay bootstrapping
  — proof present, **no promotion**. Refs #78 (does not close).

- **Partial work for #68 PR5 / bounded auto-retry scheduler:** caller-driven
  `omg job auto-retry JOB_ID|--all` one-pass tick over the existing
  `retry_job` / exact-next-attempt path (deterministic backoff, `--limit`
  default 1 / max 32, project `auto-retry.lock`, dry-run admission without
  mutation). Automatic intent admits only `state=failed` with persisted and
  recomputed `retry_class=automatic`; preserves PR4 live/unproven/
  spawn-uncertain/provider-unbound gates; never recovers, signals, or
  auto-retries `lost`/`cancelled`/`manual_only`. Hermetic coverage in
  `tests/test_jobs_auto_retry.py`. Docs: `docs/durable-jobs.md`. Does **not**
  close #68 (authenticated live Antigravity evidence remains open; Team
  job-backed workers stay on #69).

### Fixed
- **#68 PR5 P1 safe-conflict fail-closed reread:** `_is_safe_conflict`
  returns false when the post-error job reread fails (`cur is None`) —
  never `ok=True` conflict without proof that attempt advanced or state
  became nonterminal. Hermetic coverage in `tests/test_jobs_auto_retry.py`.
  Refs #68 (does not close).

- **#68 PR5 P1 strict process-identity JSON:** PID/PGID must be JSON
  integers (not bool/float), fingerprints/`pid_starttime` null-or-string
  only, and `spawn_identity.json.job_id` must exactly equal the enclosing
  job id. Malformed claims in any recorded state (including `exited`) map
  to `E_JOB_CANCEL_UNPROVEN` / `IDENTITY_UNPROVEN` — never GONE/REUSED via
  `int()`/`str()` coercion. Hermetic coverage in
  `tests/test_jobs_auto_retry.py`. Refs #68 (does not close).

- **#68 PR5 P1 classify_retry strict booleans:** `timed_out` / `overflow` must
  be JSON booleans (`true`/`false`/absent). String `"false"` / `"0"` / other
  truthy non-bools fail closed as `unknown` (`malformed_timed_out` /
  `malformed_overflow`); primary `spawn_error`/`malformed`/`parse_error`/
  `unknown` classes are classified before flag trust so contradictory
  envelopes cannot auto-retry. Hermetic coverage in
  `tests/test_jobs_auto_retry.py`. Refs #68 (does not close).

- **#68 PR5 P1 present-but-malformed / bound-but-incomplete identity:**
  `provider_process.state=bound` (or `launching`) with missing/malformed
  PID/PGID, and present-but-unparseable `spawn_identity.json`, map to
  `E_JOB_CANCEL_UNPROVEN` / `IDENTITY_UNPROVEN` — never absent/reclaimable.
  Aligns auto-retry/cancel/observe/recover/GC with PR4 unbound-launch
  semantics. Hermetic coverage in `tests/test_jobs_auto_retry.py` and
  `tests/test_jobs_recovery.py`. Refs #68 (does not close).

- **#68 PR4 cancel_requested beats racing success:** once `cancel_requested_at`
  is durable (persist-before-signal), `transition_job` /
  `transition_owned_job` / CAS remap racing `succeeded`/`failed` stamps to
  `cancelled` under the same lock. Closes the CI flake where an
  `ignore_sigterm` fake finished Adapter.run during SIGTERM→grace→SIGKILL and
  `cancel_job` returned `succeeded`. Hermetic coverage in
  `tests/test_jobs_runtime.py`. Refs #68 (does not close).

- **#68 PR4 P0 unbound provider launch window:** `provider_process.state=launching`
  with incomplete PID/PGID is never treated as provider-absent
  (`recoverable_lost`). Observe/recover classify it as `IDENTITY_UNPROVEN`
  (`provider_launch_unbound`, same gate as `cancel_job`);
  `_assert_prior_attempt_gone` refuses retry on such records so a duplicate
  attempt cannot start while an orphan provider may still be alive. Hermetic
  coverage in `tests/test_jobs_recovery.py`. Refs #68 (does not close).

### Added
- **Partial work for #68 PR4 / lease recovery:** attempt-scoped owner lease +
  runner heartbeat; read-only liveness observation on `omg job status|list|wait`;
  explicit `omg job recover` / `recover --all` reconciles expired abandoned jobs
  to `lost` after OS identity proof (CAS/generation-safe; concurrent recover one
  winner). Lost jobs reclaim only via existing exact-next-attempt
  `omg job retry` (no auto-retry scheduler). Fail-closed for live/unproven/
  orphan-provider identities; public surfaces never expose owner tokens.
  Hermetic coverage in `tests/test_jobs_lease.py` and
  `tests/test_jobs_recovery.py`. Docs: `docs/durable-jobs.md`. Does **not**
  close #68 (automatic retry scheduling and authenticated live Antigravity
  remain open).

- **#69 PR3 leader-resume task-claim reconciliation:** `omg team resume`
  (`resume_for_identity`) reconciles Team API task claims under the existing
  scale.lock after pane reconcile/relaunch — preserve coherent unexpired
  claims; release only coherent expired claims to `pending` (version +1;
  old token fenced). Additive `claim_reconcile` on resume output
  (IDs/counts only). Hermetic coverage in `tests/test_team_reconcile.py`.
  Does **not** close #69 (no job-backed workers / attempts / Hyperplan /
  catalog-v1 reconcile op / maturity promotion). Fail-closed preflight
  rejects non-string/unsafe owners, non-string / padded / whitespace-only
  tokens, filename/body id mismatches, and duplicate embedded task ids
  (zero mutation; aborts resume).

- **Partial work for issue 68 (PR3 / retry+GC+ask --background):** explicit
  `omg job retry JOB_ID --attempt N` with immutable `attempt_budget`, attempt
  archives under `.omg/jobs/<id>/attempts/NNNN/`, retry classification
  (automatic/manual_only/never/unknown; no auto-scheduler), terminal
  `omg job gc --retention-days N` (never deletes nonterminal; refuses
  ACP-bound; skips malformed; revalidates under lock), and
  `omg ask --background` thin seam (`fake`|`agy` → durable job + immediate
  `job_id`; sync ask unchanged). Reuses the existing `start_job` /
  `launch_job_runner` path. Docs: `docs/durable-jobs.md`. Does **not** close
  #68 (lease recovery / auto-retry / authenticated Antigravity still open).

- **#105 PR4 hermetic ACP resume sidecar:** Team `--provider-session` on an
  AVAILABLE `session_resume` gate starts/reuses an internal jobs-plane
  `grok-acp-session` sidecar (`grok agent stdio`, initialize → one
  `session/resume`, no-replay quiet window, atomic content-free receipt).
  Public `omg job start` still admits only `fake`/`antigravity`. Cancel is
  dual process-group teardown (not `session/close`). Hermetic fake-peer
  coverage only — no live-host maturity promotion. #105 stays open for
  close/restore-code/search/background deltas.

- **#78-E README↔JSON managed-snapshot drift gate:** hand-authored
  `docs/parity/README.md` keeps exactly one managed block between
  `BEGIN/END GENERATED PARITY INVENTORY SNAPSHOT` markers.
  `scripts/generate_parity_docs.py` validates the inventory, renders a
  deterministic snapshot (statuses, pins, maturity/classifications as code
  spans, counts only — no timestamps), and `--check` fails closed on
  stale/malformed markers. Write mode repairs only that block
  (`MANAGED_BLOCK_PATHS`, not whole-README overwrite). Inventory
  maturity/status remain bootstrapping; #78 stays open (real-source
  policies/proofs still outstanding). No `live_*` capability claims.
- **#69 PR2 (Team operation catalog v1):** immutable
  `omg_cli/team/operation_catalog.py` is the single source for Team API op
  metadata; derives `TEAM_API_OPERATIONS` / `P0_OPERATIONS` /
  `WORKER_ALLOWED_OPS` / `WORKER_DENIED_OPS`. Introspection
  `omg team api catalog` emits versioned JSON
  (`kind: omg.team.operation_catalog`, schema_version 1) with no `--input`,
  team state, tmux, `.omg`, or subprocess. Golden
  `tests/golden/team_operation_catalog_v1.json` +
  `docs/team-operation-catalog-v1.md`. Does **not** close #69 (no new ops /
  reconcile / jobs / Hyperplan).
- **#68 PR2 Antigravity job adapter wiring:** durable jobs can execute the
  existing `ProviderAdapter.run` contract with repository fake-agy coverage,
  fail-closed provider admission, and bound child-process cancellation.
  Authenticated execution was not exercised. #68 remains open for retry,
  lease recovery, GC, and ask --background.
- **#78-D completeness promotion proof gate:** fail-closed
  `omg_cli/parity_completeness.py` policy/proof validators + reproduction,
  wired into strict `omg parity check` / `scripts/check_parity_inventory.py`.
  Promoting `source_status` / `category_status` / `inventory_status` to
  `complete` without a digest-bound proof fails closed; catalogue seeds are
  not proofs. Maintainer `scripts/check_parity_completeness.py` (`--plan` /
  `--check`) may emit a candidate proof but never mutates inventory status.
  Hermetic fixtures under `tests/fixtures/parity/completeness/`; schema in
  `docs/parity/completeness-schema-v1.md`. Canonical inventory stays
  bootstrapping — promotion remains unperformed and is proof-gated.
  Does **not** close #78 (real-source policies/proofs still outstanding).

### Fixed
- **#105 PR4 P0 unproven spawn identity persist:**
  `_persist_unproven_spawn_identity` no longer swallows `update_job_fields`
  failure after an unproven post-spawn cleanup. Runner pid/pgid/starttime is
  retained in durable `spawn_identity.json` (written at Popen, before RUNNING
  commit); when job.json stays `starting`+`pid=null`, `cancel_job` uses the
  recovery identity or refuses with `E_JOB_CANCEL_UNPROVEN` for job-bearing ACP
  bindings / uncertain markers — Team stop cannot publish stopped over an
  orphan. Hermetic regression in `tests/test_jobs_acp_session.py`.
- **#78-D P0 checkout/pin binding:** completeness proofs authenticate
  `--upstream-root` to `pin_revision` (`HEAD` match + clean porcelain) and
  stamp `checkout_provenance` before hashing registry/surface bytes. Wrong
  HEAD or dirty/untracked checkouts fail closed (hermetic git fixture
  coverage).
- **#78-D P0 pin blob binding:** discovery digests use `git cat-file` at the
  pin; auth also compares worktree `hash-object` OIDs to pin blobs so
  `skip-worktree` / `assume-unchanged` mutations fail closed.
- **#78-D P0 replace-ref binding:** completeness git invocations set
  `GIT_NO_REPLACE_OBJECTS=1` and `git --no-replace-objects` so
  `refs/replace/<pin>` cannot rebind pin trees/blobs.
- **#68 PR1 PID ownership (best-effort):** cancel records a `pid_starttime`
  fingerprint at `starting→running` (Linux `/proc/<pid>/stat` starttime or
  `ps -o lstart=`). Before **each** cancel signal (SIGTERM and again before
  SIGKILL), cancel revalidates pid/pgid both `> 1`, live `getpgid(pid)` vs
  recorded PGID, and (when present) the fingerprint. Outcome is explicit:
  OK → signal; GONE (dead / ProcessLookupError) → **no** signal,
  continue cancel stamp; mismatch fail-closes with `E_JOB_PID_REUSED` /
  `E_JOB_PGID_MISMATCH` and does **not** signal. Null fingerprint (probe
  failed at start) still falls back to pid/pgid + live PGID checks only —
  **not** full OmO-style lease/nonce ownership; deferred to a later #68 slice.
- **#68 PR1 launch ownership:** parent alone commits `starting→running`
  (pid/pgid/handle); child readiness barrier polls `job.json` until that
  commit (or terminal/timeout) before `ProviderAdapter.run`, and stamps
  only `running→succeeded|failed`. Immediate post-spawn child exit and
  cancel-during-uncommitted-window fail closed (never durable `running`
  without a live handle).
- **#105 PR3 review:** `omg team resume --provider-session` evaluates
  `session_resume` with `required=False` so missing cap yields reachable
  `LEGACY` (+ `next_action`), not unconditional BLOCKED; `provider_session_result`
  fail-closes on non-`session_resume` gate capability ids.

### Added
- **#69 PR1 (renew-task-claim):** explicit `omg team api renew-task-claim`
  extends an active in-progress claim lease under the per-task lock
  (`leased_until` = now + CLAIM_LEASE_SECONDS; never shortens; same claim
  token/owner/status). Fail-closed on missing task, terminal status, token
  mismatch, expired/malformed deadline, and `expected_version` conflict.
  Does not rotate tokens, mark tasks complete, or write `passes`/`verified`.
  Hermetic coverage in `tests/test_team_api.py` (+ heartbeat non-renewal in
  reliability). Does **not** close #69 (recovery/sweeper/job wiring later).
- **Partial work for issue 68 (PR1 / jobs MVP):** durable `.omg/jobs/<id>/`
  store + `omg job start|status|wait|collect|cancel|list` with JSON CLI
  envelopes. Subprocess job runner owns `ProviderAdapter.run` (no second
  launcher). Hermetic `FakeProvider` worker (`--provider fake`);
  `--provider antigravity` returns clear `E_JOB_PROVIDER` (live spawn
  deferred). Atomic start (queued→starting before launch; launch failure →
  failed, never running with null pid). Cancel by recorded PID/PGID only
  (sibling-safe; never `pkill`/`killall`). Large outputs as
  `artifacts/` descriptors only. Does **not** close #68 (retry/GC/lease
  recovery/`ask --background` later).
- **Partial work for issue 67 (slice D / #67-D):** Team Antigravity panes
  route through adapter-owned `ProviderLaunchRequest` /
  `ProviderLaunchEnvelope` + `AntigravityProvider.build_launch_envelope`
  (argv array only; never a shell string). Team `_build_agy` /
  `build_executor_argv` consume the envelope; supervisor retains
  spawn/PTY/PID/PGID/readiness/nonce authority — **does not** call
  `ProviderAdapter.run` for interactive panes. Preserves Team contract
  (`needs_pty=True`, `-p` path placeholder + `positional-text`,
  `--dangerously-skip-permissions`, RO `--sandbox`). Descriptor gains
  optional `identity_basenames` / `provider_strategy` / `startup_strategy`
  without bumping schema v1 (resume-safe; launch-receipt semantics
  unchanged). Hermetic coverage in `tests/test_team_agy_envelope.py`.
  **Slices A–D complete** — issue #67 may be closeable after dual review +
  CI (do not merge until then).
- **Partial work for issue 67 (slice C / #67-C):** `omg ask agy` routes
  through `ProviderAdapter.run` (Antigravity headless adapter). `agy` is a
  first-class ask provider — **not** aliased to `gemini`. Legacy
  codex/claude/gemini ask paths unchanged. Artifacts / exit codes / dry-run /
  timeout (exit 4) / missing-binary (exit 3) / auth-blocked failure preserved.
  Hermetic fake-`agy` coverage in `tests/test_ask_agy.py`. Team cutover
  landed as **slice D**.
- **Partial work for issue 67 (slice B / #67-B):** headless Antigravity
  execution on `ProviderAdapter.run` — provider-neutral
  `ProviderRunRequest`/`ProviderRunResult` (+ events/usage/exit_class),
  shared `run_provider_process` (probe remains a thin wrapper; no second
  subprocess stack), `json`/`stream-json` parsers with partial-output
  preservation on timeout/cancel, session/resume *metadata* without Team
  coupling, and `omg provider antigravity run`. Hermetic fake-`agy` only —
  no live-network CI. Ask cutover landed as #67-C; Team envelope as #67-D.
- **#105 Team resume consumes host gates (PR3 / seq E first slice):**
  `provider_session_result` replaces the fixed ACP stub in Team resume/view
  envelopes. CLI `omg team resume --provider-session` probes via
  `probe_host` → `evaluate_feature_gate(session_resume, required=False)` and
  injects the `FeatureGateResult` into `resume_with_view` (runtime does not
  re-parse versions). Outcomes stay independent: reconcile / provider_session
  (`available`|`legacy`|`blocked`) / tmux view — attach success never implies
  provider resume. Absent resume → LEGACY (actionable `next_action`); BLOCKED
  still fails closed when it occurs. No ACP transport; hermetic/fixture
  host-probe scope only — no live Antigravity CI claim.
  See `docs/host-compat.md`.
- **#105 host doctor/capability probe (PR2):** canonical
  `omg_cli/host_probe.py` + `host_models.py` report active Grok version,
  tested range (`0.2.107`…`0.2.121`), compatibility, and a bounded capability
  set (resume/close/restore-code/uuid-search) with truth order
  behavior → ACP advertisement → CLI inspect → version fallback.
  Feature gates are three-state (`AVAILABLE` / `LEGACY` / `BLOCKED`);
  `omg doctor` / `omg doctor --json` surface the host block without
  auth/session/transcript/home leaks. Hermetic fixtures under
  `tests/fixtures/host/`; operator note in `docs/host-compat.md`.
  Pin ≠ forced minimum — does not require every install to run v0.2.121;
  hermetic fixtures only, no live Antigravity CI claim in this change.
- **#105 Grok Build host-baseline gate (PR1):** independent host catalogue at
  `docs/parity/upstream-snapshots/grok-build.json` for pin
  `a5589e958437d79e13db026eedcb1720bffd4063` (`0.2.121`), fail-closed
  `GROK_BUILD` pin-transition reviews under `docs/parity/reviews/`, and
  generated `docs/parity/generated/host-baseline.md` +
  `host-capability-matrix.md`. Host rows are `catalogued` only (not a
  sibling of OMC/OMX parity scoring); `host_owned` cannot claim OMG
  `omg_paths` as implementation evidence.
- **#104 real-tmux Team UX regression suite:** hermetic Layer B coverage
  (`tests/test_team_real_tmux_ux.py` + `tests/support/team_tmux_harness.py`)
  on an isolated `tmux -S` socket with fake providers under
  `tests/fixtures/providers/`. Protects same-window leader visibility/focus,
  invocation race fail-closed, one-worker death, provider-ready/blocked/exit,
  bootstrap scrollback cleanliness, operator exact-pane I/O, scale topology,
  resume reconcile-only, and stop/rollback survivors. CI jobs
  `team-real-tmux-linux` / `team-real-tmux-macos` run `-m tmux_real` and
  must pass on this PR (GitHub branch-protection “required checks” are
  admin-owned and not claimed here).
  `live_team_smoke.py --interactive-ux` emits `interactive_evidence_v1` +
  `LIVE_TEAM_INTERACTIVE_UX_OK` (optional; fail-closed without tmux).
- **#103 Team resume/view attach semantics:** `omg team resume` stays
  reconcile-only by default (never attaches because stdout is a TTY).
  Explicit `resume --view` / `omg team view` restore the exact Team
  window/leader via a pure view planner + #102 topology target +
  identity re-probe before `select-*` / `switch-client` /
  `attach-session` (no lifecycle lock across interactive attach;
  `--takeover` required for `-d`; `--print` / `--json` never execute
  client effects). Reconcile, provider-session (ACP stub:
  `no_replay=true`, `restore_code=false`), and tmux-view outcomes are
  reported separately. `--worker` delegates to #101 focus.
- **#101 identity-fenced live pane inspect/operator input:** `omg team panes|capture|focus|key|input|watch` resolve Team identity → receipt
  chain → exact-pane proof (#98) → authorize → tmux effect (`shell=False`).
  Bounded/redacted capture; key allowlist; literal `send-keys -l`; audit
  stores length/hash only; `--json` never focuses or delivers input.
  Prefer durable `omg team api` for automation.
- **#102 preserve Team tmux topology across scale/relaunch:** persisted
  `view_mode` (same_window / dedicated_window / detached_session /
  legacy_windows) is the authority for scale-up, scale-down, resume, and
  relaunch. Non-legacy scale-up splits into the exact Team window (never
  `new-window`); logical_worker_index is separated from live pane/window
  coordinates; identity receipt schema v3 + team meta schema v2 carry
  topology fingerprints; post-commit `reconcile_layout` is cosmetic
  (`layout_repair_needed` does not roll back identity). Legacy
  window-per-worker adapters remain for pre-#96 runs.
- **#99 provider-ready Team startup:** schema-v2 monotonic phases
  (`pane_created` → `provider_spawned` → `provider_ready` → `task_dispatched`;
  optional `mailbox_ack`), pane `team supervisor` consuming vetted argv
  descriptors, provider readiness adapters, and Team statuses
  `running` / `degraded` / `failed_start` / `blocked_start` /
  `unverified_start`. Legacy v1 `worker-ready` receipts are
  `wrapper_ready_legacy` only and cannot false-green `startup_status=running`.
  Review hardening: process_stable is provisional (post-stable observe catches
  delayed auth/trust); gate requires distinct provider≠supervisor identity,
  live PID, and phase history including `provider_spawned`+`provider_ready`.
  Pro #99 blockers: process_stable / provisional finalize require matching
  provider binary identity (cmdline/exe basename or descriptor allowlist);
  supervisor tees bounded provider stdout to the pane tty; `needs_pty`
  records the real provider child PID (fail-closed if unresolved); pipes are
  drained after ready; TUI idle uses a longer post-stable floor; auth after
  finalize remains out of window (documented, not infinite watch).
- **Partial work for issue 67 (slice A / #67-A):** typed `omg_cli/providers/`
  Antigravity probe (discover binary, version argv probe, compat range,
  schema-versioned capabilities envelope) plus
  `omg provider antigravity {capabilities,doctor}`. Hermetic fake-`agy`
  fixtures only — no ask/Team cutover, no fabricated live-evidence verification claims. Issue #67
  remains open for slices B–D.

### Fixed
- **#105 PR2 doctor probe:** when ACP advertisement or inspect is present,
  omitted capability keys fail closed (no version fill). Partial
  `methods: ["session/resume"]` and empty `methods: []` no longer false-green
  `session_close` via semver. Doctor JSON scrubs home prefixes in
  `project_root.path`; docs clarify fixture/env injection vs live ACP.
- **#104 B1 leader operator visibility:** `_restore_leader_focus` now
  `select-window -t %pane` then `select-pane` so session `window_active`
  flips (select-pane alone is window-local). Postconditions require
  `window_active=1`; real-tmux asserts + negative self-check reject the
  hollow window-local `pane_active` check. Hermetic CI excludes
  `tmux_real` (`not live and not tmux_real`); dedicated artifact upload
  uses `if-no-files-found: error`.
- **#104 same_window scale-up geometry:** grow Team window before
  `split-window` when headless defaults leave no space for another pane
  (macOS GHA flake: scale returned with only leader+2 panes). Harness
  pins 160x48; scale UX test asserts distinct new `pane_id` + live count.
- **#104 scale inherits `executor=fixture`:** scale-up pane records now
  use `build_fixture_pane_command` when team meta has
  `executor=fixture`. Previously scale always built grok argv; on CI
  without `grok` the pane exited and tmux destroyed it while the API
  still returned `added=1` (TimeoutError waiting for 4th pane).
- **#104 misleading dedicated-window unit test name:** renamed
  `test_inside_tmux_splits_current_window` →
  `test_inside_dedicated_window_uses_new_window` so it no longer reads as
  locking the buggy new-window default (same-window default remains
  `test_inside_same_window_default_never_calls_new_window`).
- **#67-A probe fail-closed / process contract (PR #94 re-audit):** version and
  help probes require successful exit + observed evidence (no invented
  formats/efforts/modes); `run_probe_process` uses POSIX `start_new_session` +
  `killpg` on timeout/cancel/overflow with bounded output; `provider` is
  install/global-scoped; CLI routes via `ProviderAdapter`; neutral
  `ProviderCapabilities` defaults no longer carry Antigravity-positive claims.
- **#67-A PR #94 GPT Pro re-audit round 2:** `_parse_help_supports` is
  fail-closed on structured flag/enum/subcommand boundaries only (colliding
  prose like `allows`/`plan`/`low`/`--project` cannot forge evidence);
  `run_probe_process` kills the process group on KeyboardInterrupt/BaseException
  before joining pipe readers, and version/help probes pass `cancel_event`
  (SIGINT-wired); `omg provider antigravity doctor --json` uses
  `success`/`failure` envelopes (`E_PROVIDER_DOCTOR`) instead of splatting
  `DoctorReport.ok` over `success()`. Top-level `omg doctor --strict` remains
  out of scope for the Antigravity probe in slice A.
- **#67-A PR #94 GPT Pro re-audit round 3:** post-`Popen` setup failures and
  cancel/SIGINT through the wait/join/close window always `_kill_tree`; success
  drains readers to EOF (forced stop sets truncation flags; truncated help
  fails closed); `OMG_AGY_BIN` requires `agy` basename + `Usage of agy` help
  identity; `parse_version` is first-line-anchored; tested compat window is
  fixture-backed `1.1.10` only; provider JSON/doctor errors run through
  `redact_text`/`redact_value`.
- **#67-A PR #94 GPT Pro re-audit round 4:** `Popen` and post-spawn work share
  one BaseException+`_kill_tree` region (`proc=None` then nested OSError
  convert); result construction / final cancel check kill before return;
  `_run_probe_argv` `killpg`s via returned `pid` before `KeyboardInterrupt`;
  `parse_version` rejects leading zeros and enforces explicit digit/value caps
  (ASCII `[0-9]`, typed `ProviderVersionError` — not CPython digit-guard only).

## [0.7.6] - 2026-08-06

### Fixed
- **Release install gate / `omg update` dogfood (#89):** install-time dual-pass
  doctor probe (`--strict` then non-strict) for **both** release and
  development — coexistence-only soft risks become `completed_with_warning`;
  integrity FAILs stay fail-closed. Bare `rc=2` without dual-pass evidence is
  rejected; malformed dual-pass fields never coerce into legacy `rc=0`. Dual-pass
  success also requires a consistent aggregate matrix (`strict=0` ⇒
  `relaxed=None` + `rc=0`; soft ⇒ `rc=2`) so contradictory evidence cannot
  classify as installed. Exact same-digest installs reuse a receipt only when
  mode, release asset/checksum evidence, status authority, and live Grok host
  plugin path/enabled authority match; otherwise they re-attest
  (development→release promotion writes a release receipt without host churn,
  and warning status is never downgraded) or fall through to a full
  uninstall/install/enable transaction when the host path has drifted or the
  plugin is disabled. Gate failures print a
  bounded (64 KiB) non-strict doctor transcript. Authoritative receipts record
  pending + post-publication probe hashes and the stricter status. `omg update`
  uses one `VerifiedCurrentInstall` authority; release receipts and
  unprovable/dirty development installs promote through stage `install.sh`
  (source preserved); clean development still fast-forwards +
  `install-plugin.sh`. Interactive `omg doctor --strict` coexistence semantics
  are unchanged.
- **Parity release claim gate (PR #91 Pro re-audit):** expand overclaim scan to
  `CHANGELOG.md` / `docs/skills.md`; keep forbidden-phrase restrictions active
  until `inventory_completion_claims_allowed` (category + source complete);
  bind required snapshot filename → `source`; bind refresh ack `detail` to
  promise/source_paths before→after values so same-revision stale acks cannot
  replay.
- **Parity release claim gate (PR #91 Pro re-audit round 3):** durable release
  base (previous `v*` tag / `--base-ref` / `OMG_PARITY_BASE_REF`, not `HEAD^`);
  scan intermediate pin transitions; require git-tracked HEAD-blob-matching
  review ledgers; own `docs/parity/reviews/**` under OMG-W0; keep live-maturity
  phrase scan active unless every capability reaches the top live-maturity tier;
  expose
  `--base-inventory` / `--base-ref` on `omg parity check`.
- **Parity release claim gate (PR #91 Pro re-audit round 4):** `--release` rejects
  file-only `--base-inventory` (no git provenance → endpoint-only A→A miss on
  A→B→A mid pins). Pair `--base-inventory` with `--base-ref` whose inventory
  blob matches the file; never silently prefer the file over `--base-ref`.
- **Parity release claim gate (PR #91 Pro re-audit round 2):** require committed
  pin-transition reviews under `docs/parity/reviews/`; bind deleted-change
  fingerprints; validate upstream snapshot capability schema (no duplicate /
  malformed silent skip); forbid live-evidence marketing phrases in docs and
  scrub historical CHANGELOG wording.

### Added
- **Parity full upstream inventory (#78-B):** expand the v2 catalogue with
  `source_status` (OMC/OMX/OmO/Antigravity), the #78-B category taxonomy,
  minimum OMC/OMX/OmO/Antigravity capability rows (mostly `catalogued`; no
  fake top-tier live-maturity labels), generated per-source matrices + SUMMARY (EN/zh/zh-TW),
  and NON-AUTHORITATIVE banners on historical research matrices. Inventory
  completeness ≠ product parity — `inventory_status` stays `bootstrapping` while
  `parity_governance` / `platform_live_evidence` remain open. Issue #78 stays
  open after #78-C for remaining maturity / live-evidence work.
- **Partial work for issue 78 (slice C / #78-C):** seed pinned upstream snapshot
  catalogues (`docs/parity/upstream-snapshots/`), `omg parity refresh --plan`
  review workflow, release claim gate (`--strict --release` in `release.yml`),
  and live-evidence freshness enforcement. Completeness promotion and issue #78
  remain open — hermetic catalogue only; no fake top-tier live-maturity product claims.

## [0.7.5] - 2026-08-05

### Added
- **Parity inventory v2 (#78-A):** claimability-safe schema with ordered maturity,
  expanded classifications, bootstrapping canonical inventory for open P0 gaps
  (#67/#68/#69/#78 remaining), `omg parity check|gaps`, generated
  `FEATURE-MATRIX.md`/`GAPS.md` (no percentages while bootstrapping), and CI
  `--strict` gate. Issue #78 remains open for #78-B/#78-C.

### Fixed
- **Parity claimability / upgrade safety (PR #85 Pro re-audit):** upgrade from
  older managed installs tolerates missing newly required shipping roots
  (`docs/parity`) on prior identity/rollback while still requiring them on the
  candidate package; alias maturity cannot outrank its canonical target (and
  cannot positive-claim when the target is host_impossible/excluded/
  optional_unclaimed); `--strict` requires non-empty `omg_paths` for claimable
  classifications and repo-verifiable `healthy_evidence` for `healthy` /
  top live-maturity tiers.
- **Process-fanout cancel linearization (Round 18 / R18-1):** each
  ``run_process_fanout`` worker's cancel recheck → ``Popen`` → PID publish
  runs under the same per-run ``transition_guard`` as ``_launch_grok``
  (``launch_refused_for_cancel``); refused spawns do not Popen and roll back
  prior workers (R15-4). Closes Pro R17b P1 where legacy cancel could miss a
  later fanout worker.
- **Legacy ``set_verified`` cancelled absorbing (Round 18 / R18-2):** legacy-v1
  ``set_verified`` re-reads under ``transition_guard`` and refuses
  ``cancelled`` → ``verified`` unless ``force=True`` with the in-process force
  capability; already-``verified`` is idempotent. Closes the product bypass
  where post-cancel ULW acceptance could still stamp verified.
- **``clear_active`` / ``create_run`` create.lock (Round 18 / R18-3):**
  ``active.json`` read/compare/unlink shares ``.omg/state/create.lock`` with
  ``create_run``; legacy cancel clears active only after releasing
  ``transition_guard`` (no create-while-transition lock-order reversal).
  Prevents cancel from unlinking a newer run's active pointer.
- **Legacy cancel / launch linearization (Round 17 / R17-1):**
  ``_cancel_run_legacy`` now acquires the same per-run ``transition_guard`` as
  ``_launch_grok``: commit ``cancelled`` + snapshot PIDs under the guard, then
  signal outside. Closes the Pro R16 P1 race where legacy cancel could mark
  terminal while launch still Popens after its pre-check. Legacy
  ``write_status`` treats ``cancelled``/``verified``/``completed``/``failed``
  as absorbing (guarded no-op) so post-launch writers cannot resurrect a
  cancelled run. Pipeline / process-fanout ULW remain legacy-v1 (strict-v2
  would require ``execution_lease`` on every stage write — not a cheap upgrade);
  linearized legacy cancel is the supported closure for those paths.
- **Cancel-aware `_launch_grok` gate (Round 16 / R16-1):**
  ``modes._launch_grok`` re-checks terminal status /
  pending ``cancel.request.json`` under the same ``transition_guard`` as
  ``prepare_leader_spawn`` (shared ``launch_refused_for_cancel`` with
  autopilot) so ralph/ralplan cannot Popen after cancel.
- **Autopilot FSM public contract:** split `MANUAL_TRANSITIONS` vs
  `COMMIT_ONLY_TRANSITIONS`; `omg autopilot status` `legal_next` no longer
  advertises `verified` as a `transition()` edge (use `commit_only_next` +
  `terminal_action: omg autopilot complete`). Advance-gate failures are
  recorded on status / stall JSON instead of being swallowed silently.
- **Resume ralplan hint:** drop dead `omg ralplan --resume` placeholder
  (`--resume` is ralph-only); recommend `omg state --run` + `--run` re-invoke.
- **Interview/consensus gates:** prefer CLI-owned stamps; bare
  `interview_complete` / `consensus` booleans require `break_glass=true` and
  are audited on history (`gate_audit`). StageEvidenceEnvelope v3 still planned.
- **Review/QA fingerprint recheck:** `stage_review_is_clean` / `stage_qa_is_clean`
  re-validate `diff_hash` lane stamps and `product_hash` against the current
  workspace; drifted or tampered stamps fail closed (legacy hash-less stamps
  keep weaker clean-flag-only behavior).
- **Implement→review work gate:** require workspace fingerprint drift since
  implement entry, a real on-disk CLI implementation receipt, or audited
  `no_change_reason` / inline receipt with `break_glass=true`; unattended
  auto-advance records `gate_failure` instead of silently skipping.
- **Implement-gate fingerprint (Codex PR review P1):** the implement→review
  work gate no longer reuses `qa.product_hash` (which only hashes
  `omg_cli/**/*.py`, missing changes confined to `plugin.json`, `hooks/`,
  `skills/`, `agents/`, `templates/`, or non-`.py` files under `omg_cli/`).
  A dedicated `autopilot._implement_workspace_fingerprint` helper covers
  those curated product surfaces without changing `qa.product_hash`
  semantics used for UltraQA acceptance.
- **Implementation receipt now real (Codex PR review P2):** added
  `omg_cli/implementation.py` with `stamp_implementation_receipt` /
  `read_implementation_receipt`, writing a CLI-owned
  `stages/implementation.json` (`writer=omg-cli`, `content_sha256`,
  `stamped_at`) under `.omg/state/runs/<run_id>/`. The implement→review gate
  now actually loads and trusts this on-disk stamp (fingerprint-rechecked)
  without `break_glass` — previously the docs advertised this path but no
  producer/reader existed, so it always fell through to the unauthenticated
  inline-receipt break-glass path.
- **Implementation receipt bound to implement cycle (Codex PR review P2):**
  a receipt stamped during one implement cycle no longer satisfies the
  implement→review gate for a later cycle whose fingerprint happens to
  still match (e.g. `review → ralplan → implement` with no new product
  work). `implementation.invalidate_implementation_receipt` marks any
  leftover receipt stale on every (re)entry into `implement`, mirroring
  `invalidate_quality_stages`; `read_implementation_receipt` treats
  `invalidated=true` the same as a missing file.
- **Implement-gate fingerprint excludes generated caches (Codex PR review
  P2):** `_implement_workspace_fingerprint` no longer hashes
  `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`,
  or `*.egg-info/` (mirrors this repo's own `.gitignore` cache entries) —
  previously, merely running tests or importing an `omg_cli` module during
  `implement` could write/update bytecode with zero source edits and
  falsely register as implementation work.
- **Autopilot sidecar writes:** `autopilot.json` and stage invalidation use
  temp-file + `os.replace` atomic publish (mini-WAL; cross-file WAL still planned).
- **Resume bundle v1:** `omg resume` emits partial `resume_bundle`
  (`schema_version=1`: `run_view`, `gate_failure`, `autopilot_phase`,
  `legal_next`, `provenance`) — wiki/memory/compaction deferred.
- **Tracker process identity:** lease PID match fails closed when
  `process_starttime` is unavailable (unknown ≠ same process).
- **`set_verified` force hatch (Round 14):** `force=True` requires an
  in-process capability from `enable_force_verified_for_tests()` (tests
  only); env `OMG_INTERNAL_FORCE_VERIFIED` alone is insufficient (deprecated
  no-op). Not exposed on any CLI argparse path.
- **Review gate workspace binding (Round 2 / R2-2):** `omg review`
  records `workspace_fp` (`_implement_workspace_fingerprint`) on every
  structured review stamp; `stage_review_is_clean` requires it to match the
  current workspace (missing on schema_version≥2 stamps → fail-closed).
- **QA gate workspace binding (Round 2 / R2-1):** clean UltraQA cycles
  also record `implement_workspace_fp`; `stage_qa_is_clean` rechecks it
  alongside `product_hash` so non-`omg_cli/**/*.py` product-surface drift
  after QA went clean cannot reach acceptance.
- **Blocked recovery gates (Round 2 / R2-3):** destination gates run for
  `blocked→implement` / `ralplan` / `review` / `qa` — e.g.
  `blocked→review` still requires implement work evidence;
  `blocked→qa` still requires a fresh clean review stamp; re-entering
  `review` from `blocked` invalidates stale review/QA stamps.
- **Interview trusted path envelope (Round 2 / R2-4):** `_interview_complete`
  requires a CLI-owned `interview.json` complete envelope (writer, run_id,
  spec artifact + content hash) — a bare `status=complete` string alone
  no longer unlocks `ralplan`.
- **Consensus stamp-only (Round 2 / R2-5):** `_consensus_ready` trusts only
  CLI `ralplan.json` with `writer`, `run_id`, and `accepted=true`;
  `status.ralplan_consensus` and artifact markers alone no longer unlock
  `implement`.
- **Implement-gate fingerprint entrypoints (Round 3 / R3-1):**
  `_implement_workspace_fingerprint` now includes `scripts/` and `bin/` so
  install/CLI surface edits register as implement work and bind review/QA
  freshness rechecks — previously those paths were omitted from the curated
  fingerprint tuple.
- **Ralplan stamp invalidation on replan (Round 4 / R4-1):** any
  non-interview re-entry into `ralplan` (including `review→ralplan`,
  `qa→ralplan`, and `blocked→ralplan`) invalidates the prior accepted
  `ralplan.json` stamp and review/QA stamps — a stale accepted consensus
  can no longer silently unlock `implement` without a fresh ralplan cycle.
- **Interview attach-run for autopilot (Round 4 / R4-2):** added
  `omg interview start --attach-run RUN_ID` to seed CLI interview envelopes
  under an existing autopilot run in phase `interview` (task defaults to
  the run goal; fail-closed on phase/mode mismatch or task/goal disagreement).
- **Ralplan invalidation cleared on fresh accept (Round 5 / R5-1):** writing
  `accepted=true` to CLI `ralplan.json` (legacy + strict-v2 paths) clears
  `invalidated` / `invalidated_reason` / `invalidated_at`; a new strict-v2
  consensus attempt also clears stale invalidation at cycle start so a prior
  replan fence does not block a genuinely fresh ralplan round.
- **Ralplan invalidation on any re-entry with prior stamp (Round 5 / R5-2):**
  entering `ralplan` when a CLI-owned `ralplan.json` already exists for the
  run_id invalidates that stamp and review/QA stamps regardless of `src` —
  closes `review→blocked→interview→ralplan` bypass where a detour through
  `interview` made `src=="interview"` again while a stale accepted stamp
  still sat on disk; first `interview→ralplan` with no stamp remains a no-op.
- **Interview attach-run re-authorize under lease (Round 5 / R5-3):**
  `omg interview start --attach-run` re-loads the run and re-checks
  mode/phase/non-terminal/goal match after acquiring the execution lease,
  closing a TOCTOU race between pre-lease attach checks and the interview
  envelope write.
- **Embedded ralplan bound to frozen autopilot goal + phase (Round 5 / R5-4):**
  `omg ralplan * --run RUN` against an autopilot run requires
  `phase==ralplan` and a `--run` goal that exactly matches the frozen run
  goal; `_consensus_ready` also rejects accepted stamps whose `goal` field
  disagrees with the current run goal (defense in depth).
- **Ralplan history reset on fresh replan (Round 6 / R6-1):** starting a
  new strict-v2 consensus attempt against a state left `invalidated: True`
  by a prior replan now calls `_reset_for_fresh_cycle` (wipes `history`,
  per-session `attempts`, and `round`) so `first_round` starts at 1 and the
  configured `max_rounds` ceiling is available again — previously, stale
  history past the ceiling pinned every future replan into an immediate block
  with zero stages executed.
- **Attached interview close resume includes `--run` (Round 6 / R6-2):**
  `omg interview close` on an autopilot-attached interview
  (`--attach-run`) now sets `resume_command` to
  `omg ralplan <goal> --run <run_id>` instead of a standalone
  `omg ralplan <goal>` that would spawn an orphaned ralplan run.
- **Attached interview close resume via autopilot transition (Round 7 / R7-0):**
  attach-mode `resume_command` now chains
  `omg autopilot transition --run <run_id> --phase ralplan` before
  `omg ralplan <goal> --run <run_id>` — the phase sidecar is still
  `interview` at close time, so a bare embedded ralplan would be rejected
  (phase mismatch) until the transition advances the FSM.
- **Cancelled not reachable via generic transition (Round 7 / R7-1):**
  `cancelled` is removed from `MANUAL_TRANSITIONS` / `legal_next`; only
  `omg cancel` / `cancel_run` may enter the terminal `cancelled` phase —
  `transition(..., "cancelled")` raises cleanly without mutating state.
- **ralplan_epoch gates re-entry invalidation (Round 7 / R7-2):** a
  monotonic `ralplan_epoch` on autopilot state drives quality/consensus
  invalidation on re-entry to `ralplan` (epoch ≥ 1), not stamp existence —
  the first `interview→ralplan` handoff (epoch 0→1) remains a no-op even
  when no stamp exists yet; `--skip-interview` starts at epoch 1.
- **Consensus stamp requires matching non-empty goal (Round 7 / R7-3):**
  `_consensus_ready` for autopilot runs requires a non-empty string `goal`
  on the CLI `ralplan.json` stamp that exactly matches the frozen run goal
  (missing/null/empty → fail-closed).
- **Reject legacy schema for autopilot ralplan embedding (Round 7 / R7-4):**
  `omg ralplan * --run RUN` against `mode=autopilot` rejects non-strict-v2
  run schemas — autopilot embedding no longer enters the legacy-v1 FSM path.
- **Fresh replan session reset (Round 8 / R8-1):** `_reset_for_fresh_cycle`
  now mints new `session_id` UUIDs for planner/architect/critic (not reused
  with `attempts` zeroed) so a fresh strict-v2 replan cycle does not inherit
  stale session identity from the prior round.
- **Ralplan embedding cancel race (Round 8 / R8-2):** `_authorize_autopilot_embedding`
  rejects terminal autopilot statuses and pending cancellation requests;
  strict-v2 embedding re-authorizes immediately before writing
  `accepted=true` so a concurrent `omg cancel` cannot lose the race to
  acceptance.
- **Interview sidecar cancel race (Round 8 / R8-3):** interview attach/close
  paths call `_assert_run_writable` under the execution lease immediately
  before every sidecar write — terminal runs and pending cancellation
  requests fail closed without writing `interview.json` or spec artifacts.
- **Missing ralplan_epoch conservative migration (Round 9 / R9-1):**
  pre-R7 ``autopilot.json`` sidecars that lack ``ralplan_epoch`` no longer
  default to ``0`` on load — only a run still at ``phase==interview`` with
  no CLI ``ralplan.json`` stamp and ``cycles.ralplan==0`` migrates to ``0``;
  every other missing-epoch run migrates to at least ``1`` so re-entry
  invalidates stale consensus/quality stamps. Present values must be plain
  ``int >= 0`` (bool/float/negative rejected).
- **Terminal/cancel gates on transition/resume (Round 9 / R9-2):**
  ``transition()`` re-checks ``status.json`` under the execution lease and
  refuses sidecar writes when the run is terminal or has a pending
  cancellation request; ``omg autopilot run --resume`` prefers terminal
  ``status.json`` over a stale non-terminal sidecar ``phase``;
  ``status_autopilot`` returns empty ``legal_next`` for terminal runs.
- **Idempotent attach interview resume (Round 9 / R9-3):** attach-mode
  ``omg interview close`` sets ``resume_command`` to
  ``omg autopilot run --resume <run_id>`` (single idempotent entry); re-close
  of an already-complete interview migrates stale two-step resume commands on
  disk to the current form.
- **Consensus stamp strict-v2 schema (Round 10 / R10-1):**
  ``_consensus_ready`` requires ``schema_version==2`` and
  ``lifecycle_version==2`` on CLI ``ralplan.json`` (fail-closed if missing
  or wrong) — autopilot only embeds strict-v2 RALPLAN.
- **Interview writable assert on all sidecar saves (Round 10 / R10-2):**
  ``answer_interview``, ``pressure_pass_interview``, and non-attach
  ``start_interview`` reload the run under the execution lease and call
  ``_assert_run_writable`` before ``_save`` (same cancel/terminal gate as
  attach/close).
- **Ralplan re-authorize before each sidecar save (Round 10 / R10-3):**
  strict-v2 embedded ralplan calls ``_assert_autopilot_still_writable``
  immediately before every ``save_ralplan_state`` (not only the accept
  write) so a mid-round ``omg cancel`` cannot land a history/stage save
  on a cancelled run.
- **Resume preflight advance completed interview (Round 11 / R11-1):**
  ``run_autopilot`` advances a completed interview to ralplan before any
  Grok launch so ``--resume`` does not spawn an unnecessary interview
  session.
- **Stricter missing ``ralplan_epoch`` migration (Round 11 / R11-2):**
  migrate missing epoch to ``0`` only when phase is interview, no CLI
  ralplan stamp, ``cycles.ralplan==0``, history has no post-interview
  phases (missing/corrupt history fails closed to >=1), and no clean
  review/QA stamps or implementation receipt; otherwise migrate to >=1.
  Persists ``ralplan_epoch_source`` (``native`` / ``migrated``).
- **Refuse grok launch on cancel (Round 11 / R11-3):**
  ``_launch_grok`` / resume launch re-checks ``status.json`` and pending
  ``cancel.request.json`` and refuses Popen when the run is terminal or
  has a pending cancellation request.
- **Refuse bare ``omg accept`` for autopilot (Round 12 / R12-1):**
  ``cmd_accept`` refuses autopilot-mode runs (directs operators to
  ``omg autopilot complete``); ``set_verified`` requires sidecar
  ``phase==acceptance`` for autopilot (fail-closed unless force hatch).
- **Implementation receipts require lease ``invocation_id`` (Round 12 / R12-2):**
  ``stamp_implementation_receipt`` writes the active execution-lease id;
  ``read_implementation_receipt`` rejects missing/empty ``invocation_id``
  so a hand-written ``writer=omg-cli`` file cannot forge the gate.
- **Exclusive autopilot driver flock (Round 12 / R12-3):**
  ``run_autopilot`` holds a non-blocking exclusive flock on
  ``autopilot.driver.lock`` for the whole invocation so concurrent
  ``--resume`` cannot double-launch Grok.
- **Linearize grok spawn under transition_guard (Round 13):**
  cancel check + ``Popen`` + pid publish run under a short
  ``transition_guard`` so cancel cannot miss a newly spawned Grok;
  kill the child if pid publish fails.
- **Force verified process-private only (Round 14 / R14-1):**
  ``set_verified(force=True)`` requires ``enable_force_verified_for_tests()``;
  env alone does not unlock force.
- **Implementation receipts + live lease binder (Round 14 / R14-2):**
  ``stamp_implementation_receipt`` requires a live ``ExecutionLease`` and
  rebinds ``implement_receipt_binder``; implement entry clears the binder
  so a hand-copied receipt cannot unlock the next cycle.
- **Refuse spawn on live leader pid (Round 14 / R14-3):**
  resume/spawn refuses when ``pid.json`` still matches a live leader
  (PID + starttime); also refuses a live PID without starttime (do not
  clear/spawn).
- **Require starttime for pid publication (Round 14 / R14-4):**
  ``write_pid_metadata`` fails closed without starttime; fanout kills the
  worker if publish fails after spawn.
- **Authority nonce + phase bind accept/verified (Round 14 / R14-5):**
  ``authority_nonce`` / ``authority_phase`` bind ``status.json`` ↔
  ``autopilot.json``; bare ``omg accept`` refused whenever the autopilot
  sidecar exists; ``set_verified`` requires matching nonce and
  ``authority_phase==acceptance``. Residual: dual-edit of both files under
  a writable ``.omg/state`` / OS write-deny still out of scope.
- **Tri-state PID identity UNKNOWN refuse (Round 15 / R15-1):**
  live-leader spawn classifies ``MATCH`` / ``MISMATCH`` / ``UNKNOWN``;
  ``UNKNOWN`` (alive + recorded starttime + ``process_starttime`` probe
  ``None``) refuses spawn and does not clear ``pid.json`` (unknown ≠
  reclaimable).
- **Stamp requires target-run lease (Round 15 / R15-2):**
  ``stamp_implementation_receipt`` binds via ``_require_current_lease``
  (lease root/run must match the stamp target), refuses terminal /
  cancel-pending, and requires autopilot ``phase==implement`` when the
  sidecar exists — a foreign run's live lease cannot stamp this run.
- **Ralph resume live-leader gate (Round 15 / R15-3):**
  ``modes._launch_grok`` / ralph ``run_mode`` resume share
  ``prepare_leader_spawn`` with autopilot — refuse MATCH / UNKNOWN /
  missing-starttime; clear only DEAD / MISMATCH stale leaders.
- **Fanout kill prior workers on publish fail (Round 15 / R15-4):**
  when a later worker's ``pid.json`` publish fails after ``Popen``, kill
  and reap all earlier workers in the batch, persist failure evidence,
  and mark the run failed (no orphan batch).

### Planned
- Optional residual team API ops (broadcast / await-event / preflight pack) —
  not blockers for production path; full OMX 33-op not claimed.
- Optional CI wire for live team smoke (quota-heavy; fixture path already hermetic).
- Optional PyPI/`pipx` CLI track — **shipped editable-only** (`pyproject.toml` +
  `pipx install --editable` / `pip install -e .`); non-editable wheel / PyPI
  publish still deferred (`plugin_root()` needs checkout siblings).
- Optional PR to xAI plugin-marketplace (sha-pinned) — **deferred / prep-only**
  (document prerequisites in `docs/RELEASE.md`; do not submit).
- Host Stop veto (not feasible on Grok today).
- Full OMC semantic LSP proxy (host-owned `.lsp.json` registration ships in 0.6.0;
  OMG does not claim host health or proxy hover/rename/goto operations).
- StageEvidenceEnvelope v3 / full ResumeBundle / autopilot cross-file WAL (from
  external Pro+GitHub review) — Round 1 landed partial `resume_bundle` v1 +
  atomic single-file writes; full envelope + WAL deferred.

## [0.7.4] - 2026-08-01

### Fixed
- **Team scale/resume crash recovery (window readback + identity receipts):**
  generation-scoped WAL before side effects; fail-closed scaled-window ownership
  readback (`@omg_scale_nonce` + rename → exact `display-message`); pending
  identity-receipt generations and scale-up WAL gate join/collect/stop/integrate;
  scale-down recovery binds receipt victims (not re-drain) with generation/task
  ids in errors; meta commit result-loss classifies committed/not_committed/
  unknown without treating volatile `last_scale.actions` as sole identity;
  remain-on-exit dead panes clean then commit `needs_collect` when process
  absent. Integration isolation only — not an execution sandbox.
- **Hermetic scale recovery tests** without live `tmux` (mock dead-pane cleanup
  on Ubuntu CI); stop SIGKILL process-group reaping poll.

## [0.7.3] - 2026-07-30

### Added
- **`omg autopilot run --unattended`** (#40): hands-off outer loop re-launches
  Grok after host-turn stalls (no human `go`); pauses on interview/`await`;
  `--max-stall-relaunches` budget; machine JSON on stdout, resume hints on stderr.
- **`omg team {start,launch} --plan-only`** (#27): side-effect-free plan JSON
  (no `.omg` / worktrees / tmux). `--materialize-only` alias for mutating dry-run;
  CLI never prints `Team started` for non-live modes.
- **`omg lsp validate`** and stable `E_LSP_*` codes (#28); legacy
  `check`/`symbols`/`diagnostics` return `E_LSP_HOST_OWNED` with `next_action`.
- **`omg_cli/command_registry.py`** (#29 Phase 1): authoritative top-level
  `KNOWN_SUBCOMMANDS` / `CommandSpec` inventory.
- **CLI composition (#29 / #30):** handler families extracted from `main.py`
  (`commands/inspect|install|run|memory|workflow|modes|mcp|team`); argparse
  registration leaves `main` as composition-only; global **`--json`** +
  `CommandContext` / schema_version 1 envelopes; golden envelope tests;
  `docs/cli-commands.md` + `docs/cli-contract.md`.
- **Team process-level readiness (P0-1, #62):** pane commands run
  `omg team worker-ready` before the agent binary; launch success counts
  process receipts **or** mailbox ACK so read-only posture panes are not
  structurally `failed_start`.
- **Team P0′ reliability API (#63):** heartbeat read/update, worker status,
  shutdown request/ack, orphan-cleanup; `omg doctor` soft check for team
  gate/tmux/API surface.
- **Team plane default-on (#64):** enabled unless `OMG_DISABLE_TMUX_TEAM=1`
  (legacy `OMG_EXPERIMENTAL_TMUX_TEAM=0` still disables). Fixture smoke:
  `FIXTURE_TEAM_SMOKE_OK` via `scripts/live_team_smoke.py --fixture-executor`.
- **Team P0′ task/events/manifest API (#65):** `read-task`, `update-task`
  (CAS `expected_version`), `read-manifest`, `append-event`, `read-events`;
  live process-ready gate + stop kill-grace / pane rebind.
- **Grok live team smoke (#66):** `scripts/live_team_smoke.py --live` achieved
  `LIVE_TEAM_SMOKE_OK` (2026-07-30 local; process-ready gate + stop proof).
- **Deterministic release packager + staged publish** (#26 / #42).
- **CI:** full-package static analysis entrypoint; macOS platform contract lane.

### Documentation
- Autopilot EN/zh/zh-TW + skills: primary hands-off path is
  `omg autopilot run --resume … --unattended` (no longer “forthcoming”).
- Clarified the external-agent CLI PreToolUse contract: direct provider
  execution remains denied, while passive discovery, path inspection, and inert
  literals are allowed.
- **Team plane docs sync (post-promotion):** skills / security-model / README /
  RELEASE (EN+zh+zh-TW), `CLAUDE.md`, and `templates/omg-rules.md` document
  default-on + kill switch + `LIVE_TEAM_SMOKE_OK` (local) + integration-only
  isolation honesty (no longer experimental-gate language).

## [0.7.2] - 2026-07-28

### Fixed
- Global PreToolUse external-CLI detection now distinguishes quoted arguments,
  comments, and heredoc data from executable shell syntax (for example
  `git commit -m "fix(kimi): ..."`), while still denying real substitutions,
  continued commands, shell/eval bodies, and external CLI execution.
- Live `omg team resume` relaunch now acquires the same run-dir scale lock as
  `omg team scale`, refusing concurrent scale/resume that could double-spawn
  panes or last-writer-wins `team.json`.
- `omg team api transition-task-status` now requires `worker` and binds it to
  claim/task owner (same floor as `release-task-claim`), blocking cross-worker
  token completion. Worker panes also drop client-supplied `owner_token` when
  pane env has none.

### Added
- Identity-matrix hermetic coverage for worker-env mailbox/claim/transition/
  release impersonation + `owner_token` strip when pane env has none.
- Inside-tmux shorthand launch opens a dedicated team window + split panes in
  the current session (`attach_mode=inside`); stop kills only that window/panes
  — never the shared session or leader pane. Outside TTY creates a detached
  session and prints an attach hint; non-interactive live launch requires
  `--detach` (fail-closed).
- Shorthand `omg team launch` waits for worker mailbox ACKs (body `ACK` to
  `leader-fixed`) before reporting success. Knob: `OMG_TEAM_READY_TIMEOUT_MS`
  (default 45000). Partial ACK → `startup_status=degraded`; zero →
  `failed_start`; both exit non-zero and leave state for diagnosis.
- OMX-like team launch shorthand: `omg team [N[:role]] "<goal>"` → `launch`
  (split-pane topology, goal decomposition, team-name ref index, P0 api board
  seed, `skills/omg-team`). Still gated by `OMG_EXPERIMENTAL_TMUX_TEAM=1`;
  not promoted to full live OMX `$team` parity yet.
- Team pane workers may use identity-bound `omg team api` (ACK/claim/mailbox
  self); process-fanout / spawned-subagent contexts stay denied.
- Experimental `omg team api <op> --input JSON [--json]`: OMX-shaped P0 façade
  over mailbox + claim/transition task store (11 ops). Remaining OMX ops stay
  `E_TEAM_API_UNIMPLEMENTED`; full 33-op parity is not claimed. Still gated by
  `OMG_EXPERIMENTAL_TMUX_TEAM=1`. Fail-closed: requires CLI-stamped `team.json`
  control plane before materializing mailbox/task stores.

### Changed
- `docs/security-model.md`: document that identity receipts omit `pane_command`/
  `worktree`, and that `owner_token` is a same-UID shared secret (not
  cross-user isolation).
- Hardened `scripts/live_team_smoke.py --live` as the team promotion gate:
  asserts `dry_run=false`, pane count, grok (not fixture) pane commands,
  owned worktrees, ≥N ACKs, claim→completed, and stop clears only the owned
  session; prints `LIVE_TEAM_SMOKE_OK` only if all pass. Dry path still prints
  `DRY_TEAM_SMOKE_OK`. Live attempt on 2026-07-25 exited non-zero
  (`startup_status=failed_start`, ACKs=0) — **not** promoted; experimental
  gate `OMG_EXPERIMENTAL_TMUX_TEAM=1` remains. Optional wire in
  `scripts/live_suite.sh` (`OMG_LIVE_TEAM=1` / quota-heavy).

## [0.7.1] - 2026-07-24

### Fixed
- Release-verify writer-ownership gate: assign owners for host-launch/madmax
  modules, `CODE_OF_CONDUCT.md`, docs research/superpowers trees, plans, and
  historical locale/README rename paths so tag verification no longer fails
  closed on unowned dirty records.

## [0.7.0] - 2026-07-24

Host-launch parity with OMX (bare interactive + `--madmax` + launch policy).

### Added
- OMX-aligned host launch: bare `omg` / `omg "<prompt>"` launches interactive
  Grok at safe defaults; `omg --madmax` remains full-open break-glass
  (`--always-approve` + `--permission-mode bypassPermissions`). Shared transport
  policy via `OMG_LAUNCH_POLICY` / `--direct` / `--tmux` (auto falls back;
  explicit `--tmux` fails closed). First `--` suffix is opaque.

## [0.6.0] - 2026-07-23

Evidence-gated product composition across the parity workstreams. This release
adds public CLI routes without promoting unobserved Grok-native capabilities.

### Added
- **Repository workflows:** immutable `repository-workflow/v1` registry,
  deterministic plan/task IDs and waves, explicit permission intersection,
  externally gathered task receipts, replay/effect fences, and independent
  verifier + skeptic `ship` decision. Grok `/create-workflow` and Rhai stay
  `optional_unclaimed` until stable public schema plus fresh invocation proof.
- **Session continuity:** exact create/resume/continue/fork argv routes and
  bounded immutable JSONL recovery that preserves `W_BROKEN_CHAIN` and unknown
  record warnings instead of fabricating full history.
- **Project services:** redacted deterministic fact memory, generation-fenced
  lifecycle tracker, lossless compaction checkpoints, and outbound-only,
  non-authoritative notification adapters.
- **Host discovery manifests:** conventional `.mcp.json` and `.lsp.json` using
  `${GROK_PLUGIN_ROOT}`. `omg capabilities` reports configured, installed,
  enabled, loadable, observed, healthy, and verified independently.
- **Parity/release routes:** `omg parity run` delegates the frozen W0 manifest
  engine; `omg parity release-readback` rejects missing, extra, renamed, or
  byte-drifted prebuilt assets.
- **GitHub-only install:** convenient latest-release bootstrap plus a pinned
  manual/offline archive path. Both verify `SHA256SUMS`, switch transactionally,
  run strict doctor, emit a receipt, and roll back failed activation.

### Security
- Workflow planning never launches shell agents. Runtime receipts must bind to
  planned task IDs/actors, and effective permission is the repository/host/
  launch intersection. Notifications never own run status. OMG does not probe
  private sidecars or infer native health from config files.

### Fixed
- **Workflow runner:** executor liveness is observed before draining the result
  pipe, so a child that publishes its receipt and exits between an empty poll
  and the liveness check is no longer misclassified as
  `E_WORKFLOW_EXECUTOR_EXITED_WITHOUT_RESULT` (`effect_unknown`).
- **Uninstall (host-copy model):** receipt-backed uninstall now accepts the
  receipt's verified `plugin_realpath` under Grok's `installed-plugins/` copy
  root (byte-identity still required), and rollback reinstalls the host copy
  from the immutable stage when the host already deleted the copy.
- **Team scale retry:** an aborted scale attempt (intent receipt persisted,
  signalling failed) no longer wedges the identity chain — an orphan receipt
  that matches the retried intent on every deterministic field is adopted
  idempotently; any other content stays a hard conflict.

## [0.5.0] - 2026-07-22

Grok-native parity completion: fail-closed hardening, a multi-CLI tmux **team plane**
(D0–D4), and an **in-session MCP server**. Every workstream carries a model-diverse
(Fable 5) adversarial GO plus a REAL/live test pass. The multi-CLI team plane ships behind
an explicit experimental gate; see the blast-radius note in `docs/security-model.md`.

**Live testing earned its keep — it caught THREE integration/wire bugs that unit tests +
adversarial security review all missed:** an MCP NDJSON-vs-Content-Length framing mismatch
(grok timed out connecting), multi-CLI pane prompt-delivery (a real codex pane hung because
its stdin sentinel `-` was never fed), and a team-exec/collect race (collect ran before the
panes sealed). All three found by real `grok`/`codex` in real tmux, fixed, and re-verified live.

### Fixed (fail-closed hardening — each RED→GREEN, each only makes a gate stricter)
- **verdict/ralplan (A2):** `ralplan.verifier_has_approve` raw-`or` across sibling verifier
  artifacts → cross-artifact severity aggregation; `verdict.parse_verdict` folds prose severity
  into step 2 so a fenced-example APPROVE can't short-circuit an unfenced prose REQUEST CHANGES.
- **install classifier (A1):** extracted to an importable, unit-tested
  `scripts/omg_install_classifier.py` (independent candidates, realpath both sides;
  mandatory no-false-positive on genuinely-different paths).
- **doctor --strict (B):** the `spawn_subagent` bare-substring FP on the repo's own CLAUDE.md
  — now matches routing-trigger shape; environmental FAILs stay honest.

### Fixed (live-integration bugs, found by real-CLI smoke)
- **MCP wire framing:** `omg mcp-server` now replies in the client's framing (NDJSON in →
  NDJSON out); grok could not parse the Content-Length reply and timed out.
- **multi-CLI pane prompt delivery:** codex reads the prompt via a stdin redirect; cursor/agy
  get the prompt text (grok's `--prompt-file` unchanged) — a codex pane hung indefinitely before.
- **team-exec race:** the staged pipeline now waits for panes to finish/seal before `collect`
  (bounded by `OMG_TEAM_EXEC_WAIT_SECS`); collect had run before workers sealed → integrate refused.

### Fixed (install security — the global hook could deny EVERY tool call)
- **Root cause (live, 2026-07-22):** the global PreToolUse soft-gate pointed
  `python3 "<checkout>/hooks/bin/pre_tool_use_deny.py"` — a script under
  macOS-TCC-protected `~/Documents` that also `import`ed `omg_cli`. A grok session in
  another workspace (or lacking Documents access) could not `open()` it, so `python3`
  exited **2**; grok reads a PreToolUse exit code of 2 as an *explicit deny*, so it
  blocked every tool call (even `ls`, `spawn_subagent`). The in-code fail-open never
  ran — python could not open the file. Confirmed live and fixed model-diverse
  (Codex gpt-5.6-sol max + Fable 5 design review + a real grok canary).
- **Fix:** a SELF-CONTAINED, stdlib-only standalone (`hooks/bin/omg_pretool_deny_standalone.py`,
  generated from `omg_cli/deny.py` + `_common.hook_disabled` by
  `scripts/generate_standalone_hook.py`, `--check`-guarded in CI) installed under
  `$GROK_HOME/hooks/` (always readable, non-TCC, workspace-independent). It signals
  deny ONLY via stdout JSON (grok honors that regardless of exit code) and **always
  exits 0**; the launcher `python3 -I -S "<abs>" || true` normalizes any
  interpreter/startup failure to fail-**open** (the path is `shlex.quote`d so a
  `$GROK_HOME` with shell metacharacters can't inject an `exit 2`). A live grok 0.2.106
  canary confirmed the hook's deny-JSON-at-rc0 actually blocks the command (parent
  `parent_host_signature=true`, no shim marker written); the spawned child was
  additionally capability-isolated.
- **Install/repair:** one transactional installer (`omg_cli/hook_install.py`) shared by
  `omg setup` (new; end-user path previously installed NO hook) and
  `scripts/install-plugin.sh` (new `omg install-hook` subcommand; `omg setup
  --no-global-hook` opts out). Atomic writes; migrates a prior checkout-path json and
  **quarantines** it to a non-`.json` name on failure ("no hook > broken hook").
  Plugin-bundled `hooks/hooks.json` now points at the standalone too.
- **doctor:** `check_global_pretool_hook` rewritten — realpath-under-`$GROK_HOME`
  (rejects checkout paths + symlink escapes), rejects a 2nd command hook, real `open()`
  + a behavioral subprocess smoke (allow/deny), and a soft freshness check
  (installed-vs-committed hash + TCC-home WARN). `os.access` (TCC-blind false-green)
  removed. GROK_HOME honored consistently across setup/install/doctor/uninstall.

### Added
- **`omg team` — multi-CLI tmux team plane** (behind `OMG_EXPERIMENTAL_TMUX_TEAM=1`): D0 vetted
  executor argv adapters (grok/codex/agy/cursor/gemini) → D1 grok-only start/status/collect/stop
  → D2 staged pipeline (`omg team run`) → D3 per-role multi-CLI executor panes + routing
  (reviewer roles → structured-verdict providers only, **cursor forbidden**; unknown roles
  fail-closed) → D4 dynamic scaling + resume + **ralph composition** (`omg team run --ralph`, a
  bounded loop that NEVER sets verified). `deny.py` strengthened (worker can't launch a team).
  Agent-role parity + machine-readable role taxonomy (F).
- **In-session MCP server (`omg mcp-server`, `grok mcp add`)** — 14 read + non-authoritative-proposal
  tools for Grok-native in-session parity. `verified` stays CLI-only via three fail-closed mechanisms
  (curated allowlist, structural refusal under `OMG_MCP_SERVER=1`, path-confinement). Exercised on a real Grok host.
- **`omg lsp symbols`/`diagnostics` (E):** stdlib-`ast` local probe. **`pyproject.toml` (C):**
  editable-pipx packaging.

### Scope honesty
- The multi-CLI team plane provides **integration isolation, NOT execution isolation**: executor
  panes run with operator-level machine access; only worktree ownership + seal + integrate bound
  what reaches the leader tree, and `verified` stays CLI-only. Per-provider CLI-sandbox enforcement
  is non-uniform (grok/codex CLI-enforced; agy `--sandbox` best-effort; gemini none). See
  `docs/security-model.md`.

## [0.4.3] - 2026-07-21

Local-path install refresh + codebase docs. Merged via PR #4; standing reviewer
(Fable 5) GO on the engineering bar.

### Fixed
- **`install-plugin.sh` force-refreshes a local-path install:** `grok plugin
  update` is a no-op for a local-path (frozen-snapshot) install, so a bumped
  checkout left the installed plugin snapshot stale (caught only by `omg doctor`'s
  version-drift / installed-capabilities-lock checks). The installer now detects a
  same-path install (realpath match) and force-refreshes via `grok plugin
  uninstall … && install`, erroring loudly (exit 1) if the reinstall fails;
  different-path duplicates stay WARN-only.
- **`omg update` surfaces the installer's recovery output:** on a non-zero
  `install-plugin.sh` exit it now forwards the captured stdout+stderr (previously
  it printed only `exited rc=1`, swallowing the reinstall-gap recovery message).

### Added
- **`CLAUDE.md`** — a codebase architecture guide (two-surface design, the Grok
  host contract, `capability_mode` isolation, the two fail-closed security modules
  `verdict.py`/`command_policy.py`, the worker/seal/integrate flow, and the
  version-bump gotchas incl. the `grok plugin update` no-op-for-local-path finding).

### Docs
- README/skills refreshed: the Upgrade note now documents uninstall+reinstall for
  local-path installs, plus `omg worker seal --all` and `omg note --prune`.

## [0.4.2] - 2026-07-21

ULW leader batch seal — closes the ULW→integrate gap the live suite surfaced.
Merged via PR #3; standing reviewer (Fable 5) design bless + implementation GO.

### Added
- **`omg worker seal --all [--force]`:** a leader-side batch seal — one command
  seals every prepared worktree with a real `head_sha` from `git rev-parse HEAD`,
  so real grok ULW sessions stop hand-writing envelopes with invalid head_shas
  (which `omg integrate` correctly refused). A pure driver over the existing
  fail-closed `seal_task`; join's ownership gate and integrate's
  `preflight_clean_tree` are untouched.
  - Fail-closed status discrimination: only a literal "worktree missing" is a
    benign skip; a returned `status="failed"` envelope (head==base / still-dirty)
    surfaces as `failed` (never masked as `sealed`); every other `WorkerError` is
    `error`. The CLI returns nonzero if any task failed/errored.
  - Honest trust boundary: seals only `.omg/worktrees/<run_id>/<validated task_id>`
    for task_ids in a CLI-written manifest (no provenance verification claimed);
    a traversal task_id is rejected by `validate_task_id`.
  - `--force` re-seals a worktree whose head advanced past its recorded head_sha.

## [0.4.1] - 2026-07-21

Backlog polish + a security-floor hardening pass, all reviewer-driven (Fable 5
full-branch GO). Merged via PR #2. 528 → 547 unit tests.

### Fixed (security)
- **command_policy break-glass floor:** a v0.4.0-round attempt to fix a
  false-positive (`python3 -m pytest -rc` wrongly denied) narrowed the `-c`/`-e`
  floor scan and reopened a real code-exec bypass under `--no-allowlist`. Fixed
  in layers, ending in a **fail-closed region boundary**: a bare token ends the
  interpreter region only if it is a real `.py` script (or `-m`/`--`); any other
  bare token is treated as a (possibly unknown) option's value, so a following
  `-c`/`-e`/`-p`/`--eval`/`--print` stays caught. Unknown/future interpreter
  options can no longer hide an eval flag (verified with fuzz + break-glass
  probes). Trade-off: an extensionless positional script fails closed under
  break-glass (intentional; normal mode requires `.py`).

### Fixed
- **workers.py:** ownership path normalization used `.lstrip("./")`, collapsing a
  dotfile `.config` to `config`; now `_norm_relpath` keeps dotfiles intact.
- **autopilot.py:** invalidate review/QA stamps on `review` entry from `blocked`
  too, closing the `qa→blocked→review→qa` stale-review-stamp reuse.

### Added
- **`omg note --prune`:** the `[7d]` tag is now a real TTL (drops entries older
  than 7 days; keeps `[permanent]` + unparseable-timestamp lines).
- **`doctor` installed-snapshot capabilities lock:** hashes the installed frozen
  snapshot's skills/agents against the committed lock (true OMX installed-drift;
  complements the local-checkout guard).
- **Docs-drift guard** extended from `omg goal` to every sub-actioned command.

### Notes
- Deliberately unchanged: the `deny.py` quoted/heredoc-line false-positive stays
  fail-closed (fixing it reopens the heredoc-body bypass).
- Backlog: ULW leader-side auto-seal (`omg worker seal --all`) is a designed
  feature for a later release; an unknown future interpreter option taking a
  separate `.py`-suffixed value would need adding to the arg-consuming set (no
  such real option exists today).

## [0.4.0] - 2026-07-21

OMC/OMX parity upgrade — global guidance injection, install lifecycle, and a
verdict-gate hardening pass. All work was executor-written under orchestrator
briefs and gated by an independent model-diverse standing reviewer (Fable 5,
full-branch GO). 468 → 528 unit tests; exercised against a real Grok host.

### Added
- **Global guidance injection (`~/.grok/rules/omg.md`):** the Grok-native OMC
  `CLAUDE.md` / OMX `AGENTS.md` equivalent. `omg setup` writes an always-loaded
  operating contract (tuned to Grok 4.5) via a non-destructive marker reconcile
  (`OMG:START/END`), preserving any `USER:OMG:POLICY` block, with a source-hash
  handshake and rolling backup (`omg_cli/guidance.py`, `templates/omg-rules.md`).
  `omg setup --no-global-rules` opts out. Observed in a one-time Grok host smoke
  on 2026-07-21: `grok inspect` loads it and a fresh `grok -p` quotes the contract.
- **`omg update`:** git pull + `grok plugin update` (force-refresh the frozen
  snapshot) + doctor.
- **`omg uninstall`:** `--yes`-gated removal of plugin, global hook, OMG rules
  block (preserves `USER:OMG:POLICY`), and CLI symlink; never touches project `.omg/`.
- **`omg note`:** compaction-resistant project notepad (`.omg/notepad.md`, 7d /
  `--priority` permanent TTL, `--show`).
- **Kill switches:** `DISABLE_OMG` (all hooks off; deny fails open) and
  `OMG_SKIP_HOOKS` (per-hook logical names).
- **Doctor drift checks:** global-rules status, plugin version-drift + duplicate
  detection, `[plugins].enabled`, and a local-checkout capabilities lock
  (`omg_capabilities.lock.json` + `scripts/generate_capabilities_lock.py`).
- **Self-healing installer:** `install-plugin.sh` warns on duplicate entries and
  runs `grok plugin update` + `grok plugin enable`.
- **Anti-drift docs guard:** `tests/test_docs_cli_drift.py` diffs documented `omg`
  subcommands against the real argparse choices.

### Fixed (security / correctness, each with a RED-proven regression test)
- **deny.py:** external-CLI block bypassed by multi-line commands (a denied bin on
  its own line) — `\n\r` added to the command-position class.
- **verdict.py:** run_id false-accept hardened in three layers — document-level
  poison guard, extract-ALL top-level objects, severity aggregation
  (FAILED > REQUEST_CHANGES > APPROVE), and a UNION of quote-aware + quote-agnostic
  brace scans (closes stale-object hiding via unbalanced braces in strings and odd
  prose quotes). Path-bound unbound artifacts still accepted.
- **command_policy.py:** break-glass floor now denies `python -c` via combined
  short clusters (`-ic`).
- **autopilot.py:** invalidate review/QA stamps on every (re)entry into `implement`
  (closes the `qa→blocked→implement→blocked→qa` false-green round-trip).
- **workers.py:** empty `owned_files` fails closed in join.
- **docs/skills.md(+zh):** `omg goal start`/`complete` → real `start-story`/`complete-story`.

### Notes
- Keyword triggers live in the rules file's `<workflow_routing>` section, not a
  hook — Grok's non-`PreToolUse` hooks are passive (stdout ignored).
- Known backlog: ULW worker envelope `head_sha` requires `omg worker seal`
  (leader-side / omg on the worker's PATH); installed-snapshot content-drift lock;
  duplicate same-named plugin entries need manual `grok plugin uninstall` by key.

## [0.3.2] - 2026-07-21

### Fixed
- **QA freeze allowlist UX:** reject illegal scenarios at freeze (not only at run) with operator tips (`grep`/`test`/`omg`/`python -c`); prefer project `.py` or `python3 -m pytest`.
- **pytest marker coalesce:** unquoted `-m not live` → `-m 'not live'` on QA and accept paths so marker expr is not split into a fake path.
- **Autopilot complete short-circuit:** if the run is already verified (e.g. prior `omg accept`), sync autopilot phase without re-running freeze_and_run / full acceptance.
- **`status.autopilot_phase`:** set to `verified` on `set_verified` and complete so status no longer lingers at `acceptance`.

### Added
- **Auto PRD from clean UltraQA:** `materialize_prd_from_ultraqa` for missing `prd.json` (CLI-stamped clean only; never overwrites operator PRD); wired into `omg accept` and `omg autopilot complete`.
- **`merge_status_fields`:** non-authority status metadata merge (cannot set `verified`/`status`).

### Changed
- Skills `omg-ultraqa` / `omg-autopilot`: correct freeze examples (quoted markers; no illegal basenames); document complete short-circuit + optional prd.

## [0.3.1] - 2026-07-21

### Fixed
- **strict-v2 `omg accept` / `set_verified`:** auto-acquire execution lease when caller omits lease (default ralph completion gate).
- **Verdict false-green residuals:** case-insensitive prose `FAILED`; schema_version=2 documents no longer fall through to terminal prose APPROVE; balanced JSON extract when prose trails a JSON blob.
- **Integrate strict status:** failure paths write run status `blocked` (not illegal `failed`) on schema v2.
- **Process fanout:** child env uses `safe_supervised_child_env`; shared wait deadline (not N×timeout).
- **Acceptance/QA env:** scrub runner-hijack keys (PYTHONSTARTUP/PATH-like, GIT_*, LD_PRELOAD/DYLD_*, NODE_*, npm_config_*).
- **run_id path safety:** `_safe_run_id` on fanout/modes/ask/dual_review/ralplan/interview/integrate path joiners.
- **Dual-review product wording:** sequential path marked permanent PARTIAL (not open-ended interim).
- **Skill routing:** pipeline no longer claims bare `autopilot` primary; ralplan documents v2 + `omg ask`.
- **Hooks contract tests:** stop path must not set verified.
- **CI:** Python 3.13 matrix; ignore `.ruff_cache`/`.mypy_cache`; research residue gitignored.

### Docs
- security-model: acceptance env scrub + goal-verify disk-trust residual; spawn soft fail-closed retitled as shipped.
- OPEN-ITEMS: mark interview/QA/goal ledger shipped; residual is depth/live evidence.
- `plans/`: improve-deep advisor plans + execution artifacts.

## [0.3.0] - 2026-07-21

### Added
- **R2 continuity:** `omg resume` smart routing; SessionStart writes `.omg/state/RESUME.md`; `omg resume --clear` one-shot lifecycle; louder pack via resume MD + `omg hud`.
- **R3 verdict security:** expanded APPROVE negation; fence strip (incl. unclosed ``` / `~~~`); smart-apostrophe normalize; **schema_version=2** JSON with `run_id` binding (`expected_run_id` in dual-review).
- **In-session skills:** thick `omg-ultragoal`, `omg-autopilot`, `omg-deep-interview`, `omg-ultraqa`; new `omg-wiki`, `omg-hud`, `omg-lsp`.
- **Lifestyle CLI:** `omg wiki {ingest,list,query}`, `omg hud`, `omg lsp {status,check}` (honest: no host LSP MCP).
- **Dirs:** `.omg/wiki/` scaffolded with setup/hooks.
- Research pointer: `docs/research/omc-omx-mechanism-research-pointer.md`.

### Changed
- `omg-using` router: RESUME.md hard rule; priority includes ultragoal + lifestyle routes.
- README scope honesty updated for resume/wiki/hud (still no Stop hard-pin / full LSP MCP).

### Security
- Fail-closed prose APPROVE when unfenced body negates APPROVE or only fenced APPROVE appears.
- Schema v2 run_id mismatch cannot false-green dual-review verifier artifacts.

## [0.2.6] - 2026-07-20

### Added
- **`omg --madmax`**: OMC-style break-glass host launcher — full-open Grok (`--always-approve` + `--permission-mode bypassPermissions`) in a **new tmux session** each launch (timestamp + nonce).
- Guardrails: subcommand before `--madmax` → exit 2; `--safe` / non-bypass `--permission-mode` → exit 2; root `--yolo` is not a madmax alias (stripped with note).
- Login-shell pane command + `tmux new-session -e` env forward (no secrets in pane start-command text); best-effort DA1 drain.
- Docs: dual-track install, security-model Host launcher section, `docs/RELEASE.md`, CI smoke/e2e.

### Changed
- Hermetic CI runs `scripts/smoke.sh` in addition to pytest.
- Session naming / attach policy: never reattach old madmax sessions (continuity via `grok --continue` / `--resume`).

### Security
- Documented madmax as operator break-glass (not a sandbox); detached sessions remain until `tmux kill-session`.
- Env forward via tmux `-e` (not shell `export` in pane argv).

## [0.2.5] - 2026-07-20

### Added
- Core-purpose parity CLI surfaces (goal ledger, interview, review, UltraQA, autopilot destination gates).
- Open-source packaging: MIT LICENSE, SECURITY, CONTRIBUTING, hermetic GitHub Actions CI.
- Public verification summary under `docs/research/verification-2026-07-20.md`.
- `omg --version` (reads `plugin.json`).
- Dual-track install docs (full vs plugin-only); maintainer release protocol.

### Changed
- README recommends stable home `~/.local/share/oh-my-grok`.
- Live machine evidence no longer shipped; regenerate via `docs/research/live/README.md`.
- Git history scrubbed of home paths and live suite JSON (filter-repo).
- CI runs hermetic smoke/e2e in addition to pytest.

### Security
- Isolation honesty documented in `docs/security-model.md` (capability_mode primary; PreToolUse fail-open soft-gate).
- Global PreToolUse soft-gate install path remains absolute-checkout (re-run `install-plugin.sh` after relocate).
